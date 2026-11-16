"""Window management: minimize / maximize / restore / close one window, and show the desktop.

Windows are found with bot.actions.system._visible_windows() (the same list /windows shows).
Only these four window modes exist - nothing that moves, resizes, hides or injects into windows.
close posts WM_CLOSE, so the app can still ask to save; it never kills the process.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from typing import Any, Literal

from ..utils import run_blocking
from . import system
from .registry import ActionResult, action

log = logging.getLogger("actions.windows")

SW = {"minimize": 6, "maximize": 3, "restore": 9}  # ShowWindow commands
WM_CLOSE = 0x0010
PAST = {"minimize": "Minimized", "maximize": "Maximized", "restore": "Restored", "close": "Asked to close"}
MAX_OTHERS = 5

WindowOp = Literal["minimize", "maximize", "restore", "close"]


# ------------------------------------------------------------------ OS helpers (monkeypatched in tests)
def _user32():
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.IsWindow.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return u


def _list_windows() -> list[dict[str, Any]]:
    return system._visible_windows()


def _is_window(hwnd: int) -> bool:
    return bool(_user32().IsWindow(hwnd))


def _show_window(hwnd: int, cmd: int) -> None:
    _user32().ShowWindow(hwnd, cmd)  # returns previous visibility, not success


def _post_close(hwnd: int) -> bool:
    """False if the message could not be posted (e.g. the window belongs to an elevated app)."""
    return bool(_user32().PostMessageW(hwnd, WM_CLOSE, 0, 0))


# ------------------------------------------------------------------ matching (pure)
def _norm_proc(name: str) -> str:
    name = name.lower().strip()
    return name[:-4] if name.endswith(".exe") else name


def pick_window(windows: list[dict[str, Any]], query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Case-insensitive substring match on title or process name.
    Preference: exact process-name match (foreground first), then the foreground window,
    then the first title match, then the first process match. Returns (chosen, other_matches)."""
    q = query.strip().lower()
    if not q:
        return None, []
    qp = _norm_proc(q)
    title_hits = [w for w in windows if q in w.get("title", "").lower()]
    proc_hits = [w for w in windows if qp in _norm_proc(w.get("process", "")) and w not in title_hits]
    matches = title_hits + proc_hits
    if not matches:
        return None, []
    exact = [w for w in matches if _norm_proc(w.get("process", "")) == qp]
    fg = [w for w in matches if w.get("foreground")]
    if exact:
        chosen = next((w for w in exact if w.get("foreground")), exact[0])
    elif fg:
        chosen = fg[0]
    else:
        chosen = matches[0]
    return chosen, [w for w in matches if w is not chosen]


def _label(w: dict[str, Any]) -> str:
    title = w.get("title", "")
    title = title if len(title) <= 60 else title[:59] + "…"
    return f"“{title}” ({w.get('process', '?')})"


# ------------------------------------------------------------------ actions
def _window_sync(op: str, query: str) -> ActionResult:
    if sys.platform != "win32":
        return ActionResult(text="Window control only works on Windows.", ok=False)
    chosen, others = pick_window(_list_windows(), query)
    if chosen is None:
        return ActionResult(text=f"No open window matches “{query.strip()}”. Use list_windows to see them.",
                            ok=False)
    hwnd = int(chosen["hwnd"])
    if not _is_window(hwnd):
        return ActionResult(text=f"{_label(chosen)} closed before I could {op} it.", ok=False)
    if op == "close":
        if not _post_close(hwnd):
            return ActionResult(text=f"Could not close {_label(chosen)} (it may be running as admin).", ok=False)
    else:
        _show_window(hwnd, SW[op])
    text = f"🪟 {PAST[op]} {_label(chosen)}."
    if op == "close":
        text += " It may ask to save first."
    if others:
        shown = ", ".join(_label(w) for w in others[:MAX_OTHERS])
        more = f" +{len(others) - MAX_OTHERS} more" if len(others) > MAX_OTHERS else ""
        text += f"\nAlso matched: {shown}{more}."
    return ActionResult(text=text, data={"hwnd": hwnd, "title": chosen.get("title", ""),
                                         "process": chosen.get("process", ""), "others": len(others)})


@action("window_control", "Minimize, maximize, restore or close one open window, found by part of its "
        "title or its app/process name (e.g. 'chrome', 'notepad', 'Spotify'). close asks the app to close "
        "(it may prompt to save).", dangerous=True, category="desktop",
        params={"action": "minimize | maximize | restore | close",
                "window": "part of the window title or the process name, e.g. 'brave' or 'Word'"})
async def window_control(action: WindowOp, window: str) -> ActionResult:
    if not window.strip():
        return ActionResult(text="Say which window, e.g. 'chrome' or 'notepad'.", ok=False)
    return await run_blocking(_window_sync, action, window)


def _shell_minimize_all() -> None:
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    try:
        shell = comtypes.client.CreateObject("Shell.Application", dynamic=True)
        shell.MinimizeAll()
        del shell
    finally:
        comtypes.CoUninitialize()


def _win_d() -> None:
    VK_LWIN, VK_D, KEYUP = 0x5B, 0x44, 0x2
    k = ctypes.windll.user32.keybd_event
    k(VK_LWIN, 0, 0, 0)
    k(VK_D, 0, 0, 0)
    time.sleep(0.03)
    k(VK_D, 0, KEYUP, 0)
    k(VK_LWIN, 0, KEYUP, 0)


def _minimize_all_sync() -> str:
    try:
        _shell_minimize_all()
        return "shell"
    except Exception:  # noqa: BLE001 - fall back to the Win+D shortcut
        log.debug("Shell.Application MinimizeAll failed, using Win+D", exc_info=True)
        _win_d()
        return "win+d"


@action("minimize_all", "Minimize all windows and show the desktop.", category="desktop")
async def minimize_all() -> ActionResult:
    if sys.platform != "win32":
        return ActionResult(text="Only works on Windows.", ok=False)
    try:
        how = await run_blocking(_minimize_all_sync)
    except Exception as e:  # noqa: BLE001 - actions never raise
        return ActionResult(text=f"Could not show the desktop: {e}", ok=False)
    return ActionResult(text="🖥 Showing the desktop (all windows minimized).", data={"method": how})
