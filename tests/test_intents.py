"""Tests for bot/jarvis/intents.py - the deterministic instant-intent matcher."""
from __future__ import annotations

import time

import pytest

from bot.actions.registry import REGISTRY, load_all, validate_args
from bot.jarvis import intents
from bot.jarvis.intents import Intent, match

load_all()

YT = "https://www.youtube.com"

# phrase -> list of (action, args) expected, in order
POSITIVE: dict[str, list[tuple[str, dict]]] = {
    # --- lock
    "lock the pc": [("power_lock", {})],
    "Lock my computer.": [("power_lock", {})],
    "hey edith, lock the PC please": [("power_lock", {})],
    "Edith lock": [("power_lock", {})],
    "can you lock my laptop for me": [("power_lock", {})],
    "lock the screen": [("power_lock", {})],
    "lock it up": [("power_lock", {})],
    # --- screenshot
    "screenshot": [("screenshot", {})],
    "take a screenshot": [("screenshot", {})],
    "Take a screenshot of my screen, please!": [("screenshot", {})],
    "grab a screen shot": [("screenshot", {})],
    "could you send me a screenshot": [("screenshot", {})],
    "show me my screen": [("screenshot", {})],
    "capture the screen": [("screenshot", {})],
    # --- volume
    "set volume to 30": [("volume", {"level": 30})],
    "set the volume to thirty": [("volume", {"level": 30})],
    "volume 45": [("volume", {"level": 45})],
    "Volume to fifty percent": [("volume", {"level": 50})],
    "fifty percent volume": [("volume", {"level": 50})],
    "turn the volume to 20%": [("volume", {"level": 20})],
    "set volume to twenty-five": [("volume", {"level": 25})],
    "set the volume to a hundred": [("volume", {"level": 100})],
    "volume zero": [("volume", {"level": 0})],
    "volume up": [("volume", {"change": 10})],
    "turn the volume up": [("volume", {"change": 10})],
    "turn up the volume by 20": [("volume", {"change": 20})],
    "increase the volume by fifteen percent": [("volume", {"change": 15})],
    "louder": [("volume", {"change": 10})],
    "volume down": [("volume", {"change": -10})],
    "turn the sound down by 5": [("volume", {"change": -5})],
    "lower the volume": [("volume", {"change": -10})],
    "make it quieter": [("volume", {"change": -10})],
    "max volume": [("volume", {"level": 100})],
    "set the volume to max": [("volume", {"level": 100})],
    "turn the volume all the way up": [("volume", {"level": 100})],
    "what's the volume": [("volume", {})],
    "volume": [("volume", {})],
    # --- mute
    "mute": [("mute", {"state": "on"})],
    "mute the sound": [("mute", {"state": "on"})],
    "Mute the PC.": [("mute", {"state": "on"})],
    "turn off the sound": [("mute", {"state": "on"})],
    "unmute": [("mute", {"state": "off"})],
    "un-mute the speakers": [("mute", {"state": "off"})],
    "turn the sound back on": [("mute", {"state": "off"})],
    "toggle mute": [("mute", {"state": "toggle"})],
    # --- media
    "play": [("media_key", {"key": "play_pause"})],
    "resume the music": [("media_key", {"key": "play_pause"})],
    "pause": [("media_key", {"key": "play_pause"})],
    "pause the video": [("media_key", {"key": "play_pause"})],
    "next song": [("media_key", {"key": "next"})],
    "skip this track": [("media_key", {"key": "next"})],
    "play the next song": [("media_key", {"key": "next"})],
    "previous track": [("media_key", {"key": "prev"})],
    "play the last song": [("media_key", {"key": "prev"})],
    "go back one song": [("media_key", {"key": "prev"})],
    "stop the music": [("media_key", {"key": "stop"})],
    # --- system status
    "how's my pc": [("system_status", {})],
    "How's my PC doing?": [("system_status", {})],
    "how is the computer running": [("system_status", {})],
    "system status": [("system_status", {})],
    "show me the pc stats": [("system_status", {})],
    "status report": [("system_status", {})],
    "cpu usage": [("system_status", {})],
    "what's my RAM usage": [("system_status", {})],
    "check the battery": [("system_status", {})],
    "how much ram am i using": [("system_status", {})],
    "how much battery is left": [("system_status", {})],
    "how hot is my laptop": [("system_status", {})],
    "what's my ip address": [("system_status", {})],
    "how long has my pc been on": [("system_status", {})],
    # --- gpu
    "gpu status": [("gpu_status", {})],
    "what's the gpu temperature": [("gpu_status", {})],
    "how's my gpu doing": [("gpu_status", {})],
    "graphics card usage": [("gpu_status", {})],
    # --- processes
    "top processes": [("procs", {"sort": "cpu"})],
    "show me the running processes": [("procs", {"sort": "cpu"})],
    "list processes by memory": [("procs", {"sort": "mem"})],
    "what's using my cpu": [("procs", {"sort": "cpu"})],
    "what's eating my ram": [("procs", {"sort": "mem"})],
    "which apps are using the most memory": [("procs", {"sort": "mem"})],
    # --- windows
    "list open windows": [("list_windows", {})],
    "show me all my open apps": [("list_windows", {})],
    "what apps are open": [("list_windows", {})],
    "what windows are running": [("list_windows", {})],
    "what's open on my pc": [("list_windows", {})],
    # --- open sites / search
    "open youtube": [("open_url", {"url": YT})],
    "Open YouTube please": [("open_url", {"url": YT})],
    "go to youtube.com": [("open_url", {"url": "https://youtube.com"})],
    "open github.com": [("open_url", {"url": "https://github.com"})],
    "open github dot com": [("open_url", {"url": "https://github.com"})],
    "go to x.com": [("open_url", {"url": "https://x.com"})],
    "open twitter": [("open_url", {"url": "https://x.com"})],
    "visit the reddit website": [("open_url", {"url": "https://www.reddit.com"})],
    "pull up gmail": [("open_url", {"url": "https://mail.google.com"})],
    "open google maps in the browser": [("open_url", {"url": "https://maps.google.com"})],
    "open https://example.com/Some/Path?x=1": [("open_url", {"url": "https://example.com/Some/Path?x=1"})],
    "take me to news.ycombinator.com": [("open_url", {"url": "https://news.ycombinator.com"})],
    "open bbc.co.uk": [("open_url", {"url": "https://bbc.co.uk"})],
    "google best pizza near me": [("open_url", {"url": "https://www.google.com/search?q=best+pizza+near+me"})],
    "search google for python dataclasses": [("open_url", {"url": "https://www.google.com/search?q=python+dataclasses"})],
    "search youtube for lofi music": [("open_url", {"url": YT + "/results?search_query=lofi+music"})],
    "look up cat videos on youtube": [("open_url", {"url": YT + "/results?search_query=cat+videos"})],
    "google maps": [("open_url", {"url": "https://maps.google.com"})],
    # --- record / webcam / clipboard
    "record the screen": [("record_screen", {"seconds": 10})],
    "record my screen for 30 seconds": [("record_screen", {"seconds": 30})],
    "record the screen for twenty seconds": [("record_screen", {"seconds": 20})],
    "start a screen recording for 1 minute": [("record_screen", {"seconds": 60})],
    "screen record for half a minute": [("record_screen", {"seconds": 30})],
    "take a webcam photo": [("webcam_photo", {})],
    "take a picture with the webcam": [("webcam_photo", {})],
    "snap a camera pic": [("webcam_photo", {})],
    "read the clipboard": [("clipboard_get", {})],
    "what's on my clipboard": [("clipboard_get", {})],
    "what did I just copy": [("clipboard_get", {})],
    # --- say / popup (payload keeps its case and punctuation)
    "say hello world": [("say", {"text": "hello world"})],
    'Say "Dinner is ready!"': [("say", {"text": "Dinner is ready!"})],
    "speak I'll be home in thirty minutes": [("say", {"text": "I'll be home in thirty minutes"})],
    "announce: meeting starts now": [("say", {"text": "meeting starts now"})],
    "say hello, world": [("say", {"text": "hello, world"})],
    "say hello and goodbye": [("say", {"text": "hello and goodbye"})],
    "say out loud Time to go": [("say", {"text": "Time to go"})],
    "show a popup saying Back in 5 minutes": [("show_message", {"text": "Back in 5 minutes"})],
    "show a message on the screen that says Don't touch my PC": [("show_message", {"text": "Don't touch my PC"})],
    "popup: Lunch time!": [("show_message", {"text": "Lunch time!"})],
    "display an alert saying Hello Arnav": [("show_message", {"text": "Hello Arnav"})],
    # --- power
    "shut down the pc": [("power_shutdown", {"delay": 15})],
    "shutdown": [("power_shutdown", {"delay": 15})],
    "shut down the computer now": [("power_shutdown", {"delay": 15})],
    "turn off my laptop": [("power_shutdown", {"delay": 15})],
    "shut down in five minutes": [("power_shutdown", {"delay": 300})],
    "shut down the pc in 1 hour and 30 minutes": [("power_shutdown", {"delay": 5400})],
    "power off the pc in half an hour": [("power_shutdown", {"delay": 1800})],
    "restart the pc": [("power_restart", {"delay": 15})],
    "reboot": [("power_restart", {"delay": 15})],
    "restart my computer in 2 minutes": [("power_restart", {"delay": 120})],
    "put the pc to sleep": [("power_sleep", {})],
    "put the computer in sleep mode": [("power_sleep", {})],
    "sleep mode": [("power_sleep", {})],
    "hibernate": [("power_hibernate", {})],
    "hibernate the laptop": [("power_hibernate", {})],
    "log off of windows": [("power_logoff", {})],
    "sign me out of windows": [("power_logoff", {})],
    "abort": [("power_abort", {})],
    "cancel the shutdown": [("power_abort", {})],
    "don't shut down": [("power_abort", {})],
    "abort the restart": [("power_abort", {})],
    "turn off the screen": [("power_monitor_off", {})],
    "turn the monitor off": [("power_monitor_off", {})],
    "display off": [("power_monitor_off", {})],
    # --- kill / close
    "close chrome": [("kill_process", {"target": "chrome.exe"})],
    "Close Google Chrome.": [("kill_process", {"target": "chrome.exe"})],
    "quit spotify": [("kill_process", {"target": "Spotify.exe"})],
    "close vs code": [("kill_process", {"target": "Code.exe"})],
    "shut down discord": [("kill_process", {"target": "Discord.exe"})],
    "exit notepad": [("kill_process", {"target": "notepad.exe"})],
    "kill steam": [("kill_process", {"target": "steam.exe"})],
    "kill obs": [("kill_process", {"target": "obs64.exe"})],
    "kill foobar": [("kill_process", {"target": "foobar.exe"})],
    "close foobar.exe": [("kill_process", {"target": "foobar.exe"})],
    "end the foobar process": [("kill_process", {"target": "foobar.exe"})],
    "kill process 1234": [("kill_process", {"target": "1234"})],
    "terminate 4321": [("kill_process", {"target": "4321"})],
    # --- reminders
    "Remind me in 10 minutes to stretch.": [("create_reminder", {"when": "10m", "text": "stretch"})],
    "Lock the PC!": [("power_lock", {})],
    "remind me in 10 minutes to stretch": [("create_reminder", {"when": "10m", "text": "stretch"})],
    "remind me in ten minutes to stretch": [("create_reminder", {"when": "10m", "text": "stretch"})],
    "Remind me to drink water in an hour": [("create_reminder", {"when": "1h", "text": "drink water"})],
    "remind me in 1 hour and 30 minutes to check the oven":
        [("create_reminder", {"when": "1h30m", "text": "check the oven"})],
    "set a reminder for 45 minutes to take a break": [("create_reminder", {"when": "45m", "text": "take a break"})],
    "in 5 minutes remind me to call Mom": [("create_reminder", {"when": "5m", "text": "call Mom"})],
    "remind me at 9pm to call Mom": [("create_reminder", {"when": "9pm", "text": "call Mom"})],
    "remind me to call mom at 9:30 pm": [("create_reminder", {"when": "9:30pm", "text": "call mom"})],
    "remind me tomorrow at 9 am to submit the report":
        [("create_reminder", {"when": "tomorrow 9am", "text": "submit the report"})],
    "remind me at 21:15 to sleep": [("create_reminder", {"when": "21:15", "text": "sleep"})],
    "remind me tonight at 8 to water the plants": [("create_reminder", {"when": "8pm", "text": "water the plants"})],
    "remind me in half an hour that the laundry is done":
        [("create_reminder", {"when": "30m", "text": "the laundry is done"})],
    # --- compound commands
    "mute and lock the pc": [("mute", {"state": "on"}), ("power_lock", {})],
    "Hey Edith, mute and lock the PC please": [("mute", {"state": "on"}), ("power_lock", {})],
    "take a screenshot then lock the pc": [("screenshot", {}), ("power_lock", {})],
    "pause the music, turn the volume down and lock the screen":
        [("media_key", {"key": "play_pause"}), ("volume", {"change": -10}), ("power_lock", {})],
    "close chrome and open youtube": [("kill_process", {"target": "chrome.exe"}), ("open_url", {"url": YT})],
    "mute, then say I'm in a meeting": [("mute", {"state": "on"}), ("say", {"text": "I'm in a meeting"})],
    "mute and remind me in 10 minutes to stretch":
        [("mute", {"state": "on"}), ("create_reminder", {"when": "10m", "text": "stretch"})],
    "close spotify and then shut down the pc":
        [("kill_process", {"target": "Spotify.exe"}), ("power_shutdown", {"delay": 15})],
}

