"""Command-line control panel:  python -m bot.cli [command]   (or control.bat [command])

    (no args)            interactive menu
    setup                credentials wizard (bot token, owner auto-detect, AI brain, voice)
    start                run the bot in this window
    doctor               check everything and say what to fix
    config show          print settings (secrets masked)
    config set KEY VAL   change one setting
    autostart on|off|status   visible Task Scheduler entry at logon
    logs [--audit] [-n N]     tail bot.log or audit.log

Must NOT import bot.config at module import time: it exits when .env is missing.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

APP_NAME = "Edith"
TASK_NAME = APP_NAME
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TG_API = "https://api.telegram.org/bot{token}/{method}"
SECRET_KEYS = {"BOT_TOKEN", "GROQ_API_KEY", "NVIDIA_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"}
KNOWN_KEYS = ["BOT_TOKEN", "ALLOWED_USER_IDS", "SHELL_TIMEOUT", "UPLOAD_DIR", "LLM_PROVIDER",
              "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
              "WHISPER_MODEL", "TTS_VOICE"]

# provider presets: key -> (label, LLM_PROVIDER, base_url, default model, key needed?)
# One setup for everyone: Groq (free key from console.groq.com) powers both the AI brain and
# speech-to-text (hosted whisper-large-v3). Advanced users can still point LLM_* elsewhere
# with `control config set`.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-120b"  # measured: correct tool calls on every test request, ~1-4 s

for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252: ✓/… would crash
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
con = Console()


# ------------------------------------------------------------------ .env helpers
def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def write_env(updates: dict[str, str], path: Path = ENV_FILE) -> None:
    """Update/insert keys while keeping comments, order and unrelated keys."""
    for k, v in updates.items():
        if not re.fullmatch(r"[A-Z0-9_]+", k) or "\n" in v or "\r" in v:
            raise ValueError(f"invalid setting {k!r} (keys A-Z0-9_, values single-line)")
    lines =path.read_text(encoding="utf-8").splitlines() if path.exists() else [
        f"# {APP_NAME} settings - keep this file private (it holds your bot token)."]
    pending = dict(updates)
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    restrict_to_user(path)


def restrict_to_user(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    user = os.environ.get("USERNAME", "")
    if user:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                       capture_output=True, check=False)


def mask(key: str, value: str) -> str:
    if key not in SECRET_KEYS or not value:
        return value
    return value[:4] + "…" + value[-3:] if len(value) > 10 else "•••"


# ------------------------------------------------------------------ Telegram helpers
def tg(token: str, method: str, **params) -> dict:
    r = httpx.get(TG_API.format(token=token, method=method), params=params, timeout=35)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"HTTP {r.status_code}"))
    return data["result"]


def check_token(token: str) -> str | None:
    """Return the bot's @username if the token works, else None."""
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
        return None
    try:
        return tg(token, "getMe")["username"]
    except Exception:
        return None


def detect_owner(token: str, bot_username: str, wait_s: int = 180) -> int | None:
    """Wait for someone to message the bot; let the user confirm it's them."""
    con.print(f"\nOn your phone open [bold]https://t.me/{bot_username}[/] and press [bold]START[/].")
    try:
        tg(token, "deleteWebhook")
    except Exception:
        pass
    offset, deadline = 0, time.monotonic() + wait_s
    with con.status("Waiting for your message (up to 3 min)…") as spinner:
        while time.monotonic() < deadline:
            try:
                updates = tg(token, "getUpdates", offset=offset, timeout=20)
            except Exception:
                time.sleep(2)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                frm = (u.get("message") or {}).get("from") or {}
                if frm and not frm.get("is_bot"):
                    who = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
                    if frm.get("username"):
                        who += f" (@{frm['username']})"
                    spinner.stop()
                    if Confirm.ask(f"Message from [bold]{who}[/], id {frm['id']}. Is this you?", default=True):
                        try:  # mark updates as read so the bot doesn't replay them
                            tg(token, "getUpdates", offset=offset, timeout=0)
                        except Exception:
                            pass
                        return int(frm["id"])
                    spinner.start()
    return None


