"""Tests for bot.actions.windows. All user32 / COM calls are monkeypatched: no real window is touched."""
from __future__ import annotations

import pytest

from bot.actions import windows as W
from bot.actions.registry import REGISTRY, run_action

WINS = [
    {"hwnd": 101, "title": "Inbox - Outlook", "pid": 1, "process": "OUTLOOK.EXE", "minimized": False, "foreground": False},
    {"hwnd": 102, "title": "YouTube - Brave", "pid": 2, "process": "brave.exe", "minimized": False, "foreground": False},
    {"hwnd": 103, "title": "GitHub - Brave", "pid": 2, "process": "brave.exe", "minimized": False, "foreground": True},
    {"hwnd": 104, "title": "notes.txt - Notepad", "pid": 3, "process": "notepad.exe", "minimized": True, "foreground": False},
    {"hwnd": 105, "title": "Brave Search help - Notepad", "pid": 4, "process": "notepad.exe", "minimized": False, "foreground": False},
]


@pytest.fixture
def os_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(W.sys, "platform", "win32")
    monkeypatch.setattr(W, "_list_windows", lambda: [dict(w) for w in WINS])
    monkeypatch.setattr(W, "_is_window", lambda hwnd: True)
    monkeypatch.setattr(W, "_show_window", lambda hwnd, cmd: calls.append(("show", hwnd, cmd)))

    def post(hwnd):
        calls.append(("close", hwnd))
        return True
    monkeypatch.setattr(W, "_post_close", post)
    return calls


def test_registered():
    wc, ma = REGISTRY["window_control"], REGISTRY["minimize_all"]
    assert wc.dangerous is True and ma.dangerous is False
    assert wc.category == ma.category == "desktop"
    props = wc.input_schema["properties"]
    assert props["action"]["enum"] == ["minimize", "maximize", "restore", "close"]
    assert sorted(wc.input_schema["required"]) == ["action", "window"]
    assert ma.input_schema["properties"] == {}


def test_list_windows_reuses_system(monkeypatch):
    monkeypatch.setattr(W.system, "_visible_windows", lambda: WINS[:1])
    assert W._list_windows() == WINS[:1]


@pytest.mark.parametrize("query,expected,n_others", [
    ("brave", 103, 2),          # exact process match, foreground one preferred; Notepad title also matched
    ("BRAVE.EXE", 103, 1),      # process-only query: the Notepad title no longer matches
    ("notepad", 104, 1),        # exact process, first of two (none foreground)
    ("youtube", 102, 0),        # title match
    ("outlook", 101, 0),
    ("github", 103, 0),
    ("search help", 105, 0),
])
def test_pick_window(query, expected, n_others):
    chosen, others = W.pick_window(WINS, query)
    assert chosen["hwnd"] == expected and len(others) == n_others


def test_pick_window_foreground_beats_first_title_match():
    wins = [dict(w, foreground=(w["hwnd"] == 105)) for w in WINS]
    chosen, others = W.pick_window(wins, "help")   # only 105 matches by title
    assert chosen["hwnd"] == 105 and not others
    chosen, _ = W.pick_window(wins, "- ")          # many title matches, no exact process -> foreground wins
    assert chosen["hwnd"] == 105


def test_pick_window_no_match():
    assert W.pick_window(WINS, "photoshop") == (None, [])
    assert W.pick_window(WINS, "  ") == (None, [])


@pytest.mark.parametrize("op,cmd", [("minimize", 6), ("maximize", 3), ("restore", 9)])
async def test_show_modes(os_calls, op, cmd):
    res = await run_action("window_control", action=op, window="youtube")
    assert res.ok, res.text
    assert os_calls == [("show", 102, cmd)]
    assert res.data["hwnd"] == 102 and "YouTube - Brave" in res.text


async def test_close_posts_wm_close_and_lists_others(os_calls):
    res = await run_action("window_control", action="close", window="brave")
    assert res.ok and os_calls == [("close", 103)]
    assert "save" in res.text and "Also matched" in res.text and "YouTube - Brave" in res.text
    assert res.data["others"] == 2


async def test_close_refused_by_window(os_calls, monkeypatch):
    monkeypatch.setattr(W, "_post_close", lambda hwnd: False)
    res = await run_action("window_control", action="close", window="outlook")
    assert not res.ok and "admin" in res.text


async def test_errors(os_calls, monkeypatch):
    res = await run_action("window_control", action="minimize", window="photoshop")
    assert not res.ok and "No open window" in res.text
    res = await run_action("window_control", action="minimize", window=" ")
    assert not res.ok
    res = await run_action("window_control", action="hide", window="brave")   # only four modes
    assert not res.ok
    monkeypatch.setattr(W, "_is_window", lambda hwnd: False)
    res = await run_action("window_control", action="maximize", window="outlook")
    assert not res.ok and "closed" in res.text
    assert os_calls == []


async def test_non_windows(monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "linux")
    assert not (await run_action("window_control", action="minimize", window="x")).ok
    assert not (await run_action("minimize_all")).ok


async def test_minimize_all_shell_then_fallback(monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "win32")
    used = []
    monkeypatch.setattr(W, "_shell_minimize_all", lambda: used.append("shell"))
    monkeypatch.setattr(W, "_win_d", lambda: used.append("win+d"))
    res = await run_action("minimize_all")
    assert res.ok and res.data["method"] == "shell" and used == ["shell"]

    def broken():
        raise OSError("COM unavailable")
    monkeypatch.setattr(W, "_shell_minimize_all", broken)
    res = await run_action("minimize_all")
    assert res.ok and res.data["method"] == "win+d" and used == ["shell", "win+d"]

    def also_broken():
        raise OSError("no input desktop")
    monkeypatch.setattr(W, "_win_d", also_broken)
    res = await run_action("minimize_all")
    assert not res.ok and "desktop" in res.text
