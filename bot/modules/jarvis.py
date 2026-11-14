"""Edith Telegram UI: talk to the PC by voice note, plain text or /j.

Every owner message in the private chat that is not a command (and not a file/photo, which the
files module handles) goes to Edith, so the chat itself is the command line.

Flow for one turn:
  voice note -> "🎙 Heard: ..." -> instant intent (no LLM) or the LLM tool loop
  -> one status message edited through "Thinking…" / "Running <action>…"
  -> final text answer (+ a short voice answer when the input was a voice note and /voice is on).
Every turn ends with an answer or a clear error; failed turns are not stored in memory.
Dangerous actions are confirmed via bot.confirm buttons on every path (see brain.execute_tool).

Commands: /j <text>, /voice [on|off], /persona [text|reset], /forget
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from ..jarvis import brain, voice
from ..jarvis.memory import memory
from ..security import owner_only, redact
from ..utils import TG_LIMIT, arg_text, reply_text, run_blocking

log = logging.getLogger("jarvis")

COMMANDS: list[tuple[str, str]] = [
    ("j", "Ask Edith: /j <request> (or just type, or send a voice note)"),
    ("voice", "Edith spoken replies: /voice on|off"),
    ("persona", "Show/set Edith persona: /persona [text|reset]"),
    ("forget", "Clear Edith conversation memory"),
]

MAX_VOICE_BYTES = 20 * 1024 * 1024  # Bot API download limit
# Plain text goes to Edith from a LATER handler group: commands, Confirm buttons and other
# modules' message handlers live in group 0, so they always get their updates first.
TEXT_GROUP = 1
WARMUP_DELAY = 3       # seconds after startup
KEEPALIVE_EVERY = 240  # s; keeps a local Ollama model loaded (Ollama unloads after 5 idle min)


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


class Status:
    """One status message, edited through the stages of a turn."""

    def __init__(self, update: Update, header: str = "") -> None:
        self.update = update
        self.header = header  # e.g. '🎙 Heard: "..."' stays on top
        self.msg = None
        self._last = None

    def _compose(self, stage: str) -> str:
        return "\n".join(x for x in (self.header, stage) if x)[:TG_LIMIT]

    async def set(self, stage: str) -> None:
        text = self._compose(f"⏳ {stage}" if stage else "")
        if not text or text == self._last:
            return
        self._last = text
        try:
            if self.msg is None:
                self.msg = await self.update.effective_message.reply_text(text)
            else:
                await self.msg.edit_text(text)
        except Exception:  # noqa: BLE001 - status is best effort, the answer still comes
            log.debug("status update failed", exc_info=True)

    async def finish(self, answer: str) -> None:
        """Deliver the final answer: the status keeps the transcript (if any) and the answer follows,
        or - for typed input - the status message itself becomes the answer."""
        answer = answer.strip() or "Done."
        if self.msg is not None and not self.header and len(answer) <= TG_LIMIT:
            try:
                await self.msg.edit_text(answer)
                return
            except Exception:  # noqa: BLE001
                log.debug("could not edit status into the answer", exc_info=True)
        if self.msg is not None and self.header:
            with contextlib.suppress(Exception):
                await self.msg.edit_text(self.header)  # drop the "⏳ ..." line, keep what was heard
        await reply_text(self.update, answer)


async def _speak(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id
    try:
        async with _chat_action(context.bot, chat_id, ChatAction.RECORD_VOICE):
            ogg = await voice.synthesize(text)
        await update.effective_message.reply_voice(ogg)
    except voice.VoiceError as e:
        log.warning("voice reply failed: %s", e)
        await update.effective_message.reply_text(f"(voice reply failed: {e})")
    except Exception as e:  # noqa: BLE001 - the text answer was already delivered
        log.exception("voice reply failed")
        await update.effective_message.reply_text(redact(f"(voice reply failed: {type(e).__name__})"))


async def _ask_edith(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, *,
                     spoken: bool = False, status: Status | None = None) -> None:
    """Run one Edith turn for this chat. Always ends with an answer or a clear error."""
    chat_id = update.effective_chat.id
    st = memory.get(chat_id)
    status = status or Status(update)
    if st.lock.locked():
        await status.set("Waiting for the previous request to finish…")
    async with st.lock:  # one turn at a time per chat keeps history consistent
        reply: brain.BrainReply
        try:
            async with _chat_action(context.bot, chat_id, ChatAction.TYPING):
                intents = brain.match_intents(text)
                if intents:
                    reply = await brain.run_intents(context.bot, chat_id, intents, progress=status.set)
                else:
                    await status.set("Thinking…")
                    b = brain.get_brain()
                    reply = await b.ask(context.bot, chat_id, text, history=memory.history(chat_id),
                                        persona=st.persona, progress=status.set)
        except brain.BrainConfigError as e:
            reply = brain.BrainReply(text=f"⚙️ {e}", ok=False)
        except Exception as e:  # noqa: BLE001 - never leave the owner without an answer
            log.exception("Edith turn failed")
            reply = brain.BrainReply(text=redact(f"⚠️ Edith error: {type(e).__name__}: {e}")[:1000], ok=False)
        if reply.ok:  # failed / timed-out turns must not poison the conversation
            stored = reply.text
            notes = []
            if reply.actions:
                notes.append("ran: " + ", ".join(reply.actions))
            if reply.declined:
                notes.append("owner declined: " + ", ".join(reply.declined))
            if notes:
                stored += f"\n[{'; '.join(notes)}]"
            memory.add_turn(chat_id, text, stored)
    await status.finish(reply.text)
    say = (reply.spoken or reply.text).strip()
    if spoken and st.voice and reply.ok and say:
        await _speak(update, context, say)


# ---------------------------------------------------------------- handlers
@owner_only
async def cmd_j(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = arg_text(context)
    if not text:
        await update.effective_message.reply_text(
            "Usage: /j <request>, e.g. /j how hot is my CPU?\nOr just type to me, or send a voice note.")
        return
    await _ask_edith(update, context, text)


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if text:
        await _ask_edith(update, context, text)


@owner_only
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    media = msg.voice or msg.audio
    if media is None:
        return
    if media.file_size and media.file_size > MAX_VOICE_BYTES:
        await msg.reply_text("That audio is too large (Telegram bots can download up to 20 MB).")
        return
    status = Status(update)
    await status.set("Listening…")
    try:
        async with _chat_action(context.bot, update.effective_chat.id, ChatAction.TYPING):
            tg_file = await context.bot.get_file(media.file_id)
            data = bytes(await tg_file.download_as_bytearray())
            text = await voice.transcribe(data)
    except voice.VoiceError as e:
        await status.finish(f"⚠️ {e}")
        return
    except Exception as e:  # noqa: BLE001 - download/network problems must still get an answer
        log.exception("voice note handling failed")
        await status.finish(redact(f"⚠️ Couldn't process that voice note: {type(e).__name__}: {e}")[:1000])
        return
    if not text:
        await status.finish("🎙 I didn't catch anything in that recording.")
        return
    # Same status message from here on: it keeps what was heard on top, then the turn's stages.
    status.header = f"🎙 Heard: “{text}”"
    await _ask_edith(update, context, text, spoken=True, status=status)


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
        f"Edith voice replies are {'ON 🔊' if on else 'OFF 🔇'} (spoken answers to voice notes).\n"
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


# ---------------------------------------------------------------- startup warm-up
async def warmup_job(context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    """Load Whisper and the local LLM in the background so the first request is fast.
    Failures are logged only: the bot keeps working and loads lazily instead."""
    try:
        desc = await run_blocking(voice.warmup)
        log.info("Speech-to-text ready: %s", desc)
    except Exception as e:  # noqa: BLE001
        log.warning("Whisper warm-up failed (will retry on first voice note): %s", redact(e))
    await keepalive_job(context)


async def keepalive_job(context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    """Load the local LLM / keep it loaded (no-op for cloud providers). Never raises."""
    try:
        await brain.get_brain().warmup()
    except Exception as e:  # noqa: BLE001
        log.warning("LLM warm-up skipped: %s", redact(e))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("j", cmd_j))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("persona", cmd_persona))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^jarvis:"))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_text),
                    group=TEXT_GROUP)
    if app.job_queue is not None:  # jobs run only once the bot is actually started (not in tests)
        app.job_queue.run_once(warmup_job, when=WARMUP_DELAY, name="edith-warmup")
        app.job_queue.run_repeating(keepalive_job, interval=KEEPALIVE_EVERY, first=KEEPALIVE_EVERY,
                                    name="edith-llm-keepalive")
