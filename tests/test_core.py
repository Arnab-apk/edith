"""Tests for the core slice: actions/system.py, actions/power.py, modules/system.py, modules/power.py.

Nothing real is ever locked, slept, shut down or killed: every power OS hook is replaced by a
recorder (autouse fixture) and kill tests use fake psutil processes.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import Application

from bot.actions import power, system
from bot.actions.registry import REGISTRY, run_action
from bot.config import settings
from bot.modules import power as power_mod
from bot.modules import system as system_mod

OWNER = next(iter(settings.allowed_user_ids))

EXPECTED = {
    "system_status": False, "gpu_status": False, "procs": False, "net_info": False,
    "list_windows": False, "kill_process": True,
    "power_lock": True, "power_sleep": True, "power_hibernate": True, "power_restart": True,
    "power_shutdown": True, "power_logoff": True, "power_monitor_off": True, "power_abort": False,
}


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def fake_power_os(monkeypatch):
    """Replace every OS-touching power helper with a recorder. Tests tweak `rec` to change behaviour."""
    rec = SimpleNamespace(calls=[], deferred=[], rc=0, out="", caps={"sleep": True, "hibernate": True},
                          lock_ok=True, monitor_ok=True, suspend_ok=True)

    def _run(args, timeout=10.0):
        rec.calls.append(("run", list(args)))
        return rec.rc, rec.out

    def _lock():
        rec.calls.append(("lock",))
        return rec.lock_ok

    def _suspend(hibernate):
        rec.calls.append(("suspend", hibernate))
        return rec.suspend_ok

    def _monitor():
        rec.calls.append(("monitor_off",))
        return rec.monitor_ok

    monkeypatch.setattr(power, "_run", _run)
    monkeypatch.setattr(power, "_lock_workstation", _lock)
    monkeypatch.setattr(power, "_set_suspend", _suspend)
    monkeypatch.setattr(power, "_monitor_off", _monitor)
    monkeypatch.setattr(power, "_capabilities", lambda: dict(rec.caps))
    monkeypatch.setattr(power, "_defer", lambda fn, what, delay=0: rec.deferred.append((what, fn)))
    return rec


class FakeProc:
    def __init__(self, pid, name, *, deny=False):
        self.pid, self._name, self.deny, self.killed = pid, name, deny, False
        self.info = {"pid": pid, "name": name}

    def name(self):
        return self._name

    def kill(self):
        if self.deny:
            raise system.psutil.AccessDenied(self.pid)
        self.killed = True


@pytest.fixture
def fake_procs(monkeypatch):
    procs = {p.pid: p for p in [
        FakeProc(4, "System"), FakeProc(700, "csrss.exe"), FakeProc(999, "python.exe"),
        FakeProc(1001, "notepad.exe"), FakeProc(1002, "Notepad.exe"), FakeProc(1003, "notepad++.exe"),
        FakeProc(2000, "secret.exe", deny=True), FakeProc(3000, "python.exe"),
    ]}

    def process(pid=None):
        if pid not in procs:
            raise system.psutil.NoSuchProcess(pid)
        return procs[pid]

    def wait_procs(victims, timeout=None):
        return [p for p in victims if p.killed], [p for p in victims if not p.killed]

    monkeypatch.setattr(system.psutil, "process_iter", lambda attrs=None: list(procs.values()))
    monkeypatch.setattr(system.psutil, "Process", process)
    monkeypatch.setattr(system.psutil, "wait_procs", wait_procs)
    monkeypatch.setattr(system, "_self_pids", lambda: {999})
    return procs


def fake_update(data=None, *, uid=OWNER, args=None):
    msg = SimpleNamespace(text=None, reply_text=AsyncMock())
    q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()) if data else None
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid, username="u"), effective_chat=SimpleNamespace(id=uid, type="private"),
                          effective_message=None if data else msg, callback_query=q)
    ctx = SimpleNamespace(args=args or [], bot=SimpleNamespace())
    return upd, ctx, msg


# ---------------------------------------------------------------- registry
def test_actions_registered_with_flags_and_schemas():
    for name, dangerous in EXPECTED.items():
        act = REGISTRY[name]
        assert act.dangerous is dangerous, name
        tool = act.tool_schema()
        assert tool["name"] == name and tool["input_schema"]["type"] == "object"
        assert tool["input_schema"]["additionalProperties"] is False
    assert REGISTRY["procs"].input_schema["properties"]["sort"]["enum"] == ["cpu", "mem"]
    assert REGISTRY["kill_process"].input_schema["required"] == ["target"]
    assert REGISTRY["power_shutdown"].input_schema["properties"]["delay"]["default"] == 15


def test_every_power_menu_entry_maps_to_registered_action():
    for key, (name, _, _) in power_mod.POWER_ACTIONS.items():
        assert REGISTRY[name].category == "power"
        assert REGISTRY[name].dangerous is (key != "abort")


# ---------------------------------------------------------------- power actions
async def test_shutdown_uses_delay_and_abortable(fake_power_os):
    res = await run_action("power_shutdown")
    assert res.ok and "abort" in res.text.lower()
    (kind, args), = fake_power_os.calls
    assert args[0].lower().endswith("shutdown.exe") and args[1:4] == ["/s", "/t", "15"]


@pytest.mark.parametrize("given, expected", [(0, "5"), (1, "5"), (60, "60"), (10**9, "86400")])
async def test_restart_delay_is_clamped(fake_power_os, given, expected):
    res = await run_action("power_restart", delay=given)
    assert res.ok
    assert fake_power_os.calls[0][1][1:4] == ["/r", "/t", expected]


async def test_shutdown_already_scheduled_and_failure(fake_power_os):
    fake_power_os.rc = power.ERROR_SHUTDOWN_IS_SCHEDULED
    res = await run_action("power_shutdown", delay=30)
    assert not res.ok and "already scheduled" in res.text
    fake_power_os.rc, fake_power_os.out = 5, "Access is denied."
    res = await run_action("power_restart")
    assert not res.ok and "Access is denied" in res.text


@pytest.mark.parametrize("rc, ok, needle", [(0, True, "cancelled"), (1116, True, "Nothing to cancel"),
                                            (5, False, "failed")])
async def test_abort(fake_power_os, rc, ok, needle):
    fake_power_os.rc = rc
    res = await run_action("power_abort")
    assert res.ok is ok and needle in res.text
    assert fake_power_os.calls == [("run", [power._SHUTDOWN_EXE, "/a"])]


async def test_lock(fake_power_os):
    assert (await run_action("power_lock")).ok
    fake_power_os.lock_ok = False
    assert not (await run_action("power_lock")).ok
    assert fake_power_os.calls == [("lock",), ("lock",)]


async def test_sleep_is_deferred_when_s3_available(fake_power_os):
    res = await run_action("power_sleep")
    assert res.ok and fake_power_os.calls == []  # nothing happens before the reply is sent
    (what, fn), = fake_power_os.deferred
    assert what == "sleep"
    fn()
    assert fake_power_os.calls == [("suspend", False)]


async def test_sleep_falls_back_on_modern_standby(fake_power_os):
    fake_power_os.caps = {"sleep": False, "hibernate": False}
    res = await run_action("power_sleep")
    assert res.ok and res.data["fallback"] and "Modern Standby" in res.text
    assert fake_power_os.calls == [("lock",), ("monitor_off",)] and not fake_power_os.deferred


async def test_hibernate(fake_power_os):
    fake_power_os.caps = {"sleep": False, "hibernate": False}
    res = await run_action("power_hibernate")
    assert not res.ok and "powercfg /hibernate on" in res.text and not fake_power_os.deferred
    fake_power_os.caps = {"sleep": False, "hibernate": True}
    assert (await run_action("power_hibernate")).ok
    fake_power_os.deferred[0][1]()
    assert fake_power_os.calls == [("suspend", True)]


async def test_logoff_and_monitor_off(fake_power_os):
    assert (await run_action("power_logoff")).ok
    assert fake_power_os.calls == []
    fake_power_os.deferred[0][1]()
    assert fake_power_os.calls == [("run", [power._SHUTDOWN_EXE, "/l"])]
    assert (await run_action("power_monitor_off")).ok
    assert fake_power_os.calls[-1] == ("monitor_off",)


def test_fmt_delay():
    assert power.fmt_delay(15) == "15 s"
    assert power.fmt_delay(600) == "10 min"
    assert power.fmt_delay(7260) == "2 h 1 min"


# ---------------------------------------------------------------- kill
def test_find_processes_exact_name_and_protection(fake_procs):
    found = system.find_processes("NOTEPAD")
    assert [m["pid"] for m in found] == [1001, 1002]  # exact name, .exe optional, not notepad++
    assert system.find_processes("note") == []
    assert system.find_processes("4")[0]["protected"] == "core system process"
    assert system.find_processes("csrss")[0]["protected"] == "critical Windows process"
    py = {m["pid"]: m["protected"] for m in system.find_processes("python.exe")}
    assert py == {999: "this bot itself", 3000: None}
    assert system.find_processes("123456") == []


async def test_kill_by_name_kills_only_matches(fake_procs):
    res = await run_action("kill_process", target="notepad")
    assert res.ok and res.data["killed"] == [1001, 1002]
    assert fake_procs[1001].killed and fake_procs[1002].killed and not fake_procs[1003].killed


async def test_kill_never_touches_protected_or_self(fake_procs):
    res = await run_action("kill_process", target="4")
    assert not res.ok and not fake_procs[4].killed and "Skipped" in res.text
    res = await run_action("kill_process", target="python")
    assert res.data["killed"] == [3000] and res.data["skipped"] == [999]
    assert not fake_procs[999].killed


async def test_kill_access_denied_and_missing(fake_procs):
    res = await run_action("kill_process", target="secret.exe")
    assert not res.ok and "access denied" in res.text
    res = await run_action("kill_process", target="nope")
    assert not res.ok and "No running process" in res.text


# ---------------------------------------------------------------- read-only system actions
async def test_status_with_fakes(monkeypatch):
    gpu = {"name": "FakeGPU", "utilization.gpu": 12.0, "memory.used": 1024.0, "memory.total": 6144.0,
           "temperature.gpu": 50.0, "power.draw": None}
    monkeypatch.setattr(system, "_gpus", lambda: [gpu])
    monkeypatch.setattr(system, "_public_ip", lambda: "203.0.113.5")
    monkeypatch.setattr(system, "_cpu_temp", lambda: None)
    res = await run_action("system_status")
    assert res.ok
    for needle in ("CPU:", "RAM:", "FakeGPU · 12% · 1.0/6.0 GB · 50°C", "GPU 50°C", "public 203.0.113.5",
                   "Uptime:"):
        assert needle in res.text
    monkeypatch.setattr(system, "_gpus", lambda: None)
    res = await run_action("system_status")
    assert "no NVIDIA stats" in res.text and "n/a" in res.text


def test_nvidia_parsing(monkeypatch):
    monkeypatch.setattr(system.shutil, "which", lambda exe: "nvidia-smi")
    line = "NVIDIA GeForce RTX 3050 6GB Laptop GPU, 621.14, 12, 40, 1121, 6144, 46, 3.63, [N/A], [N/A], 210, 405"
    monkeypatch.setattr(system, "_run", lambda args, timeout=5.0: (0, line))
    g, = system._nvidia_query()
    assert g["name"].startswith("NVIDIA") and g["memory.total"] == 6144 and g["power.limit"] is None
    monkeypatch.setattr(system, "_run", lambda args, timeout=5.0: (0, "9916, C:\\Windows\\explorer.exe, [N/A]"))
    assert system._nvidia_apps() == [(9916, "explorer.exe", None)]
    monkeypatch.setattr(system.shutil, "which", lambda exe: None)
    assert system._nvidia_query() is None


async def test_gpu_status_without_gpu(monkeypatch):
    monkeypatch.setattr(system, "_gpus", lambda: None)
    res = await run_action("gpu_status")
    assert not res.ok and "No GPU" in res.text


async def test_procs_real_readonly():
    res = await run_action("procs", sort="mem", limit=3)
    assert res.ok and 1 <= len(res.data["processes"]) <= 3
    mems = [r["mem"] for r in res.data["processes"]]
    assert mems == sorted(mems, reverse=True)
    assert not (await run_action("procs", sort="disk")).ok  # enum validation


async def test_list_windows_formatting(monkeypatch):
    monkeypatch.setattr(system, "_visible_windows", lambda: [
        {"hwnd": 1, "title": "Doc - Word", "pid": 10, "process": "WINWORD.EXE", "minimized": False, "foreground": True},
        {"hwnd": 2, "title": "Chat", "pid": 11, "process": "app.exe", "minimized": True, "foreground": False}])
    res = await run_action("list_windows")
    assert "★ Doc - Word — WINWORD.EXE (10)" in res.text and "[minimized]" in res.text


# ---------------------------------------------------------------- modules
def test_modules_register_owner_only_handlers():
    app = Application.builder().token("1:x").build()
    system_mod.register(app)
    power_mod.register(app)
    handlers = app.handlers[0]
    assert handlers
    for h in handlers:
        # every callback must be the security.owner_only wrapper
        assert h.callback.__code__.co_filename.endswith("security.py"), h
        assert hasattr(h.callback, "__wrapped__")
    patterns = {h.pattern.pattern for h in handlers if hasattr(h, "pattern") and h.pattern is not None}
    assert patterns == {"^system:", "^power:"}
    cmds = {c for h in handlers for c in getattr(h, "commands", ())}
    assert cmds == {name for name, _ in system_mod.COMMANDS + power_mod.COMMANDS}


async def test_power_button_goes_through_confirm(monkeypatch):
    calls = []
    monkeypatch.setattr(power_mod, "confirm_and_run",
                        AsyncMock(side_effect=lambda u, c, name, **kw: calls.append((name, kw))))
    upd, ctx, _ = fake_update("power:shutdown")
    await power_mod.on_button(upd, ctx)
    upd.callback_query.answer.assert_awaited()
    (name, kw), = calls
    assert name == "power_shutdown" and kw["delay"] == 15 and "/abort" in kw["summary"]


async def test_power_button_ignores_strangers(monkeypatch):
    cr = AsyncMock()
    monkeypatch.setattr(power_mod, "confirm_and_run", cr)
    upd, ctx, _ = fake_update("power:shutdown", uid=OWNER + 1)
    await power_mod.on_button(upd, ctx)
    cr.assert_not_awaited()


async def test_power_text_form_aliases_and_unknown(monkeypatch):
    calls = []
    monkeypatch.setattr(power_mod, "confirm_and_run",
                        AsyncMock(side_effect=lambda u, c, name, **kw: calls.append((name, kw))))
    upd, ctx, msg = fake_update(args=["reboot", "120"])
    await power_mod.cmd_power(upd, ctx)
    assert calls[-1][0] == "power_restart" and calls[-1][1]["delay"] == 120 and "2 min" in calls[-1][1]["summary"]
    upd, ctx, _ = fake_update()
    await power_mod.cmd_lock(upd, ctx)
    assert calls[-1][0] == "power_lock"
    upd, ctx, _ = fake_update()
    await power_mod.cmd_abort(upd, ctx)
    assert calls[-1][0] == "power_abort"
    n = len(calls)
    upd, ctx, msg = fake_update(args=["explode"])
    await power_mod.cmd_power(upd, ctx)
    assert len(calls) == n and "Unknown power action" in msg.reply_text.await_args.args[0]
    upd, ctx, msg = fake_update()
    await power_mod.cmd_power(upd, ctx)
    assert msg.reply_text.await_args.kwargs["reply_markup"] is power_mod.MENU


async def test_kill_command_confirms_and_refuses_protected(monkeypatch, fake_procs):
    cr = AsyncMock()
    monkeypatch.setattr(system_mod, "confirm_and_run", cr)
    upd, ctx, msg = fake_update(args=["csrss"])
    await system_mod.cmd_kill(upd, ctx)
    cr.assert_not_awaited()
    assert "Refusing" in msg.reply_text.await_args.args[0]
    upd, ctx, msg = fake_update(args=["notepad"])
    await system_mod.cmd_kill(upd, ctx)
    args, kw = cr.await_args
    assert args[2] == "kill_process" and kw["target"] == "notepad" and "1001, 1002" in kw["summary"]
    assert not fake_procs[1001].killed  # the module itself never kills - only after Confirm
