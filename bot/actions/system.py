"""System info + process actions: status, gpu, procs, kill, net, windows.

All OS access is in small private helpers (`_run`, `_nvidia_query`, `_public_ip`, ...)
so tests can monkeypatch them. Only `kill_process` is dangerous.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Any, Literal

import psutil

from ..utils import human_bytes, run_blocking
from .registry import ActionResult, action

log = logging.getLogger("actions.system")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash when run under pythonw
PUBLIC_IP_URL = "https://api.ipify.org"
PUBLIC_IP_TIMEOUT = 3.0

# Never killed, even when explicitly asked: killing these bluescreens or wrecks the session.
PROTECTED_NAMES = frozenset({
    "system", "system idle process", "idle", "registry", "memory compression", "secure system",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "lsaiso.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe",
})


# ---------------------------------------------------------------- low-level helpers (monkeypatchable)
def _run(args: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run a command (argument list, no shell). Returns (returncode, stdout or stdout+stderr on failure)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return 127, f"{args[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]}: timed out after {timeout:g}s"
    except OSError as e:
        return 1, f"{args[0]}: {e}"
    out = p.stdout.strip()
    if p.returncode != 0:
        out = (out + "\n" + p.stderr.strip()).strip()
    return p.returncode, out


def _num(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None  # "[N/A]", "[Not Supported]", ""


_NV_FIELDS = ["name", "driver_version", "utilization.gpu", "utilization.memory", "memory.used",
              "memory.total", "temperature.gpu", "power.draw", "power.limit", "fan.speed",
              "clocks.gr", "clocks.mem"]


def _nvidia_query() -> list[dict[str, Any]] | None:
    """Per-GPU stats from nvidia-smi, or None if unavailable."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    rc, out = _run([exe, f"--query-gpu={','.join(_NV_FIELDS)}", "--format=csv,noheader,nounits"])
    if rc != 0 or not out:
        return None
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(_NV_FIELDS):
            continue
        g: dict[str, Any] = dict(zip(_NV_FIELDS, parts))
        for k in _NV_FIELDS[2:]:
            g[k] = _num(g[k])
        gpus.append(g)
    return gpus or None


