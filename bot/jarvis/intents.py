"""Instant intents: a deterministic, LLM-free matcher for everyday voice/chat commands.

    from bot.jarvis.intents import match
    match("hey edith, mute and lock the pc")
    -> [Intent("mute", {"state": "on"}, "Muted."), Intent("power_lock", {}, "Locking the PC.")]

Contract (bot/modules/jarvis.py depends on it):
  * match(text) -> list[Intent] | None. None means "not sure - let the LLM handle the whole request".
  * Every Intent names a bot.actions REGISTRY action, and its args pass registry.validate_args
    (tests/test_intents.py checks this for every positive phrase). Never emits chat_id.
  * Conservative by construction: every pattern must match a whole clause (fullmatch), so any
    leftover words, questions or reasoning requests fall through to the LLM. Compound commands
    ("mute and lock the pc") only match if EVERY part matches; if the whole text could also be
    read as one payload command ("say hello and lock the pc") it is ambiguous -> None.
  * Dangerous actions are still confirmed downstream; patterns for them are deliberately stricter
    (e.g. "restart" needs "the pc", "log out" needs "of windows", only known apps can be "closed").
  * Pure: no I/O, no telegram/bot imports, precompiled regexes (well under 1 ms per call).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus


@dataclass
class Intent:
    action: str   # a name in bot.actions.registry.REGISTRY
    args: dict    # kwargs valid for that action (passes registry.validate_args)
    say: str      # short, natural spoken confirmation


MAX_INPUT = 300
MAX_PARTS = 5
# Power actions that end the session; at most one per request and it must be the last step.
_TERMINAL = {"power_shutdown", "power_restart", "power_sleep", "power_hibernate", "power_logoff"}

# ---------------------------------------------------------------- fillers / wake words
_LEADING = [re.compile(p, re.I) for p in (
    r"^(?:(?:hey|hi|hello|ok|okay|yo)[\s,]+)?(?:edith|jarvis)\b[\s,.:!]*",
    r"^(?:please|pls|kindly|just|um+|uh+|erm|so|okay|ok|alright|right)\b[\s,]+",
    r"^(?:can|could|would|will) you(?: please)?\b[\s,]*",
    r"^(?:i want you to|i need you to|i'?d like you to|go ahead and|let'?s|let us|i want to|i wanna|"
    r"i need to|i'?d like to|do me a favou?r and)\b[\s,]*",
)]
_TRAILING = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|thx|for me|right now|now|immediately|asap|"
                       r"edith|jarvis))+$", re.I)
_TRAILING_WAKE = re.compile(r"[\s,]+(?:edith|jarvis)$", re.I)
_EDGE_PUNCT = " \t\r\n.!?,;:"
_SPLIT = re.compile(r"\s*(?:,\s*(?:and then|and|then)\b|,|;|\band then\b|\bthen\b|\band also\b|\band\b|"
                    r"\balso\b|\bafter that\b)\s*", re.I)


def _strip_leading(s: str) -> str:
    """Drop wake words / politeness at the start. Trailing punctuation is kept (payloads need it)."""
    s = s.lstrip(_EDGE_PUNCT).rstrip()
    changed = True
    while changed and s:
        changed = False
        for rx in _LEADING:
            new = rx.sub("", s, count=1).lstrip(_EDGE_PUNCT)
            if new != s:
                s, changed = new, True
    return s


def _strip_trailing(s: str) -> str:
    return _TRAILING.sub("", s).strip(_EDGE_PUNCT)


# ---------------------------------------------------------------- number words
_SMALL = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: (i + 2) * 10 for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_HYPHEN_NUM = re.compile(r"\b(" + "|".join(_TENS) + r")-(" + "|".join(list(_SMALL)[1:10]) + r")\b", re.I)


def _words_to_numbers(s: str) -> str:
    """'set volume to thirty' -> 'set volume to 30', 'a hundred percent' -> '100 percent'. Case of other words kept."""
    s = _HYPHEN_NUM.sub(r"\1 \2", s)
    toks = s.split()
    out: list[str] = []
    i = 0

    def low(j: int) -> str:
        return toks[j].lower() if j < len(toks) else ""

    while i < len(toks):
        t = low(i)
        if t in ("a", "one") and low(i + 1) == "hundred":
            val, i = 100, i + 2
        elif t in _TENS:
            val, i = _TENS[t], i + 1
            if low(i) in _SMALL and 0 < _SMALL[low(i)] < 10:
                val, i = val + _SMALL[low(i)], i + 1
        elif t in _SMALL:
            val, i = _SMALL[t], i + 1
            if low(i) == "hundred":
                val, i = val * 100, i + 1
        else:
            out.append(toks[i])
            i += 1
            continue
        if val >= 100 and val % 100 == 0:  # "a hundred and twenty", "one hundred five"
            j = i + 1 if low(i) == "and" else i
            if low(j) in _TENS:
                val, j = val + _TENS[low(j)], j + 1
                if low(j) in _SMALL and 0 < _SMALL[low(j)] < 10:
                    val, j = val + _SMALL[low(j)], j + 1
                i = j
            elif low(j) in _SMALL and _SMALL[low(j)] > 0:
                val, i = val + _SMALL[low(j)], j + 1
        out.append(str(val))
    return " ".join(out)


def _norm(clause: str) -> str:
    """Lowercase command form: no apostrophes/punctuation, '%' -> percent, number words -> digits."""
    s = clause.lower().replace("’", "'").replace("'", "").replace("%", " percent ")
    s = _HYPHEN_NUM.sub(r"\1 \2", s)
    s = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", s)
    s = re.sub(r"[^a-z0-9.: ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .:")
    return _strip_trailing(_words_to_numbers(s))


# ---------------------------------------------------------------- durations / times
_UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)"
_DUR = (rf"(?:(?:\d+|an?) {_UNIT}(?: (?:and )?(?:\d+|an?) {_UNIT})?|half an hour|half a minute|"
        r"an hour and a half)")
_UNIT_SECS = {"s": 1, "m": 60, "h": 3600}


def _dur_seconds(text: str) -> int:
    t = text.lower()
    if t == "half an hour":
        return 1800
    if t == "half a minute":
        return 30
    if t == "an hour and a half":
        return 5400
    total = 0
    for n, unit in re.findall(rf"(\d+|an?) ({_UNIT})", t):
        total += (1 if n in ("a", "an") else int(n)) * _UNIT_SECS[unit[0]]
    return total


def _say_secs(s: int) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = [f"{v} {name}{'s' if v != 1 else ''}" for v, name in ((h, "hour"), (m, "minute"), (sec, "second")) if v]
    return " ".join(parts) or "0 seconds"


def _compact_secs(s: int) -> str:
    """Scheduler duration syntax: 5400 -> '1h30m', 45 -> '45s'."""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "".join(f"{v}{u}" for v, u in ((h, "h"), (m, "m"), (sec, "s")) if v) or "0s"


# ---------------------------------------------------------------- pattern helpers
_DEV = r"(?:(?:the|my|this) )?(?:pc|computer|laptop|machine|system|workstation|desktop)"
_NOUN_MEDIA = r"(?: (?:the |this |my )?(?:music|song|track|video|playback|media|audio))"
Builder = Callable[[re.Match], "Intent | None"]


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern.replace("<DEV>", _DEV).replace("<DUR>", _DUR).replace("<MEDIA>", _NOUN_MEDIA))


def _const(action: str, args: dict, say: str) -> Builder:
    return lambda m: Intent(action, dict(args), say)


def _g(m: re.Match, name: str) -> str | None:
    """Alternative branches name their groups n/n2/n3 (d/d2/d3, s/s2/s3); return whichever matched."""
    return next((v for k, v in m.groupdict().items() if v and re.fullmatch(rf"{name}\d*", k)), None)


# ---------------------------------------------------------------- builders
def _volume_set(m: re.Match) -> Intent | None:
    n = int(_g(m, "n"))
    if n > 100:
        return None
    return Intent("volume", {"level": n}, f"Volume set to {n} percent.")


def _volume_change(sign: int) -> Builder:
    def build(m: re.Match) -> Intent | None:
        n = int(_g(m, "n") or 10)
        if not 0 < n <= 100:
            return None
        return Intent("volume", {"change": sign * n},
                      f"Volume {'up' if sign > 0 else 'down'} {n} percent.")
    return build


def _with_delay(action: str, verb: str) -> Builder:
    def build(m: re.Match) -> Intent | None:
        d = _g(m, "d")
        secs = _dur_seconds(d) if d else 15
        if secs > 86_400:
            return None
        secs = max(5, secs)
        return Intent(action, {"delay": secs}, f"{verb} the PC in {_say_secs(secs)}. Say abort to cancel.")
    return build


def _record(m: re.Match) -> Intent | None:
    d = _g(m, "d")
    secs = _dur_seconds(d) if d else 10
    if not 1 <= secs <= 60:
        return None  # the recorder caps at 60 s; let the LLM explain
    return Intent("record_screen", {"seconds": secs}, f"Recording the screen for {_say_secs(secs)}.")


def _procs_sort(m: re.Match) -> Intent:
    s = _g(m, "s") or "cpu"
    sort = "cpu" if s in ("cpu", "processor") else "mem"
    return Intent("procs", {"sort": sort}, f"Here are the top processes by {'CPU' if sort == 'cpu' else 'memory'}.")


# Normalised-clause patterns (lowercase, digits, no punctuation). First match wins.
_NORM_PATTERNS: list[tuple[re.Pattern, Builder]] = [
    # --- lock
    (_rx(r"lock(?: up)?(?: <DEV>)?|lock (?:the |my )?screen|lock it(?: up)?|lock screen|screen lock"),
     _const("power_lock", {}, "Locking the PC.")),
    # --- monitor off (before shutdown so "turn off the screen" never shuts down)
    (_rx(r"(?:turn|switch|shut|power) off (?:the |my )?(?:monitors?|screens?|displays?)|"
         r"(?:turn|switch|power) (?:the |my )?(?:monitors?|screens?|displays?) off|"
         r"(?:monitors?|screens?|displays?) off|(?:blank|black out) (?:the |my )?screens?"),
     _const("power_monitor_off", {}, "Turning the display off.")),
    # --- abort a pending shutdown/restart
    (_rx(r"(?:abort|cancel|stop|halt)(?: the)?(?: pending| scheduled)? (?:shutdown|shut down|restart|reboot|power off)|"
         r"abort|dont (?:shut down|shutdown|restart|reboot|turn off)(?: <DEV>)?"),
     _const("power_abort", {}, "Shutdown cancelled.")),
    # --- shutdown / restart (abortable delay)
    (_rx(r"(?:shut ?down|power (?:off|down))(?: <DEV>)?(?: (?:in|after) (?P<d><DUR>))?|"
         r"(?:turn|switch) off <DEV>(?: (?:in|after) (?P<d2><DUR>))?|"
         r"(?:turn|switch|shut) <DEV> (?:off|down)(?: (?:in|after) (?P<d3><DUR>))?"),
     _with_delay("power_shutdown", "Shutting down")),
    (_rx(r"(?:restart|reboot) <DEV>(?: (?:in|after) (?P<d><DUR>))?|reboot(?: (?:in|after) (?P<d2><DUR>))?"),
     _with_delay("power_restart", "Restarting")),
    # --- sleep / hibernate / log off (strict: no bare "sleep" / "log out")
    (_rx(r"(?:put|send) (?:<DEV>|it) to sleep|(?:sleep|suspend) <DEV>|"
         r"(?:put )?(?:<DEV> )?(?:in|into|to) sleep mode|sleep mode"),
     _const("power_sleep", {}, "Putting the PC to sleep.")),
    (_rx(r"hibernate(?: <DEV>)?|put <DEV> (?:in|into|to) hibernat(?:e|ion)(?: mode)?|hibernation mode"),
     _const("power_hibernate", {}, "Hibernating the PC.")),
    (_rx(r"(?:log|sign) (?:me )?(?:off|out)(?: of)? (?:windows|<DEV>|my (?:windows|user) account)|"
         r"(?:logoff|logout|signout)(?: of)? (?:windows|<DEV>)"),
     _const("power_logoff", {}, "Signing out of Windows.")),
    # --- screenshot
    (_rx(r"(?:(?:take|grab|capture|get|send|snap|do)(?: me)? )?(?:a |an |the )?(?:screenshot|screen shot|screen capture|"
         r"screengrab|screen grab|screencap|print screen|printscreen)(?: of (?:the |my )?(?:screen|desktop|display|pc|"
         r"computer|monitors?))?|(?:show|send) me (?:the |my )?(?:screen|desktop)|"
         r"(?:capture|screenshot) (?:the |my )?(?:screen|desktop)"),
     _const("screenshot", {}, "Taking a screenshot.")),
    # --- screen recording
    (_rx(r"(?:record|capture) (?:the |my )?(?:screen|desktop|display)(?: for)?(?: (?P<d><DUR>))?|"
         r"(?:start |make |do |take )?(?:a )?screen ?record(?:ing)?(?: for (?P<d2><DUR>))?|"
         r"record (?:a )?(?:video|clip|recording) of (?:the |my )?screen(?: for (?P<d3><DUR>))?"),
     _record),
    # --- webcam (explicit "webcam/camera" + "photo" only)
    (_rx(r"(?:(?:take|snap|grab|capture|get)(?: me)? )?(?:a )?(?:webcam|web cam|camera) (?:photo|picture|pic|shot|"
         r"snapshot|image|selfie)|(?:take|snap|grab|capture) (?:a )?(?:photo|picture|pic|snapshot|selfie) "
         r"(?:with|from|using|on) (?:the |my )?(?:webcam|web cam|camera)"),
     _const("webcam_photo", {}, "Taking a webcam photo.")),
    # --- volume
    (_rx(r"(?:(?:set|turn|change|put|make|bring|adjust) )?(?:the )?(?:volume|sound)(?: level)? (?:to |at )?(?P<n>\d+)"
         r"(?: percent)?|(?P<n2>\d+) percent volume"),
     _volume_set),
    (_rx(r"(?:max|maximum|full|crank)(?: out| up)? (?:the )?(?:volume|sound)|"
         r"(?:(?:set|turn|put) )?(?:the )?(?:volume|sound) (?:to |at )?(?:the )?(?:max|maximum|full|full blast)|"
         r"turn (?:the )?(?:volume|sound) all the way up"),
     _const("volume", {"level": 100}, "Volume at maximum.")),
    (_rx(r"(?:(?:turn|crank|bump|pump) )?(?:the )?(?:volume|sound) up(?: by (?P<n>\d+)(?: percent)?)?(?: a (?:bit|little))?|"
         r"(?:turn|crank|bump) up (?:the )?(?:volume|sound)(?: by (?P<n2>\d+)(?: percent)?)?|"
         r"(?:increase|raise|boost)(?: the)? (?:volume|sound)(?: by (?P<n3>\d+)(?: percent)?)?|"
         r"(?:make it |a (?:bit|little) )?louder"),
     _volume_change(+1)),
    (_rx(r"(?:(?:turn|bring) )?(?:the )?(?:volume|sound) down(?: by (?P<n>\d+)(?: percent)?)?(?: a (?:bit|little))?|"
         r"(?:turn|bring) down (?:the )?(?:volume|sound)(?: by (?P<n2>\d+)(?: percent)?)?|"
         r"(?:decrease|lower|reduce)(?: the)? (?:volume|sound)(?: by (?P<n3>\d+)(?: percent)?)?|"
         r"(?:make it |a (?:bit|little) )?(?:quieter|softer)"),
     _volume_change(-1)),
    (_rx(r"(?:whats|what is) (?:the |my )?(?:current )?(?:volume|volume level|sound level)|"
         r"(?:check (?:the )?|current )?volume(?: level)?"),
     _const("volume", {}, "Checking the volume.")),
    # --- mute
    (_rx(r"(?:mute|silence)(?: (?:the |my )?(?:sound|audio|volume|speakers?|pc|computer|laptop|everything))?|"
         r"turn off (?:the |my )?(?:sound|audio|speakers?)|turn (?:the |my )?(?:sound|audio|speakers?) off"),
     _const("mute", {"state": "on"}, "Muted.")),
    (_rx(r"un ?mute(?: (?:the |my )?(?:sound|audio|volume|speakers?|pc|computer|laptop|everything))?|"
         r"turn on (?:the |my )?(?:sound|audio|speakers?)|turn (?:the |my )?(?:sound|audio|speakers?) (?:back )?on"),
     _const("mute", {"state": "off"}, "Unmuted.")),
    (_rx(r"toggle (?:mute|sound)"), _const("mute", {"state": "toggle"}, "Mute toggled.")),
    # --- media keys
    (_rx(r"(?:play|resume|unpause)<MEDIA>?|(?:continue|start) (?:the |my )?(?:music|song|track|video|playback)"),
     _const("media_key", {"key": "play_pause"}, "Playing.")),
    (_rx(r"pause<MEDIA>?"), _const("media_key", {"key": "play_pause"}, "Paused.")),
    (_rx(r"stop (?:the |my )?(?:music|song|track|video|playback|media)"),
     _const("media_key", {"key": "stop"}, "Stopped.")),
    (_rx(r"(?:next|skip)(?: (?:this |the )?(?:song|track|video))?|(?:play |go to )?(?:the )?next (?:song|track|video)"),
     _const("media_key", {"key": "next"}, "Next track.")),
    (_rx(r"(?:previous|prev)(?: (?:song|track|video))?|"
         r"(?:play |go to |go back to )?(?:the )?(?:previous|last) (?:song|track|video)|go back (?:a|one|1) (?:song|track)"),
     _const("media_key", {"key": "prev"}, "Previous track.")),
    # --- system status
    (_rx(r"(?:hows|how is|how s) (?:my |the )?(?:pc|computer|laptop|system|machine)(?: doing| running| holding up| looking)?|"
         r"(?:(?:show|check|get|give|run)(?: me)? )?(?:a |the |my )?(?:system|pc|computer|laptop|machine) "
         r"(?:status|health|stats|statistics|info|report|check|diagnostics?)|status(?: report)?|system check|health check|"
         r"(?:(?:whats|what is|show|show me|check|get) )?(?:the |my )?(?:current )?(?:cpu|processor|ram|memory|battery|"
         r"disk|disk space|storage|uptime|temperature|temps?|ip|ip address)(?: usage| use| load| level| status| percentage|"
         r" left| space| temperature)?|"
         r"how much (?:ram|memory|battery|disk space|storage)(?: is| am i| do i have)?(?: being)?(?: used| using| left| free|"
         r" remaining| available| in use)?|how (?:hot|warm) is (?:my |the )?(?:pc|computer|laptop|cpu)|"
         r"how long (?:has )?(?:my |the )?(?:pc|computer|laptop) been (?:on|running|up)"),
     _const("system_status", {}, "Here's your system status.")),
    (_rx(r"(?:(?:whats|what is|show|show me|check|get) )?(?:the |my )?(?:gpu|graphics card|video card|nvidia)"
         r"(?: usage| use| status| stats| load| temp| temperature| info)?|(?:hows|how is) (?:my |the )?gpu(?: doing)?"),
     _const("gpu_status", {}, "Here are your GPU stats.")),
    # --- processes
    (_rx(r"(?:(?:show|list|get|display)(?: me)?(?: the)? )?(?:top |running |active )?(?:processes|procs)"
         r"(?: by (?P<s>cpu|processor|memory|ram|mem))?|"
         r"(?:whats|what is) (?:using|eating|hogging) (?:my |the |all )?(?:the )?(?P<s2>cpu|processor|memory|ram)|"
         r"what processes are running|which (?:apps|programs|processes) (?:are )?(?:using|eating|hogging) "
         r"(?:the most |my |the )?(?P<s3>cpu|processor|memory|ram)"),
     _procs_sort),
    # --- open windows
    (_rx(r"(?:show|list|get|display|what are)(?: me)?(?: all)?(?: the| my)? (?:open |running )?(?:windows|apps|applications|"
         r"programs)(?: (?:that are )?(?:open|running))?|what (?:windows|apps|applications|programs) (?:are|do i have) "
         r"(?:open|running)|whats open(?: on (?:my |the )?(?:pc|computer|screen))?"),
     _const("list_windows", {}, "Here are your open windows.")),
    # --- clipboard
    (_rx(r"(?:read|show|get|check|tell)(?: me)? (?:the |my )?clipboard(?: contents?| text)?|"
         r"(?:whats|what is) (?:on|in) (?:the |my )?clipboard|what did i (?:just )?copy"),
     _const("clipboard_get", {}, "Here's your clipboard.")),
]


# ---------------------------------------------------------------- kill / close apps
# spoken name -> (process name, display name). Only these can be "closed"/"quit"; free-form names need "kill".
_APPS: dict[str, tuple[str, str]] = {}
for _names, _exe, _disp in [
    (("chrome", "google chrome"), "chrome.exe", "Chrome"),
    (("edge", "microsoft edge"), "msedge.exe", "Edge"),
    (("firefox", "mozilla firefox"), "firefox.exe", "Firefox"),
    (("brave", "brave browser"), "brave.exe", "Brave"),
    (("opera",), "opera.exe", "Opera"),
    (("spotify",), "Spotify.exe", "Spotify"),
    (("discord",), "Discord.exe", "Discord"),
    (("steam",), "steam.exe", "Steam"),
    (("vlc", "vlc player", "vlc media player"), "vlc.exe", "VLC"),
    (("notepad",), "notepad.exe", "Notepad"),
    (("notepad plus plus", "notepad++"), "notepad++.exe", "Notepad++"),
    (("vs code", "vscode", "visual studio code", "code"), "Code.exe", "VS Code"),
    (("visual studio",), "devenv.exe", "Visual Studio"),
    (("word", "microsoft word", "ms word"), "WINWORD.EXE", "Word"),
    (("excel", "microsoft excel", "ms excel"), "EXCEL.EXE", "Excel"),
    (("powerpoint", "power point", "microsoft powerpoint"), "POWERPNT.EXE", "PowerPoint"),
    (("outlook", "microsoft outlook"), "OUTLOOK.EXE", "Outlook"),
    (("teams", "microsoft teams", "ms teams"), "ms-teams.exe", "Teams"),
    (("zoom",), "Zoom.exe", "Zoom"),
    (("telegram", "telegram desktop"), "Telegram.exe", "Telegram"),
    (("whatsapp", "whats app"), "WhatsApp.Root.exe", "WhatsApp"),
    (("obs", "obs studio"), "obs64.exe", "OBS"),
    (("task manager",), "Taskmgr.exe", "Task Manager"),
    (("calculator",), "CalculatorApp.exe", "Calculator"),
    (("paint", "ms paint"), "mspaint.exe", "Paint"),
    (("settings", "windows settings"), "SystemSettings.exe", "Settings"),
]:
    for _n in _names:
        _APPS[_n] = (_exe, _disp)

_NEVER_KILL = {  # critical Windows processes, the desktop shell, and this bot's interpreter
    "system", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass", "lsaiso", "svchost",
    "fontdrvhost", "dwm", "explorer", "python", "pythonw", "py", "conhost",
}
_NOT_AN_APP = {
    "it", "this", "that", "them", "all", "everything", "window", "windows", "tab", "tabs", "app", "apps",
    "application", "applications", "program", "programs", "browser", "door", "file", "files", "folder", "popup",
    "message", "computer", "pc", "laptop", "system", "session", "call", "meeting", "game", "task", "tasks", "process",
    "me", "up", "down", "out", "edith", "jarvis", "yourself", "bot", "screen", "monitor", "display", "lid", "lights",
    "light", "time", "music", "song", "video", "sound", "audio", "chat", "conversation", "tab", "page", "machine",
}
_KILL_RX = re.compile(
    r"(?P<verb>kill|close|quit|exit|end task|end|terminate|force close|force quit|force kill|shut down|shutdown)"
    r"(?: (?:the|my|all))?(?: (?P<pre>process|pid|app|program|application))? (?P<app>.+?)"
    r"(?: (?P<noun>app|application|program|process|processes|window|windows|task))?")
_STRONG_VERBS = {"kill", "terminate", "end task", "force kill", "force quit", "force close"}
_TOKEN = re.compile(r"[a-z][a-z0-9_.]{1,40}")


def _kill(n: str) -> Intent | None:
    m = _KILL_RX.fullmatch(n)
    if not m:
        return None
    verb, app = m["verb"], m["app"].strip()
    explicit = verb in _STRONG_VERBS or bool(m["pre"]) or m["noun"] in ("process", "processes")
    if app in _APPS:
        exe, disp = _APPS[app]
        return Intent("kill_process", {"target": exe}, f"Closing {disp}.")
    if app.isdigit():  # PIDs < 100 are always core Windows processes
        if len(app) > 7 or int(app) < 100 or not explicit or verb in ("shut down", "shutdown"):
            return None
        return Intent("kill_process", {"target": app}, f"Killing process {app}.")
    base = app[:-4] if app.endswith(".exe") else app
    if not _TOKEN.fullmatch(app) or base in _NEVER_KILL or base in _NOT_AN_APP or base in _SMALL:
        return None
    if not (explicit or app.endswith(".exe")):
        return None  # "close foo" for an unknown foo is too vague for a dangerous action
    exe = app if app.endswith(".exe") else f"{app}.exe"
    return Intent("kill_process", {"target": exe}, f"Killing {exe}.")


# ---------------------------------------------------------------- raw-text matchers (payload keeps its case)
_SAY_RX = re.compile(
    r"(?:say|speak|announce)(?: out loud| aloud)?(?: on (?:the |my )?(?:pc|computer|laptop|speakers?))?[,:]?\s+"
    r"(?P<text>.+?)(?:\s+(?:out loud|aloud|on (?:the |my )?(?:pc|computer|laptop|speakers?)))?", re.I | re.S)
_SAY_REJECT = re.compile(r"(?:something|anything|a joke|a poem|a story|that|it|what|again|up|louder|more|"
                         r"(?:hi|hello|hey|goodbye|bye|sorry|thanks|thank you) to .*|that again|it again|"
                         r"something .*|anything .*|a joke .*|a poem .*|a story .*)", re.I)
_POPUP_RX = re.compile(
    r"(?:(?:show|display|put up|pop up|open|create)(?: me)?(?: an?)? (?:popup|pop-up|pop up|message box|message|alert|"
    r"dialog|notification)|popup|pop-up|pop up)(?: on (?:the |my )?(?:pc|screen|computer|laptop|desktop))?"
    r"(?:\s+(?:saying|that says|which says|reading|with(?: the)? text|with))?[,:]?\s+(?P<text>.+)", re.I | re.S)
_QUOTES = "\"'“”‘’`"


def _payload(text: str) -> str:
    return text.strip().strip(_QUOTES).strip()


def _say(c: str) -> Intent | None:
    m = _SAY_RX.fullmatch(c)
    if not m:
        return None
    text = _payload(m["text"])
    if not text or len(text) > 500 or _SAY_REJECT.fullmatch(text):
        return None
    return Intent("say", {"text": text}, "Saying it on the PC.")


def _popup(c: str) -> Intent | None:
    m = _POPUP_RX.fullmatch(c)
    if not m:
        return None
    text = _payload(m["text"])
    if not text or len(text) > 500:
        return None
    return Intent("show_message", {"text": text}, "Showing the message on the PC.")


_TIME = r"(?P<h>\d{1,2})(?:[: ](?P<m>[0-5]\d))? ?(?P<ap>[ap]\.? ?m\.?)?|(?P<noon>noon|midnight)"
_HEAD = r"(?:remind me|set (?:a |an )?reminder|create (?:a )?reminder|make (?:a )?reminder|add (?:a )?reminder)"
_WHEN_DUR = rf"(?:in|after|for) (?P<dur>{_DUR})"
_WHEN_AT = rf"(?:(?P<day>today|tomorrow|tonight) )?at (?:{_TIME})(?: (?P<day2>today|tomorrow|tonight))?"
_LINK = r"(?:to|that|about|of)"
_REMIND_RXS = [re.compile(p, re.I | re.S) for when in (_WHEN_DUR, _WHEN_AT) for p in (
    rf"{_HEAD}(?: (?:for|in))? {when} {_LINK} (?P<text>.+)",
    rf"{_HEAD} {_LINK} (?P<text>.+?) {when}",
    rf"{when} {_HEAD} {_LINK} (?P<text>.+)",
)]


def _reminder_when(m: re.Match) -> tuple[str, str] | None:
    """Returns (scheduler 'when' string, spoken phrase) or None if ambiguous."""
    gd = m.groupdict()
    if gd.get("dur"):
        secs = _dur_seconds(gd["dur"])
        if not 1 <= secs <= 30 * 86_400:
            return None
        return _compact_secs(secs), f"in {_say_secs(secs)}"
    day = (gd.get("day") or gd.get("day2") or "").lower()
    if gd.get("noon"):
        hh, mm, ap = (12, 0, "pm") if gd["noon"].lower() == "noon" else (0, 0, None)
        clock = "12pm" if ap else "0:00"
    else:
        hh, mm = int(gd["h"]), int(gd["m"]) if gd.get("m") else None
        ap = gd["ap"].lower().replace(".", "").replace(" ", "") if gd.get("ap") else None
        if ap is None and day == "tonight" and 1 <= hh <= 11:
            ap = "pm"
        if ap:
            if not 1 <= hh <= 12:
                return None
            clock = f"{hh}:{mm:02d}{ap}" if mm is not None else f"{hh}{ap}"
        else:
            if mm is None or hh > 23:
                return None  # "at 9" - am or pm? let the LLM ask
            clock = f"{hh}:{mm:02d}"
    prefix = "tomorrow " if day == "tomorrow" else ("today " if day == "today" else "")
    return prefix + clock, f"{prefix}at {clock}".strip()


def _remind(c: str) -> Intent | None:
    c = re.sub(r"\s*[,;]\s*", " ", _words_to_numbers(c))
    for rx in _REMIND_RXS:
        m = rx.fullmatch(c)
        if not m:
            continue
        text = _payload(m["text"]).rstrip(".!?")
        when = _reminder_when(m)
        if not text or len(text) > 300 or when is None:
            return None
        return Intent("create_reminder", {"when": when[0], "text": text}, f"Okay, I'll remind you {when[1]}: {text}.")
    return None


_SITES = {
    "youtube": "https://www.youtube.com", "you tube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "google mail": "https://mail.google.com", "github": "https://github.com",
    "git hub": "https://github.com", "x": "https://x.com", "twitter": "https://x.com", "reddit": "https://www.reddit.com",
    "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com",
    "linkedin": "https://www.linkedin.com", "linked in": "https://www.linkedin.com",
    "wikipedia": "https://www.wikipedia.org", "amazon": "https://www.amazon.com", "netflix": "https://www.netflix.com",
    "chatgpt": "https://chatgpt.com", "chat gpt": "https://chatgpt.com", "claude": "https://claude.ai",
    "stack overflow": "https://stackoverflow.com", "stackoverflow": "https://stackoverflow.com",
    "google maps": "https://maps.google.com", "maps": "https://maps.google.com",
    "google drive": "https://drive.google.com", "google docs": "https://docs.google.com",
    "google translate": "https://translate.google.com", "google calendar": "https://calendar.google.com",
    "google news": "https://news.google.com", "twitch": "https://www.twitch.tv", "pinterest": "https://www.pinterest.com",
    "bing": "https://www.bing.com", "duckduckgo": "https://duckduckgo.com", "hacker news": "https://news.ycombinator.com",
    "whatsapp web": "https://web.whatsapp.com", "youtube music": "https://music.youtube.com",
    "outlook web": "https://outlook.live.com", "hotstar": "https://www.hotstar.com", "flipkart": "https://www.flipkart.com",
    "prime video": "https://www.primevideo.com", "quora": "https://www.quora.com",
}
_TLDS = set("com org net io dev ai app co me tv gg in uk us ca de fr jp edu gov info xyz so sh ly be it es nl ru au "
            "br to fm live news tech site online store blog page cloud art".split())
_OPEN_RX = re.compile(
    r"(?:open(?: up)?|go to|goto|visit|navigate to|browse to|take me to|pull up|bring up|load|launch)\s+(?:the\s+)?"
    r"(?P<site>.+?)(?:\s+(?:website|web site|site|homepage|home page|web page))?"
    r"(?:\s+(?:in|on)\s+(?:the\s+|my\s+)?(?:browser|chrome|edge|brave|firefox))?", re.I)
_DOMAIN_RX = re.compile(r"(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?P<tld>[a-z]{2,24})(?:/\S*)?", re.I)
_URL_RX = re.compile(r"https?://[^\s/]+\.[^\s]+", re.I)
_SEARCH_RXS = [
    (re.compile(r"(?:search|look up|find)(?: on)? youtube for (?P<q>.+)|(?:search|look up|find) (?P<q2>.+?) on youtube|"
                r"youtube search(?: for)? (?P<q3>.+)", re.I), "https://www.youtube.com/results?search_query="),
    (re.compile(r"(?:search|look up|find)(?: on)? (?:google|the web|online|the internet) for (?P<q>.+)|"
                r"(?:search|look up) (?P<q2>.+?) on google|google search(?: for)? (?P<q3>.+)|google (?P<q4>.+)", re.I),
     "https://www.google.com/search?q="),
]


def _site_url(site: str) -> str | None:
    s = re.sub(r"\s+dot\s+", ".", site.strip(), flags=re.I).strip(_EDGE_PUNCT)
    s = re.sub(r"\s*\.\s*", ".", s)
    low = s.lower()
    if low in _SITES:
        return _SITES[low]
    if _URL_RX.fullmatch(s):
        return s
    m = _DOMAIN_RX.fullmatch(s)
    if m and m["tld"].lower() in _TLDS:
        host, _, path = s.partition("/")
        return f"https://{host.lower()}" + (f"/{path}" if path else "")
    return None


def _open(c: str) -> Intent | None:
    c = _strip_trailing(c)
    for rx, base in _SEARCH_RXS:
        m = rx.fullmatch(c)
        if m:
            q = _payload(next(v for v in m.groupdict().values() if v))
            if base.startswith("https://www.google") and f"google {q.lower()}" in _SITES and not m["q"]:
                url = _SITES[f"google {q.lower()}"]  # "google maps" = open Maps, not a search for "maps"
                return Intent("open_url", {"url": url}, f"Opening Google {q.title()}.")
            if not q or len(q) > 200 or q.lower() in ("chrome", "it", "that", "this"):
                return None
            where = "YouTube" if "youtube" in base else "Google"
            return Intent("open_url", {"url": base + quote_plus(q)}, f"Searching {where} for {q}.")
    m = _OPEN_RX.fullmatch(c)
    if not m:
        return None
    url = _site_url(m["site"])
    if url is None:
        app = m["site"].strip()
        # Only app verbs ("open/launch/start/run brave"); "go to sleep" or "visit x" are never apps.
        # A plain app name ("brave", "vs code"): open_app itself checks the app is installed.
        if re.match(r"\s*(?:open(?: up)?|launch|start|run)\s", c, re.I) \
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9+ ]{1,30}", app) and len(app.split()) <= 3 \
                and not re.search(r"\b(my|the|a|an|this|that|it|and|then|after|also|file|files|folder|"
                                  r"document|documents|downloads|desktop|pictures|sleep|bed)\b", app, re.I):
            return Intent("open_app", {"name": app}, f"Opening {app}.")
        return None  # "open my files", "open report.pdf" ... -> LLM
    name = m["site"].strip()
    return Intent("open_url", {"url": url}, f"Opening {name if name.lower() in _SITES else url.split('://', 1)[1]}.")


_RAW_MATCHERS = (_say, _popup, _remind, _open)


# ---------------------------------------------------------------- public API
def _match_clause(clause: str) -> Intent | None:
    c = _strip_leading(_TRAILING_WAKE.sub("", clause.strip()))
    if not c:
        return None
    for fn in _RAW_MATCHERS:
        hit = fn(c)
        if hit:
            return hit
    n = _norm(c)
    if not n:
        return None
    for rx, build in _NORM_PATTERNS:
        m = rx.fullmatch(n)
        if m:
            return build(m)
    return _kill(n)


def match(text: str) -> list[Intent] | None:
    """Instant intents for `text`, or None to let the LLM handle it (see module docstring)."""
    if not text or not text.strip() or len(text) > MAX_INPUT:
        return None
    text = re.sub(r"\s+", " ", text.replace("’", "'")).strip()
    pre = _strip_leading(text)
    if not pre:
        return None
    whole = _match_clause(pre)
    parts = [p for p in _SPLIT.split(pre) if p and p.strip(_EDGE_PUNCT)]
    multi: list[Intent] | None = None
    if 1 < len(parts) <= MAX_PARTS:
        multi = []
        for p in parts:
            hit = _match_clause(p)
            if hit is None:
                multi = None
                break
            multi.append(hit)
    if whole is not None and multi is not None:
        return None  # "say hello and lock the pc": one payload, or two commands? don't guess
    result = [whole] if whole is not None else multi
    if not result:
        return None
    terminal = [i for i, it in enumerate(result) if it.action in _TERMINAL]
    if len(terminal) > 1 or (terminal and terminal[0] != len(result) - 1):
        return None  # "shut down and restart", "sleep then open youtube" -> nonsense order
    deduped = [it for n, it in enumerate(result)  # "cpu and ram usage" -> one status call, not two
               if not any(p.action == it.action and p.args == it.args for p in result[:n])]
    return deduped
