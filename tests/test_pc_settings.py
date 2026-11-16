"""Tests for bot/actions/pc_settings.py. No real OS changes: netsh/powershell/registry are faked."""
from __future__ import annotations

import subprocess

import pytest

from bot.actions import pc_settings as s
from bot.actions.registry import REGISTRY, run_action

IFACE_CONNECTED = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : MediaTek Wi-Fi 6 MT7921 Wireless LAN Card
    State                  : connected
    SSID                   : TP-Link_Extender_5GHz
    Band                   : 5 GHz
    Receive rate (Mbps)    : 433.3
    Signal                 : 84%
    Profile                : TP-Link_Extender_5GHz
"""
IFACE_DISCONNECTED = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : MediaTek Wi-Fi 6 MT7921 Wireless LAN Card
    State                  : disconnected
"""
NETWORKS = """
Interface name : Wi-Fi
There are 3 networks currently visible.

SSID 1 : TP-Link_Extender_5GHz
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Encryption              : CCMP
    BSSID 1                 : 78:20:51:d5:e5:53
         Signal             : 85%
         Radio type         : 802.11ac
    BSSID 2                 : 78:20:51:d5:e5:54
         Signal             : 40%

SSID 2 : Cafe Free
    Network type            : Infrastructure
    Authentication          : Open
    Encryption              : None
    BSSID 1                 : 11:22:33:44:55:66
         Signal             : 92%

SSID 3 : AOT AP-1
    Authentication          : WPA2-Personal
    BSSID 1                 : aa:bb:cc:dd:ee:ff
         Signal             : 30%
"""
PROFILES = """
Profiles on interface Wi-Fi:

Group policy profiles (read only)
---------------------------------
    <None>

User profiles
-------------
    All User Profile     : Arnab
    All User Profile     : TP-Link_Extender_5GHz
    All User Profile     : AOT AP-1
    All User Profile     : AOT Lab AP-4
    All User Profile     : tp link
"""


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"real subprocess call: {a}")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(s, "_sleep", lambda secs: None)


class FakeNetsh:
    """Routes _run calls by command; records them. `iface` can be a list to change over time."""

    def __init__(self, iface=IFACE_CONNECTED, networks=NETWORKS, profiles=PROFILES, connect_rc=0):
        self.calls: list = []
        self.iface = iface if isinstance(iface, list) else [iface]
        self.networks, self.profiles, self.connect_rc = networks, profiles, connect_rc

    def __call__(self, args, timeout=s.TIMEOUT):
        self.calls.append(args)
        cmd = args if isinstance(args, str) else " ".join(args)
        if "show interfaces" in cmd:
            return 0, self.iface[0] if len(self.iface) == 1 else self.iface.pop(0)
        if "show networks" in cmd:
            return (1 if "permission" in self.networks else 0), self.networks
        if "show profiles" in cmd:
            return 0, self.profiles
        if cmd.startswith("netsh wlan connect"):
            return self.connect_rc, "Connection request was completed successfully."
        if "disconnect" in cmd:
            return 0, 'Disconnection request was completed successfully for interface "Wi-Fi".'
        raise AssertionError(f"unexpected command {args!r}")


@pytest.fixture
def netsh(monkeypatch):
    fake = FakeNetsh()
    monkeypatch.setattr(s, "_run", fake)
    return fake


class FakePS:
    def __init__(self, current="74", get_rc=0, set_rc=0):
        self.current, self.get_rc, self.set_rc, self.scripts = current, get_rc, set_rc, []

    def __call__(self, script):
        self.scripts.append(script)
        if "WmiSetBrightness" in script:
            return self.set_rc, "" if self.set_rc == 0 else "Invoke-CimMethod : Not supported"
        return self.get_rc, self.current if self.get_rc == 0 else "Get-CimInstance : Not supported"