def _nvidia_apps() -> list[tuple[int, str, float | None]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    rc, out = _run([exe, "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
    if rc != 0:
        return []
    apps = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[0].isdigit():
            apps.append((int(parts[0]), os.path.basename(parts[1]), _num(parts[2])))
    return apps


def _gputil_query() -> list[dict[str, Any]] | None:
    try:
        import GPUtil  # noqa: N813 - package name
        gpus = GPUtil.getGPUs()
    except Exception:  # noqa: BLE001 - GPUtil raises all sorts when no driver
        return None
    return [{"name": g.name, "driver_version": g.driver, "utilization.gpu": g.load * 100,
             "utilization.memory": None, "memory.used": g.memoryUsed, "memory.total": g.memoryTotal,
             "temperature.gpu": g.temperature, "power.draw": None, "power.limit": None,
             "fan.speed": None, "clocks.gr": None, "clocks.mem": None} for g in gpus] or None


def _gpus() -> list[dict[str, Any]] | None:
    return _nvidia_query() or _gputil_query()


_ACPI_TEMP_UNAVAILABLE = False


def _cpu_temp() -> tuple[float, str] | None:
    """Best-effort CPU/board temperature in °C. Windows rarely exposes this without admin."""
    global _ACPI_TEMP_UNAVAILABLE
    fn = getattr(psutil, "sensors_temperatures", None)  # not on Windows, but harmless elsewhere
    if fn:
        try:
            temps = [t.current for entries in fn().values() for t in entries if t.current]
            if temps:
                return max(temps), "sensor"
        except Exception:  # noqa: BLE001
            pass
    if sys.platform != "win32" or _ACPI_TEMP_UNAVAILABLE:
        return None
    rc, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                    "-ErrorAction Stop).CurrentTemperature"], timeout=6)
    vals = [_num(x) for x in out.split()] if rc == 0 else []
    vals = [v / 10 - 273.15 for v in vals if v]  # tenths of Kelvin
    vals = [v for v in vals if 0 < v < 130]
    if not vals:
        _ACPI_TEMP_UNAVAILABLE = True  # usually "access denied" without admin: don't respawn powershell
        return None
    return max(vals), "ACPI zone"


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # UDP connect sends nothing; just picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "n/a"


def _public_ip() -> str:
    try:
        req = urllib.request.Request(PUBLIC_IP_URL, headers={"User-Agent": "pc-controller"})
        with urllib.request.urlopen(req, timeout=PUBLIC_IP_TIMEOUT) as r:  # noqa: S310 - fixed https URL
            ip = r.read(64).decode("ascii", "replace").strip()
        return ip if ip and len(ip) <= 45 else "n/a"
    except Exception:  # noqa: BLE001 - offline / blocked / slow
        return "n/a"


# ---------------------------------------------------------------- formatting
def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = [f"{d}d"] if d else []
    if d or h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _fmt_gpu_line(g: dict[str, Any]) -> str:
    bits = [g["name"]]
    if g.get("utilization.gpu") is not None:
        bits.append(f"{g['utilization.gpu']:.0f}%")
    if g.get("memory.used") is not None and g.get("memory.total"):
        bits.append(f"{g['memory.used'] / 1024:.1f}/{g['memory.total'] / 1024:.1f} GB")
    if g.get("temperature.gpu") is not None:
        bits.append(f"{g['temperature.gpu']:.0f}°C")
    if g.get("power.draw") is not None:
        bits.append(f"{g['power.draw']:.0f} W")
    return " · ".join(bits)


def _disks() -> list[dict[str, Any]]:
    out = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:  # removable drive with no media, permission, ...
            continue
        out.append({"mount": part.mountpoint, "used": u.used, "total": u.total, "percent": u.percent})
    return out


def _battery() -> dict[str, Any] | None:
    try:
        b = psutil.sensors_battery()
    except Exception:  # noqa: BLE001
        return None
    if b is None:
        return None
    left = b.secsleft if isinstance(b.secsleft, int) and b.secsleft >= 0 else None
    return {"percent": b.percent, "plugged": b.power_plugged, "secsleft": left}


def _cpu_snapshot() -> dict[str, Any]:
    pct = psutil.cpu_percent(interval=0.5)
    freq = None
    try:
        f = psutil.cpu_freq()
        freq = f.current if f else None
    except Exception:  # noqa: BLE001
        pass
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    return {"cpu": pct, "threads": psutil.cpu_count(), "cores": psutil.cpu_count(logical=False),
            "freq_mhz": freq, "ram_used": vm.total - vm.available, "ram_total": vm.total,
            "ram_pct": vm.percent, "swap_used": sw.used, "swap_total": sw.total}


# ---------------------------------------------------------------- actions
@action("system_status", "Full PC health snapshot: CPU, RAM, GPU, disks, battery, temperatures, "
        "local + public IP, uptime.", category="system")
async def system_status() -> ActionResult:
    import asyncio

    snap, gpus, temp, pub = await asyncio.gather(
        run_blocking(_cpu_snapshot), run_blocking(_gpus), run_blocking(_cpu_temp), run_blocking(_public_ip))
    disks, batt, local = _disks(), _battery(), _local_ip()
    boot = psutil.boot_time()

    lines = [f"🖥 {socket.gethostname()} — {platform.system()} {platform.release()} ({platform.version()})",
             f"⏱ Uptime: {_fmt_duration(time.time() - boot)} "
             f"(since {dt.datetime.fromtimestamp(boot):%Y-%m-%d %H:%M})"]
    cpu = f"🧠 CPU: {snap['cpu']:.0f}% · {snap['cores']}C/{snap['threads']}T"
    if snap["freq_mhz"]:
        cpu += f" · {snap['freq_mhz'] / 1000:.2f} GHz"
    lines.append(cpu)
    ram = (f"💾 RAM: {human_bytes(snap['ram_used'])} / {human_bytes(snap['ram_total'])} "
           f"({snap['ram_pct']:.0f}%)")
    if snap["swap_total"]:
        ram += f" · swap {human_bytes(snap['swap_used'])} / {human_bytes(snap['swap_total'])}"
    lines.append(ram)
    if gpus:
        lines += [f"🎮 GPU: {_fmt_gpu_line(g)}" for g in gpus]
    else:
        lines.append("🎮 GPU: no NVIDIA stats (nvidia-smi/GPUtil unavailable)")
    temps = []
    if temp:
        temps.append(f"CPU {temp[0]:.0f}°C ({temp[1]})")
    temps += [f"GPU {g['temperature.gpu']:.0f}°C" for g in gpus or [] if g.get("temperature.gpu") is not None]
    lines.append("🌡 Temps: " + (" · ".join(temps) if temps else "n/a (Windows hides CPU temp without admin)"))
    if disks:
        lines.append("💽 Disks: " + " · ".join(
            f"{d['mount'].rstrip(chr(92))} {human_bytes(d['used'])}/{human_bytes(d['total'])} ({d['percent']:.0f}%)"
            for d in disks))
    if batt:
        b = f"🔋 Battery: {batt['percent']:.0f}% · " + ("plugged in" if batt["plugged"] else "on battery")
        if not batt["plugged"] and batt["secsleft"] is not None:
            b += f" · {_fmt_duration(batt['secsleft'])} left"
        lines.append(b)
    lines.append(f"🌐 IP: local {local} · public {pub}")

    data = {**snap, "gpus": gpus or [], "cpu_temp": temp[0] if temp else None, "disks": disks,
            "battery": batt, "local_ip": local, "public_ip": pub, "boot_time": boot}
    return ActionResult(text="\n".join(lines), data=data)


@action("gpu_status", "Detailed NVIDIA GPU stats (load, VRAM, temperature, power, clocks) and the "
        "processes using the GPU.", category="system")
async def gpu_status() -> ActionResult:
    gpus = await run_blocking(_gpus)
    if not gpus:
        return ActionResult(text="No GPU stats available (nvidia-smi not found and GPUtil found no NVIDIA GPU).",
                            ok=False)
    apps = await run_blocking(_nvidia_apps)

    def v(x: Any, fmt: str, unit: str = "") -> str:
        return "n/a" if x is None else f"{x:{fmt}}{unit}"

    lines = []
    for i, g in enumerate(gpus):
        lines += [f"🎮 GPU {i}: {g['name']} (driver {g.get('driver_version') or 'n/a'})",
                  f"Load: {v(g['utilization.gpu'], '.0f', '%')} · memory controller "
                  f"{v(g['utilization.memory'], '.0f', '%')}",
                  f"VRAM: {v(g['memory.used'], '.0f')} / {v(g['memory.total'], '.0f')} MiB",
                  f"Temp: {v(g['temperature.gpu'], '.0f', '°C')} · fan {v(g['fan.speed'], '.0f', '%')}",
                  f"Power: {v(g['power.draw'], '.1f', ' W')} / limit {v(g['power.limit'], '.0f', ' W')}",
                  f"Clocks: core {v(g['clocks.gr'], '.0f', ' MHz')} · mem {v(g['clocks.mem'], '.0f', ' MHz')}"]
    if apps:
        lines.append(f"\nProcesses on GPU ({len(apps)}):")
        lines += [f"  {pid:>6}  {name}" + (f"  {mem:.0f} MiB" if mem is not None else "")
                  for pid, name, mem in apps[:25]]
        if len(apps) > 25:
            lines.append(f"  … and {len(apps) - 25} more")
    return ActionResult(text="\n".join(lines), data={"gpus": gpus, "apps": [list(a) for a in apps]})


def _collect_procs(sort: str, limit: int) -> tuple[str, list[dict[str, Any]]]:
    ncpu = psutil.cpu_count() or 1
    psutil.cpu_percent(None)  # prime the system-wide counter for the header
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        if p.info["pid"] == 0:  # System Idle Process
            continue
        try:
            p.cpu_percent(None)
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.6)
    rows = []
    for p in procs:
        try:
            with p.oneshot():
                rows.append({"pid": p.pid, "name": p.info["name"] or "?",
                             "cpu": p.cpu_percent(None) / ncpu, "mem": p.memory_info().rss})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: r[sort], reverse=True)
    rows = rows[:limit]
    vm = psutil.virtual_memory()
    head = (f"Top {len(rows)} by {'CPU' if sort == 'cpu' else 'memory'} · "
            f"CPU {psutil.cpu_percent(None):.0f}% · RAM {vm.percent:.0f}%\n"
            f"{'PID':>7} {'CPU%':>5} {'MEM':>9}  NAME")
    body = [f"{r['pid']:>7} {r['cpu']:>5.1f} {human_bytes(r['mem']):>9}  {r['name'][:32]}" for r in rows]
    return "\n".join([head, *body]), rows


