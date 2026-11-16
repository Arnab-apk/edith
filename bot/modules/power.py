"""Power module: /power menu, /lock, /abort. Callback prefix "power:".

Every power action except abort is dangerous, so it always goes through
bot.confirm.confirm_and_run (Confirm/Cancel buttons) - from the menu, the text form and /lock.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from ..actions.power import DEFAULT_DELAY, MAX_DELAY, MIN_DELAY, fmt_delay
from ..confirm import confirm_and_run
from ..security import owner_only

COMMANDS = [
    ("power", "Power menu: lock, sleep, hibernate, restart, shutdown, logoff, abort, monitor off"),
    ("lock", "Lock the PC"),
    ("abort", "Cancel a pending shutdown/restart"),
    ("lockdown", "Emergency: lock the PC and block Edith until unlocked at the PC"),
]

# key -> (action name, takes delay?, confirm summary)
POWER_ACTIONS: dict[str, tuple[str, bool, str]] = {
    "lock": ("power_lock", False, "🔒 Lock the PC"),
    "sleep": ("power_sleep", False, "😴 Put the PC to sleep (the bot goes offline until it wakes)"),
    "hibernate": ("power_hibernate", False, "🛌 Hibernate the PC (the bot goes offline)"),
    "monitor_off": ("power_monitor_off", False, "🖥 Turn the display(s) off"),
    "restart": ("power_restart", True, "🔄 Restart the PC in {delay} (cancel with /abort)"),
    "shutdown": ("power_shutdown", True, "⏻ Shut down the PC in {delay} (cancel with /abort)"),
    "logoff": ("power_logoff", False, "🚪 Sign out of Windows (closes all apps, including this bot)"),
    "abort": ("power_abort", False, "✋ Cancel pending shutdown/restart"),
}
_ALIASES = {"monitor": "monitor_off", "monitoroff": "monitor_off", "screen_off": "monitor_off",
            "reboot": "restart", "off": "shutdown", "logout": "logoff", "signout": "logoff",
            "cancel": "abort", "hib": "hibernate", "standby": "sleep"}

MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔒 Lock", callback_data="power:lock"),
     InlineKeyboardButton("😴 Sleep", callback_data="power:sleep")],
    [InlineKeyboardButton("🛌 Hibernate", callback_data="power:hibernate"),
     InlineKeyboardButton("🖥 Monitor off", callback_data="power:monitor_off")],
    [InlineKeyboardButton("🔄 Restart", callback_data="power:restart"),
     InlineKeyboardButton("⏻ Shut down", callback_data="power:shutdown")],
    [InlineKeyboardButton("🚪 Log off", callback_data="power:logoff"),
     InlineKeyboardButton("✋ Abort", callback_data="power:abort")],
])

_USAGE = (f"⚡ Power menu - every action except Abort asks you to confirm.\n"
          f"Text form: /power <lock|sleep|hibernate|monitor_off|restart|shutdown|logoff|abort> [delay s]\n"
          f"Restart/shutdown wait {DEFAULT_DELAY} s by default ({MIN_DELAY}-{MAX_DELAY}) so /abort can cancel.")


async def run_power(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, delay: int | None = None) -> bool:
    """Confirm (if dangerous) and run power action `key`. Returns False for an unknown key."""
    key = _ALIASES.get(key, key)
    if key not in POWER_ACTIONS:
        return False
    name, takes_delay, summary = POWER_ACTIONS[key]
    kwargs = {}
    if takes_delay:
        d = max(MIN_DELAY, min(DEFAULT_DELAY if delay is None else delay, MAX_DELAY))
        kwargs["delay"] = d
        summary = summary.format(delay=fmt_delay(d))
    await confirm_and_run(update, context, name, summary=summary, **kwargs)
    return True


@owner_only
async def cmd_power(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.lower() for a in (context.args or [])]
    if not args:
        await update.effective_message.reply_text(_USAGE, reply_markup=MENU)
        return
    delay = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    if not await run_power(update, context, args[0], delay):
        await update.effective_message.reply_text(f"Unknown power action {args[0]!r}.\n\n{_USAGE}", reply_markup=MENU)


@owner_only
async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_power(update, context, "lock")


@owner_only
async def cmd_lockdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No Confirm on purpose: it only ever takes power away, and speed matters if the phone is stolen."""
    from ..actions import policy
    from ..actions.registry import run_action

    policy.set_lockdown(True, reason="/lockdown from Telegram")
    locked = await run_action("power_lock")
    await update.effective_message.reply_text(
        "🚨 Lockdown is ON. " + ("The PC is locked. " if locked.ok else "")
        + "Edith now refuses everything except status, volume and lock. "
        "To lift it, run at the PC: control lockdown off")


@owner_only
async def cmd_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_power(update, context, "abort")


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    key = (q.data or "").split(":", 1)[-1]
    await q.answer()  # answer first: confirm_and_run may wait up to 120 s for the Confirm tap
    # The menu message stays as-is so it can be reused.
    await run_power(update, context, key)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("power", cmd_power))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("abort", cmd_abort))
    app.add_handler(CommandHandler("lockdown", cmd_lockdown))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^power:"))