# ---------------------------------------------------------------- registry
def test_registered_actions():
    assert REGISTRY["brightness"].dangerous is False
    assert REGISTRY["dark_mode"].dangerous is False
    assert REGISTRY["wifi"].dangerous is True
    for name in ("brightness", "dark_mode", "wifi"):
        assert REGISTRY[name].category == "settings"
    assert REGISTRY["dark_mode"].input_schema["properties"]["mode"]["enum"] == ["on", "off", "toggle", "status"]
    assert REGISTRY["wifi"].input_schema["properties"]["action"]["enum"] == ["status", "list", "connect", "disconnect"]
    assert "disconnect" not in s.SAFE_WIFI_ACTIONS
    assert "cut Edith off" in REGISTRY["wifi"].description


# ---------------------------------------------------------------- brightness
async def test_brightness_get(monkeypatch):
    monkeypatch.setattr(s, "_ps", FakePS("74"))
    res = await run_action("brightness")
    assert res.ok and res.data["level"] == 74 and "74%" in res.text


@pytest.mark.parametrize("kw, current, expected", [
    ({"level": 40}, "74", 40), ({"level": 150}, "74", 100), ({"change": 10}, "95", 100),
    ({"change": -10}, "5", 0), ({"change": "-20"}, "50", 30)])
async def test_brightness_set(monkeypatch, kw, current, expected):
    ps = FakePS(current)
    monkeypatch.setattr(s, "_ps", ps)
    res = await run_action("brightness", **kw)
    assert res.ok and res.data["level"] == expected
    assert f"Brightness=[byte]{expected}" in ps.scripts[-1]


async def test_brightness_unsupported_display(monkeypatch):
    ps = FakePS(get_rc=1)
    monkeypatch.setattr(s, "_ps", ps)
    res = await run_action("brightness", level=50)
    assert not res.ok and "doesn't support" in res.text
    assert not any("WmiSetBrightness" in x for x in ps.scripts)  # never tried to set


async def test_brightness_errors(monkeypatch):
    monkeypatch.setattr(s, "_ps", FakePS(set_rc=1))
    res = await run_action("brightness", level=50)
    assert not res.ok and "Not supported" in res.text
    res = await run_action("brightness", level=10, change=5)
    assert not res.ok and "either" in res.text


# ---------------------------------------------------------------- dark mode
@pytest.fixture
def theme(monkeypatch):
    reg = {"AppsUseLightTheme": 1, "SystemUsesLightTheme": 1, "broadcasts": 0}
    monkeypatch.setattr(s, "_reg_get", lambda name: reg.get(name))
    monkeypatch.setattr(s, "_reg_set", lambda name, value: reg.__setitem__(name, value))
    monkeypatch.setattr(s, "_broadcast_theme", lambda: reg.__setitem__("broadcasts", reg["broadcasts"] + 1))
    return reg


async def test_dark_mode_status_and_set(theme):
    res = await run_action("dark_mode")
    assert res.ok and res.data["dark"] is False and "off" in res.text
    res = await run_action("dark_mode", mode="on")
    assert res.ok and res.data["dark"] is True
    assert theme["AppsUseLightTheme"] == 0 and theme["SystemUsesLightTheme"] == 0 and theme["broadcasts"] == 1
    res = await run_action("dark_mode", mode="on")
    assert "already on" in res.text and theme["broadcasts"] == 1
    res = await run_action("dark_mode", mode="toggle")
    assert res.data["dark"] is False and theme["AppsUseLightTheme"] == 1 and theme["SystemUsesLightTheme"] == 1


async def test_dark_mode_fixes_mismatched_values_and_missing(theme):
    theme.update(AppsUseLightTheme=0, SystemUsesLightTheme=1)  # apps dark, taskbar light
    res = await run_action("dark_mode", mode="on")
    assert res.ok and theme["SystemUsesLightTheme"] == 0
    del theme["AppsUseLightTheme"]  # never set = Windows default light
    assert (await run_action("dark_mode")).data["dark"] is False