NEGATIVE = [
    "", "   ", "edith", "hey edith", "please", "thanks",
    "what's the weather", "what's the weather like today?", "write me a poem about locks",
    "why is my volume low", "lock the pc and explain quantum physics", "how do I lock my pc",
    "tell me a joke", "what time is it", "who are you", "explain how a cpu works",
    "is my pc slow because of chrome", "should I restart my computer", "why did my computer restart",
    # dangerous-action false-positive traps
    "restart", "restart the song", "go to sleep", "sleep", "log out", "sign out of gmail", "turn off",
    "turn off the lights", "close the door", "close the window", "close it", "close everything", "kill time",
    "kill the lights", "close foobar", "kill explorer", "kill svchost", "kill python", "end the call", "kill 4", "kill process 0", "close 1234",
    "shut down chrome and restart the pc and sleep", "shut down the pc and open youtube", "shutdown and restart",
    "don't lock the pc", "unlock the pc", "lock the door", "kill the bot", "kill yourself",
    "take a photo", "take a selfie", "show me the camera",
    # vague / reasoning / payload ambiguity
    "say something", "say something funny", "say hi to mom", "say that again", "speak up",
    "say hello and lock the pc", "open my documents", "open report.pdf", "go to sleep now",
    "open brave then youtube", "visit spotify",
    "play despacito", "play some music", "volume 150", "set the volume to 200", "turn it up",
    "record the screen for 5 minutes", "remind me in 10 minutes", "remind me at 9 to call mom",
    "remind me to call mom", "search for my files", "open youtube and search cats", "mute and do my homework",
    "a" * 400, "lock the pc " * 40,
]


