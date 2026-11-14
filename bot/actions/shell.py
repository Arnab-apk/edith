"""Shell actions: cmd.exe / PowerShell commands, per-chat working directory, launching programs.

The per-chat working directory lives in CWD (chat id -> Path). Every shell/file action takes a
`chat_id` parameter (0 = default/Edith-less context) so slash commands and Edith share it.
Path helpers here (resolve_path, split_args, strip_quotes) are reused by bot.actions.files and
the Telegram modules.
"""
from __future__ import annotations

import base64
import codecs
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import psutil

from ..config import settings
from ..utils import run_blocking
from .registry import ActionResult, action

CWD: dict[int, Path] = {}

CHAT_PARAM = "Telegram chat id whose working directory is used (filled in automatically; 0 = default)"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024   # keep at most this much raw output in memory
MAX_OUTPUT_CHARS = 200_000           # text returned to the user/model (send_result makes a .txt if long)
MAX_TIMEOUT = 3600
KILL_GRACE = 5.0                     # seconds to wait for the tree to die after a kill
READER_GRACE = 2.0                   # seconds to drain output after the main process exits


# ---------------------------------------------------------------- path helpers
def strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def split_raw(text: str) -> list[str]:
    """Split on whitespace, keeping "quoted parts" together (quotes are kept; Windows-style,
    backslashes are NOT escapes)."""
    try:
        return shlex.split(text, posix=False)
    except ValueError:  # unbalanced quote
        return text.split()


def split_args(text: str) -> list[str]:
    """Like split_raw but with surrounding quotes removed from each token."""
    return [strip_quotes(t) for t in split_raw(text)]


def get_cwd(chat_id: int = 0) -> Path:
    p = CWD.get(chat_id)
    if p is None or not p.is_dir():
        p = Path.home()
    return p


def resolve_path(chat_id: int, raw: str) -> Path:
    """Resolve a user-supplied path (quoted, ~, %VAR%, 'D:', relative) against the chat cwd.
    Empty -> cwd. The result is absolute and normalised ('..' collapsed); links are not followed."""
    s = strip_quotes(raw or "")
    if not s:
        return get_cwd(chat_id)
    s = os.path.expanduser(os.path.expandvars(s))
    if re.fullmatch(r"[A-Za-z]:", s):
        s += "\\"
    p = Path(s)
    if not p.is_absolute():
        p = get_cwd(chat_id) / p
    return Path(os.path.abspath(p))


# ---------------------------------------------------------------- process running
def _oem_codec() -> str | None:
    try:
        import ctypes
        name = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        codecs.lookup(name)
        return name
    except Exception:  # not Windows / unknown codepage
        return None


_OEM = _oem_codec()


def decode_output(data: bytes) -> str:
    """Decode console output. UTF-16 (wmic & co) is detected; otherwise strict UTF-8 is tried
    first (OEM codepages like cp437 accept *any* byte, so they must come after), then the OEM
    codepage, then UTF-8 with replacement."""
    if not data:
        return ""
    candidates: list[str] = []
    if len(data) >= 4 and (data[:2] == b"\xff\xfe" or data[1::2].count(0) > len(data) // 4):
        candidates.append("utf-16-le")
    candidates += ["utf-8-sig"] + ([_OEM] if _OEM else []) + ["mbcs"]
    for enc in candidates:
        try:
            text = data.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = data.decode("utf-8", "replace")
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def kill_tree(pid: int) -> None:
    """Kill a process and all its descendants."""
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    try:
        procs = parent.children(recursive=True)
    except psutil.Error:
        procs = []
    procs.append(parent)
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=3)


