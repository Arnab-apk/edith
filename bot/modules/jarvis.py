"""Edith Telegram UI: voice notes and /j -> brain (tool loop over actions) -> text + voice reply.

Commands: /j <text>, /voice [on|off], /persona [text|reset], /forget
Voice notes / audio files are transcribed locally (faster-whisper) and handled like /j.
Dangerous actions the brain wants to run are confirmed via bot.confirm buttons (see brain.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from ..jarvis import brain, voice
from ..jarvis.memory import memory
from ..security import owner_only, redact
from ..utils import arg_text, reply_text

log = logging.getLogger("jarvis")

COMMANDS: list[tuple[str, str]] = [
    ("j", "Ask Edith: /j <request> (or just send a voice note)"),
    ("voice", "Edith spoken replies: /voice on|off"),
    ("persona", "Show/set Edith persona: /persona [text|reset]"),
    ("forget", "Clear Edith conversation memory"),
]

MAX_VOICE_BYTES = 20 * 1024 * 1024  # Bot API download limit


@contextlib.asynccontextmanager
async def _chat_action(bot, chat_id: int, action: str):
    """Keep 'typing…'/'recording voice…' visible while work runs (Telegram clears it after ~5 s)."""
    async def loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, action)
            await asyncio.sleep(4.5)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _speak(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id
    try:
        async with _chat_action(context.bot, chat_id, ChatAction.RECORD_VOICE):
            ogg = await voice.synthesize(text)
        await update.effective_message.reply_voice(ogg)
    except voice.VoiceError as e:
        log.warning("voice reply failed: %s", e)
        await update.effective_message.reply_text(f"(voice reply failed: {e})")


async def _ask_jarvis(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Run one Edith turn for this chat and reply with text (+ voice if enabled)."""
    chat_id = update.effective_chat.id
    st = memory.get(chat_id)
    async with st.lock:  # one turn at a time per chat keeps history consistent
        try:
            b = brain.get_brain()
            async with _chat_action(context.bot, chat_id, ChatAction.TYPING):
                reply = await b.ask(context.bot, chat_id, text, history=memory.history(chat_id),
                                    persona=st.persona)
        except brain.BrainConfigError as e:
            await update.effective_message.reply_text(f"⚙️ {e}")
            return
        except Exception as e:  # noqa: BLE001 - never leave the owner without an answer
            log.exception("Edith turn failed")
            await update.effective_message.reply_text(redact(f"⚠️ Edith error: {type(e).__name__}: {e}")[:4000])
            return
        stored = reply.text
        if reply.actions or reply.declined:
            notes = []
            if reply.actions:
                notes.append("ran: " + ", ".join(reply.actions))
            if reply.declined:
                notes.append("owner declined: " + ", ".join(reply.declined))
            stored += f"\n[{'; '.join(notes)}]"
        memory.add_turn(chat_id, text, stored)
    await reply_text(update, reply.text)
    if st.voice and reply.text.strip():
        await _speak(update, context, reply.text)


# ---------------------------------------------------------------- handlers
@owner_only
async def cmd_j(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = arg_text(context)
    if not text:
        await update.effective_message.reply_text(
            "Usage: /j <request>, e.g. /j how hot is my CPU?\nOr just send me a voice note.")
        return
    await _ask_jarvis(update, context, text)


@owner_only
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    media = msg.voice or msg.audio
    if media is None:
        return
    if media.file_size and media.file_size > MAX_VOICE_BYTES:
        await msg.reply_text("That audio is too large (Telegram bots can download up to 20 MB).")
        return
    try:
        async with _chat_action(context.bot, update.effective_chat.id, ChatAction.TYPING):
            tg_file = await context.bot.get_file(media.file_id)
            data = bytes(await tg_file.download_as_bytearray())
            text = await voice.transcribe(data)
    except voice.VoiceError as e:
        await msg.reply_text(f"⚠️ {e}")
        return
    if not text:
        await msg.reply_text("🎙 I didn't catch anything in that recording.")
        return
    await msg.reply_text(f"🎙 <i>{html.escape(text)}</i>", parse_mode=ParseMode.HTML)
    await _ask_jarvis(update, context, text)


def _voice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔊 Voice on", callback_data="jarvis:voice:on"),
        InlineKeyboardButton("🔇 Voice off", callback_data="jarvis:voice:off"),
    ]])


@owner_only
async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    arg = arg_text(context).lower()
    if arg in ("on", "off"):
        memory.set_voice(chat_id, arg == "on")
        await update.effective_message.reply_text(f"Edith voice replies {'ON 🔊' if arg == 'on' else 'OFF 🔇'}.")
        return
    on = memory.get(chat_id).voice
    await update.effective_message.reply_text(
        f"Edith voice replies are {'ON 🔊' if on else 'OFF 🔇'}.\n"
        f"Brain: {brain.describe_provider()}\nSpeech-to-text: {voice.model_info()}",
        reply_markup=_voice_keyboard())


@owner_only
async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    arg = arg_text(context)
    if not arg:
        cur = memory.get(chat_id).persona
        await update.effective_message.reply_text(
            f"Current persona ({'custom' if cur else 'default'}):\n{cur or brain.DEFAULT_PERSONA}\n\n"
            "Change it with /persona <description>, or /persona reset.")
        return
    if arg.lower() in ("reset", "default"):
        memory.set_persona(chat_id, None)
        await update.effective_message.reply_text("Persona reset to the default Edith.")
        return
    if len(arg) > 1500:
        await update.effective_message.reply_text("Persona is too long (max 1500 characters).")
        return
    memory.set_persona(chat_id, arg)
    await update.effective_message.reply_text("Persona updated. Safety rules and confirmations still apply.")


@owner_only
async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = memory.forget(update.effective_chat.id)
    await update.effective_message.reply_text(f"🧹 Memory cleared ({n} messages forgotten).")


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    if len(parts) == 3 and parts[1] == "voice" and parts[2] in ("on", "off"):
        on = parts[2] == "on"
        memory.set_voice(update.effective_chat.id, on)
        await q.answer(f"Voice {'on' if on else 'off'}")
        with contextlib.suppress(Exception):
            await q.edit_message_text(f"Edith voice replies {'ON 🔊' if on else 'OFF 🔇'}.",
                                      reply_markup=_voice_keyboard())
        return
    await q.answer()


def register(app: Application) -> None:
    app.add_handler(CommandHandler("j", cmd_j))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("persona", cmd_persona))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^jarvis:"))
