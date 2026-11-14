"""Open installed applications by name ("open brave", "launch spotify").

Index = Start Menu shortcuts (current user + all users) + Store/UWP apps (Get-StartApps),
cached for a few minutes. Launching only ever starts an app that is registered in the
Start Menu, so this is a safe, visible action (no arbitrary paths or arguments).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..utils import run_blocking
from .registry import ActionResult, action

CACHE_SECONDS = 600
_SKIP = re.compile(r"\b(uninstall|readme|help|documentation|release notes|website|license|changelog)\b", re.I)


@dataclass(frozen=True)
class App:
    name: str
    target: str   # path to a .lnk shortcut, or a UWP AppUserModelID
    kind: str     # "lnk" | "uwp"


_cache: tuple[float, list[App]] | None = None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _start_menu_dirs() -> list[Path]:
    dirs = []
    for base in (os.environ.get("APPDATA"), os.environ.get("PROGRAMDATA")):
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.is_dir()]


def _scan_shortcuts() -> list[App]:
    apps = []
    for d in _start_menu_dirs():
        for lnk in d.rglob("*.lnk"):
            if not _SKIP.search(lnk.stem):
                apps.append(App(lnk.stem, str(lnk), "lnk"))
    return apps


def _scan_store_apps() -> list[App]:
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(out.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    return [App(d["Name"], d["AppID"], "uwp") for d in data
            if d.get("Name") and d.get("AppID") and not _SKIP.search(d["Name"])]


def installed_apps(refresh: bool = False) -> list[App]:
    global _cache
    if _cache and not refresh and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    seen: dict[str, App] = {}
    for app in _scan_shortcuts() + _scan_store_apps():  # shortcuts win: they launch the same app
        seen.setdefault(_norm(app.name), app)
    apps = sorted(seen.values(), key=lambda a: a.name.lower())
    _cache = (time.monotonic(), apps)
    return apps


def find_app(query: str, apps: list[App]) -> tuple[App | None, list[App]]:
    """Best match for `query` plus close alternatives. None when nothing is a clear match."""
    q = _norm(query)
    if not q:
        return None, []
    names = {_norm(a.name): a for a in apps}
    if q in names:
        return names[q], []
    starts = [a for n, a in names.items() if n.startswith(q + " ") or n.startswith(q)]
    words = [a for n, a in names.items() if re.search(rf"\b{re.escape(q)}\b", n) and a not in starts]
    pool = starts or words
    if pool:
        best = min(pool, key=lambda a: len(a.name))  # "Brave" beats "Brave Browser Beta"
        return best, [a for a in pool if a is not best][:4]
    close = difflib.get_close_matches(q, list(names), n=5, cutoff=0.75)
    if close:
        return names[close[0]], [names[c] for c in close[1:]]
    return None, [names[c] for c in difflib.get_close_matches(q, list(names), n=5, cutoff=0.5)]


def _launch(app: App) -> None:
    if app.kind == "uwp":
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app.target}"])
    else:
        os.startfile(app.target)  # noqa: S606 - Start Menu shortcut chosen from the index above


@action("open_app", "Open an installed application by name, e.g. brave, chrome, notepad, spotify, "
        "whatsapp, vs code, settings. Use open_url for websites.", category="apps",
        params={"name": "the app's name as the owner said it"})
async def open_app(name: str) -> ActionResult:
    apps = await run_blocking(installed_apps)
    app, others = find_app(name, apps)
    if app is None:
        hint = f" Did you mean: {', '.join(a.name for a in others)}?" if others else ""
        return ActionResult(text=f"I couldn't find an installed app called {name!r}.{hint}", ok=False)
    try:
        await run_blocking(_launch, app)
    except OSError as e:
        return ActionResult(text=f"Couldn't open {app.name}: {e.strerror or e}", ok=False)
    return ActionResult(text=f"Opened {app.name}.", data={"app": app.name})


@action("list_apps", "List installed applications that can be opened, optionally filtered by a word.",
        category="apps", params={"query": "optional filter, e.g. 'browser' or 'adobe'"})
async def list_apps(query: str = "") -> ActionResult:
    apps = await run_blocking(installed_apps)
    q = _norm(query)
    hits = [a.name for a in apps if not q or q in _norm(a.name)]
    if not hits:
        return ActionResult(text=f"No installed apps match {query!r}.", ok=False)
    shown = hits[:60]
    more = f"\n…and {len(hits) - len(shown)} more" if len(hits) > len(shown) else ""
    return ActionResult(text=f"Installed apps ({len(hits)}):\n" + "\n".join(shown) + more)
