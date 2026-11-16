"""Ops module: /restart (via the `control start` watchdog) and /logs.

/restart only works when Edith runs under the watchdog (`control start` / start.bat sets EDITH_CHILD=1
for the bot process). It asks to confirm, stops the Application cleanly, and bot.cli then exits the
process with code 3 (EXIT_RESTART) so the watchdog starts a fresh copy right away. Without the
watchdog nothing would bring Edith back, so /restart refuses instead of silently stopping the bot.
"""
from __future__ import annotations

import html
import io
import os
from collections import deque
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from ..config import settings
from ..confirm import ask_confirm
from ..security import audit, owner_only, redact
from ..utils import TG_LIMIT, run_blocking

COMMANDS = [
    ("restart", "Restart Edith (asks to confirm)"),
    ("logs", "Recent log lines: /logs [n] [audit]"),
]

DEFAULT_LINES, MAX_LINES = 30, 300
_restart_requested = False


def restart_requested() -> bool:
    """True once /restart was confirmed; bot.cli turns this into exit code 3 after polling stops."""
    return _restart_requested


def under_watchdog() -> bool:
    return bool(os.environ.get("EDITH_CHILD"))


def request_restart(app: Application) -> None:
    global _restart_requested
    _restart_requested = True
    audit.info("RESTART requested by owner")
    app.stop_running()  # graceful: run_polling returns after shutting the Application down


@owner_only
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not under_watchdog():
        await msg.reply_text("Can't restart from here: Edith wasn't started with `control start` (start.bat), "
                             "so nothing would start it again. Restart it on the PC instead.")
        return
    if not await ask_confirm(context.bot, update.effective_chat.id, "Restart Edith? It's back in about 10 seconds."):
        return
    await msg.reply_text("🔄 Restarting...")
    request_restart(context.application)


def parse_logs_args(args: list[str]) -> tuple[int, bool]:
    n, audit_log = DEFAULT_LINES, False
    for a in args:
        a = a.strip().lower()
        if a.isdigit():
            n = int(a)
        elif a in ("audit", "security", "--audit"):
            audit_log = True
    return max(1, min(n, MAX_LINES)), audit_log


def tail(path: Path, n: int) -> list[str]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\n") for line in deque(f, maxlen=n)]


@owner_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n, audit_log = parse_logs_args(context.args or [])
    path = Path(settings.log_dir) / ("audit.log" if audit_log else "bot.log")
    if not path.exists():
        await update.effective_message.reply_text(f"No {path.name} yet.")
        return
    try:
        lines = await run_blocking(tail, path, n)
    except OSError as e:
        await update.effective_message.reply_text(f"Could not read {path.name}: {e}")
        return
    text = redact("\n".join(lines)) if lines else f"{path.name} is empty."
    header = f"Last {len(lines)} lines of {path.name}:"
    body = f"{html.escape(header)}\n<pre>{html.escape(text)}</pre>"
    if len(body) <= TG_LIMIT:  # measured after escaping: '<' and '&' in log lines grow the message
        await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML)
        return
    await update.effective_message.reply_document(io.BytesIO(text.encode("utf-8", "replace")),
                                                  filename=path.name.replace(".log", ".txt"), caption=header)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("logs", cmd_logs))
