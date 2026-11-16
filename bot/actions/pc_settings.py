"""PC settings: display brightness, dark mode, Wi-Fi.

OS access sits in small helpers (_run, _ps, _reg_get, _reg_set, _broadcast_theme, _sleep) so tests
can monkeypatch them. Only `wifi` is dangerous: disconnecting Wi-Fi can cut Edith off the internet.
policy.SAFE_MODES can let wifi status/list/connect skip Confirm; disconnect always confirms.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Literal, Optional

from ..utils import run_blocking
from .registry import ActionResult, action
from .shell import decode_output

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIMEOUT = 15
CONNECT_WAIT = 10  # seconds to wait for a Wi-Fi connect to finish
SAFE_WIFI_ACTIONS = frozenset({"status", "list", "connect"})  # for policy.SAFE_MODES (lead wires it)


# ---------------------------------------------------------------- low-level helpers (monkeypatchable)
def _run(args: list[str] | str, timeout: float = TIMEOUT) -> tuple[int, str]:
    """Run a program (never through a shell). Returns (returncode, decoded stdout+stderr)."""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return 127, "program not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g}s"
    except OSError as e:
        return 1, str(e)
    return p.returncode, (decode_output(p.stdout) + "\n" + decode_output(p.stderr)).strip()


def _ps(script: str) -> tuple[int, str]:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------- brightness
_GET_BRIGHTNESS = ("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop "
                   "| Select-Object -First 1).CurrentBrightness")
_SET_BRIGHTNESS = ("$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop "
                   "| Select-Object -First 1; Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
                   "-Arguments @{{Timeout=[uint32]1; Brightness=[byte]{level}}} -ErrorAction Stop | Out-Null")
_NO_BRIGHTNESS = ("This display doesn't support software brightness control (usually an external desktop "
                  "monitor - use its own buttons).")


def _get_brightness() -> int | None:
    rc, out = _ps(_GET_BRIGHTNESS)
    m = re.search(r"\b(\d{1,3})\b", out) if rc == 0 else None
    return int(m.group(1)) if m and int(m.group(1)) <= 100 else None


def _brightness(level: Optional[int], change: Optional[int]) -> ActionResult:
    current = _get_brightness()
    if current is None:
        return ActionResult(text=_NO_BRIGHTNESS, ok=False)
    if level is None and change is None:
        return ActionResult(text=f"🔆 Brightness is {current}%.", data={"level": current})
    target = max(0, min(100, int(level) if level is not None else current + int(change)))
    rc, out = _ps(_SET_BRIGHTNESS.format(level=target))  # target is a clamped int: safe to format in
    if rc != 0:
        return ActionResult(text=f"Could not set brightness: {out.splitlines()[0] if out else f'exit {rc}'}",
                            ok=False)
    return ActionResult(text=f"🔆 Brightness {current}% → {target}%.", data={"level": target, "previous": current})


@action("brightness", "Get or set the built-in screen brightness (laptops). Give `level` (0-100) to set it, "
        "or `change` (e.g. 10 / -10) to adjust it; neither just reports it.", category="settings",
        params={"level": "absolute brightness 0-100", "change": "relative change in percent points"})
async def brightness(level: Optional[int] = None, change: Optional[int] = None) -> ActionResult:
    if level is not None and change is not None:
        return ActionResult(text="Give either level or change, not both.", ok=False)
    return await run_blocking(_brightness, level, change)


# ---------------------------------------------------------------- dark mode
_THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_THEME_VALUES = ("AppsUseLightTheme", "SystemUsesLightTheme")


def _reg_get(name: str) -> int | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_KEY) as k:
            return int(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return None


def _reg_set(name: str, value: int) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _THEME_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)


def _broadcast_theme() -> None:
    """Tell running apps / Explorer the colour scheme changed (WM_SETTINGCHANGE 'ImmersiveColorSet')."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    result = ctypes.c_size_t()
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
    user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet", SMTO_ABORTIFHUNG, 1000,
                               ctypes.byref(result))


