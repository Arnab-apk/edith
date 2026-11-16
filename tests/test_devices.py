"""Tests for bot/actions/devices.py. No real OS changes: COM, powercfg, powrprof and PowerShell are faked."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from bot.actions import devices as d
from bot.actions.registry import REGISTRY, run_action

OUTPUTS = [
    ("{id-fx}", "FxSound Speakers (FxSound Audio Enhancer)"),
    ("{id-buds}", "Headphones (OnePlus Nord Buds 3 Pro)"),
    ("{id-rtk}", "Speakers (Realtek(R) Audio)"),
    ("{id-steam}", "Speakers (Steam Streaming Speakers)"),
    ("{id-tv}", "LG TV (NVIDIA High Definition Audio)"),
]
BAL, SAVER, HIGH = (d._PLAN_GUIDS[k] for k in ("balanced", "power_saver", "high_performance"))
PLANS_ONLY_BALANCED = f"""Existing Power Schemes (* Active)
-----------------------------------
Power Scheme GUID: {BAL}  (Balanced) *
"""
PLANS_ALL = f"""Existing Power Schemes (* Active)
-----------------------------------
Power Scheme GUID: {BAL}  (Balanced) *
Power Scheme GUID: {HIGH}  (High performance)
Power Scheme GUID: {SAVER}  (Power saver)
Power Scheme GUID: 11111111-2222-3333-4444-555555555555  (My Gaming Plan)
"""


@pytest.fixture(autouse=True)
def fake_os(monkeypatch):
    """Every OS hook is a recorder; tests adjust `rec`."""
    def boom(*a, **k):
        raise AssertionError(f"real subprocess call: {a}")
    monkeypatch.setattr(subprocess, "run", boom)
    rec = SimpleNamespace(outputs=list(OUTPUTS), default="{id-rtk}", set_calls=[], set_error=None,
                          plans=PLANS_ONLY_BALANCED, run_calls=[], setactive_rc=0,
                          overlay=d._MODES["balanced"][0], overlay_calls=[], overlay_ok=True, ps_rc=0, ps_out="[]")

    def set_default(dev_id):
        if rec.set_error:
            raise rec.set_error
        rec.set_calls.append(dev_id)

    def run(args, timeout=d.TIMEOUT):
        rec.run_calls.append(args)
        if args[:2] == ["powercfg", "/list"]:
            return 0, rec.plans
        if args[:2] == ["powercfg", "/setactive"]:
            return rec.setactive_rc, "" if rec.setactive_rc == 0 else "Invalid Parameters"
        raise AssertionError(f"unexpected command {args}")

    def set_overlay(guid):
        rec.overlay_calls.append(guid)
        return rec.overlay_ok

    monkeypatch.setattr(d, "_list_outputs", lambda: (list(rec.outputs), rec.default))
    monkeypatch.setattr(d, "_set_default_output", set_default)
    monkeypatch.setattr(d, "_run", run)
    monkeypatch.setattr(d, "_get_overlay", lambda: rec.overlay)
    monkeypatch.setattr(d, "_set_overlay", set_overlay)
    monkeypatch.setattr(d, "_ps", lambda script: (rec.ps_rc, rec.ps_out))
    return rec


def test_registered_actions():
    for name in ("audio_output", "energy_plan", "bluetooth"):
        assert REGISTRY[name].dangerous is False and REGISTRY[name].category == "settings"
    assert REGISTRY["bluetooth"].input_schema["properties"]["action"]["enum"] == ["status"]


# ---------------------------------------------------------------- audio output
async def test_audio_list_marks_default(fake_os):
    res = await run_action("audio_output")
    assert res.ok and res.data["default"] == "Speakers (Realtek(R) Audio)"
    assert "● Speakers (Realtek(R) Audio)" in res.text and "· LG TV" in res.text
    assert fake_os.set_calls == []


@pytest.mark.parametrize("query, dev_id", [
    ("Speakers (Steam Streaming Speakers)", "{id-steam}"),  # exact
    ("steam", "{id-steam}"), ("REALTEK", "{id-rtk}"),     # substring, any case
    ("nord buds", "{id-buds}"), ("headphones", "{id-buds}"), ("earbuds", "{id-buds}"),
    ("tv", "{id-tv}"), ("lg tv", "{id-tv}"),
    ("fxsound", "{id-fx}"), ("realtec", "{id-rtk}"),       # typo -> fuzzy
])
async def test_audio_switch(fake_os, query, dev_id):
    fake_os.default = None
    res = await run_action("audio_output", device=query)
    assert res.ok, res.text
    assert fake_os.set_calls == [dev_id] and "now plays on" in res.text


async def test_audio_ambiguous_unknown_and_already(fake_os):
    res = await run_action("audio_output", device="speakers")
    assert not res.ok and "several" in res.text and fake_os.set_calls == []
    res = await run_action("audio_output", device="bathroom radio")
    assert not res.ok and "No playback device" in res.text and "● Speakers (Realtek(R) Audio)" in res.text
    res = await run_action("audio_output", device="realtek")
    assert res.ok and "already" in res.text and fake_os.set_calls == []


async def test_audio_errors(fake_os, monkeypatch):
    fake_os.set_error = OSError("SetDefaultEndpoint failed")
    res = await run_action("audio_output", device="steam")
    assert not res.ok and "SetDefaultEndpoint failed" in res.text
    fake_os.outputs = []
    assert not (await run_action("audio_output")).ok

    def com_dead():
        raise OSError("CoInitialize failed")
    monkeypatch.setattr(d, "_list_outputs", com_dead)
    res = await run_action("audio_output")
    assert not res.ok and "CoInitialize" in res.text


# ---------------------------------------------------------------- power plans
async def test_power_plan_list(fake_os):
    fake_os.plans = PLANS_ALL
    res = await run_action("energy_plan")
    assert res.ok and res.data["active"] == "Balanced" and len(res.data["plans"]) == 4
    assert "● Balanced" in res.text and "· My Gaming Plan" in res.text and "Power mode: Balanced" in res.text
    assert not any(c[1] == "/setactive" for c in fake_os.run_calls)


@pytest.mark.parametrize("query, guid", [
    ("high performance", HIGH), ("best performance", HIGH), ("power saver", SAVER),
    ("Battery Saver", SAVER), ("My Gaming Plan", "11111111-2222-3333-4444-555555555555"),
    ("my gaming", "11111111-2222-3333-4444-555555555555"),
    (SAVER.upper(), SAVER),
])
async def test_power_plan_switch_real_plan(fake_os, query, guid):
    fake_os.plans = PLANS_ALL
    res = await run_action("energy_plan", plan=query)
    assert res.ok, res.text
    assert ["powercfg", "/setactive", guid] in fake_os.run_calls
    assert fake_os.overlay_calls == []  # a real plan exists: no power-mode change


async def test_power_plan_alias_beats_fuzzy_name(fake_os):
    """'gaming' is an alias for high performance even though a plan called 'My Gaming Plan' exists."""
    fake_os.plans = PLANS_ALL
    res = await run_action("energy_plan", plan="gaming")
    assert ["powercfg", "/setactive", HIGH] in fake_os.run_calls and res.data["active"] == "High performance"


@pytest.mark.parametrize("query, mode", [("best performance", "performance"), ("high performance", "performance"),
                                         ("power saver", "efficiency"), ("balanced", "balanced")])
async def test_power_mode_fallback_when_only_balanced(fake_os, query, mode):
    fake_os.overlay = d._MODES["performance"][0] if mode == "balanced" else d._MODES["balanced"][0]
    res = await run_action("energy_plan", plan=query)
    assert res.ok, res.text
    assert fake_os.overlay_calls == [d._MODES[mode][0]]
    assert res.data["mode"] == d._MODES[mode][1] and "Power mode" in res.text
    assert not any(c[1] == "/setactive" for c in fake_os.run_calls)  # Balanced already active


async def test_power_mode_switches_to_balanced_first(fake_os):
    fake_os.plans = PLANS_ALL.replace("(Balanced) *", "(Balanced)").replace("(Power saver)", "(Power saver) *")
    fake_os.plans = fake_os.plans.replace(f"{HIGH}  (High performance)\n", "")  # no high-perf plan
    res = await run_action("energy_plan", plan="best performance")
    assert res.ok
    assert ["powercfg", "/setactive", BAL] in fake_os.run_calls
    assert fake_os.overlay_calls == [d._MODES["performance"][0]]


async def test_power_plan_custom_name_without_alias(fake_os):
    fake_os.plans = PLANS_ALL
    res = await run_action("energy_plan", plan="My Gaming Plan")
    assert res.ok and res.data["active"] == "My Gaming Plan" and fake_os.overlay_calls == []


async def test_power_plan_failures(fake_os):
    res = await run_action("energy_plan", plan="warp speed")
    assert not res.ok and "No power plan matches" in res.text and "● Balanced" in res.text
    fake_os.overlay_ok = False
    res = await run_action("energy_plan", plan="best performance")
    assert not res.ok and "Could not set power mode" in res.text
    fake_os.overlay = None  # PC without the power-mode slider
    res = await run_action("energy_plan", plan="best performance")
    assert not res.ok and "no power-mode slider" in res.text
    fake_os.plans, fake_os.overlay = PLANS_ALL, d._MODES["balanced"][0]
    fake_os.setactive_rc = 1
    res = await run_action("energy_plan", plan="power saver")
    assert not res.ok and "Could not switch" in res.text


async def test_power_plan_already_active(fake_os):
    fake_os.plans = PLANS_ALL
    res = await run_action("energy_plan", plan="Balanced")
    assert res.ok and "already" in res.text and fake_os.overlay_calls == []
    assert not any(c[1] == "/setactive" for c in fake_os.run_calls)


def test_overlay_guid_roundtrip():
    g = d._GUID()
    import ctypes
    import uuid
    ctypes.memmove(g.b, uuid.UUID(d._MODES["performance"][0]).bytes_le, 16)
    assert str(uuid.UUID(bytes_le=bytes(g.b))) == d._MODES["performance"][0]


# ---------------------------------------------------------------- bluetooth
BT = [
    {"FriendlyName": "MediaTek Bluetooth Adapter", "Status": "OK", "Present": True,
     "InstanceId": "USB\\VID_0489&PID_E0CD&MI_00\\7&32DC556E&1&0000"},
    {"FriendlyName": "Microsoft Bluetooth Enumerator", "Status": "OK", "Present": True,
     "InstanceId": "BTH\\MS_BTHBRB\\8&1714E8C3&0&1"},
    {"FriendlyName": "OnePlus Nord Buds 3 Pro", "Status": "OK", "Present": True,
     "InstanceId": "BTHENUM\\DEV_9CDEF054BA58\\9&837CA2E&0&BLUETOOTHDEVICE_9CDEF054BA58"},
    {"FriendlyName": "OnePlus Nord Buds 3 Pro Avrcp Transport", "Status": "OK", "Present": True,
     "InstanceId": "BTHENUM\\{0000110E-0000-1000-8000-00805F9B34FB}_VID&000102B0\\9&837CA2E&0&9CDEF054BA58_C00000000"},
    {"FriendlyName": "boAt Rockerz 518", "Status": "Unknown", "Present": False,
     "InstanceId": "BTHENUM\\DEV_333355334C49\\9&837CA2E&0&BLUETOOTHDEVICE_333355334C49"},
    {"FriendlyName": "Mi Band 7", "Status": "Unknown", "Present": False, "InstanceId": "BTHLE\\DEV_C0FFEE123456\\1"},
]


async def test_bluetooth_status(fake_os):
    fake_os.ps_out = json.dumps(BT)
    res = await run_action("bluetooth")
    assert res.ok and res.data["working"] is True and res.data["adapter"] == "MediaTek Bluetooth Adapter"
    assert res.data["devices"] == {"OnePlus Nord Buds 3 Pro": True, "boAt Rockerz 518": False, "Mi Band 7": False}
    lines = res.text.splitlines()
    assert lines[0].endswith("working") and lines[2] == "● OnePlus Nord Buds 3 Pro"
    assert "Avrcp" not in res.text and "Enumerator" not in res.text


async def test_bluetooth_adapter_problem_and_missing(fake_os):
    bad = [dict(BT[0], Status="Error")]
    fake_os.ps_out = json.dumps(bad[0])  # PowerShell emits a bare object for a single item
    res = await run_action("bluetooth")
    assert res.ok and res.data["working"] is False and "problem (Error)" in res.text and "No paired" in res.text
    fake_os.ps_out = ""
    res = await run_action("bluetooth", action="status")
    assert res.ok and res.data["adapter"] is None and "No Bluetooth adapter" in res.text
    fake_os.ps_out = json.dumps([dict(BT[0], Present=False)])
    assert (await run_action("bluetooth")).data["adapter"] is None


async def test_bluetooth_errors(fake_os):
    fake_os.ps_rc, fake_os.ps_out = 1, "Get-PnpDevice : not recognized"
    assert not (await run_action("bluetooth")).ok
    fake_os.ps_rc, fake_os.ps_out = 0, "{not json"
    assert not (await run_action("bluetooth")).ok
    assert not (await run_action("bluetooth", action="on")).ok  # no toggling: enum rejects it


def test_run_decodes_and_handles_errors(monkeypatch):
    class P:
        returncode, stdout, stderr = 0, "Power Scheme GUID: x  (Équilibré) *\r\n".encode("utf-8"), b""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    assert ORIGINAL_RUN(["powercfg", "/list"]) == (0, "Power Scheme GUID: x  (Équilibré) *")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("powercfg", 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert ORIGINAL_RUN(["powercfg"])[0] == 124


ORIGINAL_RUN = d._run  # captured at import, before the autouse fixture patches it
