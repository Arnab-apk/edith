"""Telegram UI for shell actions: /cmd /ps /cd /pwd /run (callback prefix "shell:")."""
from __future__ import annotations

from pathlib import Path

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from ..actions.registry import run_action
from ..actions.shell import get_cwd, resolve_path, split_raw, strip_quotes
from ..confirm import confirm_and_run
from ..security import owner_only
from ..utils import run_blocking, send_result

COMMANDS = [
    ("cmd", "Run a cmd.exe command (asks to confirm)"),
    ("ps", "Run a PowerShell command (asks to confirm)"),
    ("cd", "Change working directory (no arg = quick picks)"),
    ("pwd", "Show working directory"),
    ("run", "Start a program or open a file (asks to confirm)"),
]

MAX_SUMMARY_CMD = 3000


def raw_args(update: Update) -> str:
    """Everything after '/command' with spacing and newlines preserved (context.args loses them)."""
    text = update.effective_message.text or update.effective_message.caption or ""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _clip(cmd: str) -> str:
    if len(cmd) <= MAX_SUMMARY_CMD:
        return cmd
    return cmd[:MAX_SUMMARY_CMD] + f"\n... (+{len(cmd) - MAX_SUMMARY_CMD} more chars not shown)"


async def _run_shell(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str, label: str, usage: str) -> None:
    command = raw_args(update)
    if not command:
        await update.effective_message.reply_text(usage)
        return
    chat_id = update.effective_chat.id
    cwd = await run_blocking(get_cwd, chat_id)
    await confirm_and_run(update, context, name, summary=f"Run in {label}\n📂 {cwd}\n> {_clip(command)}",
                          command=command, chat_id=chat_id)


@owner_only
async def cmd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_shell(update, context, "shell_cmd", "cmd.exe", "Usage: /cmd <command>   e.g. /cmd dir /b")


@owner_only
async def cmd_ps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_shell(update, context, "shell_ps", "PowerShell",
                     "Usage: /ps <command>   e.g. /ps Get-Process | Sort CPU -desc | Select -First 5")


# ---------------------------------------------------------------- /cd /pwd
def _quick_dirs() -> list[tuple[str, str]]:
    """(label, callback key) for common folders and drives that exist."""
    home = Path.home()
    out = [("🏠 Home", "home")]
    for label, key, sub in (("🖥 Desktop", "desktop", "Desktop"), ("⬇️ Downloads", "downloads", "Downloads"),
                            ("📄 Documents", "docs", "Documents")):
        if (home / sub).is_dir():
            out.append((label, key))
    try:
        for part in psutil.disk_partitions(all=False):
            letter = part.mountpoint[:1].upper()
            if letter.isalpha():
                out.append((f"💽 {letter}:", f"drv{letter}"))
    except Exception:
        pass
    return out


def _key_to_path(key: str) -> str | None:
    home = Path.home()
    fixed = {"home": home, "desktop": home / "Desktop", "downloads": home / "Downloads", "docs": home / "Documents"}
    if key in fixed:
        return str(fixed[key])
    if key.startswith("drv") and len(key) == 4 and key[3].isalpha():
        return f"{key[3]}:\\"
    return None


@owner_only
async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    path = raw_args(update)
    if path:
        await send_result(context.bot, chat_id, await run_action("cd", path=path, chat_id=chat_id))
        return
    cwd = await run_blocking(get_cwd, chat_id)
    btns = [InlineKeyboardButton(label, callback_data=f"shell:cd:{key}")
            for label, key in await run_blocking(_quick_dirs)]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    await update.effective_message.reply_text(f"📂 {cwd}\nUsage: /cd <path>  (or pick one)",
                                              reply_markup=InlineKeyboardMarkup(rows))


@owner_only
async def cmd_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await send_result(context.bot, chat_id, await run_action("pwd", chat_id=chat_id))


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":", 2)
    if len(parts) == 3 and parts[1] == "cd" and (path := _key_to_path(parts[2])):
        await q.answer()
        chat_id = update.effective_chat.id
        await send_result(context.bot, chat_id, await run_action("cd", path=path, chat_id=chat_id))
    else:
        await q.answer("Unknown button")


# ---------------------------------------------------------------- /run
def parse_run(raw: str, chat_id: int) -> tuple[str, str]:
    """'/run C:\\Program Files\\x.exe' (unquoted path that exists) -> whole text is the target;
    otherwise first (possibly quoted) token is the target and the rest are arguments (quotes kept)."""
    whole = strip_quotes(raw)
    if resolve_path(chat_id, whole).exists():
        return whole, ""
    toks = split_raw(raw)
    if not toks:
        return "", ""
    return strip_quotes(toks[0]), " ".join(toks[1:])


@owner_only
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = raw_args(update)
    if not raw:
        await update.effective_message.reply_text('Usage: /run <program|file> [args]   e.g. /run notepad  '
                                                  '/run "C:\\My Apps\\tool.exe" --flag')
        return
    chat_id = update.effective_chat.id
    target, args = await run_blocking(parse_run, raw, chat_id)
    shown = await run_blocking(resolve_path, chat_id, target)
    shown_s = str(shown) if await run_blocking(shown.exists) else target
    await confirm_and_run(update, context, "run_program",
                          summary=f"Start: {shown_s}" + (f"\nArgs: {args}" if args else ""),
                          target=target, args=args, chat_id=chat_id)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("cmd", cmd_cmd))
    app.add_handler(CommandHandler("ps", cmd_ps))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("pwd", cmd_pwd))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^shell:"))