@action("procs", "List the top processes by CPU or memory usage.", category="system",
        params={"sort": "cpu or mem", "limit": "how many rows (1-50)"})
async def procs(sort: Literal["cpu", "mem"] = "cpu", limit: int = 15) -> ActionResult:
    limit = max(1, min(int(limit), 50))
    text, rows = await run_blocking(_collect_procs, sort, limit)
    return ActionResult(text=text, data={"processes": rows})


def _self_pids() -> set[int]:
    """This bot's own process and its ancestors (python launcher, venv shim...)."""
    pids = {os.getpid()}
    try:
        for parent in psutil.Process().parents():
            pids.add(parent.pid)
    except psutil.Error:
        pass
    return pids


def _protect_reason(pid: int, name: str, self_pids: set[int]) -> str | None:
    if pid in (0, 4):
        return "core system process"
    if pid in self_pids:
        return "this bot itself"
    if name.lower() in PROTECTED_NAMES:
        return "critical Windows process"
    return None


def find_processes(target: str) -> list[dict[str, Any]]:
    """Resolve a kill target (PID or exact process name, '.exe' optional, case-insensitive).
    Returns [{pid, name, protected: reason-or-None}]. Public so modules can build confirm summaries."""
    target = target.strip()
    if not target:
        return []
    self_pids = _self_pids()
    if target.isdigit():
        pid = int(target)
        try:
            name = psutil.Process(pid).name()
        except psutil.NoSuchProcess:
            return []
        except psutil.AccessDenied:
            name = "?"
        return [{"pid": pid, "name": name, "protected": _protect_reason(pid, name, self_pids)}]
    want = target.lower()
    want_exe = want if want.endswith(".exe") else want + ".exe"
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        name = p.info["name"] or ""
        if name.lower() in (want, want_exe):
            out.append({"pid": p.info["pid"], "name": name,
                        "protected": _protect_reason(p.info["pid"], name, self_pids)})
    return out


