"""Tests for the shellfiles slice. Nothing real is launched, recycled or opened: os.startfile,
the Recycle Bin call and the process runner are monkeypatched. The only real subprocesses are
short `python -c` children used to exercise the timeout/kill-tree logic."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.actions import files as fl
from bot.actions import shell as sh
from bot.actions.registry import REGISTRY, run_action
from bot.config import settings

CHAT = 4242

EXPECTED = {
    "shell_cmd": True, "shell_ps": True, "run_program": True, "cd": False, "pwd": False,
    "list_dir": False, "get_file": False, "find_files": False, "delete_path": True,
    "make_dir": False, "move_path": True,
}


@pytest.fixture(autouse=True)
def chat_cwd(tmp_path):
    sh.CWD[CHAT] = tmp_path
    yield tmp_path
    sh.CWD.pop(CHAT, None)


# ---------------------------------------------------------------- registry
def test_actions_registered_with_flags_and_schemas():
    for name, dangerous in EXPECTED.items():
        act = REGISTRY[name]
        assert act.dangerous is dangerous, name
        sch = act.input_schema
        assert sch["type"] == "object" and sch["additionalProperties"] is False
        assert "chat_id" in sch["properties"] and sch["properties"]["chat_id"]["type"] == "integer"
        assert set(sch["required"]) <= set(sch["properties"])
        assert act.tool_schema()["name"] == name


# ---------------------------------------------------------------- path helpers / cwd
def test_split_and_strip():
    assert sh.split_args('"C:\\My Files\\a b.txt" D:\\x') == ["C:\\My Files\\a b.txt", "D:\\x"]
    assert sh.split_raw('notepad "a b.txt" -x') == ["notepad", '"a b.txt"', "-x"]
    assert sh.split_args('unbalanced "quote') == ["unbalanced", '"quote']
    assert sh.strip_quotes("'x'") == "x" and sh.strip_quotes('"') == '"'


def test_resolve_path(tmp_path):
    assert sh.resolve_path(CHAT, "") == tmp_path
    assert sh.resolve_path(CHAT, "sub\\..\\a.txt") == tmp_path / "a.txt"
    assert sh.resolve_path(CHAT, '"with space"') == tmp_path / "with space"
    assert sh.resolve_path(CHAT, "~") == Path.home()
    assert str(sh.resolve_path(CHAT, "c:")).lower() == "c:\\"


async def test_cd_pwd(tmp_path):
    (tmp_path / "Sub Dir").mkdir()
    res = await run_action("cd", path='"Sub Dir"', chat_id=CHAT)
    assert res.ok and sh.CWD[CHAT] == tmp_path / "Sub Dir"
    res = await run_action("cd", path="..", chat_id=CHAT)
    assert res.ok and sh.CWD[CHAT] == tmp_path
    res = await run_action("cd", path="nope", chat_id=CHAT)
    assert not res.ok and sh.CWD[CHAT] == tmp_path
    assert str(tmp_path) in (await run_action("pwd", chat_id=CHAT)).text
    # unknown chat falls back to home; a deleted cwd also falls back to home
    assert sh.get_cwd(987654) == Path.home()
    sh.CWD[CHAT] = tmp_path / "gone"
    assert sh.get_cwd(CHAT) == Path.home()


# ---------------------------------------------------------------- shell
def test_decode_output():
    assert sh.decode_output("héllo\r\n".encode("utf-8")) == "héllo\n"
    assert sh.decode_output("ü".encode("cp437")) in ("ü", "\x81")  # OEM fallback (cp437 here)
    assert sh.decode_output("wmic\r\n".encode("utf-16-le")) == "wmic\n"
    assert sh.decode_output(b"") == ""


def test_build_command_lines():
    line = sh.build_cmd_line('echo "a b"')
    assert line.endswith('/d /s /c "echo "a b""')
    argv = sh.build_ps_argv("Get-Date; 'x'")
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-Date" not in argv[-1]  # payload is base64, quoting can't break it


async def test_shell_actions_use_runner(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, cwd, timeout):
        calls.append((cmd, cwd, timeout))
        return 0, b"out\r\n", False, 0.1

    monkeypatch.setattr(sh, "run_process", fake_run)
    res = await run_action("shell_cmd", command="dir", chat_id=CHAT)
    assert res.ok and "out" in res.text and res.data["exit_code"] == 0
    assert calls[-1][1] == tmp_path and calls[-1][2] == settings.shell_timeout
    res = await run_action("shell_ps", command="Get-Date", chat_id=CHAT, timeout=99999)
    assert res.ok and calls[-1][2] == sh.MAX_TIMEOUT and isinstance(calls[-1][0], list)

    monkeypatch.setattr(sh, "run_process", lambda c, w, t: (None, b"partial", True, t))
    res = await run_action("shell_cmd", command="x", chat_id=CHAT, timeout=3)
    assert not res.ok and "timed out after 3s" in res.text and "partial" in res.text
    assert not (await run_action("shell_cmd", command="  ", chat_id=CHAT)).ok


def test_run_process_captures_and_exit_code(tmp_path):
    code = "import sys; print('to-out'); print('to-err', file=sys.stderr); sys.exit(3)"
    rc, out, timed_out, _ = sh.run_process([sys.executable, "-c", code], tmp_path, 30)
    assert rc == 3 and not timed_out
    assert b"to-out" in out and b"to-err" in out


def test_run_process_timeout_kills_tree(tmp_path):
    # parent spawns a sleeping child; both must die on timeout
    child = "import time; time.sleep(60)"
    code = ("import subprocess, sys, time; "
            f"p = subprocess.Popen([sys.executable, '-c', {child!r}]); print(p.pid, flush=True); time.sleep(60)")
    rc, out, timed_out, elapsed = sh.run_process([sys.executable, "-c", code], tmp_path, 2)
    assert timed_out and rc is None and elapsed < 20
    import psutil
    pid = int(out.split()[0])
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_run_program(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(sh.os, "startfile", lambda p, arguments="", cwd=None: started.append((p, arguments, cwd)),
                        raising=False)
    (tmp_path / "doc.txt").write_text("x")
    res = await run_action("run_program", target="doc.txt", chat_id=CHAT)
    assert res.ok and started[-1][0] == str(tmp_path / "doc.txt")
    res = await run_action("run_program", target="notepad", args="a.txt", chat_id=CHAT)
    assert res.ok and started[-1] == ("notepad", "a.txt", str(tmp_path))

    def boom(*a, **k):
        raise FileNotFoundError(2, "The system cannot find the file specified")
    monkeypatch.setattr(sh.os, "startfile", boom, raising=False)
    assert not (await run_action("run_program", target="nope_prog", chat_id=CHAT)).ok


# ---------------------------------------------------------------- files
async def test_list_dir(tmp_path):
    (tmp_path / "b.txt").write_bytes(b"12345")
    (tmp_path / "A folder").mkdir()
    res = await run_action("list_dir", chat_id=CHAT)
    assert res.ok
    names = [e["name"] for e in res.data["entries"]]
    assert names == ["A folder", "b.txt"]  # folders first
    assert res.data["entries"][1]["size"] == 5
    res = await run_action("list_dir", path="b.txt", chat_id=CHAT)
    assert res.ok and res.data["is_file"]
    assert not (await run_action("list_dir", path="missing", chat_id=CHAT)).ok


async def test_get_file_and_zip(tmp_path, monkeypatch):
    (tmp_path / "f.bin").write_bytes(b"x" * 10)
    res = await run_action("get_file", path="f.bin", chat_id=CHAT)
    assert res.ok and res.files[0].path == tmp_path / "f.bin" and not res.files[0].delete_after_send

    d = tmp_path / "proj"
    (d / "inner").mkdir(parents=True)
    (d / "a.txt").write_text("hello")
    (d / "inner" / "b.txt").write_text("world")
    res = await run_action("get_file", path="proj", chat_id=CHAT)
    assert res.ok, res.text
    zf = res.files[0]
    assert zf.delete_after_send and zf.filename == "proj.zip"
    with zipfile.ZipFile(zf.path) as z:
        assert sorted(n.replace("\\", "/") for n in z.namelist()) == ["proj/a.txt", "proj/inner/b.txt"]
    zf.path.unlink()

    monkeypatch.setattr(fl, "MAX_SEND_BYTES", 4)
    res = await run_action("get_file", path="f.bin", chat_id=CHAT)
    assert not res.ok and "at most" in res.text
    res = await run_action("get_file", path="proj", chat_id=CHAT)
    assert not res.ok and not res.files
    assert not (await run_action("get_file", path="C:\\", chat_id=CHAT)).ok


async def test_find_files(tmp_path, monkeypatch):
    (tmp_path / "x" / "y").mkdir(parents=True)
    (tmp_path / "x" / "y" / "Invoice_2026.PDF").write_text("1")
    (tmp_path / "invoice.txt").write_text("1")
    (tmp_path / "other.pdf").write_text("1")
    res = await run_action("find_files", pattern="*.pdf", chat_id=CHAT)
    assert res.ok and len(res.data["matches"]) == 2 and res.data["complete"]
    res = await run_action("find_files", pattern="invoice", chat_id=CHAT)
    assert len(res.data["matches"]) == 2
    res = await run_action("find_files", pattern="*", chat_id=CHAT, limit=2)
    assert len(res.data["matches"]) == 2 and not res.data["complete"]
    monkeypatch.setattr(fl, "FIND_TIME_LIMIT", -1)
    res = await run_action("find_files", pattern="*", chat_id=CHAT)
    assert not res.data["complete"] and "stopped" in res.text


async def test_delete_uses_recycle_bin(tmp_path, monkeypatch):
    target = tmp_path / "junk.txt"
    target.write_text("x")
    calls = []
    monkeypatch.setattr(fl, "recycle", lambda p: calls.append(p))
    res = await run_action("delete_path", path="junk.txt", chat_id=CHAT)
    assert res.ok and calls == [target]
    monkeypatch.setattr(fl, "recycle", lambda p: "denied")
    assert not (await run_action("delete_path", path="junk.txt", chat_id=CHAT)).ok
    assert not (await run_action("delete_path", path="missing.txt", chat_id=CHAT)).ok
    assert not (await run_action("delete_path", path="C:\\", chat_id=CHAT)).ok
    assert len(calls) == 1


async def test_make_dir_and_move(tmp_path):
    assert (await run_action("make_dir", path="a\\b", chat_id=CHAT)).ok and (tmp_path / "a" / "b").is_dir()
    assert (await run_action("make_dir", path="a", chat_id=CHAT)).ok  # already exists is fine
    (tmp_path / "f.txt").write_text("1")
    (tmp_path / "g.txt").write_text("2")
    res = await run_action("move_path", src="f.txt", dst="g.txt", chat_id=CHAT)
    assert not res.ok and "not overwriting" in res.text and (tmp_path / "g.txt").read_text() == "2"
    res = await run_action("move_path", src="f.txt", dst="a", chat_id=CHAT)  # into existing dir
    assert res.ok and (tmp_path / "a" / "f.txt").exists()
    res = await run_action("move_path", src="g.txt", dst="h.txt", chat_id=CHAT)  # rename
    assert res.ok and (tmp_path / "h.txt").exists()
    assert not (await run_action("move_path", src="a", dst="a\\b", chat_id=CHAT)).ok  # into itself
    assert not (await run_action("move_path", src="h.txt", dst="nodir\\", chat_id=CHAT)).ok


def test_safe_filename():
    assert fl.safe_filename("..\\..\\Windows\\evil.exe") == "evil.exe"
    assert fl.safe_filename("../../etc/passwd") == "passwd"
    assert fl.safe_filename("a:stream.txt") == "a_stream.txt"
    assert fl.safe_filename("CON.txt") == "_CON.txt"
    assert fl.safe_filename("..") == "upload" and fl.safe_filename(None) == "upload"
    assert fl.safe_filename("name. ") == "name"
    long = fl.safe_filename("x" * 300 + ".pdf")
    assert len(long) <= fl.MAX_NAME_LEN and long.endswith(".pdf")


def test_reserve_upload_path_never_overwrites(tmp_path):
    p1 = fl.reserve_upload_path("rep.txt", tmp_path)
    p1.write_text("first")
    p2 = fl.reserve_upload_path("rep.txt", tmp_path)
    p3 = fl.reserve_upload_path("..\\rep.txt", tmp_path)
    assert [p.name for p in (p1, p2, p3)] == ["rep.txt", "rep (1).txt", "rep (2).txt"]
    assert p1.read_text() == "first" and all(p.parent == tmp_path.resolve() for p in (p2, p3))


# ---------------------------------------------------------------- modules
def _fake_update(text: str, chat_id: int = CHAT):
    uid = next(iter(settings.allowed_user_ids))
    replies = []

    async def reply_text(t, **kw):
        replies.append(t)

    msg = SimpleNamespace(text=text, caption=None, reply_text=reply_text)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid, username="owner"), effective_message=msg,
                          effective_chat=SimpleNamespace(id=chat_id, type="private"), callback_query=None)
    return upd, replies


def test_modules_register_owner_only_handlers():
    from telegram.ext import Application, CallbackQueryHandler
    from bot.modules import files as mfiles, shell as mshell

    app = Application.builder().token("123:abc").build()
    mshell.register(app)
    mfiles.register(app)
    handlers = [h for group in app.handlers.values() for h in group]
    assert len(handlers) == len(mshell.COMMANDS) + 1 + len(mfiles.COMMANDS) + 2
    for h in handlers:
        assert hasattr(h.callback, "__wrapped__"), h.callback  # functools.wraps from owner_only
    patterns = {h.pattern.pattern for h in handlers if isinstance(h, CallbackQueryHandler)}
    assert patterns == {"^shell:", "^files:"}
    cmds = {c for h in handlers for c in getattr(h, "commands", ())}
    assert cmds == {c for c, _ in mshell.COMMANDS + mfiles.COMMANDS}


async def test_cmd_handlers_go_through_confirm(monkeypatch, tmp_path):
    from bot.modules import files as mfiles, shell as mshell
    seen = []

    async def fake_confirm(update, context, name, /, *, summary=None, **kw):
        seen.append((name, summary, kw))

    monkeypatch.setattr(mshell, "confirm_and_run", fake_confirm)
    monkeypatch.setattr(mfiles, "confirm_and_run", fake_confirm)
    ctx = SimpleNamespace(bot=None, args=[])

    upd, _ = _fake_update("/ps Get-Date\n'two  spaces'")
    await mshell.cmd_ps(upd, ctx)
    assert seen[-1][0] == "shell_ps" and seen[-1][2] == {"command": "Get-Date\n'two  spaces'", "chat_id": CHAT}

    upd, _ = _fake_update("/cmd dir")
    await mshell.cmd_cmd(upd, ctx)
    assert seen[-1][0] == "shell_cmd" and str(tmp_path) in seen[-1][1]

    upd, _ = _fake_update('/run "My App.exe" --flag "x y"')
    await mshell.cmd_run(upd, ctx)
    assert seen[-1][0] == "run_program" and seen[-1][2]["target"] == "My App.exe"
    assert seen[-1][2]["args"] == '--flag "x y"'

    upd, _ = _fake_update("/rm old file.txt")
    await mfiles.cmd_rm(upd, ctx)
    assert seen[-1][0] == "delete_path" and str(tmp_path / "old file.txt") in seen[-1][1]

    upd, _ = _fake_update('/mv "a b.txt" sub\\')
    await mfiles.cmd_mv(upd, ctx)
    assert seen[-1][0] == "move_path" and seen[-1][2] == {"src": "a b.txt", "dst": "sub\\", "chat_id": CHAT}

    upd, replies = _fake_update("/cmd")
    await mshell.cmd_cmd(upd, ctx)
    assert replies and replies[0].startswith("Usage") and len(seen) == 5


async def test_stranger_is_ignored(monkeypatch):
    from bot.modules import shell as mshell
    called = []

    async def fake_confirm(*a, **k):
        called.append(1)

    monkeypatch.setattr(mshell, "confirm_and_run", fake_confirm)
    upd, replies = _fake_update("/cmd whoami")
    upd.effective_user = SimpleNamespace(id=1, username="stranger")
    if 1 in settings.allowed_user_ids:
        pytest.skip("user id 1 is allowed in this environment")
    await mshell.cmd_cmd(upd, SimpleNamespace(bot=None, args=[]))
    assert not called and not replies
