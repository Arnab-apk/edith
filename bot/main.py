"""Entry point: python -m bot.main"""
from __future__ import annotations

import html
import importlib
import logging
import platform
from logging.handlers import RotatingFileHandler
import socket

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from . import confirm
from .actions.registry import load_all
from .config import settings
from .modules import MODULE_NAMES
from .security import RedactingFormatter, is_allowed, owner_only, redact

_fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")  # never log the token/keys
_handlers: list[logging.Handler] = [logging.StreamHandler(),
                                    RotatingFileHandler(settings.log_dir / "bot.log", maxBytes=5_000_000,
                                                        backupCount=3, encoding="utf-8")]
for _hd in _handlers:
    _hd.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=_handlers)
# httpx logs every request URL at INFO - and Bot API URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("controller")

BASE_COMMANDS: list[tuple[str, str]] = [("start", "Welcome + quick menu"), ("help", "List all commands")]
ALL_COMMANDS: list[tuple[str, str]] = list(BASE_COMMANDS)  # rebuilt by build_app()


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [f"🖥 <b>{html.escape(socket.gethostname())}</b> remote controller\n"]
    # descriptions contain things like "<pid|name>": unescaped, Telegram rejects the whole message
    lines += [f"/{name} - {html.escape(desc)}" for name, desc in ALL_COMMANDS]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Handler error", exc_info=context.error)
    # Only the owner ever hears about errors, and never with secrets in them.
    if isinstance(update, Update) and update.effective_message and is_allowed(update):
        try:
            await update.effective_message.reply_text(redact(f"⚠️ Error: {context.error}")[:4000])
        except Exception:
            pass


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(n, d[:256]) for n, d in ALL_COMMANDS])
    msg = f"✅ {socket.gethostname()} is online ({platform.system()} {platform.release()}). Send /help"
    for uid in settings.allowed_user_ids:
        try:
            await app.bot.send_message(uid, msg)
        except Exception as e:  # user may not have /start-ed the bot yet
            log.warning("Could not notify %s: %s", uid, e)


def build_app() -> Application:
    # concurrent_updates: confirm buttons are awaited inside handlers (see bot/confirm.py)
    app = (Application.builder().token(settings.bot_token).post_init(post_init)
           .concurrent_updates(True).build())
    load_all()  # register every action before modules/Edith read the registry
    ALL_COMMANDS[:] = BASE_COMMANDS  # fresh list: calling build_app() twice must not duplicate /help
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    confirm.register(app)
    for name in MODULE_NAMES:
        try:
            mod = importlib.import_module(f".modules.{name}", __package__)
            mod.register(app)
            ALL_COMMANDS.extend(mod.COMMANDS)
            log.info("Loaded module %s (%d commands)", name, len(mod.COMMANDS))
        except Exception:
            log.exception("Failed to load module %s - skipping", name)
    app.add_error_handler(on_error)
    return app


def main() -> None:
    log.info("Starting; authorized users: %s", sorted(settings.allowed_user_ids))
    build_app().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