def run_process(cmd: str | list[str], cwd: Path, timeout: float) -> tuple[int | None, bytes, bool, float]:
    """Run a process capturing stdout+stderr (merged). Returns (exit_code, output, timed_out, seconds).
    On timeout the whole process tree is killed. We wait for the *main* process rather than EOF,
    because detached grandchildren (e.g. `start notepad`) inherit the pipe and would block forever."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=flags)
    chunks: list[bytes] = []
    size = 0

    def reader() -> None:
        nonlocal size
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            if size < MAX_OUTPUT_BYTES:
                chunks.append(chunk)
                size += len(chunk)

    t = threading.Thread(target=reader, daemon=True, name=f"shell-reader-{proc.pid}")
    t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc.pid)
        try:
            proc.wait(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
    t.join(READER_GRACE)
    out = b"".join(chunks)
    if size >= MAX_OUTPUT_BYTES:
        out += b"\n... (output truncated)"
    return (None if timed_out else proc.returncode), out, timed_out, time.monotonic() - start


def _clamp_timeout(timeout: Optional[int]) -> int:
    t = settings.shell_timeout if timeout is None else timeout
    return max(1, min(int(t), MAX_TIMEOUT))


def _format(label: str, command: str, cwd: Path, rc: int | None, out: bytes, timed_out: bool,
            elapsed: float, timeout: int) -> ActionResult:
    text = decode_output(out).rstrip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(text) - MAX_OUTPUT_CHARS} more chars)"
    echo = command if len(command) <= 300 else command[:300] + " ..."
    if timed_out:
        footer = f"⏱ timed out after {timeout}s - process tree killed"
    else:
        footer = f"exit code {rc} · {elapsed:.1f}s"
    body = f"{label} {cwd}> {echo}\n\n{text or '(no output)'}\n\n[{footer}]"
    return ActionResult(text=body, ok=(not timed_out and rc == 0), code=True,
                        data={"exit_code": rc, "timed_out": timed_out, "cwd": str(cwd)})


def _powershell_exe() -> str:
    exe = shutil.which("powershell.exe")
    if exe:
        return exe
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def build_cmd_line(command: str) -> str:
    # This IS the shell action: the owner's text is handed to cmd.exe verbatim.
    # /d = skip AutoRun, /s + outer quotes = cmd strips exactly those quotes and runs the rest.
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    return f'"{comspec}" /d /s /c "{command}"'


def build_ps_argv(command: str) -> list[str]:
    # The script is base64(UTF-16LE) so no quoting can break it. We deliberately avoid
    # -EncodedCommand: it makes PowerShell write errors to stderr as CLIXML.
    b64 = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    wrapper = ("$ProgressPreference='SilentlyContinue';"
               f"& ([scriptblock]::Create([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{b64}'))))")
    return [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", wrapper]


# ---------------------------------------------------------------- actions
_SHELL_PARAMS = {"command": "the command line to run",
                 "chat_id": CHAT_PARAM,
                 "timeout": "seconds before the whole process tree is killed (default: SHELL_TIMEOUT)"}


@action("shell_cmd", "Run a command in cmd.exe on the PC (in the chat's working directory) and return "
        "its combined stdout/stderr and exit code. Non-interactive: stdin is empty.",
        dangerous=True, category="shell", params=_SHELL_PARAMS)
async def shell_cmd(command: str, chat_id: int = 0, timeout: Optional[int] = None) -> ActionResult:
    if not command.strip():
        return ActionResult(text="Empty command.", ok=False)
    cwd, t = get_cwd(chat_id), _clamp_timeout(timeout)
    rc, out, timed_out, elapsed = await run_blocking(run_process, build_cmd_line(command), cwd, t)
    return _format("cmd", command, cwd, rc, out, timed_out, elapsed, t)


@action("shell_ps", "Run a Windows PowerShell command/script (powershell -NoProfile) on the PC in the "
        "chat's working directory and return its output and exit code. Non-interactive.",
        dangerous=True, category="shell", params=_SHELL_PARAMS)
async def shell_ps(command: str, chat_id: int = 0, timeout: Optional[int] = None) -> ActionResult:
    if not command.strip():
        return ActionResult(text="Empty command.", ok=False)
    cwd, t = get_cwd(chat_id), _clamp_timeout(timeout)
    rc, out, timed_out, elapsed = await run_blocking(run_process, build_ps_argv(command), cwd, t)
    return _format("PS", command, cwd, rc, out, timed_out, elapsed, t)


@action("cd", "Change the working directory used by shell and file commands for this chat.",
        category="shell",
        params={"path": "directory: absolute, relative, '..', '~', %VAR% or a drive like 'D:'. Empty = home folder",
                "chat_id": CHAT_PARAM})
async def cd(path: str = "", chat_id: int = 0) -> ActionResult:
    def work() -> ActionResult:
        target = resolve_path(chat_id, path) if strip_quotes(path) else Path.home()
        if not target.is_dir():
            return ActionResult(text=f"Not a directory: {target}", ok=False)
        CWD[chat_id] = target
        return ActionResult(text=f"📂 {target}", data={"cwd": str(target)})

    return await run_blocking(work)


@action("pwd", "Show the current working directory for this chat.", category="shell",
        params={"chat_id": CHAT_PARAM})
async def pwd(chat_id: int = 0) -> ActionResult:
    cwd = await run_blocking(get_cwd, chat_id)
    return ActionResult(text=f"📂 {cwd}", data={"cwd": str(cwd)})


@action("run_program", "Start a program, open a file with its default app, or launch something on PATH "
        "(e.g. 'notepad', 'C:\\Tools\\app.exe', 'report.pdf'). Does not wait for it to finish.",
        dangerous=True, category="shell",
        params={"target": "program name, or path to an executable/file (relative to the working directory)",
                "args": "optional command-line arguments",
                "chat_id": CHAT_PARAM})
async def run_program(target: str, args: str = "", chat_id: int = 0) -> ActionResult:
    def work() -> ActionResult:
        raw = strip_quotes(target)
        if not raw:
            return ActionResult(text="Nothing to run.", ok=False)
        cwd = get_cwd(chat_id)
        p = resolve_path(chat_id, raw)
        try:
            if p.exists():
                os.startfile(str(p), arguments=args, cwd=str(p.parent if p.is_file() else cwd))
                shown = str(p)
            else:  # let ShellExecute search PATH / App Paths (notepad, chrome, ...)
                os.startfile(raw, arguments=args, cwd=str(cwd))
                shown = raw
        except OSError as e:
            return ActionResult(text=f"Could not start {raw}: {e.strerror or e}", ok=False)
        return ActionResult(text=f"🚀 Started {shown}" + (f" {args}" if args else ""))

    return await run_blocking(work)