def _kill(target: str) -> ActionResult:
    matches = find_processes(target)
    if not matches:
        return ActionResult(text=f"No running process matches {target!r}.", ok=False)
    skipped = [m for m in matches if m["protected"]]
    victims, failed = [], []
    for m in matches:
        if m["protected"]:
            continue
        try:
            p = psutil.Process(m["pid"])
            p.kill()
            victims.append(p)
        except psutil.NoSuchProcess:
            pass  # already gone - fine
        except psutil.AccessDenied:
            failed.append(f"{m['name']} ({m['pid']}): access denied (needs admin?)")
    gone, alive = psutil.wait_procs(victims, timeout=3)
    failed += [f"{p.pid}: still running" for p in alive]
    gone_pids = {p.pid for p in gone}
    killed = [m for m in matches if not m["protected"] and m["pid"] in gone_pids]

    lines = []
    if killed:
        pids = ", ".join(str(m["pid"]) for m in killed[:20]) + (" …" if len(killed) > 20 else "")
        lines.append(f"💀 Killed {len(killed)} × {killed[0]['name']} (PID {pids})")
    lines += [f"⚠️ {f}" for f in failed]
    lines += [f"🛡 Skipped {m['name']} ({m['pid']}): {m['protected']}" for m in skipped]
    return ActionResult(text="\n".join(lines) or "Nothing was killed.", ok=bool(killed) and not failed,
                        data={"killed": [m["pid"] for m in killed], "failed": failed,
                              "skipped": [m["pid"] for m in skipped]})


@action("kill_process", "Force-kill a process by PID or by exact executable name (e.g. 'chrome' or "
        "'chrome.exe' kills every instance). Critical Windows processes and this bot are refused.",
        dangerous=True, category="system", params={"target": "PID number or process name"})
async def kill_process(target: str) -> ActionResult:
    return await run_blocking(_kill, target)


