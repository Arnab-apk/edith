"""Devices + power: default audio output, power plans / power mode, Bluetooth status.

OS access sits in small helpers (_run, _ps, _list_outputs, _set_default_output, _get_overlay,
_set_overlay) so tests can monkeypatch them. Nothing here is dangerous.
"""
from __future__ import annotations

import ctypes
import difflib
import json
import re
import subprocess
import uuid
from typing import Literal

from ..utils import run_blocking
from .registry import ActionResult, action
from .shell import decode_output

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIMEOUT = 15


def _run(args: list[str], timeout: float = TIMEOUT) -> tuple[int, str]:
    """Run a program (argument list, never a shell). Returns (returncode, decoded stdout+stderr)."""
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


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# ================================================================== audio output
_OUTPUT_ALIASES = {  # spoken word -> name fragments to look for
    "headphones": ("headphone", "headset", "earphone", "buds", "airpods", "rockerz", "hands-free"),
    "headset": ("headset", "headphone", "hands-free", "buds"),
    "earbuds": ("buds", "airpods", "earphone", "headphone"),
    "tv": ("tv", "hdmi", "display audio", "nvidia high definition", "intel(r) display", "amd high definition"),
    "monitor": ("display audio", "hdmi", "displayport", "nvidia high definition"),
    "speakers": ("speaker",),
    "laptop": ("realtek", "speaker"),
}


def _list_outputs() -> tuple[list[tuple[str, str]], str | None]:
    """[(device id, friendly name)] of active playback devices, and the default (console) device id."""
    import comtypes
    from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
    from pycaw.utils import AudioUtilities

    comtypes.CoInitialize()
    try:
        devs = AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
        out = [(d.id, d.FriendlyName or d.id) for d in devs]
        try:
            default = AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(
                EDataFlow.eRender.value, ERole.eConsole.value).GetId()
        except Exception:  # noqa: BLE001 - no default device at all
            default = None
        del devs
        return out, default
    finally:
        comtypes.CoUninitialize()


def _set_default_output(dev_id: str) -> None:
    """Make dev_id the default playback device for every role (IPolicyConfig via pycaw)."""
    import comtypes
    from pycaw.constants import ERole
    from pycaw.utils import AudioUtilities

    comtypes.CoInitialize()
    try:
        AudioUtilities.SetDefaultDevice(dev_id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
    finally:
        comtypes.CoUninitialize()


def _pick_output(query: str, devices: list[tuple[str, str]]) -> tuple[tuple[str, str] | None, str]:
    q = _norm(query)
    names = [(d, _norm(d[1])) for d in devices]
    exact = [d for d, n in names if n == q]
    if exact:
        return exact[0], ""
    hits = [d for d, n in names if q in n]
    if not hits:
        words = q.split()
        hits = [d for d, n in names if words and all(w in n for w in words)]
    if not hits and q in _OUTPUT_ALIASES:
        hits = [d for d, n in names if any(_norm(f) in n for f in _OUTPUT_ALIASES[q])]
    if not hits and q:  # typo tolerance: best fuzzy score against the full name or any of its words
        def score(n: str) -> float:
            return max(difflib.SequenceMatcher(None, q, w).ratio() for w in [n, *n.split()])
        scored = sorted(((score(n), d) for d, n in names), key=lambda x: -x[0])
        if scored[0][0] >= 0.75 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            hits = [scored[0][1]]
    if len(hits) == 1:
        return hits[0], ""
    if hits:
        return None, f"{query!r} matches several outputs: " + "; ".join(h[1] for h in hits) + ". Say which one."
    return None, f"No playback device matches {query!r}."


def _devices_text(devices: list[tuple[str, str]], default: str | None) -> str:
    lines = [f"🔊 {len(devices)} playback devices (● default):"]
    lines += [f"{'●' if dev_id == default else '·'} {name}" for dev_id, name in devices]
    return "\n".join(lines)


def _audio_output(device: str) -> ActionResult:
    try:
        devices, default = _list_outputs()
    except Exception as e:  # noqa: BLE001 - COM failures
        return ActionResult(text=f"Could not read audio devices: {e}", ok=False)
    if not devices:
        return ActionResult(text="No active playback devices.", ok=False)
    data = {"devices": [n for _, n in devices], "default": next((n for i, n in devices if i == default), None)}
    if not device.strip():
        return ActionResult(text=_devices_text(devices, default), data=data)
    pick, err = _pick_output(device, devices)
    if pick is None:
        return ActionResult(text=err + "\n" + _devices_text(devices, default), ok=False, data=data)
    if pick[0] == default:
        return ActionResult(text=f"🔊 {pick[1]} is already the default output.", data={**data, "default": pick[1]})
    try:
        _set_default_output(pick[0])
    except Exception as e:  # noqa: BLE001
        return ActionResult(text=f"Could not switch to {pick[1]}: {e}", ok=False, data=data)
    return ActionResult(text=f"🔊 Sound now plays on {pick[1]}.", data={**data, "default": pick[1]})


@action("audio_output", "List playback devices (empty device) or switch the default sound output to one "
        "by name, e.g. 'headphones', 'speakers', 'TV'.", category="settings",
        params={"device": "output name (fuzzy); empty = list devices"})
async def audio_output(device: str = "") -> ActionResult:
    return await run_blocking(_audio_output, device)


# ================================================================== power plans / power mode
_PLAN_GUIDS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "power_saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
    "high_performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "ultimate": "e9a42b02-d5df-448d-aa00-03f14749eb61",
}
# Windows 10/11 power-mode slider (overlay on the Balanced plan; Modern Standby laptops often have only Balanced)
_MODES = {
    "efficiency": ("961cc777-2547-4f9d-8174-7d86181b8a7a", "Best power efficiency"),
    "balanced": ("00000000-0000-0000-0000-000000000000", "Balanced"),
    "performance": ("ded574b5-45a0-4f42-8737-46345c09c238", "Best performance"),
}
_ALIASES: dict[str, tuple[tuple[str, ...], str | None]] = {}  # alias -> (plan keys to try, power mode)
for _words, _plans, _mode in [
    (("balanced", "normal", "default"), ("balanced",), "balanced"),
    (("power saver", "saver", "battery saver", "power saving", "save power", "eco", "battery", "efficiency",
      "best power efficiency", "best efficiency", "low power"), ("power_saver",), "efficiency"),
    (("high performance", "high", "performance", "best performance", "max performance", "maximum performance",
      "gaming", "turbo", "fast"), ("high_performance", "ultimate"), "performance"),
    (("ultimate", "ultimate performance"), ("ultimate", "high_performance"), "performance"),
]:
    for _w in _words:
        _ALIASES[_w] = (_plans, _mode)

