"""File actions: list, send (zip dirs), find, delete (Recycle Bin), mkdir, move + upload helpers.

Paths are resolved against the per-chat working directory from bot.actions.shell.
`reserve_upload_path` (not an action) is used by bot.modules.files to store files the owner sends.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..config import settings
from ..utils import human_bytes, run_blocking
from .registry import ActionFile, ActionResult, action
from .shell import CHAT_PARAM, resolve_path

MAX_SEND_BYTES = 50 * 1024 * 1024        # Telegram bot upload limit
MAX_ZIP_RAW_BYTES = 1024 * 1024 * 1024   # refuse to compress more than this
ZIP_TIME_LIMIT = 180.0
MAX_LS_LINES = 150
MAX_LS_DATA = 500
FIND_TIME_LIMIT = 30.0
FIND_MAX_RESULTS = 500


# ---------------------------------------------------------------- helpers
def _is_link(entry: os.DirEntry) -> bool:
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True


def walk_entries(root: Path, deadline: float) -> Iterator[os.DirEntry]:
    """Yield every entry under root (depth-first). Symlinks/junctions are yielded but never
    descended into (no loops); unreadable directories are skipped. Stops at the deadline."""
    stack = [root]
    while stack:
        if time.monotonic() > deadline:
            return
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            yield e
            try:
                if e.is_dir(follow_symlinks=False) and not _is_link(e):
                    stack.append(Path(e.path))
            except OSError:
                pass


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _edith_private(p: Path) -> bool:
    from .reader import _edith_private as private
    return private(p)


def _is_drive_root(p: Path) -> bool:
    return p.parent == p


# ---------------------------------------------------------------- list
@action("list_dir", "List a folder on the PC (folders first, with file sizes). Defaults to the working directory.",
        category="files", params={"path": "folder (or file) path; empty = working directory", "chat_id": CHAT_PARAM})
async def list_dir(path: str = "", chat_id: int = 0) -> ActionResult:
    return await run_blocking(_list_dir, chat_id, path)


def _list_dir(chat_id: int, path: str) -> ActionResult:
    p = resolve_path(chat_id, path)
    if not p.exists():
        return ActionResult(text=f"Not found: {p}", ok=False)
    if not p.is_dir():
        st = p.stat()
        return ActionResult(text=f"📄 {p}\n{human_bytes(st.st_size)} · modified {_fmt_time(st.st_mtime)}",
                            data={"path": str(p), "entries": [], "is_file": True})
    rows = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    size = None if is_dir else e.stat().st_size
                except OSError:
                    is_dir, size = False, None
                rows.append((e.name, e.path, is_dir, size))
    except OSError as e:
        return ActionResult(text=f"Cannot list {p}: {e.strerror or e}", ok=False)
    rows.sort(key=lambda r: (not r[2], r[0].casefold()))
    ndirs = sum(1 for r in rows if r[2])
    lines = [f"📂 {p}  ({ndirs} folders, {len(rows) - ndirs} files)"]
    for name, _, is_dir, size in rows[:MAX_LS_LINES]:
        if is_dir:
            lines.append(f"📁 {name}\\")
        else:
            lines.append(f"📄 {name}  ({human_bytes(size) if size is not None else '?'})")
    if len(rows) > MAX_LS_LINES:
        lines.append(f"... and {len(rows) - MAX_LS_LINES} more")
    if not rows:
        lines.append("(empty)")
    entries = [{"name": n, "path": fp, "is_dir": d, "size": s} for n, fp, d, s in rows[:MAX_LS_DATA]]
    return ActionResult(text="\n".join(lines), data={"path": str(p), "entries": entries})


# ---------------------------------------------------------------- get / send
@action("get_file", "Send a file from the PC to the owner's Telegram chat. A folder is zipped first. "
        "Limit 50 MB (Telegram).", category="files",
        params={"path": "file or folder path (relative to the working directory)", "chat_id": CHAT_PARAM})
async def get_file(path: str, chat_id: int = 0) -> ActionResult:
    return await run_blocking(_get_file, chat_id, path)


def _get_file(chat_id: int, path: str) -> ActionResult:
    p = resolve_path(chat_id, path)
    if not p.exists():
        return ActionResult(text=f"Not found: {p}", ok=False)
    if _edith_private(p):
        return ActionResult(text="Edith's own settings, logs and data can't be sent (they hold the bot token, "
                                 "API keys and PIN hash).", ok=False)
    if p.is_file():
        size = p.stat().st_size
        if size > MAX_SEND_BYTES:
            return ActionResult(text=f"{p.name} is {human_bytes(size)}; Telegram bots can send at most "
                                     f"{human_bytes(MAX_SEND_BYTES)}.", ok=False)
        return ActionResult(text="", files=[ActionFile(path=p, filename=p.name, kind="document",
                                                       caption=f"{p} ({human_bytes(size)})")])
    if not p.is_dir():
        return ActionResult(text=f"Not a regular file or folder: {p}", ok=False)
    if _is_drive_root(p):
        return ActionResult(text="Refusing to zip a whole drive.", ok=False)
    return _zip_dir(p)


def _zip_dir(p: Path) -> ActionResult:
    fd, tmp_name = tempfile.mkstemp(prefix="edith_", suffix=".zip")
    os.close(fd)
    tmp = Path(tmp_name)
    deadline = time.monotonic() + ZIP_TIME_LIMIT
    raw_total, nfiles, skipped = 0, 0, 0
    error = ""
    try:
        with open(tmp, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for e in walk_entries(p, deadline):
                if _is_link(e) or _edith_private(Path(e.path)):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        continue
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    skipped += 1
                    continue
                if raw_total + size > MAX_ZIP_RAW_BYTES:
                    error = f"folder is larger than {human_bytes(MAX_ZIP_RAW_BYTES)} - too big to zip and send"
                    break
                arc = os.path.relpath(e.path, p.parent)
                try:
                    zf.write(e.path, arc)
                except (OSError, ValueError):
                    skipped += 1
                    continue
                raw_total += size
                nfiles += 1
                if fh.tell() > MAX_SEND_BYTES:
                    error = f"zip exceeds {human_bytes(MAX_SEND_BYTES)} (Telegram limit)"
                    break
            if not error and time.monotonic() > deadline:
                error = f"zipping took longer than {int(ZIP_TIME_LIMIT)}s"
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    zsize = tmp.stat().st_size
    if not error and zsize > MAX_SEND_BYTES:
        error = f"zip is {human_bytes(zsize)}, above the {human_bytes(MAX_SEND_BYTES)} Telegram limit"
    if error:
        tmp.unlink(missing_ok=True)
        return ActionResult(text=f"Cannot send {p}: {error}.", ok=False)
    note = f", {skipped} unreadable skipped" if skipped else ""
    caption = f"{p} - {nfiles} files, {human_bytes(raw_total)} -> {human_bytes(zsize)}{note}"
    return ActionResult(text="", files=[ActionFile(path=tmp, filename=f"{p.name or 'folder'}.zip",
                                                   kind="document", caption=caption, delete_after_send=True)],
                        data={"files": nfiles, "skipped": skipped})


# ---------------------------------------------------------------- find
@action("find_files", "Search for files/folders by name under a folder (recursive, bounded by count and "
        f"{int(FIND_TIME_LIMIT)}s). Pattern supports * and ?; plain text matches anywhere in the name.",
        category="files",
        params={"pattern": "name pattern, e.g. '*.pdf' or 'invoice'",
                "root": "folder to search; empty = working directory",
                "chat_id": CHAT_PARAM,
                "limit": f"maximum results (1-{FIND_MAX_RESULTS})"})
async def find_files(pattern: str, root: str = "", chat_id: int = 0, limit: int = 50) -> ActionResult:
    return await run_blocking(_find, chat_id, pattern, root, limit)


def _find(chat_id: int, pattern: str, root: str, limit: int) -> ActionResult:
    pat = pattern.strip().strip("\"'")
    if not pat:
        return ActionResult(text="Empty pattern.", ok=False)
    if not any(c in pat for c in "*?["):
        pat = f"*{pat}*"
    pat = pat.casefold()
    limit = max(1, min(int(limit), FIND_MAX_RESULTS))
    base = resolve_path(chat_id, root)
    if not base.is_dir():
        return ActionResult(text=f"Not a directory: {base}", ok=False)
    deadline = time.monotonic() + FIND_TIME_LIMIT
    hits: list[str] = []
    scanned = 0
    for e in walk_entries(base, deadline):
        scanned += 1
        if fnmatch.fnmatchcase(e.name.casefold(), pat):
            try:
                hits.append(e.path + ("\\" if e.is_dir() else ""))
            except OSError:
                hits.append(e.path)
            if len(hits) >= limit:
                break
    if len(hits) >= limit:
        stop = f" (stopped at {limit} results)"
    elif time.monotonic() > deadline:
        stop = f" (stopped after {int(FIND_TIME_LIMIT)}s, {scanned} entries scanned - results incomplete)"
    else:
        stop = ""
    head = f"🔎 {len(hits)} match(es) for {pattern!r} under {base}{stop}"
    return ActionResult(text="\n".join([head, *hits]), data={"matches": hits, "complete": not stop})


# ---------------------------------------------------------------- delete (Recycle Bin)
def recycle(path: Path) -> str | None:
    """Move a file/folder to the Recycle Bin via SHFileOperationW. Returns an error string or None.
    (Volumes without a Recycle Bin, e.g. some network/removable drives, delete permanently - Windows rule.)"""
    try:
        import ctypes
        from ctypes import wintypes
        shell32 = ctypes.windll.shell32
    except (ImportError, AttributeError):
        return "Recycle Bin is only available on Windows"

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                    ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 0x3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
    s = str(path)
    buf = ctypes.create_unicode_buffer(s, len(s) + 2)  # pFrom must be double-NUL terminated
    op = SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(buf, ctypes.c_void_p), None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        return f"SHFileOperation failed (code 0x{rc:X})"
    if op.fAnyOperationsAborted:
        return "operation was aborted"
    return None


@action("delete_path", "Delete a file or folder by moving it to the Recycle Bin (recoverable).",
        dangerous=True, category="files",
        params={"path": "file or folder to delete (relative to the working directory)", "chat_id": CHAT_PARAM})
async def delete_path(path: str, chat_id: int = 0) -> ActionResult:
    return await run_blocking(_delete, chat_id, path)


def _delete(chat_id: int, path: str) -> ActionResult:
    if not path.strip().strip("\"'"):
        return ActionResult(text="No path given.", ok=False)
    p = resolve_path(chat_id, path)
    if not p.exists() and not p.is_symlink():
        return ActionResult(text=f"Not found: {p}", ok=False)
    if _is_drive_root(p):
        return ActionResult(text="Refusing to delete a drive root.", ok=False)
    err = recycle(p)
    if err:
        return ActionResult(text=f"Could not delete {p}: {err}", ok=False)
    return ActionResult(text=f"🗑 Moved to Recycle Bin: {p}")


# ---------------------------------------------------------------- mkdir / move
@action("make_dir", "Create a folder (and missing parent folders).", category="files",
        params={"path": "folder to create (relative to the working directory)", "chat_id": CHAT_PARAM})
async def make_dir(path: str, chat_id: int = 0) -> ActionResult:
    def work() -> ActionResult:
        if not path.strip().strip("\"'"):
            return ActionResult(text="No path given.", ok=False)
        p = resolve_path(chat_id, path)
        if p.is_dir():
            return ActionResult(text=f"Already exists: {p}")
        if p.exists():
            return ActionResult(text=f"A file with that name exists: {p}", ok=False)
        try:
            p.mkdir(parents=True)
        except OSError as e:
            return ActionResult(text=f"Cannot create {p}: {e.strerror or e}", ok=False)
        return ActionResult(text=f"📁 Created {p}")

    return await run_blocking(work)


@action("move_path", "Move or rename a file/folder. If the destination is an existing folder the item is "
        "moved into it. Never overwrites an existing file.", dangerous=True, category="files",
        params={"src": "file or folder to move", "dst": "new path or existing destination folder",
                "chat_id": CHAT_PARAM})
async def move_path(src: str, dst: str, chat_id: int = 0) -> ActionResult:
    return await run_blocking(_move, chat_id, src, dst)


def _move(chat_id: int, src: str, dst: str) -> ActionResult:
    if not src.strip() or not dst.strip():
        return ActionResult(text="Need both a source and a destination.", ok=False)
    s = resolve_path(chat_id, src)
    d = resolve_path(chat_id, dst)
    if not s.exists():
        return ActionResult(text=f"Not found: {s}", ok=False)
    if _is_drive_root(s):
        return ActionResult(text="Refusing to move a drive root.", ok=False)
    if d.is_dir():
        d = d / s.name
    elif dst.strip().strip("\"'").endswith(("\\", "/")):
        return ActionResult(text=f"Destination folder does not exist: {d}", ok=False)
    same = os.path.normcase(str(d)) == os.path.normcase(str(s))  # equal except case = rename case only
    if same and str(d) == str(s):
        return ActionResult(text="Source and destination are the same.", ok=False)
    if d.exists() and not same:
        return ActionResult(text=f"Destination already exists (not overwriting): {d}", ok=False)
    if s.is_dir() and os.path.normcase(str(d)).startswith(os.path.normcase(str(s)) + os.sep):
        return ActionResult(text="Cannot move a folder into itself.", ok=False)
    if not d.parent.is_dir():
        return ActionResult(text=f"Destination folder does not exist: {d.parent}", ok=False)
    try:
        shutil.move(str(s), str(d))
    except (OSError, shutil.Error) as e:
        return ActionResult(text=f"Move failed: {e}", ok=False)
    return ActionResult(text=f"➡️ Moved {s}\n→ {d}")


# ---------------------------------------------------------------- uploads (used by the module)
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
             *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10)),
             "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
MAX_NAME_LEN = 150


def safe_filename(name: Optional[str], default: str = "upload") -> str:
    """Reduce an untrusted filename to a safe single path component (no dirs, no traversal,
    no ADS ':' / reserved device names / trailing dots)."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not name or set(name) <= {".", "_", " "}:
        name = default
    if name.split(".", 1)[0].strip().upper() in _RESERVED:
        name = "_" + name
    if len(name) > MAX_NAME_LEN:
        stem, ext = os.path.splitext(name)
        ext = ext[:20]
        name = stem[:MAX_NAME_LEN - len(ext)] + ext
    return name


def reserve_upload_path(filename: Optional[str], directory: Path | None = None) -> Path:
    """Create (atomically, O_EXCL) an empty file in the upload dir with a sanitised, unique name
    and return its path. Never overwrites: 'a.txt' -> 'a (1).txt' -> 'a (2).txt' ..."""
    d = Path(directory or settings.upload_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    stem, ext = os.path.splitext(name)
    for i in range(10_000):
        cand = d / (name if i == 0 else f"{stem} ({i}){ext}")
        if cand.resolve().parent != d:  # belt and braces against traversal
            raise ValueError(f"unsafe upload name {filename!r}")
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return cand
    raise RuntimeError("could not find a free upload filename")
