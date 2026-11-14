"""System module: /status /procs /kill /net /apps /gpu. Callback prefix "system:"."""
from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from ..actions.registry import ActionResult, run_action
from ..actions.system import find_processes
from ..confirm import confirm_and_run
from ..security import owner_only
from ..utils import TG_LIMIT, arg_text, run_blocking, send_result

COMMANDS = [
    ("status", "PC health: CPU, RAM, GPU, disks, battery, temps, IPs, uptime"),
    ("procs", "Top processes: /procs [cpu|mem] [limit]"),
    ("kill", "Kill a process: /kill <pid|name> (asks to confirm)"),
    ("net", "Network interfaces, traffic and connections"),
    ("apps", "Visible application windows"),
    ("gpu", "Detailed GPU stats"),
]

_STATUS_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="system:status")]])


def _procs_kb(sort: str, limit: int) -> InlineKeyboardMarkup:
    other = "mem" if sort == "cpu" else "cpu"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data=f"system:procs:{sort}:{limit}"),
        InlineKeyboardButton(f"Sort by {other.upper()}", callback_data=f"system:procs:{other}:{limit}"),
    ]])


async def _show(update: Update, context: ContextTypes.DEFAULT_TYPE, res: ActionResult, *,
                pre: bool = False, kb: InlineKeyboardMarkup | None = None, edit: bool = False) -> None:
    """Send (or edit in place) an ActionResult with optional <pre> formatting and buttons.
    Falls back to send_result for oversized text, files or failures."""
    if res.files or not res.ok or not res.text.strip():
        await send_result(context.bot, update.effective_chat.id, res)
        return
    body = f"<pre>{html.escape(res.text)}</pre>" if pre else html.escape(res.text)
    if len(body) > TG_LIMIT:
        await send_result(context.bot, update.effective_chat.id, res)
        return
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        return
    await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=kb)


def _parse_procs_args(args: list[str]) -> tuple[str, int]:
    sort, limit = "cpu", 15
    for a in args:
        a = a.lower()
        if a in ("cpu", "mem", "memory", "ram"):
            sort = "cpu" if a == "cpu" else "mem"
        elif a.isdigit():
            limit = max(1, min(int(a), 50))
    return sort, limit


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, context, await run_action("system_status"), kb=_STATUS_KB)


@owner_only
async def cmd_procs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sort, limit = _parse_procs_args(context.args or [])
    await _show(update, context, await run_action("procs", sort=sort, limit=limit), pre=True,
                kb=_procs_kb(sort, limit))


@owner_only
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = arg_text(context)
    if not target:
        await update.effective_message.reply_text(
            "Usage: /kill <pid|name>\nExamples: /kill 1234 · /kill notepad · /kill chrome.exe\n"
            "A name kills every process with that exact executable name.")
        return
    matches = await run_blocking(find_processes, target)
    if not matches:
        await update.effective_message.reply_text(f"No running process matches {target!r}.")
        return
    killable = [m for m in matches if not m["protected"]]
    if not killable:
        reasons = "; ".join(f"{m['name']} ({m['pid']}): {m['protected']}" for m in matches[:5])
        await update.effective_message.reply_text(f"🛡 Refusing to kill: {reasons}")
        return
    shown = ", ".join(str(m["pid"]) for m in killable[:15]) + (" …" if len(killable) > 15 else "")
    summary = f"Kill {len(killable)} × {killable[0]['name']} (PID {shown})"
    if len(killable) != len(matches):
        summary += f"\n({len(matches) - len(killable)} protected match(es) will be skipped)"
    await confirm_and_run(update, context, "kill_process", summary=summary, target=target)


@owner_only
async def cmd_net(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, context, await run_action("net_info"))


@owner_only
async def cmd_apps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, context, await run_action("list_windows"))


@owner_only
async def cmd_gpu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, context, await run_action("gpu_status"), pre=True)


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    if parts[1:2] == ["status"]:
        await q.answer("Refreshing…")
        await _show(update, context, await run_action("system_status"), kb=_STATUS_KB, edit=True)
    elif parts[1:2] == ["procs"]:
        sort, limit = _parse_procs_args(parts[2:])
        await q.answer("Refreshing…")
        await _show(update, context, await run_action("procs", sort=sort, limit=limit), pre=True,
                    kb=_procs_kb(sort, limit), edit=True)
    else:
        await q.answer()


def register(app: Application) -> None:
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("procs", cmd_procs))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("net", cmd_net))
    app.add_handler(CommandHandler("apps", cmd_apps))
    app.add_handler(CommandHandler("gpu", cmd_gpu))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^system:"))
