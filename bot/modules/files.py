"""Telegram UI for file actions: /ls /get /find /rm /mkdir /mv + saving documents/photos the owner
sends into settings.upload_dir. Callback prefix "files:" (inline browser buttons from /ls)."""
from __future__ import annotations

import itertools
from collections import OrderedDict
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from ..actions.files import reserve_upload_path
from ..actions.registry import ActionResult, run_action
from ..actions.shell import resolve_path, split_raw, strip_quotes
from ..confirm import confirm_and_run
from ..security import owner_only
from ..utils import TG_LIMIT, human_bytes, run_blocking, send_result
from .shell import raw_args

COMMANDS = [
    ("ls", "List a folder (tap to browse/download)"),
    ("get", "Send a file to Telegram (folders are zipped, 50 MB max)"),
    ("find", "Find files by name: /find <pattern> [folder]"),
    ("rm", "Move a file/folder to the Recycle Bin (asks to confirm)"),
    ("mkdir", "Create a folder"),
    ("mv", "Move/rename: /mv <src> <dst> (asks to confirm)"),
]

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API getFile limit
BUTTONS_PER_LISTING = 30
_MAX_LISTINGS = 50
# listing id -> (chat_id, listed folder, [entry (path, is_dir)]) for the /ls browser buttons
_LISTINGS: OrderedDict[str, tuple[int, str, list[tuple[str, bool]]]] = OrderedDict()
_ids = itertools.count(1)


def _remember(chat_id: int, folder: str, entries: list[tuple[str, bool]]) -> str:
    lid = format(next(_ids), "x")
    _LISTINGS[lid] = (chat_id, folder, entries)
    while len(_LISTINGS) > _MAX_LISTINGS:
        _LISTINGS.popitem(last=False)
    return lid


def _label(name: str, is_dir: bool) -> str:
    name = name if len(name) <= 28 else name[:26] + "…"
    return ("📁 " if is_dir else "📄 ") + name


async def _send_listing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, res: ActionResult) -> None:
    folder = res.data.get("path")
    entries = res.data.get("entries")
    if not res.ok or not folder or entries is None or res.data.get("is_file"):
        await send_result(context.bot, chat_id, res)
        return
    shown = entries[:BUTTONS_PER_LISTING]
    lid = _remember(chat_id, folder, [(e["path"], e["is_dir"]) for e in shown])
    btns = [InlineKeyboardButton(_label(e["name"], e["is_dir"]), callback_data=f"files:o:{lid}:{i}")
            for i, e in enumerate(shown)]
    rows = [[InlineKeyboardButton("⬆️ Up", callback_data=f"files:up:{lid}")]]
    rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    markup = InlineKeyboardMarkup(rows)
    if len(res.text) <= TG_LIMIT:
        await context.bot.send_message(chat_id, res.text, reply_markup=markup)
    else:
        await send_result(context.bot, chat_id, res)
        await context.bot.send_message(chat_id, f"📂 {folder}", reply_markup=markup)


@owner_only
async def cmd_ls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await _send_listing(context, chat_id, await run_action("list_dir", path=raw_args(update), chat_id=chat_id))


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """files:o:<lid>:<i> opens entry i (folder -> cd + list, file -> send); files:up:<lid> -> parent."""
    q = update.callback_query
    chat_id = update.effective_chat.id
    parts = (q.data or "").split(":")
    entry = _LISTINGS.get(parts[2]) if len(parts) >= 3 else None
    if entry is None or entry[0] != chat_id:
        await q.answer("This listing expired - run /ls again.")
        return
    _, folder, entries = entry
    if parts[1] == "up" and len(parts) == 3:
        target, is_dir = str(Path(folder).parent), True
    elif parts[1] == "o" and len(parts) == 4 and parts[3].isdigit() and int(parts[3]) < len(entries):
        target, is_dir = entries[int(parts[3])]
    else:
        await q.answer("Unknown button")
        return
    await q.answer()
    if is_dir:
        res = await run_action("cd", path=target, chat_id=chat_id)
        if not res.ok:
            await send_result(context.bot, chat_id, res)
            return
        await _send_listing(context, chat_id, await run_action("list_dir", path=target, chat_id=chat_id))
    else:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        await send_result(context.bot, chat_id, await run_action("get_file", path=target, chat_id=chat_id))