_PLAN_RX = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s+\((.*?)\)\s*(\*)?\s*$",
                      re.I | re.M)


class _GUID(ctypes.Structure):
    _fields_ = [("b", ctypes.c_ubyte * 16)]


def _get_overlay() -> str | None:
    """Current power-mode overlay GUID, or None where unsupported."""
    try:
        g = _GUID()
        if ctypes.WinDLL("powrprof").PowerGetEffectiveOverlayScheme(ctypes.byref(g)) != 0:
            return None
        return str(uuid.UUID(bytes_le=bytes(g.b)))
    except (OSError, AttributeError):
        return None


def _set_overlay(guid: str) -> bool:
    try:
        g = _GUID()
        ctypes.memmove(g.b, uuid.UUID(guid).bytes_le, 16)
        return ctypes.WinDLL("powrprof").PowerSetActiveOverlayScheme(ctypes.byref(g)) == 0
    except (OSError, AttributeError, ValueError):
        return False


def _plans() -> tuple[list[dict], str]:
    rc, out = _run(["powercfg", "/list"])
    if rc != 0:
        return [], out
    return [{"guid": g.lower(), "name": n.strip(), "active": bool(star)} for g, n, star in _PLAN_RX.findall(out)], out


def _mode_name(guid: str | None) -> str | None:
    return next((name for g, name in _MODES.values() if g == guid), None)


def _plans_text(plans: list[dict], overlay: str | None) -> str:
    lines = ["⚡ Power plans (● active):"] + [f"{'●' if p['active'] else '·'} {p['name']}" for p in plans]
    active = next((p for p in plans if p["active"]), None)
    mode = _mode_name(overlay)
    if mode and active and active["guid"] == _PLAN_GUIDS["balanced"]:
        lines.append(f"Power mode: {mode}")
    return "\n".join(lines)


def _setactive(plan: dict) -> ActionResult | None:
    rc, out = _run(["powercfg", "/setactive", plan["guid"]])
    if rc != 0:
        return ActionResult(text=f"Could not switch to {plan['name']}: {out[:200] or f'exit {rc}'}", ok=False)
    return None


