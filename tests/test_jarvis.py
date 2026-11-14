"""Edith slice tests. No network, no model downloads, no real OS actions:
LLM clients, Whisper, edge-tts and Telegram are all faked."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
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
        def __init__(self, name, device, compute_type, **kw):
            created.append((name, device, compute_type))
            self.device = device
            assert kw.get("cpu_threads", 1) >= 1 if device == "cpu" else not kw

        def transcribe(self, audio, **kw):
            assert audio.dtype == np.float32
            if self.device == "cuda":
                raise RuntimeError("cublas64_12.dll not found")
            assert kw["beam_size"] == 1 and kw["vad_filter"] and "Edith" in kw["initial_prompt"]
            return (iter([SimpleNamespace(text=" take a "), SimpleNamespace(text="screenshot ")]),
                    SimpleNamespace(language="en", language_probability=0.99))

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
    for h in (mod.cmd_j, mod.on_text, mod.on_voice, mod.cmd_voice, mod.cmd_persona, mod.cmd_forget,
              mod.on_button):
        assert hasattr(h, "__wrapped__"), f"{h.__name__} must be @owner_only"
    app = Application.builder().token("123:abc").build()
    mod.register(app)
    hs = app.handlers[0]
    assert len(hs) == 6
    cb = [h for h in hs if type(h).__name__ == "CallbackQueryHandler"][0]
    assert cb.pattern.pattern == "^jarvis:"
    assert [h.callback for h in app.handlers[mod.TEXT_GROUP]] == [mod.on_text]  # later group
    # warm-up is scheduled, not run: registering must never block or load models
    jobs = {j.name for j in app.job_queue.jobs()}
    assert {"edith-warmup", "edith-llm-keepalive"} <= jobs


class FakeSent:
    """A message the bot sent; records edits."""

    def __init__(self, owner, text):
        self.owner, self.text = owner, text

    async def edit_text(self, text, **kw):
        self.owner.edits.append(text)
        self.text = text


class FakeMessage:
    def __init__(self, text="/j hi"):
        self.replies: list[tuple[str, object]] = []
        self.edits: list[str] = []
        self.voice = None
        self.audio = None
        self.text = text

    async def reply_text(self, text, **kw):
        self.replies.append(("text", text))
        return FakeSent(self, text)

    async def reply_voice(self, data, **kw):
        self.replies.append(("voice", data))

    async def reply_document(self, *a, **kw):
        self.replies.append(("document", kw.get("filename")))


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
        async def ask(self, bot, chat_id, text, *, history, persona, progress):
            assert chat_id == 555 and text == "hi" and history == []
            await progress("Running get_status…")
            return brain.BrainReply(text="Hello, all systems nominal.", actions=["get_status"])
    monkeypatch.setattr(brain, "get_brain", lambda: StubBrain())
    monkeypatch.setattr(brain, "match_intents", lambda text: None)

    async def fake_synth(text, **kw):
        return b"OggS-fake"
    monkeypatch.setattr(voice, "synthesize", fake_synth)

    owner = next(iter(settings.allowed_user_ids))
    upd, msg = _update(owner)
    ctx = SimpleNamespace(args=["hi"], bot=FakeBot())
    await mod.cmd_j(upd, ctx)
    # one status message, edited through the stages and finally into the answer; typed -> no voice
    assert msg.replies == [("text", "⏳ Thinking…")]
    assert msg.edits == ["⏳ Running get_status…", "Hello, all systems nominal."]
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
    monkeypatch.setattr(brain, "match_intents", lambda text: None)
    upd, msg = _update(next(iter(settings.allowed_user_ids)))
    await mod.cmd_j(upd, SimpleNamespace(args=["hi"], bot=FakeBot()))
    assert msg.edits[-1] == "⚙️ Edith has no Claude API key."


# ====================================================================== round 2: never silent
import asyncio  # noqa: E402


def _openai_brain(fake, monkeypatch, base="http://localhost:11434/v1", model="qwen3:4b", **kw):
    monkeypatch.setenv("LLM_BASE_URL", base)
    return brain.OpenAICompatBrain(fake, model=model, **kw)


class RaisingOpenAI(FakeOpenAI):
    def __init__(self, exc):
        super().__init__([])
        self.exc = exc

    async def _create(self, **kw):
        self.requests.append(kw)
        raise self.exc


def _req():
    import httpx2
    return httpx2.Request("POST", "http://localhost:11434/v1/chat/completions")


def _oreq():
    import httpx
    return httpx.Request("POST", "http://localhost:11434/v1/chat/completions")


async def test_openai_timeout_and_refused_messages(monkeypatch):
    import openai

    b = _openai_brain(RaisingOpenAI(openai.APITimeoutError(request=_oreq())), monkeypatch)
    r = await b.ask(FakeBot(), 1, "hi")
    assert not r.ok and "qwen3:4b" in r.text and "did not answer within 60 seconds" in r.text
    assert "Ollama" in r.text

    b = _openai_brain(RaisingOpenAI(openai.APIConnectionError(request=_oreq())), monkeypatch)
    r = await b.ask(FakeBot(), 1, "hi")
    assert not r.ok and r.text.startswith("Ollama is not running") and "start Ollama" in r.text

    b = _openai_brain(RaisingOpenAI(openai.APITimeoutError(request=_oreq())), monkeypatch,
                      base="https://api.groq.com/openai/v1", model="llama-3.3-70b")
    r = await b.ask(FakeBot(), 1, "hi")
    assert "llama-3.3-70b" in r.text and "control config set LLM_MODEL" in r.text and "Ollama" not in r.text


async def test_openai_hung_model_hits_hard_backstop(monkeypatch):
    class Hang(FakeOpenAI):
        async def _create(self, **kw):
            await asyncio.sleep(60)
    b = _openai_brain(Hang([]), monkeypatch)
    b.timeout = 0.05
    r = await asyncio.wait_for(b.ask(FakeBot(), 1, "hi"), 5)
    assert not r.ok and "did not answer" in r.text


def test_clients_use_short_timeout_and_one_retry(monkeypatch):
    monkeypatch.setenv("EDITH_LLM_TIMEOUT", "42")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3:4b")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    c = brain.OpenAICompatBrain().client
    assert c.max_retries == 1 and c.timeout == 42
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    a = brain.AnthropicBrain().client
    assert a.max_retries == 1 and a.timeout == 42


async def test_anthropic_timeout_message(actions):
    import anthropic

    class T(FakeAnthropic):
        async def _create(self, **kw):
            raise anthropic.APITimeoutError(request=_req())
    r = await brain.AnthropicBrain(T([])).ask(FakeBot(), 1, "x")
    assert not r.ok and "did not answer within" in r.text and "claude-opus-5-5" in r.text


async def test_turn_budget_stops_the_loop(monkeypatch, actions):
    monkeypatch.setattr(brain, "turn_budget", lambda: -1.0)
    fake = FakeOpenAI([])
    r = await _openai_brain(fake, monkeypatch).ask(FakeBot(), 1, "x")
    assert not r.ok and "taking too long" in r.text and fake.requests == []


# ====================================================================== round 2: local-model quality
def test_strip_think_variants():
    assert brain.strip_think("<think>\nhmm\n</think>\n\nYour CPU is at 12%.") == "Your CPU is at 12%."
    assert brain.strip_think("reasoning leaked</think>Answer.") == "Answer."
    assert brain.strip_think("<think>never closed, all reasoning") == ""
    assert brain.strip_think("Plain answer.") == "Plain answer."


async def test_think_text_never_reaches_the_owner(monkeypatch, actions):
    fake = FakeOpenAI([omsg(content="<think>The user wants a greeting.</think>Hello! Ready when you are.")])
    r = await _openai_brain(fake, monkeypatch).ask(FakeBot(), 1, "hello Edith")
    assert r.text == "Hello! Ready when you are." and r.ok


async def test_empty_answer_is_a_failed_turn(monkeypatch, actions):
    fake = FakeOpenAI([omsg(content="<think>only thinking</think>")])
    r = await _openai_brain(fake, monkeypatch).ask(FakeBot(), 1, "hi")
    assert not r.ok and "didn't get an answer" in r.text


def test_no_think_switch_only_for_hybrid_qwen3(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    assert brain.OpenAICompatBrain(object(), model="qwen3:4b")._system(None).endswith("/no_think")
    assert "/no_think" not in brain.OpenAICompatBrain(object(), model="qwen3:4b-instruct")._system(None)
    assert "/no_think" not in brain.OpenAICompatBrain(object(), model="llama3.2:3b")._system(None)


async def test_local_requests_are_compact_with_keep_alive(monkeypatch):
    from bot.actions.registry import load_all

    saved = dict(r.REGISTRY)
    load_all()  # the real 50 actions
    try:
        fake = FakeOpenAI([omsg(content="ok")])
        b = _openai_brain(fake, monkeypatch)
        assert b.compact
        await b.ask(FakeBot(), 1, "What is my CPU and RAM usage right now?")
        req = fake.requests[0]
        names = [t["function"]["name"] for t in req["tools"]]
        assert "system_status" in names and len(names) <= 10
        assert len(json.dumps(req["tools"])) < 3000  # vs ~19k chars for the full list
        assert req["extra_body"] == {"keep_alive": "30m"}
        assert req["messages"][0]["content"].startswith(brain.DEFAULT_PERSONA)
        assert "Rules:\n1." in req["messages"][0]["content"]  # short numbered rules
        for t in req["tools"]:
            assert "chat_id" not in t["function"]["parameters"]["properties"]  # S-6 still holds

        remote = FakeOpenAI([omsg(content="ok")])
        rb = _openai_brain(remote, monkeypatch, base="https://api.groq.com/openai/v1")
        assert not rb.compact
        await rb.ask(FakeBot(), 1, "What is my CPU usage?")
        assert len(remote.requests[0]["tools"]) == len(r.REGISTRY) and "extra_body" not in remote.requests[0]
    finally:
        r.REGISTRY.clear()
        r.REGISTRY.update(saved)


def test_select_tools_picks_relevant_ones():
    from bot.actions.registry import load_all

    saved = dict(r.REGISTRY)
    load_all()
    try:
        tools = brain.model_tools()

        def pick(text, ctx=""):
            return [t["name"] for t in brain.select_tools(text, tools, context=ctx)]
        assert pick("What is my CPU and RAM usage right now?") == ["system_status"]
        assert {"procs", "system_status"} <= set(pick("check my battery and tell me the top 3 processes"))
        assert "screenshot" in pick("show me the screen")
        assert set(pick("hello Edith", ctx="What is my CPU and RAM usage?")) == set(brain.CORE_TOOLS)
        assert all(len(pick(q)) <= 10 for q in ("open chrome and play music and mute", "kill the process"))
        compact = brain.compact_tools([t for t in tools if t["name"] in ("power_shutdown", "create_reminder")])
        by = {t["name"]: t for t in compact}
        assert by["power_shutdown"]["description"].endswith("Asks the owner to confirm.")
        assert all(len(t["description"]) <= 150 for t in compact)
    finally:
        r.REGISTRY.clear()
        r.REGISTRY.update(saved)


# ====================================================================== round 2: sloppy tool calls + fix B
async def test_sloppy_tool_calls_are_tolerated(actions, confirm):
    reply = brain.BrainReply(text="")
    # dict instead of JSON string, wrong case + prefix in the name, extra keys, enum case
    out, err = await brain.execute_tool(FakeBot(), 1, "functions.Get_Status", {"detail": "FULL", "x": 1}, reply)
    assert not err and actions[-1] == ("get_status", {"detail": "full"})
    out, err = await brain.execute_tool(FakeBot(), 1, "kill_proc", '{"pid": "1234"}', reply)  # numeric string
    assert not err and actions[-1] == ("kill_proc", {"pid": 1234})
    out, err = await brain.execute_tool(FakeBot(), 1, "kill_proc", {"pid": "77.0"}, reply)
    assert not err and actions[-1] == ("kill_proc", {"pid": 77})
    out, err = await brain.execute_tool(FakeBot(), 1, "get_statuz", {}, reply)
    assert err and "Close matches: get_status" in out
    out, err = await brain.execute_tool(FakeBot(), 1, "get_status", "{broken", reply)
    assert err and "Invalid JSON" in out


async def test_tool_calls_keep_provider_fields(monkeypatch, actions):
    """Fix B: Gemini 3 needs extra_content.thought_signature echoed back on round 2."""
    from openai.types.chat import ChatCompletion

    first = ChatCompletion.model_validate({
        "id": "c1", "object": "chat.completion", "created": 0, "model": "gemini-3-flash",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "get_status", "arguments": "{}"},
                            "extra_content": {"google": {"thought_signature": "SIG=="}}}]}}]})
    fake = FakeOpenAI([first, omsg(content="All good.")])
    b = _openai_brain(fake, monkeypatch, base="https://generativelanguage.googleapis.com/v1beta/openai/",
                      model="gemini-3-flash")
    r = await b.ask(FakeBot(), 1, "status?")
    assert r.text == "All good."
    sent = fake.requests[1]["messages"][2]["tool_calls"][0]
    assert sent["extra_content"] == {"google": {"thought_signature": "SIG=="}}
    assert sent["id"] == "call_1" and sent["function"]["name"] == "get_status"


async def test_tool_call_written_as_text_is_executed(monkeypatch, actions):
    fake = FakeOpenAI([
        omsg(content='<tool_call>\n{"name": "get_status", "arguments": {"detail": "short"}}\n</tool_call>'),
        omsg(content="CPU 12 percent."),
    ])
    r = await _openai_brain(fake, monkeypatch).ask(FakeBot(), 1, "cpu?")
    assert actions == [("get_status", {"detail": "short"})] and r.text == "CPU 12 percent."
    assert "Tool results:" in fake.requests[1]["messages"][-1]["content"]


# ====================================================================== round 2: instant path
def test_match_intents_is_defensive(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "bot.jarvis.intents", None)  # module missing -> ImportError
    monkeypatch.delattr(sys.modules["bot.jarvis"], "intents", raising=False)  # package attribute would mask it
    assert brain.match_intents("lock the pc") is None

    class Broken:
        @staticmethod
        def match(text):
            raise RuntimeError("bug")
    monkeypatch.setitem(sys.modules, "bot.jarvis.intents", Broken)
    monkeypatch.setattr(sys.modules["bot.jarvis"], "intents", Broken, raising=False)
    assert brain.match_intents("lock the pc") is None


@dataclass
class _Intent:
    action: str
    args: dict
    say: str


async def test_instant_intents_skip_llm_but_still_confirm(monkeypatch, actions, confirm):
    confirm["answer"] = False
    reply = await brain.run_intents(FakeBot(), 1, [
        _Intent("get_status", {}, "Here's your system status."),
        _Intent("get_status", {}, "Here's your system status."),  # duplicate -> runs once
        _Intent("kill_proc", {"pid": 5}, "Killing it."),
    ])
    assert actions == [("get_status", {"detail": "short"})]  # dangerous one declined, never ran
    assert confirm["asked"] == ["Edith wants to run: kill_proc(pid=5)"]
    assert reply.declined == ["kill_proc"]
    assert "Here's your system status.\nCPU 12%, RAM 40%" in reply.text  # numbers shown in text
    assert reply.spoken == "Here's your system status. Okay, I cancelled kill proc."


async def test_turn_uses_instant_path_without_llm(monkeypatch, tmp_path, actions):
    from bot.config import settings
    from bot.modules import jarvis as mod

    monkeypatch.setattr(mod, "memory", Memory(state_path=tmp_path / "s.json", default_voice=True))
    monkeypatch.setattr(brain, "match_intents", lambda text: [_Intent("snap", {}, "Taking a screenshot.")])

    def no_llm():
        raise AssertionError("the LLM must not be called for an instant intent")
    monkeypatch.setattr(brain, "get_brain", no_llm)
    upd, msg = _update(next(iter(settings.allowed_user_ids)))
    bot = FakeBot()
    await mod.on_text(upd, SimpleNamespace(args=[], bot=bot))
    assert actions == [("snap", {})]
    assert [s[0] for s in bot.sent if s[0] != "send_chat_action"] == ["send_photo"]
    assert msg.edits[-1] == "Taking a screenshot.\nScreenshot taken."


# ====================================================================== round 2: plain chat routing
def test_plain_text_reaches_edith_and_nothing_else():
    """Plain owner text goes to Edith from a later group; no group-0 handler of ANY module wants it,
    and commands/edits never reach Edith's text handler."""
    import importlib

    from telegram import Update
    from telegram.ext import Application

    from bot.modules import MODULE_NAMES
    from bot.modules import jarvis as mod

    app = Application.builder().token("123:abc").build()
    for name in MODULE_NAMES:
        importlib.import_module(f"bot.modules.{name}").register(app)

    def upd(text, *, edited=False):
        key = "edited_message" if edited else "message"
        msg = {"message_id": 1, "date": 0, "text": text,
               "chat": {"id": 5, "type": "private"}, "from": {"id": 5, "is_bot": False, "first_name": "o"}}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return Update.de_json({"update_id": 1, key: msg}, app.bot)

    plain = upd("what is my cpu usage")
    takers = [h for hs in app.handlers.values() for h in hs if h.check_update(plain)]
    assert [h.callback for h in takers] == [mod.on_text]
    text_handler = app.handlers[mod.TEXT_GROUP][0]
    assert not text_handler.check_update(upd("/status"))
    assert not text_handler.check_update(upd("edited", edited=True))


