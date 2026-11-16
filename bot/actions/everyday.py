"""Everyday conveniences: toast notification, play something on YouTube/Spotify, what's playing, print a file.

- toast: Windows toast via Windows PowerShell 5.1's WinRT bridge. The script is a constant; the title
  and message travel in environment variables, so user text is never parsed as PowerShell.
  Falls back to the show_message popup.
- media_info: Windows media session via the optional `winrt` packages (imported lazily); without them
  it falls back to reading Spotify / YouTube window titles.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from typing import Any, Literal
from urllib.parse import quote, quote_plus, urlsplit

from ..utils import run_blocking
from . import desktop, system
from .registry import ActionResult, action
from .shell import CHAT_PARAM, resolve_path

log = logging.getLogger("actions.everyday")

TITLE_MAX, MESSAGE_MAX, QUERY_MAX = 100, 500, 200
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # XML-invalid control chars (keeps \t \n \r)

WINRT_PACKAGES = "winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation"


# ================================================================== toast
_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [System.Security.SecurityElement]::Escape($env:EDITH_TOAST_TITLE)
$m = [System.Security.SecurityElement]::Escape($env:EDITH_TOAST_MESSAGE)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>")
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def _powershell_exe() -> str:
    # Windows PowerShell 5.1 specifically: pwsh 7 has no WinRT type projection.
    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return exe if os.path.exists(exe) else "powershell.exe"


def _clean(s: str, limit: int) -> str:
    s = _CTRL.sub("", s or "").strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _toast_sync(title: str, message: str) -> tuple[bool, str]:
    env = dict(os.environ, EDITH_TOAST_TITLE=title, EDITH_TOAST_MESSAGE=message)
    try:
        r = subprocess.run([_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", _TOAST_PS], env=env, capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[:300]


@action("toast", "Show a Windows notification (toast) on the PC.", category="desktop",
        params={"title": "short heading", "message": "notification text"})
async def toast(title: str, message: str) -> ActionResult:
    title, message = _clean(title, TITLE_MAX) or "Edith", _clean(message, MESSAGE_MAX)
    if not message:
        return ActionResult(text="Nothing to show.", ok=False)
    if sys.platform != "win32":
        return ActionResult(text="Notifications only work on Windows.", ok=False)
    ok, err = await run_blocking(_toast_sync, title, message)
    if ok:
        return ActionResult(text="🔔 Notification shown on the PC.")
    log.info("toast failed (%s) - falling back to a popup", err)
    res = await desktop.show_message(text=message, title=title)
    if res.ok:
        res.text = "💬 Notifications unavailable - showed a popup on the PC instead."
    return res


# ================================================================== play media
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_SPOTIFY_HOSTS = {"open.spotify.com"}


def _startfile(target: str) -> None:
    os.startfile(target)  # type: ignore[attr-defined]


def _looks_like_url(q: str) -> bool:
    return "://" in q or q.lower().startswith(("www.", "youtu.be/", "youtube.com/", "open.spotify.com/"))


def media_target(query: str, service: str) -> tuple[str, str | None]:
    """Return (primary target, web fallback or None). Raises ValueError for a bad query."""
    q = " ".join(query.split())
    if not q:
        raise ValueError("say what to play")
    if len(q) > QUERY_MAX:
        raise ValueError(f"query too long (max {QUERY_MAX} chars)")
    hosts = _YT_HOSTS if service == "youtube" else _SPOTIFY_HOSTS
    if _looks_like_url(q):
        url = desktop.normalize_url(q)
        if (urlsplit(url).hostname or "").lower() not in hosts:
            raise ValueError(f"that is not a {service} link (use open_url for other sites)")
        return url, None
    if service == "youtube":
        return f"https://www.youtube.com/results?search_query={quote_plus(q)}", None
    return f"spotify:search:{quote(q, safe='')}", f"https://open.spotify.com/search/{quote(q, safe='')}"


def _play_sync(primary: str, fallback: str | None) -> str:
    try:
        _startfile(primary)
        return primary
    except OSError:
        if fallback is None:
            raise
        _startfile(fallback)  # Spotify app not installed -> web player
        return fallback


@action("play_media", "Play music or a video: opens a YouTube search (or a YouTube link) in the browser, "
        "or a Spotify search in the Spotify app.", category="media",
        params={"query": "song, artist, video or a youtube/spotify link", "service": "youtube | spotify"})
async def play_media(query: str, service: Literal["youtube", "spotify"] = "youtube") -> ActionResult:
    try:
        primary, fallback = media_target(query, service)
    except ValueError as e:
        return ActionResult(text=f"Not played: {e}", ok=False)
    try:
        opened = await run_blocking(_play_sync, primary, fallback)
    except OSError as e:
        return ActionResult(text=f"Could not open {service}: {e}", ok=False)
    where = "Spotify web player" if opened.startswith("https://open.spotify") else (
        "Spotify" if opened.startswith("spotify:") else "YouTube")
    q = " ".join(query.split())
    return ActionResult(text=f"▶ Opened {where}: {q[:80]}", data={"url": opened})


# ================================================================== now playing
def _winrt_session_sync() -> dict[str, Any] | None:
    """Current media session via winrt (pywinrt) or the older winsdk package. Raises ImportError if neither."""
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    except ImportError:
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

    async def query() -> dict[str, Any] | None:
        mgr = await Manager.request_async()
        session = mgr.get_current_session()
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        status = session.get_playback_info().playback_status
        return {"title": props.title or "", "artist": props.artist or "", "album": props.album_title or "",
                "app": session.source_app_user_model_id or "",
                "status": getattr(status, "name", str(status)).lower()}

    return asyncio.run(query())  # own loop: this runs in a worker thread


def _app_name(app_id: str) -> str:
    a = app_id.lower()
    for key, name in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"), ("brave", "Brave"),
                      ("firefox", "Firefox"), ("zune", "Media Player"), ("vlc", "VLC")):
        if key in a:
            return name
    return app_id.rsplit("!", 1)[0].rsplit("\\", 1)[-1].removesuffix(".exe") or "unknown app"


def _from_window_titles(windows: list[dict[str, Any]]) -> list[str]:
    """Spotify shows 'Artist - Song' as its title while playing; browsers show '<video> - YouTube'."""
    found = []
    for w in windows:
        title, proc = w.get("title", ""), w.get("process", "").lower()
        if proc == "spotify.exe" and title and not title.lower().startswith("spotify"):
            found.append(f"🎵 Spotify: {title[:80]}")
        elif " - YouTube" in title:
            found.append(f"▶ YouTube: {title.split(' - YouTube')[0][:80]}")
    return found[:3]


def _now_playing_sync() -> ActionResult:
    try:
        info = _winrt_session_sync()
    except ImportError:
        info = "no-winrt"
    except Exception as e:  # noqa: BLE001 - winrt errors are unpredictable; fall back to titles
        log.debug("winrt media session failed", exc_info=True)
        info = f"winrt-error: {e}"
    if isinstance(info, dict):
        who = f"{info['artist']} - " if info["artist"] else ""
        state = "" if info["status"] in ("", "playing") else f" ({info['status']})"
        return ActionResult(text=f"🎵 {who}{info['title'] or 'unknown title'}{state} · {_app_name(info['app'])}",
                            data=info)
    if info is None:
        return ActionResult(text="Nothing is playing right now.", data={"playing": False})
    titles = _from_window_titles(system._visible_windows())
    if titles:
        return ActionResult(text="\n".join(titles), data={"source": "window titles"})
    hint = " (install winrt packages for full info)" if info == "no-winrt" else ""
    return ActionResult(text=f"Can't tell what's playing{hint}.", ok=False)


@action("media_info", "Say what song or video is playing on the PC right now (title, artist, app).",
        category="media")
async def now_playing() -> ActionResult:
    if sys.platform != "win32":
        return ActionResult(text="Only works on Windows.", ok=False)
    return await run_blocking(_now_playing_sync)


# ================================================================== print
def _default_printer() -> str | None:
    try:
        import win32print
        return win32print.GetDefaultPrinter() or None
    except Exception:  # noqa: BLE001 - no pywin32 / no default printer
        return None


def _print_sync(chat_id: int, path: str) -> ActionResult:
    p = resolve_path(chat_id, path)
    if not p.exists():
        return ActionResult(text=f"Not found: {p}", ok=False)
    if not p.is_file():
        return ActionResult(text=f"Not a file: {p}", ok=False)
    printer = _default_printer()
    try:
        os.startfile(str(p), "print")  # type: ignore[attr-defined]
    except OSError as e:
        return ActionResult(text=f"Can't print {p.name}: no app is set up to print {p.suffix or 'this'} files "
                                 f"({e.strerror or e}).", ok=False)
    to = f" to {printer}" if printer else " to the default printer"
    return ActionResult(text=f"🖨 Sent {p.name}{to}.", data={"path": str(p), "printer": printer})


@action("print_file", "Print a file on the PC's default printer.", dangerous=True, category="files",
        params={"path": "file to print (relative to the working directory)", "chat_id": CHAT_PARAM})
async def print_file(path: str, chat_id: int = 0) -> ActionResult:
    if not path.strip():
        return ActionResult(text="Say which file to print.", ok=False)
    if sys.platform != "win32":
        return ActionResult(text="Printing only works on Windows.", ok=False)
    return await run_blocking(_print_sync, chat_id, path)
