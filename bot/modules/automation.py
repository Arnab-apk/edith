"""Telegram UI for automations: /remind /every /at /jobs /cancel /when /macro.

Creating anything whose steps include a dangerous action asks for Confirm (bot.confirm.ask_confirm)
showing the exact steps. Callback data prefix: "automation:".
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .. import automation as auto
from ..automation import macros, scheduler, triggers
from ..actions.registry import ActionResult
from ..confirm import ask_confirm
from ..security import owner_only
from ..utils import reply_text, send_result

log = logging.getLogger("modules.automation")

COMMANDS = [
    ("remind", "Reminder: /remind 10m stretch · /remind tomorrow 9:00 call mom"),
    ("every", "Repeat an action: /every 30m screenshot (min 1m)"),
    ("at", "Run an action at a time: /at 22:00 power_shutdown · /at 08:00 daily notify hi"),
    ("jobs", "List reminders, jobs, triggers and macros"),
    ("cancel", "Cancel a reminder/job/trigger: /cancel 3"),
    ("when", "Trigger: /when cpu>90 for 2m notify · battery<20 · process x.exe starts"),
    ("macro", "Macros: /macro save|run|list|del"),
]

USAGE = {
    "remind": "Usage: /remind <when> <text>\nwhen: 10m · 2h30m · 22:00 · 9pm · tomorrow 9:00",
    "every": "Usage: /every <interval> <action> [args]   (min 1 minute; several steps: a; b)\n"
             "e.g. /every 30m screenshot",
    "at": "Usage: /at HH:MM [daily] <action> [args]\ne.g. /at 22:00 power_shutdown · /at 9:00 daily notify text=Standup",
    "when": "Usage: /when <condition> [for 2m] [cooldown 15m] [action args]\n"
            "conditions: cpu>90 · ram>90 · battery<20 · temp>85 · idle>30m · process chrome.exe starts|stops\n"
            "default action: notify",
    "macro": "Usage:\n/macro save <name>: <action args>; <action args>...\n/macro run <name>\n/macro list\n"
             "/macro del <name>",
}


def _args(update: Update) -> str:
    """Raw text after the /command (keeps quotes and spacing)."""
    text = update.effective_message.text or ""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _create(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: auto.Plan) -> None:
    chat_id = update.effective_chat.id
    act = "create_trigger" if plan.kind == "trigger" else "schedule_action"
    if plan.dangerous and not await ask_confirm(context.bot, chat_id, plan.summary(), action=act):
        await reply_text(update, "Not created.")
        return
    task = await scheduler.commit(plan, chat_id)
    note = await triggers.readability_note(plan) if plan.kind == "trigger" else ""
    await reply_text(update, f"✅ #{task.id} {task.label}" + (f"\n{note}" if note else "")
                     + ("\n⚠️ Dangerous steps approved - you'll be notified each time they run."
                        if task.dangerous else ""))


async def _guarded(update: Update, usage_key: str, coro_factory) -> None:
    try:
        await coro_factory()
    except auto.AutomationError as e:
        await reply_text(update, f"⚠️ {e}\n\n{USAGE[usage_key]}" if usage_key in USAGE else f"⚠️ {e}")


@owner_only
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _args(update)
    if not text:
        await reply_text(update, USAGE["remind"])
        return
    await _guarded(update, "remind", lambda: _create(update, context, scheduler.plan_reminder(text)))


@owner_only
async def cmd_every(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _args(update)
    if not text:
        await reply_text(update, USAGE["every"])
        return
    await _guarded(update, "every", lambda: _create(update, context, scheduler.plan_every(text)))


@owner_only
async def cmd_at(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _args(update)
    if not text:
        await reply_text(update, USAGE["at"])
        return
    await _guarded(update, "at", lambda: _create(update, context, scheduler.plan_at(text)))


@owner_only
async def cmd_when(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _args(update)
    if not text:
        await reply_text(update, USAGE["when"])
        return
    await _guarded(update, "when", lambda: _create(update, context, triggers.plan_trigger(text)))


def _jobs_keyboard(tasks: list[auto.Task]) -> InlineKeyboardMarkup | None:
    if not tasks:
        return None
    buttons = [InlineKeyboardButton(f"❌ #{t.id}", callback_data=f"automation:cancel:{t.id}") for t in tasks[:30]]
    return InlineKeyboardMarkup([buttons[i:i + 5] for i in range(0, len(buttons), 5)])


@owner_only
async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await scheduler.list_text()
    tasks = await auto.db("list_tasks")
    kb = _jobs_keyboard(tasks)
    if len(text) > 4000 or kb is None:
        await reply_text(update, text)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = _args(update).lstrip("#")
    if not raw.isdigit():
        await reply_text(update, "Usage: /cancel <id>   (see /jobs)")
        return
    ok = await scheduler.cancel(int(raw))
    await reply_text(update, f"🗑 Cancelled #{raw}." if ok else f"No automation #{raw}.")


@owner_only
async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _args(update)
    sub, _, rest = text.partition(" ")
    sub, rest = sub.lower(), rest.strip()
    chat_id = update.effective_chat.id
    try:
        if sub == "save":
            name, plan = macros.plan_macro(rest)
            if plan.dangerous and not await ask_confirm(context.bot, chat_id, plan.summary()
                                                         + "\n(Running it will ask for confirmation again.)", action="save_macro"):
                await reply_text(update, "Not saved.")
                return
            await macros.save(name, plan)
            await reply_text(update, f"💾 Saved {plan.label}")
        elif sub == "run":
            if not rest:
                raise auto.AutomationError("Which macro?")
            result = await macros.run_macro(context.bot, chat_id, rest.split()[0], confirm=True)
            await send_result(context.bot, chat_id, result)
        elif sub in ("list", "ls", ""):
            listing = await macros.list_text()
            names = [n for n, _ in await auto.db("list_macros")][:30]
            if names:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"▶️ {n}", callback_data=f"automation:mrun:{n}")]
                                           for n in names])
                await update.effective_message.reply_text(listing[:4000], reply_markup=kb)
            else:
                await reply_text(update, listing + "\n\n" + USAGE["macro"])
        elif sub in ("del", "delete", "rm"):
            if not rest:
                raise auto.AutomationError("Which macro?")
            ok = await macros.delete(rest.split()[0])
            await reply_text(update, f"🗑 Deleted macro {rest.split()[0]!r}." if ok else "No such macro.")
        else:
            await reply_text(update, USAGE["macro"])
    except auto.AutomationError as e:
        await reply_text(update, f"⚠️ {e}\n\n{USAGE['macro']}")


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":", 2)
    if len(parts) != 3:
        await q.answer()
        return
    _, verb, arg = parts
    chat_id = update.effective_chat.id
    if verb == "cancel" and arg.isdigit():
        ok = await scheduler.cancel(int(arg))
        await q.answer(f"Cancelled #{arg}" if ok else f"#{arg} not found")
        try:
            await context.bot.send_message(chat_id, f"🗑 Cancelled #{arg}." if ok else f"No automation #{arg}.")
        except Exception:
            pass
    elif verb == "mrun":
        await q.answer(f"Running {arg}…")
        try:
            result = await macros.run_macro(context.bot, chat_id, arg, confirm=True)
        except auto.AutomationError as e:
            result = ActionResult(text=str(e), ok=False)
        await send_result(context.bot, chat_id, result)
    else:
        await q.answer()


def register(app: Application) -> None:
    auto.set_app(app)
    for name, func in (("remind", cmd_remind), ("every", cmd_every), ("at", cmd_at), ("jobs", cmd_jobs),
                       ("cancel", cmd_cancel), ("when", cmd_when), ("macro", cmd_macro)):
        app.add_handler(CommandHandler(name, func))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^automation:"))
    if app.job_queue is None:
        log.error("JobQueue unavailable (install python-telegram-bot[job-queue]); automations disabled")
        return
    scheduler.restore(app)
    triggers.start(app)