# ====================================================================== round 2: feedback + voice flow
async def test_voice_note_flow_heard_status_text_and_voice(monkeypatch, tmp_path):
    from bot.config import settings
    from bot.modules import jarvis as mod

    monkeypatch.setattr(mod, "memory", Memory(state_path=tmp_path / "s.json", default_voice=True))
    monkeypatch.setattr(brain, "match_intents", lambda text: None)

    async def fake_transcribe(data):
        assert data == b"OGGDATA"
        return "what is my cpu"
    monkeypatch.setattr(voice, "transcribe", fake_transcribe)
    spoken = []

    async def fake_synth(text, **kw):
        spoken.append(text)
        return b"OggS"
    monkeypatch.setattr(voice, "synthesize", fake_synth)

    class StubBrain:
        async def ask(self, bot, chat_id, text, *, history, persona, progress):
            await progress("Thinking…")
            return brain.BrainReply(text="Your CPU is at 12 percent.")
    monkeypatch.setattr(brain, "get_brain", lambda: StubBrain())

    class TgFile:
        async def download_as_bytearray(self):
            return bytearray(b"OGGDATA")

    bot = FakeBot()

    async def get_file(fid):
        return TgFile()
    bot.get_file = get_file
    upd, msg = _update(next(iter(settings.allowed_user_ids)))
    msg.voice = SimpleNamespace(file_id="f1", file_size=1000)
    await mod.on_voice(upd, SimpleNamespace(args=[], bot=bot))
    heard = "🎙 Heard: “what is my cpu”"
    assert msg.replies[0] == ("text", "⏳ Listening…")
    assert f"{heard}\n⏳ Thinking…" in msg.edits and msg.edits[-1] == heard  # transcript kept
    assert ("text", "Your CPU is at 12 percent.") in msg.replies and msg.replies[-1] == ("voice", b"OggS")
    assert spoken == ["Your CPU is at 12 percent."]