@owner_only
async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    path = raw_args(update)
    if not path:
        await update.effective_message.reply_text("Usage: /get <file or folder>")
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    await send_result(context.bot, chat_id, await run_action("get_file", path=path, chat_id=chat_id))


@owner_only
async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    toks = split_raw(raw_args(update))
    if not toks:
        await update.effective_message.reply_text('Usage: /find <pattern> [folder]   e.g. /find *.pdf "D:\\My Docs"')
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    res = await run_action("find_files", pattern=strip_quotes(toks[0]), root=strip_quotes(" ".join(toks[1:])),
                           chat_id=chat_id)
    await send_result(context.bot, chat_id, res)


@owner_only
async def cmd_rm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    path = raw_args(update)
    if not path:
        await update.effective_message.reply_text("Usage: /rm <file or folder>  (goes to the Recycle Bin)")
        return
    chat_id = update.effective_chat.id
    full = await run_blocking(resolve_path, chat_id, path)
    await confirm_and_run(update, context, "delete_path", summary=f"Move to Recycle Bin:\n{full}",
                          path=path, chat_id=chat_id)


@owner_only
async def cmd_mkdir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    path = raw_args(update)
    if not path:
        await update.effective_message.reply_text("Usage: /mkdir <folder>")
        return
    chat_id = update.effective_chat.id
    await send_result(context.bot, chat_id, await run_action("make_dir", path=path, chat_id=chat_id))


@owner_only
async def cmd_mv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    toks = split_raw(raw_args(update))
    if len(toks) != 2:
        await update.effective_message.reply_text('Usage: /mv <src> <dst>  - quote paths with spaces, '
                                                  'e.g. /mv "old name.txt" "D:\\Backup\\"')
        return
    chat_id = update.effective_chat.id
    src, dst = strip_quotes(toks[0]), strip_quotes(toks[1])
    s_full = await run_blocking(resolve_path, chat_id, src)
    d_full = await run_blocking(resolve_path, chat_id, dst)
    await confirm_and_run(update, context, "move_path", summary=f"Move\n{s_full}\n→ {d_full}",
                          src=src, dst=dst, chat_id=chat_id)


# ---------------------------------------------------------------- uploads
@owner_only
async def on_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a document or photo the owner sends into settings.upload_dir (never overwrites)."""
    msg = update.effective_message
    if msg.document:
        tg_obj, name, size = msg.document, msg.document.file_name or f"file_{msg.message_id}", msg.document.file_size
    elif msg.photo:
        tg_obj = msg.photo[-1]  # largest size
        name, size = f"photo_{msg.date:%Y%m%d_%H%M%S}.jpg", tg_obj.file_size
    else:
        return
    if size and size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text(f"⚠️ {name} is {human_bytes(size)}; Telegram bots can only download files up to "
                             f"{human_bytes(TG_DOWNLOAD_LIMIT)}.")
        return
    dest = await run_blocking(reserve_upload_path, name)
    try:
        tg_file = await tg_obj.get_file()
        await tg_file.download_to_drive(dest)
    except Exception as e:
        await run_blocking(dest.unlink, missing_ok=True)
        await msg.reply_text(f"⚠️ Could not save {name}: {e}")
        return
    real = await run_blocking(lambda: dest.stat().st_size)
    await msg.reply_text(f"📥 Saved {dest} ({human_bytes(real)})")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("ls", cmd_ls))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("mkdir", cmd_mkdir))
    app.add_handler(CommandHandler("mv", cmd_mv))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^files:"))
    app.add_handler(MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, on_upload))
