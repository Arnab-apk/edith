"""Condition triggers polled every AUTOMATION_POLL_SECONDS (default 30 s).

Syntax (text after /when):
    cpu>90 [for 2m] [cooldown 15m] [then] <steps>      steps default to `notify`
    ram>90 · battery<20 (only while unplugged) · battery>95 · temp>85 (if readable)
    idle>30m  (no keyboard/mouse input for 30 min)
    process chrome.exe starts|stops
    file in Downloads · file *.pdf in "C:\\My Folder"   (a NEW file appears; steps may use {file})
    network lost · network back      (lost is reported when the connection returns - Telegram needs it)
    usb plugged · usb removed        (removable drives; steps may use {drive})
    pc locked · pc unlocked

Anti-spam: a trigger fires at most once per cooldown (default 15 min for levels, 1 min for process
events, none for the other events), level conditions must hold for the whole `for` window, and that
window restarts after firing. Event triggers baseline when created, so existing state never fires.
"""
from __future__ import annotations

import ctypes
import fnmatch
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (JOB_PREFIX, AutomationError, Plan, Task, db, describe_steps, fmt_duration, parse_steps,
               run_steps, step_is_dangerous)

log = logging.getLogger("automation.triggers")

POLL_SECONDS = max(5, int(os.getenv("AUTOMATION_POLL_SECONDS", "30")))
DEFAULT_COOLDOWN = 15 * 60
DEFAULT_PROCESS_COOLDOWN = 60
MIN_COOLDOWN = 60
POLL_JOB = f"{JOB_PREFIX}triggers"

METRIC_ALIASES = {"cpu": "cpu", "ram": "ram", "mem": "ram", "memory": "ram", "battery": "battery",
                  "bat": "battery", "temp": "temp", "temperature": "temp", "idle": "idle"}
UNITS = {"cpu": "%", "ram": "%", "battery": "%", "temp": "°C", "idle": ""}

EVENT_COOLDOWN = {"process": DEFAULT_PROCESS_COOLDOWN, "file": 0, "net": 0, "usb": 0, "lock": 0}
EVENT_METRICS = set(EVENT_COOLDOWN)
PLACEHOLDERS = {"file": "file", "drive": "usb"}    # {placeholder} -> metric that provides it
MAX_EVENTS_PER_POLL = 3        # steps run for at most this many new files/drives per poll
MAX_FOLDER_ENTRIES = 5000
NET_FAIL_POLLS = 2             # consecutive failed checks before the network counts as lost
NET_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 53))
PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload", ".!ut")
IGNORED_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}
KNOWN_FOLDERS = {"downloads": "Downloads", "desktop": "Desktop", "documents": "Documents",
                 "pictures": "Pictures", "videos": "Videos", "music": "Music"}
EXAMPLES = ("Examples: cpu>90 for 2m · ram>90 · battery<20 · temp>85 · idle>30m · process chrome.exe starts · "
            "file *.pdf in Downloads · network lost · network back · usb plugged · usb removed · pc locked · "
            "pc unlocked")

_FILE_RE = re.compile(r"""^\s*(?:new\s+)?files?\s+(?:(?!in\s)("[^"]+"|\S+)\s+)?in\s+("[^"]+"|'[^']+'|\S+)(?:\s+|$)""",
                      re.I)
_NET_RE = re.compile(r"^\s*(?:network|internet|connection|wifi)\s+(lost|down|offline|drops|dropped|"
                     r"back|restored|up|online|returns)\b\s*", re.I)
_USB_RE = re.compile(r"^\s*usb(?:\s+drive)?\s+(plugged|inserted|connected|added|in|removed|unplugged|ejected|"
                     r"disconnected|out)\b\s*", re.I)
_LOCK_RE = re.compile(r"^\s*(?:pc|screen|computer|session|windows)\s+(locked|locks|lock|unlocked|unlocks|"
                      r"unlock)\b\s*", re.I)
_LEVEL_RE = re.compile(r"^\s*([a-z]+)\s*(>=|<=|>|<)\s*(\d+(?:\.\d+)?)(%|°c|[a-z]+)?(?:\s+|$)", re.I)
_PROC_RE = re.compile(r"^\s*process\s+(\"[^\"]+\"|\S+)\s+(starts|started|start|stops|stopped|stop|exits)\b\s*", re.I)