async def test_failed_turn_is_not_stored_and_never_silent(monkeypatch, tmp_path):
    from bot.config import settings
    from bot.modules import jarvis as mod

    mem = Memory(state_path=tmp_path / "s.json", default_voice=True)
    monkeypatch.setattr(mod, "memory", mem)
    monkeypatch.setattr(brain, "match_intents", lambda text: None)

    class Failing:
        async def ask(self, *a, **kw):
            return brain.BrainReply(text="The local model qwen3:4b did not answer within 60 seconds.", ok=False)
    monkeypatch.setattr(brain, "get_brain", lambda: Failing())
    upd, msg = _update(next(iter(settings.allowed_user_ids)))
    await mod.cmd_j(upd, SimpleNamespace(args=["hi"], bot=FakeBot()))
    assert msg.edits[-1].startswith("The local model qwen3:4b did not answer")
    assert mem.history(555) == []

    class Crashing:
        async def ask(self, *a, **kw):
            raise RuntimeError("kaboom")
    monkeypatch.setattr(brain, "get_brain", lambda: Crashing())
    upd, msg = _update(next(iter(settings.allowed_user_ids)))
    await mod.cmd_j(upd, SimpleNamespace(args=["hi"], bot=FakeBot()))
    assert "Edith error: RuntimeError: kaboom" in msg.edits[-1] and mem.history(555) == []


