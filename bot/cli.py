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
SECRET_KEYS = {"BOT_TOKEN", "ANTHROPIC_API_KEY", "LLM_API_KEY"}
KNOWN_KEYS = ["BOT_TOKEN", "ALLOWED_USER_IDS", "SHELL_TIMEOUT", "UPLOAD_DIR", "LLM_PROVIDER",
              "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
              "WHISPER_MODEL", "TTS_VOICE"]

# provider presets: key -> (label, LLM_PROVIDER, base_url, default model, key needed?)
PROVIDERS = {
    "1": ("Claude - Anthropic API key (paid, best quality)", "anthropic", "", "claude-opus-5-5", True),
    "2": ("Ollama on this PC (free, private, offline)", "openai_compat", "http://localhost:11434/v1", "qwen3:8b", False),
    "3": ("Google Gemini (free tier key: aistudio.google.com)", "openai_compat",
          "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash", True),
    "4": ("Groq (free tier key: console.groq.com, very fast)", "openai_compat",
          "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", True),
    "5": ("Skip - plain commands only", "", "", "", False),
}

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

    # 3. AI brain
    con.print("\n[bold]3. Edith AI brain[/] (voice + natural language; plain commands work without it)")
    for k, (label, *_rest) in PROVIDERS.items():
        con.print(f"  {k}) {label}")
    choice = Prompt.ask("  Choose", choices=list(PROVIDERS), default="2")
    _, provider, base, model, needs_key = PROVIDERS[choice]
    upd = {"BOT_TOKEN": token, "ALLOWED_USER_IDS": ids}
    if provider == "anthropic":
        key = getpass.getpass("  Anthropic API key (hidden): ").strip() or env.get("ANTHROPIC_API_KEY", "")
        upd |= {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": key,
                "CLAUDE_MODEL": Prompt.ask("  Claude model", default=env.get("CLAUDE_MODEL") or model)}
    elif provider:
        key = "ollama"
        if needs_key:
            key = getpass.getpass("  API key (hidden): ").strip() or env.get("LLM_API_KEY", "")
        model = Prompt.ask("  Model", default=model)
        upd |= {"LLM_PROVIDER": provider, "LLM_BASE_URL": base, "LLM_API_KEY": key, "LLM_MODEL": model}
        if choice == "2":
            _ollama_prepare(model)

    # 4. misc
    con.print("\n[bold]4. Extras[/]")
    upd["TTS_VOICE"] = Prompt.ask("  Voice for spoken replies", default=env.get("TTS_VOICE", "en-GB-SoniaNeural"))
    upd["UPLOAD_DIR"] = Prompt.ask("  Folder for files you send from the phone",
                                   default=env.get("UPLOAD_DIR", str(Path.home() / "Downloads" / "TelegramUploads")))
    upd.setdefault("SHELL_TIMEOUT", env.get("SHELL_TIMEOUT", "60"))
    upd.setdefault("WHISPER_MODEL", env.get("WHISPER_MODEL", "small"))

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


def _ollama_prepare(model: str) -> None:
    if not shutil.which("ollama"):
        con.print("  [yellow]Ollama is not installed[/]: get it from https://ollama.com/download, then run "
                  f"[bold]ollama pull {model}[/]")
        return
    if Confirm.ask(f"  Download {model} with Ollama now?", default=True):
        subprocess.run(["ollama", "pull", model], check=False)


def cmd_start(_: argparse.Namespace | None = None) -> int:
    env = read_env()
    if not env.get("BOT_TOKEN") or not env.get("ALLOWED_USER_IDS"):
        con.print("[red]Not configured yet.[/] Running setup first…")
        return cmd_setup()
    con.print(Panel.fit(f"[bold green]{APP_NAME} is running[/]\nOpen your bot on your phone and send /help.\n"
                        "Press Ctrl+C or close this window to stop.", border_style="green"))
    from . import main as bot_main  # imported late: loads config from .env
    try:
        bot_main.main()
    except KeyboardInterrupt:
        pass
    return 0


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
        base = env.get("LLM_BASE_URL", "")
        ok = None
        try:
            r = httpx.get(base.rstrip("/") + "/models", timeout=5,
                          headers={"Authorization": f"Bearer {env.get('LLM_API_KEY', '')}"})
            ok = r.status_code == 200
        except Exception:
            ok = False
        _row(t, ok, "AI brain", f"{env.get('LLM_MODEL', '')} @ {base}")
    else:
        _row(t, None, "AI brain", "not configured (Edith voice mode off)")

    try:
        import ctypes

        import ctranslate2

        from .jarvis.voice import _add_nvidia_dll_dirs
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
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, text=True)
        con.print("[green]✓ autostart removed[/]" if r.returncode == 0 else "Autostart was not on.")
        return 0
    start = ROOT / "start.bat"
    r = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED",
                        "/TR", f'"{start}"'], capture_output=True, text=True)
    if r.returncode == 0:
        con.print(f"[green]✓[/] '{TASK_NAME}' will start at logon (visible in Task Scheduler; "
                  "remove with: control.bat autostart off)")
    else:
        con.print(f"[red]Could not create task:[/] {r.stderr.strip() or r.stdout.strip()}")
    return r.returncode


def cmd_logs(ns: argparse.Namespace) -> int:
    path = ROOT / "logs" / ("audit.log" if ns.audit else "bot.log")
    if not path.exists():
        con.print(f"No {path.name} yet.")
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-ns.n:]
    con.print("\n".join(lines), markup=False, highlight=False)
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
    sub.add_parser("start", help="run the bot").set_defaults(fn=cmd_start)
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
