"""Condition triggers polled every AUTOMATION_POLL_SECONDS (default 30 s).

Syntax (text after /when):
    cpu>90 [for 2m] [cooldown 15m] [then] <steps>      steps default to `notify`
    ram>90 · battery<20 (only while unplugged) · battery>95 · temp>85 (if readable)
    idle>30m  (no keyboard/mouse input for 30 min)
    process chrome.exe starts|stops

Anti-spam: a trigger fires at most once per cooldown (default 15 min, process events 1 min),
level conditions must hold for the whole `for` window, and that window restarts after firing.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from . import (JOB_PREFIX, AutomationError, Plan, Task, db, describe_steps, fmt_duration, parse_steps,
               run_steps)

log = logging.getLogger("automation.triggers")

POLL_SECONDS = max(5, int(os.getenv("AUTOMATION_POLL_SECONDS", "30")))
DEFAULT_COOLDOWN = 15 * 60
DEFAULT_PROCESS_COOLDOWN = 60
MIN_COOLDOWN = 60
POLL_JOB = f"{JOB_PREFIX}triggers"

METRIC_ALIASES = {"cpu": "cpu", "ram": "ram", "mem": "ram", "memory": "ram", "battery": "battery",
                  "bat": "battery", "temp": "temp", "temperature": "temp", "idle": "idle"}
UNITS = {"cpu": "%", "ram": "%", "battery": "%", "temp": "°C", "idle": ""}

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
    m = _PROC_RE.match(text)
    if m:
        proc = m.group(1).strip('"')
        event = "starts" if m.group(2).lower().startswith("start") else "stops"
        spec: dict[str, Any] = {"metric": "process", "process": proc, "event": event, "for": 0,
                                "cooldown": DEFAULT_PROCESS_COOLDOWN}
        rest = text[m.end():]
    else:
        m = _LEVEL_RE.match(text)
        if not m:
            raise AutomationError("Bad condition. Examples: cpu>90 for 2m · ram>90 · battery<20 · temp>85 · "
                                  "idle>30m · process chrome.exe starts")
        metric = METRIC_ALIASES.get(m.group(1).lower())
        if metric is None:
            raise AutomationError(f"Unknown metric {m.group(1)!r}. Use cpu, ram, battery, temp, idle or process.")
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
            if spec["metric"] == "process":
                raise AutomationError("'for' is not supported with process triggers.")
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


def describe_condition(spec: dict[str, Any]) -> str:
    if spec["metric"] == "process":
        s = f"process {spec['process']} {spec['event']}"
    elif spec["metric"] == "idle":
        s = f"idle>{fmt_duration(spec['value'])}"
    else:
        s = f"{spec['metric']}{spec['op']}{spec['value']:g}{UNITS[spec['metric']]}"
    if spec.get("for"):
        s += f" for {fmt_duration(spec['for'])}"
    return s + f" (cooldown {fmt_duration(spec['cooldown'])})"


def plan_trigger(text: str) -> Plan:
    spec, rest = parse_condition(text)
    steps = parse_steps(rest or "notify")
    return Plan(kind="trigger", spec=spec, steps=steps,
                label=f"when {describe_condition(spec)}: {describe_steps(steps)}")


async def readability_note(plan: Plan) -> str:
    """'' if the trigger's metric can be read on this PC, else a warning for the owner."""
    from ..utils import run_blocking
    metric = plan.spec.get("metric")
    if metric not in ("battery", "temp", "idle"):
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


def sample(metrics: set[str]) -> dict[str, Any]:
    """Read only the metrics that are needed. Missing/unreadable -> None."""
    out: dict[str, Any] = {}
    readers = {"cpu": _cpu_percent, "battery": _battery, "temp": _temperature,
               "idle": _idle_seconds, "process": _process_names}
    for m in metrics:
        try:
            if m == "ram":
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
    """Update state; return a human 'reading' string if the trigger should fire now, else None."""
    metric = spec["metric"]
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
        snap = await run_blocking(sample, {t.spec["metric"] for t in tasks})
        now = time.time() if now is None else now
        for t in tasks:
            st = _STATE.setdefault(t.id, TriggerState(last_fired=t.last_fired))
            reading = evaluate(t.spec, st, snap, now)
            if reading is None:
                continue
            fired += 1
            await db("set_last_fired", t.id, now)
            header = f"🔔 Trigger #{t.id}: {describe_condition(t.spec)} - {reading}"
            await run_steps(bot, t.chat_id, t.steps, header=header, approved_dangerous=t.dangerous)
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