# ---------------------------------------------------------------- parsing
def _take_kw_duration(text: str, kw: str) -> tuple[float | None, str]:
    from .scheduler import take_duration
    toks = text.split()
    if toks and toks[0].lower() == kw:
        secs, rest = take_duration(" ".join(toks[1:]))
        return secs, rest
    return None, text


def parse_condition(text: str) -> tuple[dict[str, Any], str]:
    """Parse the condition part; returns (spec, remaining text = steps)."""
    from .scheduler import parse_duration
    spec: dict[str, Any] | None = None
    m = _FILE_RE.match(text)
    if m:
        spec = {"metric": "file", "folder": resolve_folder(m.group(2)), "pattern": _norm_pattern(m.group(1))}
    elif (m := _NET_RE.match(text)):
        spec = {"metric": "net", "event": "lost" if m.group(1).lower() in
                ("lost", "down", "offline", "drops", "dropped") else "back"}
    elif (m := _USB_RE.match(text)):
        spec = {"metric": "usb", "event": "removed" if m.group(1).lower() in
                ("removed", "unplugged", "ejected", "disconnected", "out") else "plugged"}
    elif (m := _LOCK_RE.match(text)):
        spec = {"metric": "lock", "event": "unlocked" if "unlock" in m.group(1).lower() else "locked"}
    if spec is not None:
        spec.update({"for": 0, "cooldown": EVENT_COOLDOWN[spec["metric"]]})
        rest = text[m.end():]
    elif (m := _PROC_RE.match(text)):
        proc = m.group(1).strip('"')
        event = "starts" if m.group(2).lower().startswith("start") else "stops"
        spec = {"metric": "process", "process": proc, "event": event, "for": 0,
                "cooldown": DEFAULT_PROCESS_COOLDOWN}
        rest = text[m.end():]
    else:
        m = _LEVEL_RE.match(text)
        if not m:
            raise AutomationError(f"Bad condition. {EXAMPLES}")
        metric = METRIC_ALIASES.get(m.group(1).lower())
        if metric is None:
            raise AutomationError(f"Unknown metric {m.group(1)!r}. {EXAMPLES}")
        op = m.group(2)[0]  # >= behaves like > (polling granularity makes the difference moot)
        unit = (m.group(4) or "").lower()
        if metric == "idle":
            if op != ">":
                raise AutomationError("Use idle>DURATION, e.g. idle>30m.")
            value = parse_duration(m.group(3) + (unit or "m"))
        else:
            if unit not in ("", "%", "c", "°c"):
                raise AutomationError(f"Unexpected unit {unit!r} for {metric}.")
            value = float(m.group(3))
            if metric in ("cpu", "ram", "battery") and not 0 <= value <= 100:
                raise AutomationError("Percent must be between 0 and 100.")
        spec = {"metric": metric, "op": op, "value": value, "for": 0, "cooldown": DEFAULT_COOLDOWN}
        rest = text[m.end():]
    # optional modifiers, any order: for <dur>, cooldown <dur>
    for _ in range(2):
        dur, rest = _take_kw_duration(rest, "for")
        if dur is not None:
            if spec["metric"] in EVENT_METRICS:
                raise AutomationError(f"'for' is not supported with {spec['metric']} triggers (they are events).")
            spec["for"] = dur
        cd, rest = _take_kw_duration(rest, "cooldown")
        if cd is not None:
            spec["cooldown"] = max(MIN_COOLDOWN, cd)
    rest = rest.strip()
    for lead in ("then ", "do ", ": ", "-> "):
        if rest.lower().startswith(lead):
            rest = rest[len(lead):]
    if rest.startswith(":"):
        rest = rest[1:]
    return spec, rest.strip()


def _home() -> Path:
    return Path.home()


def resolve_folder(raw: str) -> str:
    """'Downloads' / '~/x' / '"C:\\My Folder"' -> absolute existing folder path."""
    raw = raw.strip().strip("\"'")
    key = raw.lower().rstrip("\\/")
    p = _home() / KNOWN_FOLDERS[key] if key in KNOWN_FOLDERS else Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        raise AutomationError(f"Use a full folder path or Downloads/Desktop/Documents/Pictures/Videos/Music, "
                              f"not {raw!r}. Quote paths with spaces.")
    if not p.is_dir():
        raise AutomationError(f"Folder not found: {p}")
    return str(p.resolve())


