"""Tests for bot.actions.everyday. subprocess / os.startfile / winrt / windows are monkeypatched:
no toast is shown, nothing is opened or printed."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from bot.actions import desktop as D
from bot.actions import everyday as E
from bot.actions.registry import REGISTRY, run_action


@pytest.fixture(autouse=True)
def on_windows(monkeypatch):
    monkeypatch.setattr(E.sys, "platform", "win32")


def test_registered():
    flags = {n: REGISTRY[n].dangerous for n in ("toast", "play_media", "media_info", "print_file")}
    assert flags == {"toast": False, "play_media": False, "media_info": False, "print_file": True}
    assert REGISTRY["play_media"].input_schema["properties"]["service"]["enum"] == ["youtube", "spotify"]
    assert REGISTRY["print_file"].input_schema["required"] == ["path"]


# ------------------------------------------------------------------ toast
async def test_toast_passes_text_via_env_not_script(monkeypatch):
    seen = {}

    def fake_run(cmd, env, **kw):
        seen.update(cmd=cmd, env=env)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(E.subprocess, "run", fake_run)
    evil = "$(Remove-Item C:\\x) <b>&\x07"
    res = await run_action("toast", title="Hi", message=evil)
    assert res.ok and "Notification" in res.text
    assert seen["env"]["EDITH_TOAST_MESSAGE"] == "$(Remove-Item C:\\x) <b>&"   # control char stripped
    assert seen["env"]["EDITH_TOAST_TITLE"] == "Hi"
    script = seen["cmd"][-1]
    assert script == E._TOAST_PS and "Remove-Item" not in script
    assert "SecurityElement]::Escape" in script and seen["cmd"][1:3] == ["-NoProfile", "-NonInteractive"]


async def test_toast_falls_back_to_popup(monkeypatch):
    monkeypatch.setattr(E.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    shown, done = [], threading.Event()

    def box(text, title):
        shown.append((text, title))
        done.set()
    monkeypatch.setattr(D, "_message_box", box)
    res = await run_action("toast", title="", message="Dinner is ready")
    assert res.ok and "popup" in res.text
    assert await asyncio.to_thread(done.wait, 5)
    assert shown == [("Dinner is ready", "Edith")]


async def test_toast_timeout_falls_back(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("powershell", 20)
    monkeypatch.setattr(E.subprocess, "run", slow)
    monkeypatch.setattr(D, "_message_box", lambda text, title: None)
    assert (await run_action("toast", title="t", message="m")).ok


async def test_toast_validation():
    assert not (await run_action("toast", title="t", message="  ")).ok
    assert len(E._clean("x" * 900, E.MESSAGE_MAX)) == E.MESSAGE_MAX


# ------------------------------------------------------------------ play_media
@pytest.mark.parametrize("query,service,primary,fallback", [
    ("lofi hip hop & chill", "youtube", "https://www.youtube.com/results?search_query=lofi+hip+hop+%26+chill", None),
    ("https://www.youtube.com/watch?v=abc123", "youtube", "https://www.youtube.com/watch?v=abc123", None),
    ("youtu.be/abc123", "youtube", "https://youtu.be/abc123", None),
    ("AC/DC back in black", "spotify", "spotify:search:AC%2FDC%20back%20in%20black",
     "https://open.spotify.com/search/AC%2FDC%20back%20in%20black"),
    ("https://open.spotify.com/track/xyz", "spotify", "https://open.spotify.com/track/xyz", None),
])
def test_media_target(query, service, primary, fallback):
    assert E.media_target(query, service) == (primary, fallback)


@pytest.mark.parametrize("query,service", [
    ("", "youtube"), ("x" * 300, "youtube"), ("https://evil.example/watch", "youtube"),
    ("https://www.youtube.com/watch?v=1", "spotify"), ("javascript://youtube.com", "youtube"),
])
def test_media_target_rejects(query, service):
    with pytest.raises(ValueError):
        E.media_target(query, service)


async def test_play_media_opens(monkeypatch):
    opened = []
    monkeypatch.setattr(E, "_startfile", opened.append)
    res = await run_action("play_media", query="  daft   punk ")
    assert res.ok and opened == ["https://www.youtube.com/results?search_query=daft+punk"]
    assert "YouTube" in res.text
    assert not (await run_action("play_media", query="https://evil.example/x")).ok
    assert not (await run_action("play_media", query="x", service="netflix")).ok
    assert len(opened) == 1


async def test_play_media_spotify_falls_back_to_web(monkeypatch):
    opened = []

    def startfile(target):
        if target.startswith("spotify:"):
            raise OSError("no application is associated")
        opened.append(target)
    monkeypatch.setattr(E, "_startfile", startfile)
    res = await run_action("play_media", query="queen", service="spotify")
    assert res.ok and opened == ["https://open.spotify.com/search/queen"] and "web player" in res.text


async def test_play_media_open_failure(monkeypatch):
    def broken(target):
        raise OSError("nope")
    monkeypatch.setattr(E, "_startfile", broken)
    assert not (await run_action("play_media", query="x")).ok


# ------------------------------------------------------------------ media_info
async def test_media_info_winrt(monkeypatch):
    monkeypatch.setattr(E, "_winrt_session_sync", lambda: {
        "title": "Get Lucky", "artist": "Daft Punk", "album": "RAM", "status": "playing",
        "app": "Spotify.exe"})
    res = await run_action("media_info")
    assert res.ok and res.text == "🎵 Daft Punk - Get Lucky · Spotify"
    monkeypatch.setattr(E, "_winrt_session_sync", lambda: {
        "title": "Song", "artist": "", "album": "", "status": "paused", "app": "Chrome"})
    assert (await run_action("media_info")).text == "🎵 Song (paused) · Chrome"


async def test_now_playing_nothing(monkeypatch):
    monkeypatch.setattr(E, "_winrt_session_sync", lambda: None)
    res = await run_action("media_info")
    assert res.ok and "Nothing is playing" in res.text


async def test_now_playing_without_winrt_uses_window_titles(monkeypatch):
    def no_winrt():
        raise ImportError("No module named 'winrt'")
    monkeypatch.setattr(E, "_winrt_session_sync", no_winrt)
    monkeypatch.setattr(E.system, "_visible_windows", lambda: [
        {"title": "Spotify Premium", "process": "Spotify.exe"},                    # paused -> ignored
        {"title": "Rick Astley - Never Gonna Give You Up - YouTube - Brave", "process": "brave.exe"},
        {"title": "notes.txt - Notepad", "process": "notepad.exe"},
    ])
    res = await run_action("media_info")
    assert res.ok and res.text == "▶ YouTube: Rick Astley - Never Gonna Give You Up"
    monkeypatch.setattr(E.system, "_visible_windows", lambda: [{"title": "Daft Punk - One More Time",
                                                                "process": "Spotify.exe"}])
    assert (await run_action("media_info")).text == "🎵 Spotify: Daft Punk - One More Time"
    monkeypatch.setattr(E.system, "_visible_windows", lambda: [])
    res = await run_action("media_info")
    assert not res.ok and "winrt" in res.text


def test_winrt_really_missing_raises_importerror():
    if sys.modules.get("winrt") or sys.modules.get("winsdk"):
        pytest.skip("winrt installed")
    with pytest.raises(ImportError):
        E._winrt_session_sync()


# ------------------------------------------------------------------ print_file
async def test_print_file(monkeypatch, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    calls = []
    monkeypatch.setattr(E.os, "startfile", lambda p, verb=None: calls.append((p, verb)), raising=False)
    monkeypatch.setattr(E, "_default_printer", lambda: "Office Laser")
    res = await run_action("print_file", path=str(f))
    assert res.ok and calls == [(str(f), "print")] and "Office Laser" in res.text


async def test_print_file_errors(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(E.os, "startfile", lambda p, verb=None: calls.append(p), raising=False)
    assert not (await run_action("print_file", path=str(tmp_path / "missing.txt"))).ok
    assert not (await run_action("print_file", path=str(tmp_path))).ok          # a folder
    assert not (await run_action("print_file", path="  ")).ok
    assert calls == []

    f = tmp_path / "data.xyz"
    f.write_text("x")

    def no_verb(p, verb=None):
        raise OSError(1155, "No application is associated with the specified file for this operation")
    monkeypatch.setattr(E.os, "startfile", no_verb, raising=False)
    res = await run_action("print_file", path=str(f))
    assert not res.ok and ".xyz" in res.text
