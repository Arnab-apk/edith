"""Media commands: /volume /mute /media (media-key buttons) /say.

Thin Telegram layer over bot.actions.media. Callback data: "media:key:<key>", "media:mute", "media:refresh".
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from ..actions.media import KEY_LABELS
from ..actions.registry import run_action
from ..security import owner_only
from ..utils import send_result

COMMANDS: list[tuple[str, str]] = [
    ("volume", "Volume: /volume [0-100 | +N | -N]"),
    ("mute", "Mute toggle: /mute [on|off]"),
    ("media", "Media-key buttons (play/pause, next, volume)"),
    ("say", "Speak text out loud on the PC"),
]

PANEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("⏮", callback_data="media:key:prev"),
     InlineKeyboardButton("⏯", callback_data="media:key:play_pause"),
     InlineKeyboardButton("⏭", callback_data="media:key:next")],
    [InlineKeyboardButton("🔉 −", callback_data="media:key:vol_down"),
     InlineKeyboardButton("🔇", callback_data="media:mute"),
     InlineKeyboardButton("🔊 +", callback_data="media:key:vol_up")],
    [InlineKeyboardButton("🔄 Refresh", callback_data="media:refresh")],
])


def parse_volume_arg(arg: str) -> dict:
    """'' -> {} (report) | '40' -> level | '+10'/'-10' -> change | 'up'/'down' -> ±10. Raises ValueError."""
    a = arg.strip().lower().rstrip("%")
    if not a:
        return {}
    if a in ("up", "louder"):
        return {"change": 10}
    if a in ("down", "quieter"):
        return {"change": -10}
    if a[0] in "+-" and a[1:].isdigit():
        return {"change": int(a)}
    if a.isdigit():
        n = int(a)
        if not 0 <= n <= 100:
            raise ValueError("volume must be 0-100")
        return {"level": n}
    raise ValueError("use /volume 0-100, +N or -N")


async def _panel_text() -> str:
    res = await run_action("volume")
    return f"🎵 Media controls\n{res.text}"


@owner_only
async def cmd_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        kw = parse_volume_arg(" ".join(context.args or []))
    except ValueError as e:
        await update.effective_message.reply_text(f"⚠️ {e}")
        return
    res = await run_action("volume", **kw)
    if not res.ok:
        await send_result(context.bot, update.effective_chat.id, res)
        return
    await update.effective_message.reply_text(f"🎵 Media controls\n{res.text}", reply_markup=PANEL_KB)


@owner_only
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args or ["toggle"])[0].lower()
    state = {"on": "on", "off": "off", "unmute": "off", "toggle": "toggle"}.get(arg)
    if state is None:
        await update.effective_message.reply_text("Usage: /mute [on|off]")
        return
    await send_result(context.bot, update.effective_chat.id, await run_action("mute", state=state))


@owner_only
async def cmd_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(await _panel_text(), reply_markup=PANEL_KB)


@owner_only
async def cmd_say(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or [])
    reply = update.effective_message.reply_to_message
    if not text and reply is not None:
        text = reply.text or reply.caption or ""
    if not text.strip():
        await update.effective_message.reply_text("Usage: /say <text> (or reply to a message with /say)")
        return
    await send_result(context.bot, update.effective_chat.id, await run_action("say", text=text))


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    if len(parts) == 3 and parts[1] == "key" and parts[2] in KEY_LABELS:
        res = await run_action("media_key", key=parts[2])
    elif parts[1:] == ["mute"]:
        res = await run_action("mute", state="toggle")
    elif parts[1:] == ["refresh"]:
        res = None
    else:
        await q.answer()
        return
    await q.answer((res.text if res else "Refreshed")[:200])
    try:
        await q.edit_message_text(await _panel_text(), reply_markup=PANEL_KB)
    except BadRequest:  # "message is not modified" when nothing changed
        pass


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["volume", "vol"], cmd_volume))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("media", cmd_media))
    app.add_handler(CommandHandler("say", cmd_say))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^media:"))