# ------------------------------------------------------------------ commands
def cmd_setup(_: argparse.Namespace | None = None) -> int:
    env = read_env()
    con.print(Panel.fit(f"[bold]{APP_NAME} - setup[/]\nControl this PC from Telegram on your phone.\n"
                        "Only the Telegram account you link here can use it.", border_style="cyan"))

    # 1. bot token
    token, bot_user = env.get("BOT_TOKEN", ""), None
    if token and (bot_user := check_token(token)) and Confirm.ask(f"Keep current bot @{bot_user}?", default=True):
        pass
    else:
        con.print("\n[bold]1. Telegram bot[/]\n  In Telegram open [bold]@BotFather[/], send /newbot, choose a name and a "
                  "username ending in 'bot'.\n  It replies with a token like 123456789:AAH…  (keep it secret)")
        bot_user = None
        while not bot_user:
            token = getpass.getpass("  Paste the bot token (hidden): ").strip()
            bot_user = check_token(token)
            if not bot_user:
                con.print("  [red]Telegram rejected that token[/] - copy it again from @BotFather.")
        con.print(f"  [green]✓[/] token works - your bot is [bold]@{bot_user}[/]")

    # 2. owner
    con.print("\n[bold]2. Link your Telegram account[/]")
    ids = env.get("ALLOWED_USER_IDS", "")
    if ids and Confirm.ask(f"Keep allowed users [bold]{ids}[/]?", default=True):
        pass
    else:
        owner = detect_owner(token, bot_user)
        if owner is None:
            con.print("  No message received. Get your numeric id from [bold]@userinfobot[/].")
            owner = IntPrompt.ask("  Your Telegram user id")
        extra = Prompt.ask("  Extra allowed user ids, comma separated (Enter = none)", default="")
        ids = ",".join([str(owner)] + [x for x in re.split(r"[,\s]+", extra) if x.isdigit()])
        con.print(f"  [green]✓[/] allowed: {ids}")

    # 3. AI brain + speech: one Groq key does both
    con.print("\n[bold]3. Groq key[/] (free: powers Edith's AI brain and voice recognition)\n"
              "  Open [bold]https://console.groq.com/keys[/], sign in, click [bold]Create API Key[/]. "
              "It starts with gsk_")
    upd = {"BOT_TOKEN": token, "ALLOWED_USER_IDS": ids}
    key = env.get("GROQ_API_KEY", "") or env.get("LLM_API_KEY", "")
    if not (key.startswith("gsk_") and Confirm.ask("Keep the Groq key already saved?", default=True)):
        key = ""
    while True:
        if not key:
            key = getpass.getpass("  Paste the Groq key (hidden, Enter to skip): ").strip()
        if not key:
            con.print("  [yellow]Skipped[/] - instant commands and slash commands work; "
                      "add the key later with: control setup")
            break
        probe_env = {"LLM_BASE_URL": GROQ_BASE_URL, "LLM_MODEL": GROQ_MODEL, "LLM_API_KEY": key}
        with con.status("Testing the key with a real question…"):
            ok, detail = probe_llm(probe_env, timeout=40)
        if ok:
            con.print(f"  [green]✓[/] {detail}")
            break
        con.print(f"  [red]That did not work:[/] {detail}")
        key = ""
    if key:
        upd |= {"LLM_PROVIDER": "openai_compat", "LLM_BASE_URL": GROQ_BASE_URL, "GROQ_API_KEY": key,
                "LLM_API_KEY": key, "LLM_MODEL": GROQ_MODEL, "STT_PROVIDER": "groq"}

    # sensible defaults, no questions (change later with: control config set KEY VALUE)
    upd["TTS_VOICE"] = env.get("TTS_VOICE") or "en-GB-SoniaNeural"
    upd["UPLOAD_DIR"] = env.get("UPLOAD_DIR") or str(Path.home() / "Downloads" / "TelegramUploads")
    upd["SHELL_TIMEOUT"] = env.get("SHELL_TIMEOUT") or "60"
    upd["WHISPER_MODEL"] = env.get("WHISPER_MODEL") or "small"

    if ENV_FILE.exists():
        (ROOT / ".env.bak").write_text(ENV_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        restrict_to_user(ROOT / ".env.bak")
    write_env(upd)
    con.print("\n[green]✓ Saved .env[/] (readable only by your Windows account)")

    if Confirm.ask("Start automatically when you log in? (visible Task Scheduler entry)", default=False):
        cmd_autostart(argparse.Namespace(state="on"))
    if Confirm.ask("Start the bot now?", default=True):
        return cmd_start()
    con.print("Run [bold]control.bat start[/] (or start.bat) whenever you want to use it.")
    return 0


_LOCK_HANDLE = None  # kept open for the life of the process


def acquire_single_instance(lock_path: Path | None = None) -> bool:
    """True if this is the only running bot. Two bots on one token fight over Telegram updates
    ("Conflict: terminated by other getUpdates request") and commands get lost."""
    global _LOCK_HANDLE
    lock_path = lock_path or (ROOT / "data" / "edith.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _LOCK_HANDLE = fh
    return True


def is_local_ollama(base_url: str) -> bool:
    return bool(re.match(r"^https?://(localhost|127\.0\.0\.1):11434(/|$)", base_url.strip()))


def ollama_up(timeout: float = 2.0) -> bool:
    try:
        return httpx.get("http://localhost:11434/api/version", timeout=timeout).status_code == 200
    except Exception:
        return False


def ensure_ollama(model: str, wait_s: int = 30) -> str | None:
    """Start the local Ollama server if needed and check the model is downloaded.
    Returns None when ready, else a human-readable problem."""
    if not ollama_up():
        exe = shutil.which("ollama")
        if not exe:
            return "Ollama is not installed. Get it from https://ollama.com/download"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and not ollama_up():
            time.sleep(1)
        if not ollama_up():
            return "Ollama did not start. Open the Ollama app, then start Edith again."
    try:
        tags = httpx.get("http://localhost:11434/api/tags", timeout=5).json().get("models", [])
    except Exception as e:
        return f"Could not list Ollama models: {e}"
    names = {m.get("name", "") for m in tags} | {m.get("name", "").removesuffix(":latest") for m in tags}
    if model and model not in names:
        return f"Model {model!r} is not downloaded. Run: ollama pull {model}"
    return None


def probe_llm(env: dict[str, str], timeout: float = 45.0) -> tuple[bool, str]:
    """Send one tiny real chat request. Reachable-but-hanging models must not look healthy."""
    base, model = env.get("LLM_BASE_URL", "").rstrip("/"), env.get("LLM_MODEL", "")
    key = env.get("LLM_API_KEY", "")
    t0 = time.monotonic()
    try:
        r = httpx.post(base + "/chat/completions", timeout=timeout,
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": model,
                             "messages": [{"role": "user", "content": "Reply with the single word: ready"}]})
    except httpx.TimeoutException:
        return False, f"{model}: no answer within {timeout:.0f}s (model hangs - pick another LLM_MODEL)"
    except Exception as e:
        return False, f"{model}: cannot connect to {base} ({type(e).__name__})"
    if r.status_code != 200:
        detail = r.text[:140].replace("\n", " ")
        if key:
            detail = detail.replace(key, "<key>")
        return False, f"{model}: HTTP {r.status_code} {detail}"
    return True, f"{model} answered in {time.monotonic() - t0:.1f}s @ {base}"


# ------------------------------------------------------------------ start + watchdog
# `control start` is a small supervisor: it runs the bot as a child process (same console, EDITH_CHILD=1)
# and starts it again when it asks to (/restart -> exit 3) or crashes. Only the child holds the
# single-instance lock, so a restarted child can take it again.
EXIT_RESTART = 3          # child asks for a restart (bot.modules.ops /restart)
EXIT_ALREADY_RUNNING = 4  # another Edith holds the lock: retrying can't help
STOP_CODES = {0, 130, 0xC000013A, -2}  # clean exit, Ctrl+C (cli / Windows STATUS_CONTROL_C_EXIT / SIGINT)
CRASH_BACKOFF = (5, 15, 60)  # seconds before restart after the 1st, 2nd, 3rd+ crash
MAX_CRASHES = 5              # ... within CRASH_WINDOW seconds -> give up
CRASH_WINDOW = 600
WATCHDOG_LOG = ROOT / "logs" / "bot.log"


def _watchdog_log(message: str, level: str = "WARNING") -> None:
    """Append one line to logs/bot.log (same format as the bot's own lines). Opened and closed per
    line so it never blocks the bot's log rotation."""
    try:
        WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S") + f",{int(time.time() * 1000) % 1000:03d}"
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {level} watchdog: {message}\n")
    except OSError:
        pass


def _spawn_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "bot.cli", "start"], cwd=ROOT,
                            env={**os.environ, "EDITH_CHILD": "1"})