def _dark_mode(mode: str) -> ActionResult:
    apps_light = _reg_get("AppsUseLightTheme")
    is_dark = apps_light == 0  # missing value = Windows default (light)
    if mode == "status":
        return ActionResult(text=f"🌓 Dark mode is {'on' if is_dark else 'off'}.", data={"dark": is_dark})
    want_dark = (not is_dark) if mode == "toggle" else (mode == "on")
    if want_dark == is_dark and _reg_get("SystemUsesLightTheme") == apps_light:
        return ActionResult(text=f"🌓 Dark mode is already {'on' if is_dark else 'off'}.", data={"dark": is_dark})
    try:
        for name in _THEME_VALUES:
            _reg_set(name, 0 if want_dark else 1)
    except OSError as e:
        return ActionResult(text=f"Could not change the theme: {e}", ok=False)
    try:
        _broadcast_theme()
    except Exception:  # noqa: BLE001 - the registry change still applies; apps just refresh later
        pass
    return ActionResult(text=f"🌓 Dark mode {'on' if want_dark else 'off'}.", data={"dark": want_dark})


@action("dark_mode", "Turn Windows dark mode on, off, toggle it, or report it (apps + taskbar).",
        category="settings", params={"mode": "on | off | toggle | status"})
async def dark_mode(mode: Literal["on", "off", "toggle", "status"] = "status") -> ActionResult:
    return await run_blocking(_dark_mode, mode)


# ---------------------------------------------------------------- wifi
_LOCATION_HINT = ("Windows blocked the Wi-Fi scan: turn on Location (Settings > Privacy & security > Location, "
                  "and 'Let desktop apps access your location').")


def _netsh(*args: str) -> tuple[int, str]:
    return _run(["netsh", "wlan", *args])


def _fields(block: str) -> dict[str, str]:
    out = {}
    for line in block.splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() and k.strip().lower() not in out:
            out[k.strip().lower()] = v.strip()
    return out


def _interfaces(text: str) -> list[dict[str, str]]:
    blocks = re.split(r"\n(?=\s*Name\s+:)", "\n" + text)
    return [f for f in (_fields(b) for b in blocks) if "name" in f]


def _profiles() -> list[str] | None:
    rc, out = _netsh("show", "profiles")
    if rc != 0:
        return None
    return [m.group(1).strip() for m in re.finditer(r"^\s*[^:\n]*Profile\s*:\s*(.+?)\s*$", out, re.M)]


def _status_text(ifaces: list[dict[str, str]]) -> str:
    lines = []
    for f in ifaces:
        state = f.get("state", "?")
        if state.lower() == "connected" and f.get("ssid"):
            bits = [f"📶 {f['name']}: connected to {f['ssid']}"]
            bits += [f"signal {f['signal']}"] if f.get("signal") else []
            bits += [f.get("band", "")] if f.get("band") else []
            rate = f.get("receive rate (mbps)")
            bits += [f"{rate} Mbps"] if rate else []
            lines.append(" · ".join(bits))
        else:
            lines.append(f"📶 {f['name']}: {state}")
    return "\n".join(lines)


def _wifi_status() -> ActionResult:
    rc, out = _netsh("show", "interfaces")
    if rc != 0:
        return ActionResult(text=f"Wi-Fi status unavailable: {out[:200] or f'exit {rc}'}", ok=False)
    ifaces = _interfaces(out)
    if not ifaces:
        return ActionResult(text="No Wi-Fi adapter found." if "no wireless" in out.lower() or not out.strip()
                            else out[:800])
    connected = [f for f in ifaces if f.get("state", "").lower() == "connected"]
    return ActionResult(text=_status_text(ifaces),
                        data={"connected": bool(connected), "ssid": connected[0].get("ssid") if connected else None})


def _wifi_list() -> ActionResult:
    rc, out = _netsh("show", "networks", "mode=bssid")
    if "location" in out.lower() and "permission" in out.lower():
        return ActionResult(text=_LOCATION_HINT, ok=False)
    if rc != 0:
        return ActionResult(text=f"Wi-Fi scan failed: {out[:200] or f'exit {rc}'}", ok=False)
    nets: dict[str, dict] = {}
    for block in re.split(r"\n(?=SSID \d+\s*:)", out):
        m = re.match(r"SSID \d+\s*:\s*(.*)", block.strip())
        if not m:
            continue
        ssid = m.group(1).strip() or "(hidden)"
        sig = max((int(s) for s in re.findall(r"Signal\s*:\s*(\d+)%", block)), default=0)
        f = _fields(block)
        prev = nets.get(ssid)
        if not prev or sig > prev["signal"]:
            nets[ssid] = {"ssid": ssid, "signal": sig, "auth": f.get("authentication", "")}
    saved = {p.lower() for p in (_profiles() or [])}
    _rc, cur_out = _netsh("show", "interfaces")
    current = {f.get("ssid", "").lower() for f in _interfaces(cur_out) if f.get("state", "").lower() == "connected"}
    rows = sorted(nets.values(), key=lambda n: -n["signal"])[:15]
    if not rows:
        return ActionResult(text="No Wi-Fi networks visible.", data={"networks": []})
    lines = [f"📡 {len(nets)} Wi-Fi networks (● connected, ★ saved):"]
    for n in rows:
        mark = "●" if n["ssid"].lower() in current else ("★" if n["ssid"].lower() in saved else "·")
        lock = "" if n["auth"].lower() == "open" else " 🔒"
        lines.append(f"{mark} {n['ssid']} — {n['signal']}%{lock}")
    for n in nets.values():
        n["saved"] = n["ssid"].lower() in saved
    return ActionResult(text="\n".join(lines), data={"networks": rows})


