"""Owner-only access control + audit logging.

Every handler MUST be wrapped with @owner_only. Unauthorized users get no reply
at all (the bot stays silent so it can't be probed), and the attempt is logged.
"""
from __future__ import annotations

import functools
import logging
import os
from logging.handlers import RotatingFileHandler
import re

from telegram import Update
from telegram.ext import ContextTypes

from .config import settings

# ---------------------------------------------------------------- secret redaction
SECRET_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY")
_SECRET_PATTERNS = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),       # Telegram bot tokens
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"),           # Anthropic keys
    re.compile(r"\b(?:sk|gsk|AIza)[A-Za-z0-9_-]{20,}"),   # OpenAI / Groq / Google-style keys
]


def redact(text: object) -> str:
    """Remove the bot token and API keys from text bound for logs or chat."""
    s = str(text)
    secrets_ = [settings.bot_token] + [os.getenv(k, "") for k in SECRET_ENV_KEYS]
    for sec in sorted({x.strip() for x in secrets_ if x and len(x.strip()) >= 8}, key=len, reverse=True):
        s = s.replace(sec, "[REDACTED]")
    for rx in _SECRET_PATTERNS:
        s = rx.sub("[REDACTED]", s)
    return s


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs secrets from the final line, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


audit = logging.getLogger("audit")
# rotating: strangers spamming the bot must not fill the disk
_h = RotatingFileHandler(settings.log_dir / "audit.log", maxBytes=5_000_000, backupCount=3,
                         encoding="utf-8")
_h.setFormatter(RedactingFormatter("%(asctime)s %(message)s"))
audit.addHandler(_h)
audit.setLevel(logging.INFO)


def is_allowed(update: Update) -> bool:
    """Allowed = an allowlisted user in a PRIVATE chat. Groups/channels are refused even for the
    owner: screenshots, files and shell output would otherwise be visible to every member."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or user.id not in settings.allowed_user_ids:
        return False
    return chat is None or chat.type == "private"


def owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        what = (
            update.effective_message.text if update.effective_message and update.effective_message.text
            else (update.callback_query.data if update.callback_query else type(update).__name__)
        )
        if not is_allowed(update):
            audit.warning("DENIED user=%s (%s) action=%r",
                          user.id if user else None, user.username if user else None, what)
            if update.callback_query:
                await update.callback_query.answer()
            return None
        audit.info("OK user=%s action=%r", user.id, what)
        return await func(update, context, *args, **kwargs)

    return wrapper