def _wait(proc) -> int:
    """Wait for the child, waking every second so Ctrl+C reaches the supervisor promptly on Windows."""
    while True:
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            continue


def _stop_child(proc, grace: float = 20.0) -> None:
    """Ctrl+C went to the whole console, so the child is already shutting down: give it time,
    then terminate it if it hangs (or on a second Ctrl+C)."""
    try:
        proc.wait(timeout=grace)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()


def supervise(spawn=_spawn_child, sleep=time.sleep, clock=time.monotonic) -> int:
    """Run the bot child until it stops on purpose; restart it on request or after a crash."""
    crashes: list[float] = []
    while True:
        proc = spawn()
        try:
            rc = _wait(proc)
        except KeyboardInterrupt:
            _stop_child(proc)
            con.print(f"\n{APP_NAME} stopped.")
            return 0
        if rc == EXIT_RESTART:
            _watchdog_log("restart requested - starting Edith again", "INFO")
            con.print(f"[cyan]↻ Restarting {APP_NAME}…[/]")
            continue
        if rc in STOP_CODES:
            return 0
        if rc == EXIT_ALREADY_RUNNING:
            return 1  # the child already explained
        now = clock()
        crashes = [t for t in crashes if now - t < CRASH_WINDOW] + [now]
        if len(crashes) >= MAX_CRASHES:
            why = (f"Edith crashed {len(crashes)} times in {CRASH_WINDOW // 60} minutes (last exit code {rc}) - "
                   "giving up so it doesn't loop forever. See logs/bot.log, fix the problem, then start it again.")
            _watchdog_log(why, "ERROR")
            con.print(f"[red]✗ {why}[/]")
            return 1
        delay = CRASH_BACKOFF[min(len(crashes), len(CRASH_BACKOFF)) - 1]
        _watchdog_log(f"Edith stopped unexpectedly (exit code {rc}); restarting in {delay}s "
                      f"(crash {len(crashes)} of {MAX_CRASHES} allowed in {CRASH_WINDOW // 60} min)")
        con.print(f"[yellow]! {APP_NAME} stopped unexpectedly (exit code {rc}). Restarting in {delay}s…[/] "
                  "(Ctrl+C to stop)")
        try:
            sleep(delay)
        except KeyboardInterrupt:
            con.print(f"\n{APP_NAME} stopped.")
            return 0


