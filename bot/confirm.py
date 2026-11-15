"""Shared Confirm/Cancel flow for dangerous actions (used by modules AND the Edith brain).

    from bot.confirm import ask_confirm, confirm_and_run

    ok = await ask_confirm(context.bot, chat_id, "Kill process chrome.exe (PID 1234)?")
    if ok: ...

    # or, the common case in a module handler:
    await confirm_and_run(update, context, "kill", pid=1234)

ask_confirm() awaits the owner's button press, so the Application MUST be built with
concurrent_updates(True) (main.py does this) - otherwise the button press could never be processed.
Callback data: "confirm:<token>:y|n". The handler is registered once by main.py via register().
"""
from __future__ import annotations

import asyncio
import html
import secrets

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from .actions import policy
from .actions.registry import describe_call, get_action, run_action
from .security import audit, owner_only
from .utils import send_result

CONFIRM_TIMEOUT = 120  # seconds; unanswered prompts count as Cancel
_PENDING: dict[str, tuple[asyncio.Future[bool], int]] = {}


async def ask_confirm(bot: Bot, chat_id: int, summary: str, *, timeout: float = CONFIRM_TIMEOUT) -> bool:
    """Send Confirm/Cancel buttons and wait for the owner. Returns True only on Confirm."""
    token = secrets.token_hex(8)
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    _PENDING[token] = (fut, chat_id)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{token}:y"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"confirm:{token}:n"),
    ]])
    msg = await bot.send_message(chat_id, f"⚠️ <b>Confirm?</b>\n{html.escape(summary)}",
                                 parse_mode=ParseMode.HTML, reply_markup=kb)
    try:
        ok = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        ok = False
        try:
            await msg.edit_text(f"⌛ Expired (not run): {summary}")
        except Exception:
            pass
    finally:
        _PENDING.pop(token, None)
    audit.info("CONFIRM %s %r", "YES" if ok else "NO", summary)
    return ok


async def confirm_and_run(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str,
                          /, *, summary: str | None = None, **kwargs) -> None:
    """Run a registered action from a module handler, confirming first if it is dangerous,
    then send its ActionResult to the chat."""
    chat_id = update.effective_chat.id
    if policy.needs_confirm(get_action(name), kwargs):
        if not await ask_confirm(context.bot, chat_id, summary or describe_call(name, kwargs)):
            return
    result = await run_action(name, **kwargs)
    await send_result(context.bot, chat_id, result)


@owner_only
async def _on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, token, choice = q.data.split(":", 2)
    except ValueError:
        await q.answer()
        return
    entry = _PENDING.get(token)
    if entry is None or entry[1] != update.effective_chat.id:
        await q.answer("This confirmation expired.")
        return
    fut, _ = entry
    ok = choice == "y"
    if not fut.done():
        fut.set_result(ok)
    await q.answer("Confirmed" if ok else "Cancelled")
    try:
        await q.edit_message_text(("✅ Confirmed: " if ok else "❌ Cancelled: ") + (q.message.text or "").split("\n", 1)[-1])
    except Exception:
        pass


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(_on_button, pattern=r"^confirm:"))