def _norm_pattern(p: str | None) -> str:
    """None -> '*'; 'pdf' / '.pdf' -> '*.pdf'; wildcards and exact names unchanged."""
    if not p:
        return "*"
    p = p.strip("\"'")
    if any(c in p for c in "*?["):
        return p
    if p.startswith("."):
        return "*" + p
    return p if "." in p else f"*.{p}"


def describe_condition(spec: dict[str, Any]) -> str:
    metric = spec["metric"]
    if metric == "file":
        s = f"new file {spec['pattern']} in {spec['folder']}"
    elif metric == "net":
        s = f"network {spec['event']}"
    elif metric == "usb":
        s = f"usb {spec['event']}"
    elif metric == "lock":
        s = f"pc {spec['event']}"
    elif metric == "process":
        s = f"process {spec['process']} {spec['event']}"
    elif spec["metric"] == "idle":
        s = f"idle>{fmt_duration(spec['value'])}"
    else:
        s = f"{spec['metric']}{spec['op']}{spec['value']:g}{UNITS[spec['metric']]}"
    if spec.get("for"):
        s += f" for {fmt_duration(spec['for'])}"
    return s + (f" (cooldown {fmt_duration(spec['cooldown'])})" if spec.get("cooldown") else "")


def _uses(value: Any, token: str) -> bool:
    if isinstance(value, str):
        return token in value
    if isinstance(value, list):
        return any(_uses(v, token) for v in value)
    return False


def _check_placeholders(spec: dict[str, Any], steps: list[dict[str, Any]]) -> None:
    for step in steps:
        for ph, metric in PLACEHOLDERS.items():
            if not any(_uses(v, "{" + ph + "}") for v in (step.get("args") or {}).values()):
                continue
            if spec["metric"] != metric:
                raise AutomationError(f"{{{ph}}} only works in {metric} triggers.")
            if step_is_dangerous(step):
                raise AutomationError(f"{{{ph}}} can't be used in a dangerous step ({step['action']}): a file or "
                                      "drive name must never become part of a command. Use it in notify or a "
                                      "safe action.")


def substitute(steps: list[dict[str, Any]], values: dict[str, str]) -> list[dict[str, Any]]:
    """Fill {file}/{drive} into string args of SAFE steps (dangerous steps are always left literal)."""
    if not values:
        return steps

    def fill(v: Any) -> Any:
        if isinstance(v, str):
            for k, val in values.items():
                v = v.replace("{" + k + "}", val)
            return v
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v

    return [s if step_is_dangerous(s) else
            {"action": s["action"], "args": {k: fill(v) for k, v in (s.get("args") or {}).items()}}
            for s in steps]


def plan_trigger(text: str) -> Plan:
    spec, rest = parse_condition(text)
    steps = parse_steps(rest or "notify")
    _check_placeholders(spec, steps)
    return Plan(kind="trigger", spec=spec, steps=steps,
                label=f"when {describe_condition(spec)}: {describe_steps(steps)}")


async def readability_note(plan: Plan) -> str:
    """'' if the trigger's metric can be read on this PC, else a warning for the owner."""
    from ..utils import run_blocking
    metric = plan.spec.get("metric")
    if metric not in ("battery", "temp", "idle", "lock"):
        return ""
    snap = await run_blocking(sample, {metric})
    if snap.get(metric) is None:
        return (f"Note: {metric} is not readable on this PC right now; the trigger will stay silent "
                "until it is.")
    return ""


# ---------------------------------------------------------------- sampling (blocking; run in a thread)
_cpu_prev: tuple[float, float] | None = None
_temp_sources = {"nvidia": True, "acpi": True}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _cpu_percent() -> float | None:
    """CPU % since the previous poll, from our own cpu_times deltas (independent of other modules)."""
    import psutil
    global _cpu_prev
    t = psutil.cpu_times()
    total, idle = sum(t), t.idle
    prev, _cpu_prev = _cpu_prev, (total, idle)
    if prev is None or total <= prev[0]:
        return psutil.cpu_percent(interval=0.5)
    return max(0.0, min(100.0, 100.0 * (1 - (idle - prev[1]) / (total - prev[0]))))