def cmd_start(_: argparse.Namespace | None = None) -> int:
    env = read_env()
    if not env.get("BOT_TOKEN") or not env.get("ALLOWED_USER_IDS"):
        con.print("[red]Not configured yet.[/] Running setup first…")
        return cmd_setup()
    if not os.environ.get("EDITH_CHILD"):
        return supervise()
    return _run_bot(env)


def _run_bot(env: dict[str, str]) -> int:
    """The bot process itself (EDITH_CHILD=1): lock, local AI, banner, polling."""
    if not acquire_single_instance():
        con.print("[red]Edith is already running[/] in another window. Close that one first "
                  "(two copies fight over Telegram messages).")
        return EXIT_ALREADY_RUNNING
    if env.get("LLM_PROVIDER") == "openai_compat" and is_local_ollama(env.get("LLM_BASE_URL", "")):
        with con.status("Starting the local AI model (Ollama)…"):
            problem = ensure_ollama(env.get("LLM_MODEL", ""))
        if problem:
            con.print(f"[yellow]![/] AI brain: {problem}\n  Instant voice commands and slash commands still work.")
        else:
            con.print(f"[green]✓[/] Local AI ready: {env.get('LLM_MODEL')}")
    con.print(Panel.fit(f"[bold green]{APP_NAME} is running[/]\nOpen your bot on your phone and send /help.\n"
                        "Restarts by itself if it crashes. Press Ctrl+C or close this window to stop.",
                        border_style="green"))
    from . import main as bot_main  # imported late: loads config from .env
    try:
        bot_main.main()
    except KeyboardInterrupt:
        return 0
    except Exception:  # logged to bot.log (traceback included); the watchdog restarts us
        import logging
        logging.getLogger("controller").exception("Edith crashed")
        return 1
    from .modules import ops
    return EXIT_RESTART if ops.restart_requested() else 0