def _pick_profile(network: str, profiles: list[str]) -> tuple[str | None, str]:
    want = network.strip().lower()
    exact = [p for p in profiles if p.lower() == want]
    if exact:
        return exact[0], ""
    partial = [p for p in profiles if want in p.lower()]
    if len(partial) == 1:
        return partial[0], ""
    if partial:
        return None, f"{network!r} matches several saved networks: {', '.join(partial[:8])}. Be more specific."
    return None, (f"{network!r} is not a saved Wi-Fi network. Saved: {', '.join(profiles[:12]) or 'none'}. "
                  "Connect to it once on the PC first (Edith never types Wi-Fi passwords).")


def _wifi_connect(network: str) -> ActionResult:
    if not network.strip():
        return ActionResult(text="Which network? Give its name (use action 'list' to see them).", ok=False)
    profiles = _profiles()
    if profiles is None:
        return ActionResult(text="Could not read saved Wi-Fi profiles.", ok=False)
    profile, err = _pick_profile(network, profiles)
    if profile is None:
        return ActionResult(text=err, ok=False)
    if '"' in profile:
        return ActionResult(text="That profile name contains a quote character; connect to it manually.", ok=False)
    _rc, cur = _netsh("show", "interfaces")
    if any(f.get("profile", "").lower() == profile.lower() and f.get("state", "").lower() == "connected"
           for f in _interfaces(cur)):
        return ActionResult(text=f"📶 Already connected to {profile}.", data={"ssid": profile})
    # Hand-built command line: netsh needs name="..." quoted; the profile comes from netsh's own list.
    rc, out = _run(f'netsh wlan connect name="{profile}"')
    if rc != 0:
        return ActionResult(text=f"Connect to {profile} failed: {out[:200] or f'exit {rc}'}", ok=False)
    for _ in range(CONNECT_WAIT):
        _sleep(1)
        _rc, cur = _netsh("show", "interfaces")
        for f in _interfaces(cur):
            if f.get("state", "").lower() == "connected" and f.get("profile", "").lower() == profile.lower():
                return ActionResult(text=f"📶 Connected to {f.get('ssid') or profile}"
                                         + (f" · signal {f['signal']}" if f.get("signal") else "") + ".",
                                    data={"ssid": f.get("ssid") or profile})
    return ActionResult(text=f"Asked Windows to connect to {profile}, but it isn't connected after "
                             f"{CONNECT_WAIT} s (out of range or wrong password?).", ok=False)


def _wifi_disconnect() -> ActionResult:
    rc, out = _netsh("disconnect")
    if rc != 0:
        return ActionResult(text=f"Disconnect failed: {out[:200] or f'exit {rc}'}", ok=False)
    return ActionResult(text="📴 Wi-Fi disconnected. If that was the PC's only connection, Edith stays offline "
                             "until it reconnects.")


@action("wifi", "Wi-Fi: 'status' (current network + signal), 'list' (visible networks), 'connect' to a SAVED "
        "network by name, or 'disconnect'. Disconnect can cut Edith off the internet if Wi-Fi is the only "
        "connection, so only do it when the owner clearly asks.", dangerous=True, category="settings",
        params={"action": "status | list | connect | disconnect",
                "network": "saved network name (connect only)"})
async def wifi(action: Literal["status", "list", "connect", "disconnect"] = "status", network: str = "") -> ActionResult:
    if action == "status":
        return await run_blocking(_wifi_status)
    if action == "list":
        return await run_blocking(_wifi_list)
    if action == "connect":
        return await run_blocking(_wifi_connect, network)
    return await run_blocking(_wifi_disconnect)