def _pairs(result: list[Intent] | None) -> list[tuple[str, dict]] | None:
    return None if result is None else [(i.action, i.args) for i in result]


@pytest.mark.parametrize("phrase, expected", list(POSITIVE.items()), ids=list(POSITIVE))
def test_positive(phrase, expected):
    assert _pairs(match(phrase)) == expected


@pytest.mark.parametrize("phrase", NEGATIVE, ids=[p[:40] or "<empty>" for p in NEGATIVE])
def test_negative(phrase):
    assert match(phrase) is None


def test_every_intent_is_a_valid_registry_call():
    for phrase in POSITIVE:
        result = match(phrase)
        assert result, phrase
        for it in result:
            assert isinstance(it, Intent)
            assert it.action in REGISTRY, (phrase, it.action)
            assert "chat_id" not in it.args
            clean = validate_args(REGISTRY[it.action], dict(it.args))
            assert clean == it.args, (phrase, it.args, clean)  # no coercion needed: exact types
            assert isinstance(it.say, str) and 0 < len(it.say) <= 200 and it.say.strip() == it.say


def test_dangerous_intents_are_flagged_in_registry():
    """Sanity: the matcher can produce dangerous actions; they stay dangerous so jarvis confirms them."""
    dangerous = {it.action for p in POSITIVE for it in match(p) if REGISTRY[it.action].dangerous}
    assert {"power_lock", "power_shutdown", "kill_process", "webcam_photo"} <= dangerous