def _row(t: Table, ok: bool | None, what: str, detail: str = "") -> None:
    mark = {True: "[green]✓[/]", False: "[red]✗[/]", None: "[yellow]![/]"}[ok]
    t.add_row(mark, what, detail)


def cmd_doctor(_: argparse.Namespace | None = None) -> int:
    t = Table(title=f"{APP_NAME} - doctor", show_header=False, box=None)
    env = read_env()
    _row(t, sys.version_info[:2] == (3, 12), "Python", sys.version.split()[0] + " (3.12 expected)")
    _row(t, ENV_FILE.exists(), ".env", "found" if ENV_FILE.exists() else "missing - run setup")
    user = check_token(env.get("BOT_TOKEN", "")) if env.get("BOT_TOKEN") else None
    _row(t, bool(user), "Bot token", f"@{user}" if user else "missing or rejected by Telegram")
    ids = env.get("ALLOWED_USER_IDS", "")
    _row(t, bool(re.fullmatch(r"\d+(,\d+)*", ids)), "Allowed users", ids or "none")

    prov = env.get("LLM_PROVIDER", "")
    if prov == "anthropic":
        _row(t, bool(env.get("ANTHROPIC_API_KEY")), "AI brain", f"Claude {env.get('CLAUDE_MODEL', '')}")
    elif prov == "openai_compat":
        problem = None
        if is_local_ollama(env.get("LLM_BASE_URL", "")):
            problem = ensure_ollama(env.get("LLM_MODEL", ""))
        if problem:
            _row(t, False, "AI brain", problem)
        else:
            with con.status("Asking the AI model a test question…"):
                ok, detail = probe_llm(env)
            _row(t, ok, "AI brain", detail)
    else:
        _row(t, None, "AI brain", "not configured (Edith voice mode off)")

    try:
        import ctypes

        import ctranslate2

        from .assistant.voice import _add_nvidia_dll_dirs
        _add_nvidia_dll_dirs()
        cuda = ctranslate2.get_cuda_device_count()
        libs = True
        if cuda and os.name == "nt":
            try:
                ctypes.WinDLL("cublas64_12.dll")
            except OSError:
                libs = False
        if cuda and libs:
            _row(t, True, "Speech-to-text", "GPU (CUDA)")
        elif cuda:
            _row(t, None, "Speech-to-text", "CPU - NVIDIA GPU found but CUDA libs missing: "
                 "uv pip install -r requirements-gpu.txt")
        else:
            _row(t, None, "Speech-to-text", "CPU (slower, works)")
    except Exception as e:
        _row(t, False, "Speech-to-text", f"faster-whisper unavailable: {e}")

    if ENV_FILE.exists() and user:
        os.environ.setdefault("BOT_TOKEN", env["BOT_TOKEN"])
        try:
            from .actions.registry import load_all
            n = len(load_all())
            _row(t, n > 0, "Actions", f"{n} registered")
        except BaseException as e:  # config may sys.exit
            _row(t, False, "Actions", str(e))
    from .actions import policy
    from . import pin as _pin
    _row(t, True, "Guardrails", f"{policy.summary()}, {_pin.pin_status()}")
    auto = _task_exists()
    _row(t, None if not auto else True, "Autostart", "on" if auto else "off")
    con.print(t)
    return 0


