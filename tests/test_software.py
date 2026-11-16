"""winget actions: the process runner is monkeypatched - nothing is searched, installed or removed."""
from __future__ import annotations

import pytest

from bot.actions import software as sw
from bot.actions.registry import REGISTRY, run_action

# trimmed copy of real `winget search 7zip` output (with a spinner prefix like the live tool prints)
SEARCH_OUT = (
    "\r   - \r   \\ \r"
    "Name                               Id                        Version         Match         Source\r\n"
    "-------------------------------------------------------------------------------------------------\r\n"
    "7-Zip                              7zip.7zip                 26.06           Moniker: 7zip winget\r\n"
    "NanaZip                            M2Team.NanaZip            7.0.1882.0      Tag: 7zip     winget\r\n"
    "7zr                                7zip.7zr                  26.06                         winget\r\n"
).encode("utf-8")


@pytest.fixture
def runner(monkeypatch):
    calls = []
    state = {"result": (0, b"", False, 0.1)}

    def fake(args, cwd, timeout):
        calls.append((args, timeout))
        return state["result"]

    monkeypatch.setattr(sw, "run_process", fake)
    monkeypatch.setattr(sw, "winget_exe", lambda: "winget.exe")
    return calls, state


def test_registered_flags():
    expected = {"search_software": False, "install_app": True, "uninstall_app": True}
    for name, dangerous in expected.items():
        act = REGISTRY[name]
        assert act.dangerous is dangerous and act.category == "apps"
        assert act.input_schema["additionalProperties"] is False and act.input_schema["required"]


def test_parse_table():
    rows = sw.parse_table(sw.decode_output(SEARCH_OUT))
    assert [r["id"] for r in rows] == ["7zip.7zip", "M2Team.NanaZip", "7zip.7zr"]
    assert rows[0]["name"] == "7-Zip" and rows[0]["version"] == "26.06" and rows[0]["source"] == "winget"
    assert rows[2]["match"] == ""
    assert sw.parse_table("No package found matching input criteria.") == []


@pytest.mark.parametrize("pid,ok", [("VideoLAN.VLC", True), ("7zip.7zip", True), ("Microsoft.VCRedist.2015+.x64", True),
                                    ("a", False), ("-h", False), ("vlc; calc", False), ("x y", False),
                                    ("Foo&Bar", False), ("--override", False), ("a" * 129, False)])
def test_id_validation(pid, ok):
    assert sw.valid_id(pid) is ok


async def test_search(runner):
    calls, state = runner
    state["result"] = (0, SEARCH_OUT, False, 1.0)
    res = await run_action("search_software", query="7zip")
    assert res.ok and "1. 7-Zip | id: 7zip.7zip | v26.06 | winget" in res.text
    assert len(res.data["results"]) == 3
    args, timeout = calls[-1]
    assert args == ["winget.exe", "search", "7zip", "--accept-source-agreements", "--disable-interactivity"]
    assert timeout == sw.SEARCH_TIMEOUT


async def test_search_caps_rows(runner, monkeypatch):
    calls, state = runner
    state["result"] = (0, SEARCH_OUT, False, 1.0)
    monkeypatch.setattr(sw, "MAX_ROWS", 2)
    res = await run_action("search_software", query="7zip")
    assert "top 2" in res.text and len(res.data["results"]) == 2


async def test_search_no_results_and_errors(runner):
    calls, state = runner
    state["result"] = (-1978335212, b"No package found matching input criteria.\r\n", False, 1.0)  # 0x8A150014 signed
    res = await run_action("search_software", query="zzqq")
    assert res.ok and "No packages found" in res.text
    state["result"] = (None, b"", True, 120)
    assert not (await run_action("search_software", query="x")).ok
    for bad in ("", "-source msstore", 'a"b', "x" * 101):
        res = await run_action("search_software", query=bad)
        assert not res.ok
    assert len(calls) == 2  # invalid queries never reach winget


async def test_install_and_uninstall_argv(runner):
    calls, state = runner
    res = await run_action("install_app", package_id="VideoLAN.VLC")
    assert res.ok and "installed" in res.text
    args, timeout = calls[-1]
    assert args == ["winget.exe", "install", "--id", "VideoLAN.VLC", "-e", "--accept-package-agreements",
                    "--accept-source-agreements", "--disable-interactivity"]
    assert timeout == 900 and isinstance(args, list)
    res = await run_action("uninstall_app", package_id="VideoLAN.VLC")
    assert res.ok and "uninstalled" in res.text
    assert calls[-1][0] == ["winget.exe", "uninstall", "--id", "VideoLAN.VLC", "-e",
                            "--accept-source-agreements", "--disable-interactivity"]


async def test_install_failures(runner):
    calls, state = runner
    n = len(calls)
    res = await run_action("install_app", package_id="vlc & calc")
    assert not res.ok and "Invalid package id" in res.text and len(calls) == n
    state["result"] = (0x8A150061, b"Found an existing package already installed.", False, 3)
    res = await run_action("install_app", package_id="VideoLAN.VLC")
    assert res.ok and "already installed" in res.text
    state["result"] = (0x8A150014, b"No package found", False, 3)
    res = await run_action("uninstall_app", package_id="Not.There")
    assert not res.ok and "search_software" in res.text
    state["result"] = (1603, b"\r  \xe2\x96\x88\xe2\x96\x88\xe2\x96\x92  50%\r\nInstaller failed with exit code: 1603\r\n", False, 9)
    res = await run_action("install_app", package_id="Some.App")
    assert not res.ok and "Installer failed with exit code: 1603" in res.text and "█" not in res.text
    state["result"] = (None, b"", True, 900)
    res = await run_action("install_app", package_id="Some.App")
    assert not res.ok and "timed out" in res.text


async def test_winget_missing(monkeypatch):
    monkeypatch.setattr(sw, "winget_exe", lambda: None)
    for name, kw in (("search_software", {"query": "vlc"}), ("install_app", {"package_id": "VideoLAN.VLC"}),
                     ("uninstall_app", {"package_id": "VideoLAN.VLC"})):
        res = await run_action(name, **kw)
        assert not res.ok and "App Installer" in res.text
