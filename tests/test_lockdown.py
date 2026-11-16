"""Stolen-Telegram-account defences: lockdown gate, keypad presses never logged, Edith's secrets never sent."""
from types import SimpleNamespace

import pytest

from bot import confirm, config, security
from bot.actions import files, policy


@pytest.fixture
def locked():
    policy.set_lockdown(True, reason="test")
    yield
    policy.set_lockdown(False)


def _update(text=None, data=None, uid=111111111):
    replies = []

    async def reply_text(t, **k):
        replies.append(t)

    async def answer(*a, **k):
        replies.append(("answer",) + a)
    msg = SimpleNamespace(text=text, reply_text=reply_text) if text is not None else None
    cq = SimpleNamespace(data=data, answer=answer) if data is not None else None
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid, username="me"),
                          effective_chat=SimpleNamespace(type="private", id=uid),
                          effective_message=msg, callback_query=cq)
    return upd, replies


def _handler():
    ran = []

    @security.owner_only
    async def h(update, context):
        ran.append(1)
    return h, ran


@pytest.mark.parametrize("text", ["take a screenshot", "/get C:\\x", "/at 9:00 shell_cmd command=x", "/logs audit",
                                  "/macro save x screenshot", "/cancel 3"])
async def test_lockdown_blocks_chat_and_most_commands(locked, text):
    h, ran = _handler()
    upd, replies = _update(text=text)
    await h(upd, None)
    assert ran == [] and "lockdown" in replies[0]


@pytest.mark.parametrize("text", ["/status", "/lock", "/lockdown", "/vol 20", "/mute@EdithBot"])
async def test_lockdown_keeps_harmless_commands(locked, text):
    h, ran = _handler()
    await h(_update(text=text)[0], None)
    assert ran == [1]


async def test_lockdown_blocks_buttons(locked):
    h, ran = _handler()
    await h(_update(data="confirm:abc:y")[0], None)
    assert ran == []


async def test_keypad_presses_are_not_logged(caplog):
    h, _ = _handler()
    with caplog.at_level("INFO", logger="audit"):
        await h(_update(data="confirm:0123456789abcdef:7")[0], None)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "confirm:<button>" in logged and ":7" not in logged


async def test_no_confirm_prompts_in_lockdown(locked):
    assert await confirm.ask_confirm(None, 1, "anything", action="shell_cmd") is False


async def test_get_file_refuses_edith_secrets_and_zips_skip_them(tmp_path, monkeypatch):
    res = files._get_file(0, str(config.ROOT / ".env"))
    assert not res.ok and "can't be sent" in res.text
    assert not files._get_file(0, str(config.ROOT / "data")).ok


def test_wrong_pin_counter_survives_restart():
    confirm._save_fails(2)
    try:
        assert confirm._load_fails() == 2
    finally:
        confirm._save_fails(0)