def cmd_config(ns: argparse.Namespace) -> int:
    env = read_env()
    if ns.action == "set":
        key = ns.key.upper()
        if key not in KNOWN_KEYS and not Confirm.ask(f"{key} is not a known setting. Set anyway?", default=False):
            return 1
        write_env({key: ns.value})
        con.print(f"[green]✓[/] {key} = {mask(key, ns.value)}  (restart the bot to apply)")
        return 0
    t = Table(show_header=True)
    t.add_column("Setting")
    t.add_column("Value")
    for k in KNOWN_KEYS + sorted(set(env) - set(KNOWN_KEYS)):
        t.add_row(k, mask(k, env.get(k, "")) or "[dim]-[/]")
    con.print(t)
    return 0


def _task_exists() -> bool:
    if os.name != "nt":
        return False
    return subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True).returncode == 0


def cmd_autostart(ns: argparse.Namespace) -> int:
    if ns.state == "status":
        con.print("Autostart is " + ("[green]on[/]" if _task_exists() else "off"))
        return 0
    if ns.state == "off":
        if not _task_exists():
            con.print("Autostart was not on.")
            return 0
        r = _ps(f"Unregister-ScheduledTask -TaskName {_q(TASK_NAME)} -Confirm:$false")
        con.print("[green]✓ autostart removed[/]" if r.returncode == 0
                  else f"[red]Could not remove the task:[/] {r.stderr.strip()}")
        return r.returncode
    r = _ps(autostart_script(ROOT / "start.bat", ROOT))
    if r.returncode == 0 and _task_exists():
        con.print(f"[green]✓[/] '{TASK_NAME}' will start 30 s after you sign in (visible in Task Scheduler; "
                  "remove with: control autostart off)")
        return 0
    con.print(f"[red]Could not create the task:[/] {(r.stderr or r.stdout).strip()[-400:]}")
    return 1


def _q(s: object) -> str:
    """PowerShell single-quoted literal."""
    return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", str(s)) + "'"


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                           "$ErrorActionPreference='Stop'; " + script],
                          capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def autostart_script(start: Path, workdir: Path) -> str:
    """Register a per-user logon task. Unlike `schtasks /SC ONLOGON` this needs no admin rights.
    It also runs on battery, is never killed after 72 h, and waits 30 s so the network is up."""
    return "; ".join([
        "$u = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        f"$a = New-ScheduledTaskAction -Execute {_q(start)} -WorkingDirectory {_q(workdir)}",
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $u",
        "$t.Delay = 'PT30S'",
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew",
        "$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName {_q(TASK_NAME)} -Action $a -Trigger $t -Settings $s -Principal $p "
        f"-Description {_q('Starts Edith (Telegram PC control) when you sign in. Remove: control autostart off')} "
        "-Force | Out-Null",
    ])


def cmd_logs(ns: argparse.Namespace) -> int:
    path = ROOT / "logs" / ("audit.log" if ns.audit else "bot.log")
    if not path.exists():
        con.print(f"No {path.name} yet.")
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-ns.n:]
    con.print("\n".join(lines), markup=False, highlight=False)
    return 0


def cmd_pin(ns: argparse.Namespace) -> int:
    from . import pin
    if ns.state == "status":
        os.environ["EDITH_PIN_HASH"] = read_env().get("EDITH_PIN_HASH", "")
        con.print(pin.pin_status())
        return 0
    if ns.state == "off":
        write_env({"EDITH_PIN_HASH": ""})
        con.print("[green]✓[/] PIN removed. Restart Edith (or send /restart) to apply.")
        return 0
    import getpass
    first = getpass.getpass("New PIN (4-8 digits, hidden): ").strip()
    if getpass.getpass("Repeat the PIN: ").strip() != first:
        con.print("[red]The PINs don't match.[/] Nothing changed.")
        return 1
    try:
        value = pin.make_pin_hash(first)
    except ValueError as e:
        con.print(f"[red]{e}[/]")
        return 1
    write_env({"EDITH_PIN_HASH": value})
    con.print("[green]✓[/] PIN set (only a salted hash is stored). Shell, installs, deleting, automations and "
              "shutdown/restart now need it on a keypad in Telegram. Restart Edith (or send /restart) to apply.")
    return 0