def _battery() -> tuple[float, bool] | None:
    import psutil
    b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    return None if b is None else (float(b.percent), bool(b.power_plugged))


def _temperature() -> float | None:
    """Max readable temperature in °C: psutil sensors, NVIDIA GPU, ACPI thermal zone (often needs admin)."""
    import psutil
    temps: list[float] = []
    if hasattr(psutil, "sensors_temperatures"):
        try:
            temps += [e.current for es in psutil.sensors_temperatures().values() for e in es if e.current]
        except Exception:
            pass
    if _temp_sources["nvidia"]:
        exe = shutil.which("nvidia-smi")
        try:
            if not exe:
                raise FileNotFoundError
            out = subprocess.run([exe, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            temps += [float(x) for x in out.stdout.split() if x.strip().replace(".", "", 1).isdigit()]
        except Exception:
            _temp_sources["nvidia"] = False
    if _temp_sources["acpi"] and sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                 "-ErrorAction Stop).CurrentTemperature"],
                capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
            vals = [int(x) / 10 - 273.15 for x in out.stdout.split() if x.isdigit()]
            if not vals:
                raise ValueError("no ACPI temps")
            temps += [v for v in vals if 0 < v < 150]
        except Exception:
            _temp_sources["acpi"] = False
    return max(temps) if temps else None


def _idle_seconds() -> float | None:
    if sys.platform != "win32":
        return None

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def _process_names() -> set[str]:
    import psutil
    names = set()
    for p in psutil.process_iter(["name"]):
        n = (p.info.get("name") or "").lower()
        if n:
            names.add(n)
    return names


def _list_folder(folder: str) -> dict[str, float] | None:
    """Finished files in a folder (name -> mtime). Partial downloads are skipped until renamed."""
    out: dict[str, float] = {}
    try:
        it = os.scandir(folder)
    except OSError:
        return None
    with it:
        for e in it:
            if len(out) >= MAX_FOLDER_ENTRIES:
                break
            name = e.name
            low = name.lower()
            if low.endswith(PARTIAL_SUFFIXES) or low in IGNORED_FILES or name.startswith("~$"):
                continue
            try:
                if e.is_file():
                    out[name] = e.stat().st_mtime
            except OSError:
                continue
    return out


def _internet_up() -> bool:
    """Cheap reachability check: TCP connect to a public anycast IP (no DNS), 2 s timeout each."""
    for host, port in NET_PROBES:
        try:
            socket.create_connection((host, port), timeout=2).close()
            return True
        except OSError:
            continue
    return False


def _drive_type(root: str) -> int:
    if sys.platform != "win32":
        return 0
    return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))


def _removable_drives() -> set[str]:
    import psutil
    DRIVE_REMOVABLE = 2
    out = set()
    for p in psutil.disk_partitions(all=False):
        if "removable" in (p.opts or "").lower() or _drive_type(p.mountpoint) == DRIVE_REMOVABLE:
            out.add(p.mountpoint)
    return out


