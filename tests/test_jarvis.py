"""Edith slice tests. No network, no model downloads, no real OS actions:
LLM clients, Whisper, edge-tts and Telegram are all faked."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Literal

import av
import numpy as np
import pytest
from anthropic.types.beta import BetaMessage

from bot.actions import registry as r
from bot.jarvis import brain, voice
from bot.jarvis.memory import Memory


# ====================================================================== fixtures / fakes
@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(r.REGISTRY)
    r.REGISTRY.clear()
    yield
    r.REGISTRY.clear()
    r.REGISTRY.update(saved)


@pytest.fixture
def actions():
    calls: list[tuple[str, dict]] = []

    @r.action("get_status", "Show PC status.", category="system")
    async def get_status(detail: Literal["short", "full"] = "short") -> r.ActionResult:
        calls.append(("get_status", {"detail": detail}))
        return r.ActionResult(text="CPU 12%, RAM 40%")

    @r.action("snap", "Take a screenshot.", category="desktop")
    async def snap() -> r.ActionResult:
        calls.append(("snap", {}))
        return r.ActionResult(text="Screenshot taken.",
                              files=[r.ActionFile(data=b"PNG", filename="s.png", kind="photo")])

    @r.action("kill_proc", "Kill a process.", dangerous=True, category="system")
    async def kill_proc(pid: int) -> r.ActionResult:  # fake: never touches a real process
        calls.append(("kill_proc", {"pid": pid}))
        return r.ActionResult(text=f"killed {pid}")

    return calls


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        if not name.startswith("send_"):
            raise AttributeError(name)

        async def _send(*a, **kw):
            self.sent.append((name, a, kw))
        return _send


@pytest.fixture
def confirm(monkeypatch):
    state = {"answer": True, "asked": []}

    async def fake_ask_confirm(bot, chat_id, summary, **kw):
        state["asked"].append(summary)
        return state["answer"]
    monkeypatch.setattr(brain, "ask_confirm", fake_ask_confirm)
    return state


def amsg(content, stop_reason):
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


def tool_use(name, inp, id_="tu_1"):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


class FakeAnthropic:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        # snapshot messages: the brain keeps appending to the same list
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return self.responses.pop(0)


# ====================================================================== memory
def test_memory_cap_forget_and_prefs(tmp_path):
    path = tmp_path / "state.json"
    m = Memory(max_turns=2, state_path=path, default_voice=True)
    for i in range(5):
        m.add_turn(7, f"u{i}", f"a{i}")
    h = m.history(7)
    assert [x["content"] for x in h] == ["u3", "a3", "u4", "a4"]
    assert h[0]["role"] == "user"
    m.set_voice(7, False)
    m.set_persona(7, "  Grumpy butler  ")
    assert m.forget(7) == 4 and m.history(7) == []

    m2 = Memory(max_turns=2, state_path=path, default_voice=True)  # prefs persisted, history not
    assert m2.get(7).voice is False and m2.get(7).persona == "Grumpy butler"
    assert m2.history(7) == []
    m2.set_persona(7, "")
    assert m2.get(7).persona is None


# ====================================================================== brain helpers
def test_openai_tool_conversion(actions):
    tools = brain.openai_tools(r.tool_schemas())
    by_name = {t["function"]["name"]: t for t in tools}
    assert set(by_name) == {"get_status", "snap", "kill_proc"}
    f = by_name["get_status"]
    assert f["type"] == "function"
    assert f["function"]["parameters"]["properties"]["detail"]["enum"] == ["short", "full"]
    assert "additionalProperties" not in f["function"]["parameters"]
    assert "DANGEROUS" in by_name["kill_proc"]["function"]["description"]
    assert "additionalProperties" in r.REGISTRY["get_status"].input_schema  # registry not mutated


def test_system_prompt_rules_survive_persona():
    p = brain.system_prompt("You are a pirate.")
    assert p.startswith("You are a pirate.")
    assert "Never claim you did something" in p
    assert "Edith" in brain.system_prompt(None)


def test_missing_provider_config(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(brain.BrainConfigError, match="ANTHROPIC_API_KEY"):
        brain.AnthropicBrain().client

    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    with pytest.raises(brain.BrainConfigError, match="LLM_BASE_URL"):
        brain.OpenAICompatBrain().client
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(brain.BrainConfigError, match="LLM_API_KEY"):
        brain.OpenAICompatBrain().client
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(brain.BrainConfigError, match="LLM_MODEL"):
        brain.OpenAICompatBrain().client

    monkeypatch.setenv("LLM_PROVIDER", "nonsense")
    with pytest.raises(brain.BrainConfigError):
        brain.make_brain()


# ====================================================================== anthropic loop
async def test_anthropic_safe_tool_then_answer(actions, confirm):
    fake = FakeAnthropic([
        amsg([{"type": "thinking", "thinking": "", "signature": "sig"},
              tool_use("get_status", {"detail": "full"})], "tool_use"),
        amsg([{"type": "text", "text": "Your CPU is at 12 percent."}], "end_turn"),
    ])
    b = brain.AnthropicBrain(fake, effort="low", fallbacks="default")
    bot = FakeBot()
    hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."}]
    reply = await b.ask(bot, 42, "how is my pc", history=hist)

    assert reply.text == "Your CPU is at 12 percent."
    assert reply.actions == ["get_status"]
    assert actions == [("get_status", {"detail": "full"})]
    assert confirm["asked"] == []  # safe action: no confirmation
    assert hist == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."}]

    req1, req2 = fake.requests
    assert req1["model"] == "claude-opus-5-5"
    assert req1["output_config"] == {"effort": "low"}
    assert "thinking" not in req1 and "temperature" not in req1
    assert req1["fallbacks"] == "default" and req1["betas"] == ["server-side-fallback-2026-07-01"]
    assert req1["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req1["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in req1["tools"][:-1])
    assert "cache_control" not in r.REGISTRY[req1["tools"][-1]["name"]].tool_schema()  # no leak
    assert req1["messages"][:2] == hist and "how is my pc" in req1["messages"][2]["content"]
    # round 2: prefix unchanged (append-only), assistant content verbatim, one tool_result message
    assert req2["messages"][:3] == req1["messages"]
    assert req2["messages"][3]["content"][0].type == "thinking"
    res = req2["messages"][4]["content"]
    assert res == [{"type": "tool_result", "tool_use_id": "tu_1",
                    "content": "CPU 12%, RAM 40%", "is_error": False}]


async def test_anthropic_dangerous_declined_never_runs(actions, confirm):
    confirm["answer"] = False
    fake = FakeAnthropic([
        amsg([tool_use("kill_proc", {"pid": 1234})], "tool_use"),
        amsg([{"type": "text", "text": "Cancelled, nothing was killed."}], "end_turn"),
    ])
    reply = await brain.AnthropicBrain(fake).ask(FakeBot(), 42, "kill 1234")
    assert actions == []  # never ran
    assert confirm["asked"] == ["Edith wants to run: kill_proc(pid=1234)"]
    assert reply.actions == [] and reply.declined == ["kill_proc"]
    tr = fake.requests[1]["messages"][-1]["content"][0]
    assert tr["is_error"] is True and "declined" in tr["content"] and "NOT run" in tr["content"]


async def test_anthropic_dangerous_confirmed_and_files_sent(actions, confirm):
    fake = FakeAnthropic([
        amsg([tool_use("kill_proc", {"pid": 99}, "a"), tool_use("snap", {}, "b")], "tool_use"),
        amsg([{"type": "text", "text": "Done, and here's your screen."}], "end_turn"),
    ])
    bot = FakeBot()
    reply = await brain.AnthropicBrain(fake).ask(bot, 42, "kill 99 and screenshot")
    assert [c[0] for c in actions] == ["kill_proc", "snap"]
    assert reply.actions == ["kill_proc", "snap"]
    assert len(confirm["asked"]) == 1  # only the dangerous one
    assert [s[0] for s in bot.sent] == ["send_photo"]  # file went to chat, not to the model
    results = fake.requests[1]["messages"][-1]["content"]
    assert [x["tool_use_id"] for x in results] == ["a", "b"]  # both results in ONE message
    assert "sent to the owner's chat" in results[1]["content"]


async def test_anthropic_unknown_tool_bad_args_and_step_limit(actions, confirm):
    loop = [amsg([tool_use("nope", {}, "x")], "tool_use"),
            amsg([tool_use("get_status", {"detail": "huge"}, "y")], "tool_use")]
    fake = FakeAnthropic(loop)
    reply = await brain.AnthropicBrain(fake, max_rounds=2).ask(FakeBot(), 1, "loop")
    assert "too many steps" in reply.text
    r1 = fake.requests[1]["messages"][-1]["content"][0]
    assert r1["is_error"] and "Unknown tool" in r1["content"]
    assert actions == []  # bad enum rejected by run_action validation


async def test_anthropic_refusal_and_api_error(actions, confirm):
    fake = FakeAnthropic([amsg([], "refusal")])
    reply = await brain.AnthropicBrain(fake).ask(FakeBot(), 1, "x")
    assert reply.text == "I can't help with that one."

    import anthropic
    import httpx2

    class Boom(FakeAnthropic):
        async def _create(self, **kw):
            raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    reply = await brain.AnthropicBrain(Boom([])).ask(FakeBot(), 1, "x")
    assert "can't reach Claude" in reply.text


async def test_anthropic_no_fallbacks_when_off(actions):
    fake = FakeAnthropic([amsg([{"type": "text", "text": "ok"}], "end_turn")])
    await brain.AnthropicBrain(fake, fallbacks="off").ask(FakeBot(), 1, "x")
    assert "fallbacks" not in fake.requests[0] and "betas" not in fake.requests[0]


# ====================================================================== openai_compat loop
def omsg(content=None, calls=None):
    tcs = None
    if calls:
        tcs = [SimpleNamespace(id=i, type="function", function=SimpleNamespace(name=n, arguments=a))
               for i, n, a in calls]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tcs))])


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.requests.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
        return self.responses.pop(0)


async def test_openai_compat_loop(actions, confirm, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    confirm["answer"] = False
    fake = FakeOpenAI([
        omsg(calls=[("c1", "get_status", json.dumps({"detail": "short"})),
                    ("c2", "kill_proc", '{"pid": 5}'),
                    ("c3", "get_status", "{not json")]),
        omsg(content="CPU is fine; I did not kill anything."),
    ])
    b = brain.OpenAICompatBrain(fake, model="qwen3:8b")
    reply = await b.ask(FakeBot(), 9, "status and kill 5", history=[{"role": "user", "content": "yo"},
                                                                     {"role": "assistant", "content": "hey"}])
    assert reply.text == "CPU is fine; I did not kill anything."
    assert actions == [("get_status", {"detail": "short"})]
    assert reply.declined == ["kill_proc"]
    req1, req2 = fake.requests
    assert req1["model"] == "qwen3:8b" and req1["messages"][0]["role"] == "system"
    assert req1["tools"][0]["type"] == "function"
    tool_msgs = [m for m in req2["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3"]
    assert tool_msgs[0]["content"] == "CPU 12%, RAM 40%"
    assert tool_msgs[1]["content"].startswith("ERROR:") and "declined" in tool_msgs[1]["content"]
    assert "Invalid JSON" in tool_msgs[2]["content"]
    assert req2["messages"][:4] == req1["messages"]  # append-only
    assert req2["messages"][4]["tool_calls"][1]["function"]["name"] == "kill_proc"


# ====================================================================== voice
def _sine_mp3(seconds=1.0, rate=24000) -> bytes:
    buf = io.BytesIO()
    with av.open(buf, "w", format="mp3") as c:
        s = c.add_stream("libmp3lame", rate=rate)
        s.layout = "mono"
        t = np.arange(int(rate * seconds)) / rate
        pcm = (np.sin(2 * np.pi * 440 * t) * 0.3 * 32767).astype(np.int16)
        for i in range(0, len(pcm), 1152):
            fr = av.AudioFrame.from_ndarray(pcm[i:i + 1152].reshape(1, -1), format="s16", layout="mono")
            fr.rate = rate
            for p in s.encode(fr):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)
    return buf.getvalue()


async def test_tts_produces_ogg_opus(monkeypatch):
    mp3 = _sine_mp3()
    seen = {}

    async def fake_edge(text, v, rate):
        seen.update(text=text, voice=v)
        return mp3
    monkeypatch.setattr(voice, "_edge_tts_mp3", fake_edge)
    monkeypatch.setenv("TTS_VOICE", "en-GB-RyanNeural")
    ogg = await voice.synthesize("**Hello** there, see https://example.com")
    assert ogg[:4] == b"OggS"
    with av.open(io.BytesIO(ogg)) as c:
        st = c.streams.audio[0]
        assert st.codec_context.name == "opus" and st.rate == 48000
    assert seen == {"text": "Hello there, see the link", "voice": "en-GB-RyanNeural"}
    pcm = voice.decode_audio(ogg)  # round trip through our own decoder
    assert abs(pcm.size / voice.SAMPLE_RATE - 1.0) < 0.1


async def test_tts_errors_are_voiceerror(monkeypatch):
    async def broken(*a):
        raise ConnectionError("offline")
    monkeypatch.setattr(voice, "_edge_tts_mp3", broken)
    with pytest.raises(voice.VoiceError, match="offline"):
        await voice.synthesize("hi")
    with pytest.raises(voice.VoiceError, match="nothing to say"):
        await voice.synthesize("  **  ")


def test_speakable_truncates_long_text():
    out = voice.speakable("Sentence one. " * 300)
    assert len(out) < voice.MAX_TTS_CHARS + 60 and out.endswith("in the text message.")


def test_decode_audio_stops_at_length_cap(monkeypatch):
    """A long recording is rejected instead of being decoded into gigabytes of PCM."""
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000 * 5)  # 5 s
    monkeypatch.setattr(voice, "MAX_AUDIO_SECONDS", 2)
    with pytest.raises(voice.VoiceError, match="longer than"):
        voice.decode_audio(buf.getvalue())


def test_decode_rejects_garbage():
    with pytest.raises(voice.VoiceError):
        voice.decode_audio(b"not audio at all" * 20)


async def test_transcribe_with_fake_whisper_cuda_fallback(monkeypatch):
    import faster_whisper

    created = []

    class FakeWhisper:
        def __init__(self, name, device, compute_type):
            created.append((name, device, compute_type))
            self.device = device

        def transcribe(self, audio, **kw):
            assert audio.dtype == np.float32
            if self.device == "cuda":
                raise RuntimeError("cublas64_12.dll not found")
            return iter([SimpleNamespace(text=" take a "), SimpleNamespace(text="screenshot ")]), None

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisper)
    monkeypatch.setattr(voice, "_cuda_available", lambda: True)
    monkeypatch.setattr(voice, "_model", None)
    monkeypatch.setenv("WHISPER_MODEL", "small")
    monkeypatch.setenv("WHISPER_DEVICE", "auto")
    text = await voice.transcribe(voice.to_ogg_opus(_sine_mp3()))
    assert text == "take a screenshot"
    assert created == [("small", "cuda", "float16"), ("small", "cpu", "int8")]
    assert "cpu/int8" in voice.model_info()
    await voice.transcribe(voice.to_ogg_opus(_sine_mp3()))
    assert len(created) == 2  # loaded once
    monkeypatch.setattr(voice, "_model", None)


# ====================================================================== telegram module
def test_module_contract():
    from telegram.ext import Application

    from bot.modules import jarvis as mod

    names = [c for c, _ in mod.COMMANDS]
    assert names == ["j", "voice", "persona", "forget"]
    for h in (mod.cmd_j, mod.on_voice, mod.cmd_voice, mod.cmd_persona, mod.cmd_forget, mod.on_button):
        assert hasattr(h, "__wrapped__"), f"{h.__name__} must be @owner_only"
    app = Application.builder().token("123:abc").build()
    mod.register(app)
    hs = app.handlers[0]
    assert len(hs) == 6
    cb = [h for h in hs if type(h).__name__ == "CallbackQueryHandler"][0]
    assert cb.pattern.pattern == "^jarvis:"


class FakeMessage:
    def __init__(self):
        self.replies: list[tuple[str, object]] = []
        self.voice = None
        self.audio = None
        self.text = "/j hi"

    async def reply_text(self, text, **kw):
        self.replies.append(("text", text))

    async def reply_voice(self, data, **kw):
        self.replies.append(("voice", data))


def _update(user_id):
    msg = FakeMessage()
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="o"),
                           effective_chat=SimpleNamespace(id=555, type="private"), effective_message=msg,
                           callback_query=None), msg


async def test_cmd_j_end_to_end(monkeypatch, tmp_path):
    from bot.config import settings
    from bot.modules import jarvis as mod

    mem = Memory(state_path=tmp_path / "s.json", default_voice=True)
    monkeypatch.setattr(mod, "memory", mem)

    class StubBrain:
        async def ask(self, bot, chat_id, text, *, history, persona):
            assert chat_id == 555 and text == "hi" and history == []
            return brain.BrainReply(text="Hello, all systems nominal.", actions=["get_status"])
    monkeypatch.setattr(brain, "get_brain", lambda: StubBrain())

    async def fake_synth(text, **kw):
        return b"OggS-fake"
    monkeypatch.setattr(voice, "synthesize", fake_synth)

    owner = next(iter(settings.allowed_user_ids))
    upd, msg = _update(owner)
    ctx = SimpleNamespace(args=["hi"], bot=FakeBot())
    await mod.cmd_j(upd, ctx)
    assert msg.replies == [("text", "Hello, all systems nominal."), ("voice", b"OggS-fake")]
    h = mem.history(555)
    assert h[0] == {"role": "user", "content": "hi"} and "[ran: get_status]" in h[1]["content"]

    # strangers get silence
    upd2, msg2 = _update(owner + 1)
    await mod.cmd_j(upd2, ctx)
    assert msg2.replies == []


async def test_cmd_j_reports_config_error(monkeypatch, tmp_path):
    from bot.config import settings
    from bot.modules import jarvis as mod

    monkeypatch.setattr(mod, "memory", Memory(state_path=tmp_path / "s.json", default_voice=False))

    def boom():
        raise brain.BrainConfigError("Edith has no Claude API key.")
    monkeypatch.setattr(brain, "get_brain", boom)
    upd, msg = _update(next(iter(settings.allowed_user_ids)))
    await mod.cmd_j(upd, SimpleNamespace(args=["hi"], bot=FakeBot()))
    assert msg.replies == [("text", "⚙️ Edith has no Claude API key.")]