def _power_plan(plan: str) -> ActionResult:
    plans, raw = _plans()
    if not plans:
        return ActionResult(text=f"Could not read power plans: {raw[:200] or 'no output'}", ok=False)
    overlay = _get_overlay()
    data = {"plans": [p["name"] for p in plans], "active": next((p["name"] for p in plans if p["active"]), None),
            "mode": _mode_name(overlay)}
    if not plan.strip():
        return ActionResult(text=_plans_text(plans, overlay), data=data)

    q = _norm(plan)
    by_guid = {p["guid"]: p for p in plans}
    target, mode = by_guid.get(plan.strip().lower()), None
    if target is None and q in _ALIASES:  # aliases first: "balanced" must also reset the power mode
        keys, mode = _ALIASES[q]
        target = next((by_guid[_PLAN_GUIDS[k]] for k in keys if _PLAN_GUIDS[k] in by_guid), None)
        if target is not None and target["guid"] != _PLAN_GUIDS["balanced"]:
            mode = None  # a real plan exists for this alias: just switch to it
        elif mode is not None:
            target = by_guid.get(_PLAN_GUIDS["balanced"])  # power modes only work on top of Balanced
    if target is None and mode is None:
        target = next((p for p in plans if _norm(p["name"]) == q), None)
        hits = [p for p in plans if q and q in _norm(p["name"])]
        if target is None and len(hits) == 1:
            target = hits[0]
    if target is None:
        return ActionResult(text=f"No power plan matches {plan!r}.\n" + _plans_text(plans, overlay), ok=False,
                            data=data)

    switched = not target["active"]
    if switched:
        err = _setactive(target)
        if err:
            return err
    data = {**data, "active": target["name"]}
    if mode is None or overlay is None and mode == "balanced":
        return ActionResult(text=f"⚡ Power plan: {target['name']}" + ("." if switched else " (already active)."),
                            data=data)
    guid, mode_name = _MODES[mode]
    if overlay is None:
        return ActionResult(text=f"⚡ Power plan: {target['name']}. This PC has no power-mode slider, so "
                                 f"{mode_name!r} isn't available.", ok=False, data=data)
    if overlay == guid:
        already = "" if switched else " (already set)"
        return ActionResult(text=f"⚡ Power mode: {mode_name}{already}.", data={**data, "mode": mode_name})
    if not _set_overlay(guid):
        return ActionResult(text=f"Could not set power mode {mode_name}.", ok=False, data=data)
    return ActionResult(text=f"⚡ Power mode: {mode_name} (plan {target['name']}).", data={**data, "mode": mode_name})


@action("energy_plan", "List power plans (empty plan) or switch: a plan name, or 'balanced', 'power saver', "
        "'high performance' / 'best performance'. Uses the Windows power-mode slider when the plan doesn't exist.",
        category="settings", params={"plan": "plan name or alias; empty = list plans"})
async def energy_plan(plan: str = "") -> ActionResult:
    return await run_blocking(_power_plan, plan)


# ================================================================== bluetooth (status only)
_BT_SCRIPT = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
              "Select-Object FriendlyName, Status, Present, InstanceId | ConvertTo-Json -Compress")
_SERVICE_RX = re.compile(r"\b(?:avrcp|transport|enumerator|rfcomm|identification service|protocol)\b", re.I)


def _bluetooth_status() -> ActionResult:
    rc, out = _ps(_BT_SCRIPT)
    if rc != 0:
        return ActionResult(text=f"Bluetooth status unavailable: {out[:200] or f'exit {rc}'}", ok=False)
    try:
        items = json.loads(out) if out.strip() else []
    except ValueError:
        return ActionResult(text="Could not read Bluetooth devices.", ok=False)
    if isinstance(items, dict):
        items = [items]
    adapters, devices = [], {}
    for it in items:
        iid = str(it.get("InstanceId") or "").upper()
        name = str(it.get("FriendlyName") or "").strip()
        status = str(it.get("Status") or "")
        present = bool(it.get("Present"))
        if not name:
            continue
        if iid.startswith(("BTHENUM\\DEV_", "BTHLE\\DEV_")):
            on = present and status.upper() == "OK"
            devices[name] = devices.get(name, False) or on
        elif not iid.startswith(("BTH\\", "BTHENUM\\", "BTHLE\\", "BTHLEDEVICE\\")) and not _SERVICE_RX.search(name):
            if present:
                adapters.append((name, status))
    if not adapters:
        return ActionResult(text="🔵 No Bluetooth adapter found (none built in, or it's disabled in Device Manager).",
                            data={"adapter": None, "working": False, "devices": {}})
    name, status = adapters[0]
    working = status.upper() == "OK"
    lines = [f"🔵 Bluetooth adapter: {name} — " + ("working" if working else f"problem ({status or 'unknown'})")]
    if devices:
        lines.append("Paired devices (● connected):")
        lines += [f"{'●' if on else '·'} {n}" for n, on in sorted(devices.items(), key=lambda kv: (not kv[1], kv[0]))]
    else:
        lines.append("No paired devices.")
    return ActionResult(text="\n".join(lines), data={"adapter": name, "working": working, "devices": devices})


@action("bluetooth", "Bluetooth status: adapter state and paired devices (which are connected). Read-only.",
        category="settings", params={"action": "status"})
async def bluetooth(action: Literal["status"] = "status") -> ActionResult:
    return await run_blocking(_bluetooth_status)