async def test_dark_mode_bad_mode_and_write_error(theme, monkeypatch):
    assert not (await run_action("dark_mode", mode="purple")).ok

    def denied(name, value):
        raise PermissionError("access denied")
    monkeypatch.setattr(s, "_reg_set", denied)
    res = await run_action("dark_mode", mode="on")
    assert not res.ok and "access denied" in res.text


# ---------------------------------------------------------------- wifi
async def test_wifi_status(netsh):
    res = await run_action("wifi")
    assert res.ok and res.data == {"connected": True, "ssid": "TP-Link_Extender_5GHz"}
    assert "connected to TP-Link_Extender_5GHz" in res.text and "84%" in res.text
    netsh.iface = [IFACE_DISCONNECTED]
    res = await run_action("wifi", action="status")
    assert res.data["connected"] is False and "disconnected" in res.text


async def test_wifi_list(netsh):
    res = await run_action("wifi", action="list")
    assert res.ok
    assert [n["ssid"] for n in res.data["networks"]] == ["Cafe Free", "TP-Link_Extender_5GHz", "AOT AP-1"]
    assert res.data["networks"][1]["signal"] == 85  # best BSSID wins
    lines = res.text.splitlines()
    assert lines[1].startswith("· Cafe Free") and "🔒" not in lines[1]       # open, not saved
    assert lines[2].startswith("● TP-Link_Extender_5GHz")                     # connected
    assert lines[3].startswith("★ AOT AP-1") and "🔒" in lines[3]            # saved


async def test_wifi_list_location_blocked(netsh):
    netsh.networks = "Network shell commands need location permission to access WLAN information."
    res = await run_action("wifi", action="list")
    assert not res.ok and "Location" in res.text


@pytest.mark.parametrize("network, profile", [("AOT AP-1", "AOT AP-1"), ("aot ap-1", "AOT AP-1"),
                                              ("arnab", "Arnab"), ("Lab", "AOT Lab AP-4")])
async def test_wifi_connect_saved_profile(netsh, network, profile):
    netsh.iface = [IFACE_CONNECTED, IFACE_DISCONNECTED,
                   IFACE_CONNECTED.replace("TP-Link_Extender_5GHz", profile)]
    res = await run_action("wifi", action="connect", network=network)
    assert res.ok, res.text
    assert f'netsh wlan connect name="{profile}"' in netsh.calls
    assert res.data["ssid"] == profile


@pytest.mark.parametrize("network, needle", [("Starbucks", "not a saved"), ("AOT", "several"), ("", "Which network")])
async def test_wifi_connect_refuses_unknown_or_ambiguous(netsh, network, needle):
    res = await run_action("wifi", action="connect", network=network)
    assert not res.ok and needle in res.text
    assert not any(isinstance(c, str) and c.startswith("netsh wlan connect") for c in netsh.calls)


async def test_wifi_connect_already_and_timeout(netsh):
    res = await run_action("wifi", action="connect", network="TP-Link_Extender_5GHz")
    assert res.ok and "Already connected" in res.text
    netsh.iface = [IFACE_DISCONNECTED]
    res = await run_action("wifi", action="connect", network="Arnab")
    assert not res.ok and "isn't connected" in res.text
    netsh.connect_rc = 1
    res = await run_action("wifi", action="connect", network="Arnab")
    assert not res.ok and "failed" in res.text


async def test_wifi_disconnect(netsh):
    res = await run_action("wifi", action="disconnect")
    assert res.ok and "offline" in res.text
    assert netsh.calls == [["netsh", "wlan", "disconnect"]]


def test_run_decodes_and_handles_missing_program(monkeypatch):
    class P:
        returncode, stdout, stderr = 0, "Profil : Café\r\n".encode("utf-8"), b""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    assert s._run(["netsh"]) == (0, "Profil : Café")

    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", missing)
    assert s._run(["netsh"])[0] == 127
