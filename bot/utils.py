"""Shared helpers for all modules."""
from __future__ import annotations

import asyncio
import html
import io
from typing import Callable, TypeVar

from telegram import Update
from telegram.constants import ParseMode

T = TypeVar("T")
TG_LIMIT = 4000  # Telegram hard limit is 4096 chars


async def run_blocking(func: Callable[..., T], *args, **kwargs) -> T:
    """Run blocking/sync code (psutil, pyautogui, subprocess, COM...) off the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


async def reply_text(update: Update, text: str, *, code: bool = False, filename: str = "output.txt") -> None:
    """Reply with text. If code=True wrap in <pre>. If too long, send as a .txt document instead."""
    msg = update.effective_message
    text = text if text.strip() else "(no output)"
    if len(text) > TG_LIMIT:
        await msg.reply_document(io.BytesIO(text.encode("utf-8", "replace")), filename=filename,
                                 caption=f"Output was {len(text)} chars - sent as file.")
        return
    if code:
        await msg.reply_text(f"<pre>{html.escape(text)}</pre>", parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text(text)


async def send_result(bot, chat_id: int, result) -> None:
    """Deliver an actions.registry.ActionResult (text + files) to a chat.
    Works without an Update, so automations/Edith can use it too."""
    from .actions.registry import ActionResult  # local import: utils must not depend on actions at import time

    if not isinstance(result, ActionResult):
        result = ActionResult(text=str(result))
    text = result.text or ""
    if not result.ok and text:
        text = "⚠️ " + text
    if len(text) > TG_LIMIT:
        await bot.send_document(chat_id, io.BytesIO(text.encode("utf-8", "replace")), filename="output.txt",
                                caption=f"Output was {len(text)} chars - sent as file.")
    elif text.strip() and result.code and len(html.escape(text)) + 11 <= 4096:
        await bot.send_message(chat_id, f"<pre>{html.escape(text)}</pre>", parse_mode=ParseMode.HTML)
    elif text.strip():
        await bot.send_message(chat_id, text)
    elif not result.files:
        await bot.send_message(chat_id, "✅ Done" if result.ok else "⚠️ Failed")
    senders = {"photo": bot.send_photo, "video": bot.send_video, "voice": bot.send_voice,
               "audio": bot.send_audio, "document": bot.send_document}
    for f in result.files:
        payload = f.data if f.data is not None else f.path.read_bytes()
        kw = {"filename": f.filename} if f.kind in ("document", "video", "audio") else {}
        try:
            await senders[f.kind](chat_id, io.BytesIO(payload), caption=f.caption[:1024] or None, **kw)
        finally:
            if f.delete_after_send and f.path is not None:
                try:
                    f.path.unlink(missing_ok=True)
                except OSError:
                    pass


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def arg_text(context) -> str:
    """Full argument string after the /command."""
    return " ".join(context.args or []).strip()