def cmd_lockdown(ns: argparse.Namespace) -> int:
    from .actions import policy
    info = policy.lockdown_info()
    if ns.state == "status":
        con.print(f"[red]LOCKDOWN ON[/] since {info.get('since', '?')} ({info.get('reason', '')})" if info
                  else "No lockdown.")
        return 0
    if ns.state == "on":
        policy.set_lockdown(True, reason="turned on at the PC")
        con.print("[red]Lockdown ON.[/] Edith refuses everything except status, volume and lock.")
        return 0
    policy.set_lockdown(False)
    con.print("[green]✓ Lockdown lifted.[/]" if info else "There was no lockdown.")
    return 0


def menu() -> int:
    options = [("Start the bot", cmd_start), ("Setup / change credentials", cmd_setup),
               ("Check everything (doctor)", cmd_doctor),
               ("Show settings", lambda: cmd_config(argparse.Namespace(action="show"))),
               ("Toggle autostart at login",
                lambda: cmd_autostart(argparse.Namespace(state="off" if _task_exists() else "on"))),
               ("Show recent log", lambda: cmd_logs(argparse.Namespace(audit=False, n=40))),
               ("Show security audit log", lambda: cmd_logs(argparse.Namespace(audit=True, n=40))),
               ("Quit", None)]
    if not ENV_FILE.exists():
        con.print("[yellow]Not configured yet - starting setup.[/]")
        return cmd_setup()
    while True:
        con.print(Panel.fit(f"[bold]{APP_NAME}[/]", border_style="cyan"))
        for i, (label, _) in enumerate(options, 1):
            con.print(f"  {i}) {label}")
        n = IntPrompt.ask("Choose", choices=[str(i) for i in range(1, len(options) + 1)], default=1)
        label, fn = options[n - 1]
        if fn is None:
            return 0
        fn()
        if fn is cmd_start:
            return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="control", description=f"{APP_NAME} control panel")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="credentials wizard").set_defaults(fn=cmd_setup)
    sub.add_parser("start", help="run the bot (restarts it after a crash or /restart)").set_defaults(fn=cmd_start)
    sub.add_parser("doctor", help="check configuration").set_defaults(fn=cmd_doctor)
    c = sub.add_parser("config", help="show or change settings")
    c.add_argument("action", choices=["show", "set"])
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    c.set_defaults(fn=cmd_config)
    a = sub.add_parser("autostart", help="start at logon via Task Scheduler")
    a.add_argument("state", choices=["on", "off", "status"])
    a.set_defaults(fn=cmd_autostart)
    lg = sub.add_parser("logs", help="show recent log lines")
    lg.add_argument("--audit", action="store_true")
    lg.add_argument("-n", type=int, default=40)
    lg.set_defaults(fn=cmd_logs)
    pn = sub.add_parser("pin", help="PIN for the riskiest actions (typed on a keypad in Telegram)")
    pn.add_argument("state", choices=["set", "off", "status"])
    pn.set_defaults(fn=cmd_pin)
    ld = sub.add_parser("lockdown", help="emergency lockdown: block Edith until turned off here")
    ld.add_argument("state", choices=["on", "off", "status"])
    ld.set_defaults(fn=cmd_lockdown)
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    if ns.cmd == "config" and ns.action == "set" and (not ns.key or ns.value is None):
        con.print("usage: control config set KEY VALUE")
        return 2
    try:
        return menu() if ns.cmd is None else (ns.fn(ns) or 0)
    except (KeyboardInterrupt, EOFError):
        con.print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