async def test_warmup_jobs_never_raise(monkeypatch):
    from bot.modules import jarvis as mod

    def bad_whisper():
        raise RuntimeError("no model")
    monkeypatch.setattr(voice, "warmup", bad_whisper)

    class BadBrain:
        async def warmup(self):
            raise ConnectionError("ollama down")
    monkeypatch.setattr(brain, "get_brain", lambda: BadBrain())
    await mod.warmup_job(None)  # must not raise
    await mod.keepalive_job(None)


async def test_ollama_preload_uses_native_keep_alive(monkeypatch):
    import httpx

    posted = []

    class FakeResp:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted.append((url, json))
            return FakeResp()
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    await _openai_brain(object(), monkeypatch).warmup()
    assert posted == [("http://localhost:11434/api/generate", {"model": "qwen3:4b", "keep_alive": "30m"})]
    posted.clear()
    await _openai_brain(object(), monkeypatch, base="https://api.groq.com/openai/v1").warmup()
    monkeypatch.setenv("EDITH_KEEP_ALIVE", "off")
    await _openai_brain(object(), monkeypatch).warmup()
    assert posted == []


# ====================================================================== round 2: voice on any laptop
def test_pick_model_for_hardware(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "auto")
    monkeypatch.setattr(voice, "_physical_cores", lambda: 4)
    monkeypatch.setattr(voice, "_ram_gb", lambda: 16.0)
    assert voice.pick_model("cuda") == "small" and voice.pick_model("cpu") == "small"
    monkeypatch.setattr(voice, "_physical_cores", lambda: 2)
    assert voice.pick_model("cpu") == "base"  # weak laptop: ~2x faster model
    monkeypatch.setattr(voice, "_physical_cores", lambda: 8)
    monkeypatch.setattr(voice, "_ram_gb", lambda: 4.0)
    assert voice.pick_model("cpu") == "base"
    monkeypatch.setenv("WHISPER_MODEL", "tiny.en")
    assert voice.pick_model("cpu") == "tiny.en"
    monkeypatch.setenv("WHISPER_LANGUAGE", "auto")
    assert voice.language() is None
    monkeypatch.setenv("WHISPER_LANGUAGE", "EN")
    assert voice.language() == "en"


