"""Tests for the watchdog in bot/cli.py (cmd_start / supervise) and bot/modules/ops.py (/restart, /logs).

No real processes are started and nothing really restarts: children, sleep and the clock are fakes.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import Application

from bot import cli
from bot.config import settings
from bot.modules import MODULE_NAMES
from bot.modules import ops

OWNER = next(iter(settings.allowed_user_ids))


# ================================================================== watchdog
class FakeChild:
    def __init__(self, rc):
        self.rc, self.terminated, self.killed = rc, False, False

    def wait(self, timeout=None):
        if isinstance(self.rc, BaseException):
            exc, self.rc = self.rc, 0  # Ctrl+C once; afterwards the child exits cleanly
            raise exc
        return self.rc

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class Harness:
    """spawn() hands out children with the given exit codes; records sleeps and log lines."""

    def __init__(self, codes, *, step=1.0):
        self.codes, self.children, self.sleeps, self.t, self.step = list(codes), [], [], 0.0, step

    def spawn(self):
        child = FakeChild(self.codes.pop(0))
        self.children.append(child)
        return child

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.t += secs

    def clock(self):
        self.t += self.step
        return self.t

    def run(self):
        return cli.supervise(spawn=self.spawn, sleep=self.sleep, clock=self.clock)


@pytest.fixture(autouse=True)
def watchdog_log(tmp_path, monkeypatch):
    path = tmp_path / "bot.log"
    monkeypatch.setattr(cli, "WATCHDOG_LOG", path)
    return path


def test_clean_exit_stops(watchdog_log):
    h = Harness([0])
    assert h.run() == 0
    assert len(h.children) == 1 and h.sleeps == [] and not watchdog_log.exists()


@pytest.mark.parametrize("rc", [130, 0xC000013A, -2])
def test_ctrl_c_exit_codes_stop(rc):
    h = Harness([rc])
    assert h.run() == 0 and len(h.children) == 1


def test_requested_restart_is_immediate(watchdog_log):
    h = Harness([cli.EXIT_RESTART, cli.EXIT_RESTART, 0])
    assert h.run() == 0
    assert len(h.children) == 3 and h.sleeps == []
    log = watchdog_log.read_text(encoding="utf-8")
    assert log.count("INFO watchdog: restart requested") == 2


def test_crash_restarts_with_backoff(watchdog_log):
    h = Harness([1, 1, 1, 1, 0])
    assert h.run() == 0
    assert h.sleeps == [5, 15, 60, 60] and len(h.children) == 5
    lines = watchdog_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and all(" WARNING watchdog: Edith stopped unexpectedly (exit code 1)" in ln for ln in lines)
    assert "restarting in 5s" in lines[0] and "restarting in 60s" in lines[3]


def test_gives_up_after_five_crashes_in_ten_minutes(watchdog_log, capsys):
    h = Harness([1, 2, 1, 1, 9, 0])
    assert h.run() == 1
    assert len(h.children) == 5  # the 5th crash is not restarted
    assert h.sleeps == [5, 15, 60, 60]
    last = watchdog_log.read_text(encoding="utf-8").splitlines()[-1]
    assert "ERROR watchdog: Edith crashed 5 times in 10 minutes (last exit code 9) - giving up" in last
    assert "giving up" in capsys.readouterr().out


def test_crashes_spread_out_never_give_up():
    h = Harness([1] * 8 + [0], step=200.0)  # one crash every ~3+ minutes
    assert h.run() == 0 and len(h.children) == 9


def test_already_running_is_not_retried():
    h = Harness([cli.EXIT_ALREADY_RUNNING])
    assert h.run() == 1 and len(h.children) == 1 and h.sleeps == []


def test_ctrl_c_in_supervisor_waits_for_child():
    h = Harness([KeyboardInterrupt()])
    assert h.run() == 0
    assert len(h.children) == 1 and not h.children[0].terminated  # child shut down by itself


def test_ctrl_c_hung_child_is_terminated():
    class Hung(FakeChild):
        def wait(self, timeout=None):
            if not hasattr(self, "interrupted"):
                self.interrupted = True
                raise KeyboardInterrupt
            raise subprocess.TimeoutExpired("bot", timeout)
    child = Hung(0)
    assert cli.supervise(spawn=lambda: child, sleep=lambda s: None) == 0
    assert child.terminated and child.killed


def test_ctrl_c_during_backoff_stops():
    def sleep(_):
        raise KeyboardInterrupt
    h = Harness([1])
    assert cli.supervise(spawn=h.spawn, sleep=sleep, clock=h.clock) == 0 and len(h.children) == 1


def test_wait_polls_until_child_exits():
    calls = []

    class Slow:
        def wait(self, timeout=None):
            calls.append(timeout)
            if len(calls) < 3:
                raise subprocess.TimeoutExpired("bot", timeout)
            return 7
    assert cli._wait(Slow()) == 7 and calls == [1, 1, 1]


def test_spawn_child_marks_child_and_uses_same_python(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kw: seen.update(args=args, **kw) or "proc")
    monkeypatch.delenv("EDITH_CHILD", raising=False)
    assert cli._spawn_child() == "proc"
    assert seen["args"] == [cli.sys.executable, "-m", "bot.cli", "start"]
    assert seen["env"]["EDITH_CHILD"] == "1" and seen["cwd"] == cli.ROOT
    assert "creationflags" not in seen  # same console: Ctrl+C reaches both


# ------------------------------------------------------------------ cmd_start routing
@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(cli, "read_env", lambda *a: {"BOT_TOKEN": "1:x", "ALLOWED_USER_IDS": "1"})


def test_cmd_start_supervises_when_not_child(configured, monkeypatch):
    monkeypatch.delenv("EDITH_CHILD", raising=False)
    monkeypatch.setattr(cli, "supervise", lambda: 42)
    monkeypatch.setattr(cli, "acquire_single_instance", lambda: pytest.fail("supervisor must not take the lock"))
    assert cli.cmd_start() == 42


def test_cmd_start_unconfigured_runs_setup(monkeypatch):
    monkeypatch.setattr(cli, "read_env", lambda *a: {})
    monkeypatch.setattr(cli, "cmd_setup", lambda *a: 7)
    monkeypatch.setattr(cli, "supervise", lambda: pytest.fail("no watchdog before setup"))
    assert cli.cmd_start() == 7


@pytest.fixture
def child(configured, monkeypatch):
    monkeypatch.setenv("EDITH_CHILD", "1")
    monkeypatch.setattr(cli, "supervise", lambda: pytest.fail("child must not supervise"))
    monkeypatch.setattr(cli, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(ops, "_restart_requested", False)
    import bot.main as bot_main
    return bot_main


def test_child_exit_codes(child, monkeypatch):
    monkeypatch.setattr(child, "main", lambda: None)
    assert cli.cmd_start() == 0
    monkeypatch.setattr(child, "main", lambda: setattr(ops, "_restart_requested", True))
    assert cli.cmd_start() == cli.EXIT_RESTART

    def crash():
        raise RuntimeError("boom")
    monkeypatch.setattr(child, "main", crash)
    assert cli.cmd_start() == 1

    def ctrl_c():
        raise KeyboardInterrupt
    monkeypatch.setattr(child, "main", ctrl_c)
    assert cli.cmd_start() == 0


def test_child_lock_taken_returns_already_running(child, monkeypatch):
    monkeypatch.setattr(cli, "acquire_single_instance", lambda: False)
    monkeypatch.setattr(child, "main", lambda: pytest.fail("must not start polling"))
    assert cli.cmd_start() == cli.EXIT_ALREADY_RUNNING


def test_start_help_mentions_restart():
    assert cli.build_parser().parse_args(["start"]).fn is cli.cmd_start


# ================================================================== ops module
def fake_update(uid=OWNER, args=None):
    msg = SimpleNamespace(text="/x", reply_text=AsyncMock(), reply_document=AsyncMock())
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid, username="o"), effective_chat=SimpleNamespace(id=uid, type="private"),
                          effective_message=msg, callback_query=None)
    app = MagicMock()
    ctx = SimpleNamespace(args=args or [], bot=SimpleNamespace(), application=app)
    return upd, ctx, msg, app


def test_module_listed_and_handlers_owner_only():
    assert "ops" in MODULE_NAMES
    app = Application.builder().token("1:x").build()
    ops.register(app)
    handlers = app.handlers[0]
    assert {c for h in handlers for c in h.commands} == {name for name, _ in ops.COMMANDS} == {"restart", "logs"}
    for h in handlers:
        assert h.callback.__code__.co_filename.endswith("security.py") and hasattr(h.callback, "__wrapped__")


@pytest.fixture
def restart_env(monkeypatch):
    monkeypatch.setattr(ops, "_restart_requested", False)
    monkeypatch.setenv("EDITH_CHILD", "1")


async def test_restart_confirmed_stops_app(restart_env, monkeypatch):
    ask = AsyncMock(return_value=True)
    monkeypatch.setattr(ops, "ask_confirm", ask)
    upd, ctx, msg, app = fake_update()
    await ops.cmd_restart(upd, ctx)
    ask.assert_awaited_once()
    assert msg.reply_text.await_args.args[0].startswith("🔄 Restarting")
    app.stop_running.assert_called_once()
    assert ops.restart_requested() is True


async def test_restart_cancelled_does_nothing(restart_env, monkeypatch):
    monkeypatch.setattr(ops, "ask_confirm", AsyncMock(return_value=False))
    upd, ctx, msg, app = fake_update()
    await ops.cmd_restart(upd, ctx)
    app.stop_running.assert_not_called()
    msg.reply_text.assert_not_awaited()
    assert ops.restart_requested() is False


async def test_restart_refused_without_watchdog(restart_env, monkeypatch):
    monkeypatch.delenv("EDITH_CHILD")
    ask = AsyncMock(return_value=True)
    monkeypatch.setattr(ops, "ask_confirm", ask)
    upd, ctx, msg, app = fake_update()
    await ops.cmd_restart(upd, ctx)
    ask.assert_not_awaited()
    app.stop_running.assert_not_called()
    assert "control start" in msg.reply_text.await_args.args[0]


async def test_restart_ignores_strangers(restart_env, monkeypatch):
    ask = AsyncMock(return_value=True)
    monkeypatch.setattr(ops, "ask_confirm", ask)
    upd, ctx, msg, app = fake_update(uid=OWNER + 1)
    await ops.cmd_restart(upd, ctx)
    ask.assert_not_awaited()
    app.stop_running.assert_not_called()
    msg.reply_text.assert_not_awaited()


# ------------------------------------------------------------------ /logs
@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "settings", SimpleNamespace(log_dir=tmp_path))
    return tmp_path


@pytest.mark.parametrize("args, expected", [
    ([], (30, False)), (["50"], (50, False)), (["audit"], (30, True)), (["audit", "10"], (10, True)),
    (["10", "AUDIT"], (10, True)), (["9999"], (300, False)), (["0"], (1, False)), (["junk"], (30, False))])
def test_parse_logs_args(args, expected):
    assert ops.parse_logs_args(args) == expected


async def test_logs_tail_and_redaction(logs_dir):
    lines = [f"2026-11-16 10:00:{i:02d} INFO x: line {i}" for i in range(50)]
    lines[-1] += f" token={settings.bot_token} <tag> & more"
    (logs_dir / "bot.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    upd, ctx, msg, _ = fake_update(args=["5"])
    await ops.cmd_logs(upd, ctx)
    sent = msg.reply_text.await_args.args[0]
    assert sent.startswith("Last 5 lines of bot.log:") and "line 45" in sent and "line 44" not in sent
    assert settings.bot_token not in sent and "[REDACTED]" in sent
    assert "&lt;tag&gt; &amp; more" in sent  # HTML-escaped inside <pre>
    assert msg.reply_text.await_args.kwargs["parse_mode"] == "HTML"


async def test_logs_audit_and_missing(logs_dir):
    upd, ctx, msg, _ = fake_update(args=["audit"])
    await ops.cmd_logs(upd, ctx)
    assert msg.reply_text.await_args.args[0] == "No audit.log yet."
    (logs_dir / "audit.log").write_text("2026 OK user=1 action='/status'\n", encoding="utf-8")
    await ops.cmd_logs(upd, ctx)
    assert "audit.log" in msg.reply_text.await_args.args[0] and "/status" in msg.reply_text.await_args.args[0]
    (logs_dir / "bot.log").write_text("", encoding="utf-8")
    upd, ctx, msg, _ = fake_update()
    await ops.cmd_logs(upd, ctx)
    assert "bot.log is empty" in msg.reply_text.await_args.args[0]


async def test_long_logs_sent_as_document(logs_dir):
    # 300 lines * ~40 chars > 4000 after escaping -> .txt document
    (logs_dir / "bot.log").write_text("\n".join(f"line {i} <<<&&&>>> padding padding" for i in range(400)),
                                      encoding="utf-8")
    upd, ctx, msg, _ = fake_update(args=["300"])
    await ops.cmd_logs(upd, ctx)
    msg.reply_text.assert_not_awaited()
    doc = msg.reply_document.await_args
    assert doc.kwargs["filename"] == "bot.txt" and doc.kwargs["caption"] == "Last 300 lines of bot.log:"
    body = doc.args[0].getvalue().decode("utf-8")
    assert body.splitlines()[0] == "line 100 <<<&&&>>> padding padding" and len(body.splitlines()) == 300


async def test_logs_escaping_counted_before_choosing_text(logs_dir):
    """~3000 raw chars of '<' would be ~12000 escaped: must go as a document, not a rejected message."""
    (logs_dir / "bot.log").write_text("<" * 3000 + "\n", encoding="utf-8")
    upd, ctx, msg, _ = fake_update(args=["1"])
    await ops.cmd_logs(upd, ctx)
    msg.reply_text.assert_not_awaited()
    msg.reply_document.assert_awaited_once()


async def test_logs_ignores_strangers(logs_dir):
    (logs_dir / "bot.log").write_text("secret stuff\n", encoding="utf-8")
    upd, ctx, msg, _ = fake_update(uid=OWNER + 1)
    await ops.cmd_logs(upd, ctx)
    msg.reply_text.assert_not_awaited()
    msg.reply_document.assert_not_awaited()
