"""PIN for the riskiest actions: bot/pin.py helpers + the keypad flow in bot/confirm.py.
Fake bot and fake callback queries only - nothing real runs."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import confirm, pin
from bot.actions import policy
from bot.actions import registry as r
from bot.config import settings

OWNER = sorted(settings.allowed_user_ids)[0]
PIN = "4821"


@pytest.fixture
def pin_on(monkeypatch):
    monkeypatch.setenv("EDITH_PIN_HASH", pin.make_pin_hash(PIN, iterations=1000))
    monkeypatch.delenv("EDITH_PIN_ACTIONS", raising=False)
    monkeypatch.setattr(confirm, "_wrong_pins", 0)
    yield


@pytest.fixture
def pin_off(monkeypatch):
    monkeypatch.delenv("EDITH_PIN_HASH", raising=False)
    monkeypatch.delenv("EDITH_PIN_ACTIONS", raising=False)


# ================================================================ bot/pin.py
def test_hash_format_and_check(monkeypatch):
    h = pin.make_pin_hash("123456")
    algo, it, salt, digest = h.split("$")
    assert algo == "pbkdf2_sha256" and it == "200000" and len(bytes.fromhex(salt)) == 16 and len(digest) == 64
    assert pin.make_pin_hash("123456") != h                         # random salt
    monkeypatch.setenv("EDITH_PIN_HASH", h)
    assert pin.pin_enabled() and pin.pin_status() == "PIN on"
    assert pin.check_pin("123456")
    for bad in ("123457", "12345", "", "abcd", "1234567890", None):
        assert not pin.check_pin(bad)


@pytest.mark.parametrize("bad", ["123", "123456789", "12a4", " 1234", "", "١٢٣٤"])
def test_make_pin_hash_rejects_bad_pins(bad):
    with pytest.raises(ValueError):
        pin.make_pin_hash(bad)


def test_malformed_hash_fails_closed(monkeypatch):
    for value in ("garbage", "md5$1$aa$bb", f"pbkdf2_sha256$10$aa${'0' * 64}", "pbkdf2_sha256$x$zz$yy"):
        monkeypatch.setenv("EDITH_PIN_HASH", value)
        assert pin.pin_enabled()                                    # still required...
        assert not pin.check_pin("1234")                            # ...but nothing unlocks it
        assert "INVALID" in pin.pin_status()
        assert pin.pin_required("shell_cmd")


def test_pin_required(pin_on, monkeypatch):
    for name in policy.ALWAYS_CONFIRM | {"power_shutdown", "power_restart", "power_logoff"}:
        assert pin.pin_required(name), name
    assert not pin.pin_required("kill_process") and not pin.pin_required("screenshot")
    assert not pin.pin_required(None) and not pin.pin_required("")
    monkeypatch.setenv("EDITH_PIN_ACTIONS", "kill_process, Webcam_Photo")
    assert pin.pin_required("kill_process") and pin.pin_required("webcam_photo")


def test_pin_not_required_when_off(pin_off):
    assert not pin.pin_enabled() and pin.pin_status() == "PIN off"
    assert not pin.pin_required("shell_cmd")


# ================================================================ fakes for the Telegram side
class FakeMsg:
    def __init__(self, bot, text, reply_markup):
        self.bot, self.text, self.reply_markup = bot, text, reply_markup

    async def edit_text(self, text, **kw):
        self.text = text


class FakeBot:
    def __init__(self):
        self.messages: list[FakeMsg] = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        m = FakeMsg(self, text, reply_markup)
        m.chat_id = chat_id
        self.messages.append(m)
        return m


def buttons(msg: FakeMsg) -> list[str]:
    return [b.callback_data for row in msg.reply_markup.inline_keyboard for b in row]


async def press(bot: FakeBot, msg: FakeMsg, data: str, *, user: int = OWNER, chat: int = OWNER,
                chat_type: str = "private") -> list[str]:
    answers: list[str] = []

    async def answer(text=None, **kw):
        answers.append(text)

    async def edit_message_text(text, reply_markup=None, **kw):
        msg.text = text
        if reply_markup is not None:
            msg.reply_markup = reply_markup

    q = SimpleNamespace(data=data, answer=answer, edit_message_text=edit_message_text,
                        message=SimpleNamespace(text=msg.text))
    upd = SimpleNamespace(callback_query=q, effective_message=None,
                          effective_user=SimpleNamespace(id=user, username="u"),
                          effective_chat=SimpleNamespace(id=chat, type=chat_type))
    await confirm._on_button(upd, SimpleNamespace(bot=bot))
    return answers


async def start_prompt(bot: FakeBot, action: str | None, summary: str = "Run dir", timeout: float = 5):
    task = asyncio.create_task(confirm.ask_confirm(bot, OWNER, summary, timeout=timeout, action=action))
    for _ in range(10):
        await asyncio.sleep(0)
        if bot.messages:
            break
    msg = bot.messages[-1]
    token = buttons(msg)[0].split(":")[1]
    return task, msg, token


async def type_pin(bot, msg, token, digits):
    for d in digits:
        await press(bot, msg, f"confirm:{token}:{d}")


# ================================================================ keypad flow
async def test_keypad_layout_and_masking(pin_on):
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "shell_cmd")
    assert [d.rsplit(":", 1)[1] for d in buttons(msg)] == list("123456789") + ["b", "0", "k", "n"]
    assert "PIN required" in msg.text and "Run dir" in msg.text
    await type_pin(bot, msg, token, "482")
    assert "•••" in msg.text and "482" not in msg.text
    await press(bot, msg, f"confirm:{token}:b")
    assert "••</code>" in msg.text
    await press(bot, msg, f"confirm:{token}:n")
    assert await task is False and confirm._PENDING == {}


async def test_correct_pin_confirms(pin_on):
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "shell_cmd")
    await type_pin(bot, msg, token, PIN)
    answers = await press(bot, msg, f"confirm:{token}:k")
    assert answers == ["PIN accepted"]
    assert await task is True
    assert PIN not in msg.text and msg.text.startswith("✅ Confirmed (PIN)")


async def test_plain_yes_cannot_bypass_pin(pin_on):
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "delete_path")
    answers = await press(bot, msg, f"confirm:{token}:y")           # forged plain "Confirm"
    assert answers == ["Enter the PIN, then ✅ OK."] and not task.done()
    assert await press(bot, msg, f"confirm:{token}:k") == ["Enter the PIN first."] and not task.done()
    await press(bot, msg, f"confirm:{token}:n")
    assert await task is False


async def test_pin_prompt_is_bound_to_owner_and_chat(pin_on):
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "shell_cmd")
    await type_pin(bot, msg, token, PIN)
    await press(bot, msg, f"confirm:{token}:k", user=999)            # stranger: silently ignored
    assert not task.done()
    await press(bot, msg, f"confirm:{token}:k", chat=-100, chat_type="group")
    assert not task.done()
    assert await press(bot, msg, "confirm:0000000000000000:k") == ["This confirmation expired."]
    await press(bot, msg, f"confirm:{token}:k")
    assert await task is True
    assert await press(bot, msg, f"confirm:{token}:k") == ["This confirmation expired."]   # replay


async def test_wrong_pins_retry_then_lockdown(pin_on, monkeypatch):
    calls = []
    monkeypatch.setattr(policy, "set_lockdown", lambda on, reason="": calls.append((on, reason)), raising=False)
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "power_shutdown")
    await type_pin(bot, msg, token, "0000")
    assert await press(bot, msg, f"confirm:{token}:k") == ["Wrong PIN"]
    assert "2 tries left" in msg.text and not task.done()            # same prompt, can retry
    await type_pin(bot, msg, token, "1111")
    await press(bot, msg, f"confirm:{token}:k")
    assert "1 try left" in msg.text
    await type_pin(bot, msg, token, "2222")
    await press(bot, msg, f"confirm:{token}:k")
    assert await task is False
    assert calls == [(True, "3 wrong PINs")]
    assert msg.text.startswith("🔒 Not run")
    assert "LOCKDOWN" in bot.messages[-1].text


async def test_wrong_count_spans_prompts_and_resets_on_success(pin_on, monkeypatch):
    calls = []
    monkeypatch.setattr(policy, "set_lockdown", lambda on, reason="": calls.append(reason), raising=False)
    bot = FakeBot()
    for _ in range(2):                                             # 2 wrong, then give up each time
        task, msg, token = await start_prompt(bot, "shell_cmd")
        await type_pin(bot, msg, token, "9999")
        await press(bot, msg, f"confirm:{token}:k")
        await press(bot, msg, f"confirm:{token}:n")
        assert await task is False
    assert confirm._wrong_pins == 2
    task, msg, token = await start_prompt(bot, "shell_cmd")
    await type_pin(bot, msg, token, PIN)
    await press(bot, msg, f"confirm:{token}:k")
    assert await task is True and confirm._wrong_pins == 0 and calls == []


async def test_lockdown_missing_is_tolerated(pin_on, monkeypatch):
    monkeypatch.delattr(policy, "set_lockdown", raising=False)
    monkeypatch.setattr(confirm, "_wrong_pins", 2)
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "shell_cmd")
    await type_pin(bot, msg, token, "0000")
    await press(bot, msg, f"confirm:{token}:k")
    assert await task is False and "cancelled" in bot.messages[-1].text


async def test_pin_prompt_times_out(pin_on):
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "shell_cmd", timeout=0.05)
    await type_pin(bot, msg, token, "48")
    assert await task is False and msg.text.startswith("⌛ Expired") and confirm._PENDING == {}


async def test_digit_limit(pin_on):
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "shell_cmd")
    await type_pin(bot, msg, token, "1234567890")
    assert confirm._PENDING[token].digits == "12345678"
    await press(bot, msg, f"confirm:{token}:n")
    await task


async def test_plain_prompt_unchanged_without_pin(pin_off):
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "shell_cmd")
    assert [d.rsplit(":", 1)[1] for d in buttons(msg)] == ["y", "n"]
    assert await press(bot, msg, f"confirm:{token}:5") == [None] and not task.done()  # digits ignored
    await press(bot, msg, f"confirm:{token}:y")
    assert await task is True


async def test_non_pin_action_gets_plain_prompt_even_with_pin_on(pin_on):
    bot = FakeBot()
    task, msg, token = await start_prompt(bot, "kill_process")
    assert [d.rsplit(":", 1)[1] for d in buttons(msg)] == ["y", "n"]
    await press(bot, msg, f"confirm:{token}:y")
    assert await task is True


# ================================================================ confirm_and_run (slash-command path)
@pytest.fixture
def fake_actions():
    ran = []

    @r.action("t_pinbox", "PIN test action.", dangerous=True, category="test")
    async def t_pinbox() -> r.ActionResult:
        ran.append(1)
        return r.ActionResult(text="ran")
    yield ran
    r.REGISTRY.pop("t_pinbox", None)


async def test_confirm_and_run_uses_pin_even_if_confirm_is_skipped(pin_on, monkeypatch, fake_actions):
    monkeypatch.setenv("EDITH_SKIP_CONFIRM", "all")
    monkeypatch.setenv("EDITH_PIN_ACTIONS", "t_pinbox")
    assert policy.needs_confirm(r.REGISTRY["t_pinbox"], {})         # a PIN action always prompts
    asked = []

    async def fake_ask(bot, chat_id, summary, **kw):
        asked.append(kw.get("action"))
        return False
    monkeypatch.setattr(confirm, "ask_confirm", fake_ask)
    sent = []

    async def fake_send(bot, chat_id, res):
        sent.append(res.text)
    monkeypatch.setattr(confirm, "send_result", fake_send)
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=OWNER))
    await confirm.confirm_and_run(upd, SimpleNamespace(bot=None), "t_pinbox")
    assert asked == ["t_pinbox"] and fake_actions == [] and sent == []
    monkeypatch.setenv("EDITH_PIN_ACTIONS", "")
    await confirm.confirm_and_run(upd, SimpleNamespace(bot=None), "t_pinbox")
    assert fake_actions == [1] and sent == ["ran"]                  # no PIN, confirm skipped -> runs


async def test_brain_passes_action_name(monkeypatch):
    from bot.assistant import brain
    r.load_all()
    seen = []

    async def fake_ask(bot, chat_id, summary, **kw):
        seen.append(kw.get("action"))
        return False
    monkeypatch.setattr(brain, "ask_confirm", fake_ask)
    reply = brain.BrainReply(text="")
    text, is_err = await brain.execute_tool(None, OWNER, "shell_cmd", {"command": "dir"}, reply)
    assert seen == ["shell_cmd"] and is_err