def test_prompt_echo_is_never_a_command():
    p = voice.DEFAULT_PROMPT
    assert voice._is_prompt_echo("Edith, screenshot, CPU, RAM, battery.", p)
    assert voice._is_prompt_echo("shut down, restart, open Chrome", p)
    assert not voice._is_prompt_echo("Edith take a screenshot", p)
    assert not voice._is_prompt_echo("mute", p)  # too short to judge: kept


async def test_sticky_language_skips_detection_and_rechecks(monkeypatch):
    calls = []

    def seg(text, lp):
        return SimpleNamespace(text=text, avg_logprob=lp, no_speech_prob=0.1)

    class W:
        def transcribe(self, audio, **kw):
            calls.append(kw["language"])
            if len(calls) == 3:  # third note: forced "en" result looks poor -> re-detect
                return iter([seg("bonjour", -1.5)]), SimpleNamespace(language="en", language_probability=1.0)
            detected = "fr" if len(calls) >= 3 else "en"
            return iter([seg("ok", -0.2)]), SimpleNamespace(language=detected, language_probability=0.95)

    monkeypatch.setattr(voice, "_model", W())
    monkeypatch.setattr(voice, "_model_device", "cpu")
    monkeypatch.setattr(voice, "_sticky_lang", None)
    monkeypatch.setattr(voice, "decode_audio", lambda data: np.ones(16000, dtype=np.float32))
    monkeypatch.setenv("WHISPER_LANGUAGE", "auto")
    await voice.transcribe(b"x")
    await voice.transcribe(b"x")
    await voice.transcribe(b"x")
    assert calls == [None, "en", "en", None]  # detect once, reuse, re-detect when poor
    assert voice._sticky_lang == "fr"


