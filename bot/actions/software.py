"""Find / install / uninstall software with winget (Windows Package Manager).

argv lists only (never a shell); package ids are validated by regex before use.
install_app / uninstall_app are dangerous=True: they run installers, so callers always confirm.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .registry import ActionResult, action
from .shell import decode_output, run_process
from ..utils import run_blocking

SEARCH_TIMEOUT = 120
INSTALL_TIMEOUT = 900
MAX_ROWS = 10
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{1,127}$")
_QUERY_RE = re.compile(r"^[^\x00-\x1f\"\-][^\x00-\x1f\"]{0,99}$")  # no leading '-': it would be a winget flag
_PROGRESS = re.compile(r"^[\s\-\\|/█▒░■□▓.%0-9KMGBkmgb]*$")  # spinner / progress-bar lines

# winget HRESULTs (as unsigned 32-bit) -> short explanation
WINGET_ERRORS = {
    0x8A150014: "no package found matching that id/query",
    0x8A150061: "the package is already installed",
    0x8A15002B: "no newer version is available",
}


def winget_exe() -> str | None:
    exe = shutil.which("winget")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = Path(local) / "Microsoft" / "WindowsApps" / "winget.exe"
        if cand.exists():
            return str(cand)
    return None


_MISSING = ("winget (Windows Package Manager) is not installed. Install 'App Installer' from the "
            "Microsoft Store, then try again.")


def valid_id(package_id: str) -> bool:
    return bool(_ID_RE.match(package_id))


def _run(args: list[str], timeout: int) -> tuple[int | None, str, bool]:
    """Run winget with an argv list. Returns (exit code as unsigned int or None on timeout, text, timed_out)."""
    rc, out, timed_out, _ = run_process(args, Path.home(), timeout)
    if rc is not None and rc < 0:
        rc &= 0xFFFFFFFF
    return rc, decode_output(out), timed_out


def clean_lines(text: str) -> list[str]:
    """Drop spinner/progress-bar noise; keep the last state of each carriage-return-overwritten line."""
    lines = []
    for raw in text.split("\n"):
        line = raw.split("\r")[-1].rstrip()
        if line.strip() and not _PROGRESS.match(line):
            lines.append(line)
    return lines


def parse_table(text: str) -> list[dict[str, str]]:
    """Parse winget's fixed-width table (header, dashes, rows) using the header column offsets."""
    lines = [raw.split("\r")[-1].rstrip() for raw in text.split("\n")]
    for i, line in enumerate(lines[:-1]):
        if re.search(r"\bId\b", line) and re.search(r"\bVersion\b", line) and set(lines[i + 1].strip()) == {"-"}:
            header = line
            break
    else:
        return []
    cols = [(m.group(), m.start()) for m in re.finditer(r"\S+", header)]
    rows = []
    for line in lines[i + 2:]:
        if not line.strip():
            continue
        row = {}
        for j, (name, start) in enumerate(cols):
            end = cols[j + 1][1] if j + 1 < len(cols) else None
            row[name.lower()] = line[start:end].strip() if end is not None else line[start:].strip()
        if row.get("id"):
            rows.append(row)
    return rows


def _explain(rc: int | None, text: str) -> str:
    if rc in WINGET_ERRORS:
        return WINGET_ERRORS[rc]
    tail = clean_lines(text)[-4:]
    code = f"exit code 0x{rc:08X}" if rc is not None else "no exit code"
    return f"{code}" + (": " + " | ".join(tail) if tail else "")


# ---------------------------------------------------------------- actions
@action("search_software", "Search the winget catalog for installable software; returns name, id, version, "
        "source. Use the id with install_app.", category="apps",
        params={"query": "app name or keyword, e.g. 'vlc' or '7zip'"})
async def search_software(query: str) -> ActionResult:
    q = query.strip()
    if not _QUERY_RE.match(q):
        return ActionResult(text="Give a short search term (1-100 chars, no quotes or control characters).", ok=False)
    exe = winget_exe()
    if not exe:
        return ActionResult(text=_MISSING, ok=False)
    rc, text, timed_out = await run_blocking(
        _run, [exe, "search", q, "--accept-source-agreements", "--disable-interactivity"], SEARCH_TIMEOUT)
    if timed_out:
        return ActionResult(text=f"winget search timed out after {SEARCH_TIMEOUT}s. Try again later.", ok=False)
    rows = parse_table(text)
    if rc == 0x8A150014 or (rc == 0 and not rows):
        return ActionResult(text=f"No packages found for {q!r}. Try a shorter or different name.",
                            data={"results": []})
    if rc != 0:
        return ActionResult(text=f"winget search failed: {_explain(rc, text)}", ok=False)
    shown = rows[:MAX_ROWS]
    lines = [f"{len(rows)} result(s) for {q!r}" + (f", top {len(shown)}" if len(rows) > len(shown) else "") + ":"]
    for n, r in enumerate(shown, 1):
        ver = r.get("version", "")
        ver = f"v{ver}" if ver and ver.lower() != "unknown" else "v?"
        lines.append(f"{n}. {r.get('name', '?')} | id: {r['id']} | {ver} | {r.get('source', '')}".rstrip(" |"))
    return ActionResult(text="\n".join(lines),
                        data={"results": [{k: r.get(k, "") for k in ("name", "id", "version", "source")} for r in shown]})


async def _install_like(verb: str, package_id: str) -> ActionResult:
    pid = package_id.strip()
    if not valid_id(pid):
        return ActionResult(text=f"Invalid package id {package_id!r}. Use the exact id from search_software "
                                 "(e.g. 'VideoLAN.VLC').", ok=False)
    exe = winget_exe()
    if not exe:
        return ActionResult(text=_MISSING, ok=False)
    args = [exe, verb, "--id", pid, "-e"]
    if verb == "install":
        args.append("--accept-package-agreements")
    args += ["--accept-source-agreements", "--disable-interactivity"]
    rc, text, timed_out = await run_blocking(_run, args, INSTALL_TIMEOUT)
    past = "installed" if verb == "install" else "uninstalled"
    if timed_out:
        return ActionResult(text=f"winget {verb} of {pid} timed out after {INSTALL_TIMEOUT}s and was stopped. "
                                 "The installer may need user interaction on the PC.", ok=False)
    if rc == 0:
        return ActionResult(text=f"✅ {pid} {past}.", data={"id": pid, "exit_code": 0})
    if rc == 0x8A150061 and verb == "install":
        return ActionResult(text=f"{pid} is already installed.", data={"id": pid, "exit_code": rc})
    hint = " Check the id with search_software." if rc == 0x8A150014 else ""
    return ActionResult(text=f"winget {verb} {pid} failed: {_explain(rc, text)}.{hint}", ok=False,
                        data={"id": pid, "exit_code": rc})


@action("install_app", "Install software on the PC with winget, by exact package id from search_software. "
        "Runs an installer (may take minutes).", dangerous=True, category="apps",
        params={"package_id": "exact winget id, e.g. 'VideoLAN.VLC'"})
async def install_app(package_id: str) -> ActionResult:
    return await _install_like("install", package_id)


@action("uninstall_app", "Uninstall software from the PC with winget, by exact package id.",
        dangerous=True, category="apps",
        params={"package_id": "exact winget id, e.g. 'VideoLAN.VLC'"})
async def uninstall_app(package_id: str) -> ActionResult:
    return await _install_like("uninstall", package_id)