def test_reminder_when_strings_parse_with_scheduler():
    from bot.automation import scheduler

    now = scheduler.now_local().replace(hour=0, minute=1, second=0, microsecond=0)
    for phrase in POSITIVE:
        for it in match(phrase):
            if it.action == "create_reminder":
                _when, rest = scheduler.parse_when(f"{it.args['when']} XYZ", now=now)
                assert rest.strip() == "XYZ", (phrase, it.args["when"])


def test_number_words():
    n = intents._words_to_numbers
    assert n("set volume to thirty") == "set volume to 30"
    assert n("twenty five percent") == "25 percent"
    assert n("twenty-five") == "25"
    assert n("a hundred and twenty") == "120"
    assert n("one hundred five") == "105"
    assert n("Say Hello") == "Say Hello"
    assert n("in fifteen minutes") == "in 15 minutes"


def test_fast():
    phrases = list(POSITIVE) + NEGATIVE
    start = time.perf_counter()
    for p in phrases:
        match(p)
    per_call_ms = (time.perf_counter() - start) * 1000 / len(phrases)
    assert per_call_ms < 1.0, per_call_ms


def test_pure_module_has_no_telegram_or_bot_imports():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(intents))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(x.startswith(("telegram", "bot")) or x == "" for x in names), names


@pytest.mark.parametrize("phrase,app", [
    ("open notepad", "notepad"), ("open spotify", "spotify"), ("open brave", "brave"),
    ("launch vs code", "vs code"), ("hey edith please open whatsapp", "whatsapp"),
])
def test_open_installed_app(phrase, app):
    got = match(phrase)
    assert got is not None and [(i.action, i.args) for i in got] == [("open_app", {"name": app})]