def test_spoken_replies_are_short():
    out = voice.speakable("<think>secret plan</think>" + "This is a sentence. " * 100)
    assert "secret" not in out and len(out) <= voice.MAX_TTS_CHARS + 40


# ---------------------------------------------------------------- hosted NVIDIA speech-to-text
def test_stt_provider_selection(monkeypatch):
    for k in ("STT_PROVIDER", "NVIDIA_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert voice.stt_provider() == "local"
    monkeypatch.setenv("LLM_API_KEY", "nvapi-test")
    assert voice.stt_provider() == "nvidia"
    monkeypatch.setenv("STT_PROVIDER", "local")
    assert voice.stt_provider() == "local"
    monkeypatch.setenv("STT_PROVIDER", "auto")
    monkeypatch.setenv("LLM_API_KEY", "AIza-not-nvidia")
    assert voice.stt_provider() == "local"


async def test_nvidia_stt_used_and_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "nvapi-test")
    monkeypatch.delenv("STT_PROVIDER", raising=False)
    monkeypatch.setattr(voice, "_transcribe_nvidia_sync", lambda data: "hosted text")
    monkeypatch.setattr(voice, "_transcribe_sync", lambda data: "local text")
    assert await voice.transcribe(b"x") == "hosted text"

    def boom(data):
        raise ConnectionError("nvapi-test must never be shown")
    monkeypatch.setattr(voice, "_transcribe_nvidia_sync", boom)
    assert await voice.transcribe(b"x") == "local text"
