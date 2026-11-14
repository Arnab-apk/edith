"""Power actions: lock, sleep, hibernate, restart, shutdown, logoff, abort, monitor off.

Every action except power_abort is dangerous=True. Restart/shutdown always use a delay
(shutdown.exe /t N, N >= MIN_DELAY) so power_abort can cancel them. Sleep/hibernate/logoff are
deferred by a couple of seconds so the Telegram reply is delivered before the PC goes down.

All OS calls go through module-level helpers (_run, _lock_workstation, _set_suspend, _monitor_off,
_defer, _capabilities) so tests can monkeypatch them.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
from typing import Callable

from ..utils import run_blocking
from .registry import ActionResult, action

log = logging.getLogger("actions.power")

DEFAULT_DELAY = 15
MIN_DELAY = 5          # always leave time for power_abort
MAX_DELAY = 86_400     # 24 h; longer schedules belong in automations
DEFER_SECONDS = 2.0    # let the reply reach Telegram before sleeping/logging off
ERROR_NO_SHUTDOWN_IN_PROGRESS = 1116
ERROR_SHUTDOWN_IS_SCHEDULED = 1190

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SHUTDOWN_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shutdown.exe")
_ABORT_HINT = "Cancel with /abort (or the power_abort action)."


# ---------------------------------------------------------------- low-level helpers (monkeypatchable)
def _run(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                           creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} timed out"
    return p.returncode, (p.stdout + "\n" + p.stderr).strip()


def _shutdown(*args: str) -> tuple[int, str]:
    return _run([_SHUTDOWN_EXE, *args])


def _lock_workstation() -> bool:
    return bool(ctypes.WinDLL("user32").LockWorkStation())


def _set_suspend(hibernate: bool) -> bool:
    """Blocks until the PC wakes up again (when it works). Returns False on failure."""
    return bool(ctypes.WinDLL("powrprof").SetSuspendState(bool(hibernate), False, False))


def _monitor_off() -> bool:
    HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, OFF = 0xFFFF, 0x0112, 0xF170, 2
    user32 = ctypes.WinDLL("user32")
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    return bool(user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, OFF))


def _capabilities() -> dict[str, bool]:
    """{'sleep': S1-S3 available, 'hibernate': hibernation enabled}."""
    if sys.platform != "win32":
        return {"sleep": False, "hibernate": False}
    p = ctypes.WinDLL("powrprof")
    return {"sleep": bool(p.IsPwrSuspendAllowed()), "hibernate": bool(p.IsPwrHibernateAllowed())}


def _defer(fn: Callable[[], object], what: str, delay: float = DEFER_SECONDS) -> None:
    """Run fn on a daemon timer thread after `delay` s; failures are logged (the reply is already sent)."""
    def runner() -> None:
        try:
            if fn() is False:
                log.warning("%s: OS call reported failure", what)
        except Exception:  # noqa: BLE001
            log.exception("%s failed", what)

    t = threading.Timer(delay, runner)
    t.daemon = True
    t.start()


def _clamp_delay(delay: int) -> int:
    return max(MIN_DELAY, min(int(delay), MAX_DELAY))


def fmt_delay(s: int) -> str:
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min" + (f" {s % 60} s" if s % 60 else "")
    return f"{s // 3600} h {s % 3600 // 60} min"


# ---------------------------------------------------------------- actions
@action("power_lock", "Lock the PC (Win+L). Apps keep running.", dangerous=True, category="power")
async def power_lock() -> ActionResult:
    if not await run_blocking(_lock_workstation):
        return ActionResult(text="Lock failed (LockWorkStation refused - no interactive session?).", ok=False)
    return ActionResult(text="🔒 PC locked.")


@action("power_monitor_off", "Turn the display(s) off. Moving the mouse or pressing a key turns them back on.",
        dangerous=True, category="power")
async def power_monitor_off() -> ActionResult:
    if not await run_blocking(_monitor_off):
        return ActionResult(text="Could not send the display-off message.", ok=False)
    return ActionResult(text="🖥 Display turned off.")


@action("power_sleep", "Put the PC to sleep. On Modern Standby PCs (no S3 sleep) it locks and turns the "
        "display off instead, which lets Windows drop into standby.", dangerous=True, category="power")
async def power_sleep() -> ActionResult:
    caps = await run_blocking(_capabilities)
    if caps["sleep"]:
        _defer(lambda: _set_suspend(False), "sleep")
        return ActionResult(text=f"😴 Going to sleep in {DEFER_SECONDS:.0f} s. The bot is offline until the PC wakes.")
    # Modern Standby (S0 low-power idle): Windows has no public API to force standby.
    locked = await run_blocking(_lock_workstation)
    off = await run_blocking(_monitor_off)
    return ActionResult(
        text="😴 This PC has no classic sleep (S3) - it uses Modern Standby, which apps can't force. "
             f"Did the closest thing instead: {'locked' if locked else 'lock FAILED'}, "
             f"{'display off' if off else 'display-off FAILED'}. Windows enters standby on its own idle timer.",
        ok=locked or off, data={"fallback": True})


@action("power_hibernate", "Hibernate the PC (saves RAM to disk and powers off).", dangerous=True,
        category="power")
async def power_hibernate() -> ActionResult:
    caps = await run_blocking(_capabilities)
    if not caps["hibernate"]:
        return ActionResult(text="Hibernation is disabled on this PC. Enable it from an admin terminal with "
                                 "`powercfg /hibernate on`, then try again.", ok=False)
    _defer(lambda: _set_suspend(True), "hibernate")
    return ActionResult(text=f"🛌 Hibernating in {DEFER_SECONDS:.0f} s. The bot is offline until the PC is powered on.")


async def _schedule(kind: str, delay: int) -> ActionResult:
    delay = _clamp_delay(delay)
    flag, verb = ("/r", "Restart") if kind == "restart" else ("/s", "Shutdown")
    rc, out = await run_blocking(_shutdown, flag, "/t", str(delay), "/c",
                                 f"{verb} requested from Telegram (Edith).")
    if rc == ERROR_SHUTDOWN_IS_SCHEDULED:
        return ActionResult(text=f"A shutdown/restart is already scheduled. {_ABORT_HINT}", ok=False)
    if rc != 0:
        return ActionResult(text=f"{verb} failed (exit {rc}): {out or 'no output'}", ok=False)
    icon = "🔄" if kind == "restart" else "⏻"
    return ActionResult(text=f"{icon} {verb} in {fmt_delay(delay)}. {_ABORT_HINT} "
                             "Unsaved work in open apps will be lost.", data={"delay": delay})


@action("power_restart", "Restart the PC after a delay (default 15 s) that can be cancelled with power_abort.",
        dangerous=True, category="power", params={"delay": f"seconds before restart ({MIN_DELAY}-{MAX_DELAY})"})
async def power_restart(delay: int = DEFAULT_DELAY) -> ActionResult:
    return await _schedule("restart", delay)


@action("power_shutdown", "Shut down the PC after a delay (default 15 s) that can be cancelled with power_abort.",
        dangerous=True, category="power", params={"delay": f"seconds before shutdown ({MIN_DELAY}-{MAX_DELAY})"})
async def power_shutdown(delay: int = DEFAULT_DELAY) -> ActionResult:
    return await _schedule("shutdown", delay)


@action("power_logoff", "Sign out the current Windows user (closes all apps, including this bot).",
        dangerous=True, category="power")
async def power_logoff() -> ActionResult:
    _defer(lambda: _shutdown("/l")[0] == 0, "logoff")
    return ActionResult(text=f"🚪 Signing out in {DEFER_SECONDS:.0f} s. The bot stops until you sign in again.")


@action("power_abort", "Cancel a pending (delayed) shutdown or restart.", category="power")
async def power_abort() -> ActionResult:
    rc, out = await run_blocking(_shutdown, "/a")
    if rc == 0:
        return ActionResult(text="✋ Pending shutdown/restart cancelled.")
    if rc == ERROR_NO_SHUTDOWN_IN_PROGRESS:
        return ActionResult(text="Nothing to cancel - no shutdown or restart is pending.")
    return ActionResult(text=f"Abort failed (exit {rc}): {out or 'no output'}", ok=False)
