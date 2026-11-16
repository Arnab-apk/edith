"""Shared Confirm/Cancel flow for dangerous actions (used by modules AND the Edith brain).

    from bot.confirm import ask_confirm, confirm_and_run

    ok = await ask_confirm(context.bot, chat_id, "Kill process chrome.exe (PID 1234)?", action="kill_process")
    if ok: ...

    # or, the common case in a module handler:
    await confirm_and_run(update, context, "kill", pid=1234)

ask_confirm() awaits the owner's button press, so the Application MUST be built with
concurrent_updates(True) (main.py does this) - otherwise the button press could never be processed.

Callback data: "confirm:<token>:y|n" for plain prompts. When `action` needs the PIN (bot.pin), the
prompt is a digit keypad instead: "confirm:<token>:0-9|b (backspace)|k (OK)|n (cancel)"; "y" is
refused on a PIN prompt. Tokens are random, single-use and bound to the chat. 3 wrong PINs in a row
put Edith in lockdown. The handler is registered once by main.py via register().
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
from dataclasses import dataclass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from . import pin as pinmod
from .actions import policy
from .actions.registry import describe_call, get_action, run_action
from .security import audit, owner_only
from .utils import run_blocking, send_result

log = logging.getLogger("confirm")

CONFIRM_TIMEOUT = 120  # seconds; unanswered prompts count as Cancel
MAX_PIN_LEN = 8
MAX_WRONG_PINS = 3


@dataclass
class _Pending:
    fut: asyncio.Future[bool]
    chat_id: int
    summary: str
    pin: bool = False
    digits: str = ""
    checking: bool = False
    note: str = ""


_PENDING: dict[str, _Pending] = {}
def _fails_file():
    from .actions.policy import lockdown_file
    return lockdown_file().with_name("pin_fails.txt")


def _load_fails() -> int:
    try:
        return max(0, int(_fails_file().read_text(encoding="utf-8").strip() or 0))
    except (OSError, ValueError):
        return 0


def _save_fails(n: int) -> None:
    try:
        f = _fails_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(n), encoding="utf-8")
    except OSError:
        log.exception("could not save the wrong-PIN counter")


_wrong_pins = _load_fails()   # consecutive wrong PINs, kept across restarts (reset by a correct PIN)


# ---------------------------------------------------------------- rendering
def _plain_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{token}:y"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"confirm:{token}:n"),
    ]])


def _pin_keyboard(token: str) -> InlineKeyboardMarkup:
    def b(label: str, code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"confirm:{token}:{code}")

    rows = [[b(d, d) for d in row] for row in ("123", "456", "789")]
    rows.append([b("⌫", "b"), b("0", "0"), b("✅ OK", "k")])
    rows.append([b("❌ Cancel", "n")])
    return InlineKeyboardMarkup(rows)


def _pin_text(p: _Pending) -> str:
    shown = "•" * len(p.digits) if p.digits else "_"
    lines = [f"🔐 <b>PIN required</b>\n{html.escape(p.summary)}", f"\nPIN: <code>{shown}</code>"]
    if p.note:
        lines.append(html.escape(p.note))
    return "\n".join(lines)


# ---------------------------------------------------------------- public API
async def ask_confirm(bot: Bot, chat_id: int, summary: str, *, timeout: float = CONFIRM_TIMEOUT,
                      action: str | None = None) -> bool:
    """Ask the owner and wait. Returns True only on Confirm (or the correct PIN when `action` needs one)."""
    from .actions.policy import lockdown_info
    if lockdown_info() is not None:  # no prompts (and no PIN guessing) while locked down
        audit.warning("LOCKDOWN: confirmation refused for %r", summary)
        return False
    token = secrets.token_hex(8)
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    need_pin = pinmod.pin_required(action)
    p = _Pending(fut=fut, chat_id=chat_id, summary=summary, pin=need_pin)
    _PENDING[token] = p
    try:
        if need_pin:
            msg = await bot.send_message(chat_id, _pin_text(p), parse_mode=ParseMode.HTML,
                                         reply_markup=_pin_keyboard(token))
        else:
            msg = await bot.send_message(chat_id, f"⚠️ <b>Confirm?</b>\n{html.escape(summary)}",
                                         parse_mode=ParseMode.HTML, reply_markup=_plain_keyboard(token))
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
    audit.info("CONFIRM %s%s %r", "YES" if ok else "NO", " (PIN)" if need_pin else "", summary)
    return ok


async def confirm_and_run(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str,
                          /, *, summary: str | None = None, **kwargs) -> None:
    """Run a registered action from a module handler, confirming first if it is dangerous (with the
    PIN keypad when the action needs the PIN), then send its ActionResult to the chat."""
    chat_id = update.effective_chat.id
    if pinmod.pin_required(name) or policy.needs_confirm(get_action(name), kwargs):
        if not await ask_confirm(context.bot, chat_id, summary or describe_call(name, kwargs), action=name):
            return
    result = await run_action(name, **kwargs)
    await send_result(context.bot, chat_id, result)


# ---------------------------------------------------------------- button handler
def _lockdown(reason: str) -> bool:
    try:
        from .actions.policy import set_lockdown  # added by the lead; may not exist yet
    except ImportError:
        log.error("lockdown requested (%s) but policy.set_lockdown is not available", reason)
        return False
    try:
        set_lockdown(True, reason=reason)
        return True
    except Exception:
        log.exception("set_lockdown failed")
        return False


async def _edit(q, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        pass  # e.g. "message is not modified"


async def _on_pin_press(q, context, token: str, p: _Pending, choice: str) -> None:
    global _wrong_pins
    if p.checking:
        await q.answer("Checking…")
        return
    if choice == "n":
        if not p.fut.done():
            p.fut.set_result(False)
        await q.answer("Cancelled")
        await _edit(q, f"❌ Cancelled: {html.escape(p.summary)}")
        return
    if choice.isdigit() and len(choice) == 1:
        if len(p.digits) < MAX_PIN_LEN:
            p.digits += choice
        p.note = ""
        await q.answer()
        await _edit(q, _pin_text(p), _pin_keyboard(token))
        return
    if choice == "b":
        p.digits, p.note = p.digits[:-1], ""
        await q.answer()
        await _edit(q, _pin_text(p), _pin_keyboard(token))
        return
    if choice != "k":   # includes "y": a PIN prompt can't be confirmed without the PIN
        await q.answer("Enter the PIN, then ✅ OK.")
        return
    if not p.digits:
        await q.answer("Enter the PIN first.")
        return
    p.checking = True
    try:
        ok = await run_blocking(pinmod.check_pin, p.digits)
    finally:
        p.checking = False
    p.digits = ""
    if p.fut.done():
        return
    if ok:
        _wrong_pins = 0
        _save_fails(0)
        audit.info("PIN OK %r", p.summary)
        p.fut.set_result(True)
        await q.answer("PIN accepted")
        await _edit(q, f"✅ Confirmed (PIN): {html.escape(p.summary)}")
        return
    _wrong_pins = max(_wrong_pins, _load_fails()) + 1
    _save_fails(_wrong_pins)
    audit.warning("PIN WRONG (%d in a row) %r", _wrong_pins, p.summary)
    if _wrong_pins >= MAX_WRONG_PINS:
        _wrong_pins = 0
        _save_fails(0)  # lifting the lockdown at the PC gives a fresh set of tries
        locked = _lockdown(f"{MAX_WRONG_PINS} wrong PINs")
        p.fut.set_result(False)
        await q.answer("Wrong PIN")
        await _edit(q, f"🔒 Not run - {MAX_WRONG_PINS} wrong PINs: {html.escape(p.summary)}")
        try:
            await context.bot.send_message(
                p.chat_id, f"🔒 {MAX_WRONG_PINS} wrong PINs in a row. "
                + ("Edith is now in LOCKDOWN: dangerous actions are blocked until you unlock it on the PC."
                   if locked else "The action was cancelled."))
        except Exception:
            log.exception("could not send lockdown notice")
        return
    left = MAX_WRONG_PINS - _wrong_pins
    p.note = f"❌ Wrong PIN - {left} {'try' if left == 1 else 'tries'} left before lockdown."
    await q.answer("Wrong PIN")
    await _edit(q, _pin_text(p), _pin_keyboard(token))


@owner_only
async def _on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, token, choice = q.data.split(":", 2)
    except ValueError:
        await q.answer()
        return
    p = _PENDING.get(token)
    if p is None or p.chat_id != update.effective_chat.id or p.fut.done():
        await q.answer("This confirmation expired.")
        return
    if p.pin:
        await _on_pin_press(q, context, token, p, choice)
        return
    if choice not in ("y", "n"):
        await q.answer()
        return
    ok = choice == "y"
    p.fut.set_result(ok)
    await q.answer("Confirmed" if ok else "Cancelled")
    try:
        await q.edit_message_text(("✅ Confirmed: " if ok else "❌ Cancelled: ") + (q.message.text or "").split("\n", 1)[-1])
    except Exception:
        pass


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(_on_button, pattern=r"^confirm:"))