def _collect_net() -> str:
    io1 = psutil.net_io_counters()
    time.sleep(1.0)
    io2 = psutil.net_io_counters()
    addrs, stats = psutil.net_if_addrs(), psutil.net_if_stats()
    pernic = psutil.net_io_counters(pernic=True)
    lines = [f"🌐 Now: ▼ {human_bytes(io2.bytes_recv - io1.bytes_recv)}/s  ▲ {human_bytes(io2.bytes_sent - io1.bytes_sent)}/s"
             f" · since boot ▼ {human_bytes(io2.bytes_recv)} ▲ {human_bytes(io2.bytes_sent)}", "", "Interfaces (up):"]
    down = []
    for nic, st in sorted(stats.items()):
        if "loopback" in nic.lower():
            continue
        if not st.isup:
            down.append(nic)
            continue
        ips = [a.address for a in addrs.get(nic, []) if a.family == socket.AF_INET]
        ips += [a.address.split("%")[0] for a in addrs.get(nic, [])
                if a.family == socket.AF_INET6 and not a.address.lower().startswith("fe80")]
        speed = f" {st.speed} Mbps" if st.speed else ""
        io = pernic.get(nic)
        tot = f" · ▼ {human_bytes(io.bytes_recv)} ▲ {human_bytes(io.bytes_sent)}" if io else ""
        lines.append(f"• {nic}{speed}: {', '.join(ips) or 'no IP'}{tot}")
    if down:
        lines.append(f"Down: {', '.join(down)}")
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError) as e:
        lines.append(f"\nConnections: unavailable ({e})")
        return "\n".join(lines)
    by_status: dict[str, int] = {}
    by_name: dict[str, int] = {}
    names: dict[int, str] = {}
    listen: set[int] = set()
    for c in conns:
        by_status[c.status] = by_status.get(c.status, 0) + 1
        if c.status == psutil.CONN_ESTABLISHED and c.pid:
            if c.pid not in names:
                try:
                    names[c.pid] = psutil.Process(c.pid).name()
                except psutil.Error:
                    names[c.pid] = f"pid {c.pid}"
            by_name[names[c.pid]] = by_name.get(names[c.pid], 0) + 1
        if c.status == psutil.CONN_LISTEN and c.laddr:
            listen.add(c.laddr.port)
    order = sorted(by_status.items(), key=lambda kv: -kv[1])
    lines.append(f"\nConnections: {len(conns)} — " + ", ".join(f"{k} {v}" for k, v in order if k != "NONE"))
    top = sorted(by_name.items(), key=lambda kv: -kv[1])[:8]
    if top:
        lines.append("Most connected: " + ", ".join(f"{n} {c}" for n, c in top))
    if listen:
        ports = sorted(listen)
        lines.append(f"Listening TCP ports ({len(ports)}): " + ", ".join(map(str, ports[:30]))
                     + (" …" if len(ports) > 30 else ""))
    return "\n".join(lines)


@action("net_info", "Network overview: current throughput, interfaces with IPs, connection counts, "
        "most-connected processes and listening ports.", category="system")
async def net_info() -> ActionResult:
    return ActionResult(text=await run_blocking(_collect_net))


def _visible_windows() -> list[dict[str, Any]]:
    """Visible, non-cloaked, non-tool top-level windows with a title (what Alt-Tab roughly shows)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    HWND = wintypes.HWND
    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsIconic.argtypes = [HWND]
    user32.GetWindow.argtypes = [HWND, wintypes.UINT]
    user32.GetWindow.restype = HWND
    user32.GetWindowLongW.argtypes = [HWND, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetForegroundWindow.restype = HWND
    dwmapi.DwmGetWindowAttribute.argtypes = [HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

    GW_OWNER, GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = 4, -20, 0x80, 14
    fg = user32.GetForegroundWindow()
    found: list[tuple[int, str, int, bool]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
                return True
            if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            cloaked = wintypes.DWORD()
            if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                            ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == "Program Manager":
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((hwnd or 0, buf.value, pid.value, bool(user32.IsIconic(hwnd))))
        except Exception:  # noqa: BLE001 - never let an exception escape a ctypes callback
            log.debug("window enum error", exc_info=True)
        return True

    user32.EnumWindows(cb, 0)
    out = []
    for hwnd, title, pid, minimized in found:
        try:
            pname = psutil.Process(pid).name()
        except psutil.Error:
            pname = "?"
        out.append({"hwnd": hwnd, "title": title, "pid": pid, "process": pname,
                    "minimized": minimized, "foreground": bool(fg) and hwnd == fg})
    return out


@action("list_windows", "List visible application windows (title, process, PID; ★ = focused, "
        "minimized marked).", category="system")
async def list_windows() -> ActionResult:
    wins = await run_blocking(_visible_windows)
    if not wins:
        return ActionResult(text="No visible application windows.")
    lines = [f"🪟 {len(wins)} open windows:"]
    for w in wins:
        mark = "★ " if w["foreground"] else "• "
        lines.append(f"{mark}{w['title'][:70]} — {w['process']} ({w['pid']})"
                     + (" [minimized]" if w["minimized"] else ""))
    return ActionResult(text="\n".join(lines), data={"windows": wins})