def _is_locked() -> bool | None:
    """True when the interactive desktop is not 'Default' (lock screen / Winlogon). Read-only check."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    DESKTOP_SWITCHDESKTOP, UOI_NAME = 0x0100, 2
    h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h:
        return True   # the secure (lock) desktop can't be opened by a normal process
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(h, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return None
        return buf.value.lower() != "default"
    finally:
        user32.CloseDesktop(h)


def sample_key(spec: dict[str, Any]) -> str:
    return f"file:{spec['folder']}" if spec["metric"] == "file" else spec["metric"]


def sample(metrics: set[str]) -> dict[str, Any]:
    """Read only the metrics/keys that are needed ('file:<folder>' lists a folder). Unreadable -> None."""
    out: dict[str, Any] = {}
    readers = {"cpu": _cpu_percent, "battery": _battery, "temp": _temperature,
               "idle": _idle_seconds, "process": _process_names, "net": _internet_up,
               "usb": _removable_drives, "lock": _is_locked}
    for m in metrics:
        try:
            if m.startswith("file:"):
                out[m] = _list_folder(m[5:])
            elif m == "ram":
                import psutil
                out[m] = psutil.virtual_memory().percent
            else:
                out[m] = readers[m]()
        except Exception:
            log.debug("could not read %s", m, exc_info=True)
            out[m] = None
    return out


# ---------------------------------------------------------------- evaluation (pure)
@dataclass
class TriggerState:
    since: float | None = None          # when the level condition started holding
    prev_running: bool | None = None    # process triggers: last observed state (None = no baseline yet)
    last_fired: float | None = None    # None = never fired
    extra: dict[str, Any] = field(default_factory=dict)


_STATE: dict[int, TriggerState] = {}


def forget(task_id: int) -> None:
    _STATE.pop(task_id, None)


def _proc_match(pattern: str, names: set[str]) -> bool:
    p = pattern.lower()
    return p in names or (not p.endswith(".exe") and f"{p}.exe" in names)


def _cooled(st: TriggerState, spec: dict[str, Any], now: float) -> bool:
    return st.last_fired is None or now - st.last_fired >= spec["cooldown"]


def evaluate(spec: dict[str, Any], st: TriggerState, snap: dict[str, Any], now: float) -> str | None:
    """Update state; return a human 'reading' string if the trigger should fire now, else None.
    Event triggers may also leave st.extra["vars"] = [{placeholder: value}, ...] (one entry per event)."""
    metric = spec["metric"]
    st.extra.pop("vars", None)
    if metric in _EVENT_EVAL:
        return _EVENT_EVAL[metric](spec, st, snap.get(sample_key(spec)), now)
    if metric == "process":
        names = snap.get("process")
        if names is None:
            return None
        running = _proc_match(spec["process"], names)
        prev, st.prev_running = st.prev_running, running
        if prev is None:
            return None
        hit = (not prev and running) if spec["event"] == "starts" else (prev and not running)
        if hit and _cooled(st, spec, now):
            st.last_fired = now
            return f"{spec['process']} {'started' if running else 'stopped'}"
        return None
    raw = snap.get(metric)
    if raw is None:
        st.since = None
        return None
    if metric == "battery":
        value, plugged = raw
        holds = (value < spec["value"] and not plugged) if spec["op"] == "<" else value > spec["value"]
    else:
        value = raw
        holds = value > spec["value"] if spec["op"] == ">" else value < spec["value"]
    if not holds:
        st.since = None
        return None
    if st.since is None:
        st.since = now
    if now - st.since >= spec.get("for", 0) and _cooled(st, spec, now):
        st.last_fired = now
        st.since = None  # the `for` window must elapse again before the next fire
        if metric == "idle":
            return f"idle for {fmt_duration(value)}"
        return f"{metric} is {value:.0f}{UNITS[metric]}"
    return None


def _eval_file(spec: dict[str, Any], st: TriggerState, listing: dict[str, float] | None, now: float) -> str | None:
    if listing is None:
        return None
    known = st.extra.get("known")
    if known is None:   # first look: everything not newer than the reference time is baseline
        ref = st.extra.get("ref")
        known = {n for n, mt in listing.items() if ref is None or mt <= ref}
    pat = spec["pattern"].lower()
    new = sorted(n for n in listing if n not in known and fnmatch.fnmatch(n.lower(), pat))
    st.extra["known"] = set(listing)
    if not new or not _cooled(st, spec, now):
        return None
    st.last_fired = now
    shown = new[:MAX_EVENTS_PER_POLL]
    st.extra["vars"] = [{"file": str(Path(spec["folder"]) / n)} for n in shown]
    more = f" (+{len(new) - len(shown)} more)" if len(new) > len(shown) else ""
    return (f"new file {new[0]}" if len(new) == 1 else f"{len(new)} new files: {', '.join(shown)}") + more


def _eval_net(spec: dict[str, Any], st: TriggerState, up: bool | None, now: float) -> str | None:
    if up is None:
        return None
    if not up:
        st.extra.setdefault("first_fail", now)
        st.extra["fails"] = st.extra.get("fails", 0) + 1
        if st.extra["fails"] >= NET_FAIL_POLLS:
            st.extra.setdefault("down_since", st.extra["first_fail"])
        return None
    st.extra.pop("fails", None)
    st.extra.pop("first_fail", None)
    down_since = st.extra.pop("down_since", None)
    if down_since is None or not _cooled(st, spec, now):
        return None
    st.last_fired = now
    offline = fmt_duration(now - down_since)
    if spec["event"] == "lost":   # queued notice: Telegram was unreachable while it was down
        when = time.strftime("%H:%M", time.localtime(down_since))
        return f"internet was lost at {when} for about {offline} (back now)"
    return f"internet is back after about {offline} offline"


def _eval_usb(spec: dict[str, Any], st: TriggerState, drives: set[str] | None, now: float) -> str | None:
    if drives is None:
        return None
    prev = st.extra.get("drives")
    st.extra["drives"] = set(drives)
    if prev is None:
        return None
    changed = sorted(drives - prev) if spec["event"] == "plugged" else sorted(prev - drives)
    if not changed or not _cooled(st, spec, now):
        return None
    st.last_fired = now
    shown = changed[:MAX_EVENTS_PER_POLL]
    st.extra["vars"] = [{"drive": d} for d in shown]
    return f"USB drive {', '.join(shown)} {'plugged in' if spec['event'] == 'plugged' else 'removed'}"


def _eval_lock(spec: dict[str, Any], st: TriggerState, locked: bool | None, now: float) -> str | None:
    if locked is None:
        return None
    if st.prev_running is None:          # baseline
        st.prev_running = locked
        return None
    if locked == st.prev_running:
        st.extra.pop("pending", None)
        return None
    # debounce: a change must be seen on two polls in a row (a UAC prompt briefly looks 'locked')
    if st.extra.get("pending") != locked:
        st.extra["pending"] = locked
        return None
    st.extra.pop("pending", None)
    st.prev_running = locked
    hit = locked if spec["event"] == "locked" else not locked
    if not hit or not _cooled(st, spec, now):
        return None
    st.last_fired = now
    return "PC was locked" if locked else "PC was unlocked"


_EVENT_EVAL = {"file": _eval_file, "net": _eval_net, "usb": _eval_usb, "lock": _eval_lock}


async def on_created(task: Task) -> None:
    """Baseline event triggers right away so files/drives/states that already exist never fire."""
    if task.kind != "trigger" or task.spec.get("metric") not in ("process", "file", "usb", "lock"):
        return
    from ..utils import run_blocking
    try:
        snap = await run_blocking(sample, {sample_key(task.spec)})
        st = TriggerState()
        evaluate(task.spec, st, snap, time.time())
        st.last_fired = None
        st.extra.pop("vars", None)
        _STATE[task.id] = st
    except Exception:
        log.exception("could not baseline trigger #%s", task.id)


# ---------------------------------------------------------------- polling job
_polling = False


async def poll_once(bot: Any, now: float | None = None) -> int:
    """Check every trigger once; returns how many fired. Never raises."""
    global _polling
    if _polling:          # a slow sample (e.g. temp via PowerShell) must not overlap the next poll
        return 0
    _polling = True
    fired = 0
    try:
        from ..utils import run_blocking
        tasks: list[Task] = await db("list_tasks", "trigger")
        if not tasks:
            return 0
        snap = await run_blocking(sample, {sample_key(t.spec) for t in tasks})
        now = time.time() if now is None else now
        for t in tasks:
            st = _STATE.get(t.id)
            if st is None:   # e.g. after a restart: files newer than the last check count as new
                st = _STATE[t.id] = TriggerState(last_fired=t.last_fired,
                                                 extra={"ref": max(t.created, t.last_fired or 0)})
            reading = evaluate(t.spec, st, snap, now)
            if reading is None:
                continue
            fired += 1
            await db("set_last_fired", t.id, now)
            header = f"🔔 Trigger #{t.id}: {describe_condition(t.spec)} - {reading}"
            for values in st.extra.pop("vars", None) or [{}]:
                await run_steps(bot, t.chat_id, substitute(t.steps, values), header=header,
                                approved_dangerous=t.dangerous)
    except Exception:
        log.exception("trigger poll failed")
    finally:
        _polling = False
    return fired


async def _poll_callback(context: Any) -> None:
    await poll_once(context.bot)


def start(app: Any) -> None:
    """Start the single repeating poll job (idempotent)."""
    jq = app.job_queue
    if jq.get_jobs_by_name(POLL_JOB):
        return
    jq.run_repeating(_poll_callback, interval=POLL_SECONDS, first=POLL_SECONDS, name=POLL_JOB)
    log.info("trigger polling every %ss", POLL_SECONDS)
