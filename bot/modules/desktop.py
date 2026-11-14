"""Desktop commands: /screenshot /record /webcam /clip /url /msg /type /hotkey /mouse.

Thin Telegram layer over bot.actions.desktop. Dangerous actions (type/hotkey/mouse/webcam) go
through bot.confirm.confirm_and_run so the owner always gets a Confirm button first.
"""
from __future__ import annotations

import io

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from ..actions.registry import ActionResult, run_action
from ..confirm import confirm_and_run
from ..security import owner_only
from ..utils import send_result

COMMANDS: list[tuple[str, str]] = [
    ("screenshot", "Screenshot of all monitors [monitor] [full]"),
    ("record", "Record the screen N seconds (max 60)"),
    ("webcam", "Take one webcam photo"),
    ("clip", "Clipboard: /clip get | /clip set <text>"),
    ("url", "Open a http(s) URL in the PC browser"),
    ("msg", "Show a popup message on the PC"),
    ("type", "Type text on the PC (confirm)"),
    ("hotkey", "Press keys, e.g. ctrl+c (confirm)"),
    ("mouse", "Mouse: move x y | moveby dx dy | click [x y] | right | double | scroll N"),
]

SHOT_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("🔄 Again", callback_data="desktop:shot"),
    InlineKeyboardButton("🗂 Full-res", callback_data="desktop:shotfull"),
    InlineKeyboardButton("🎥 Record 10s", callback_data="desktop:rec:10"),
]])

MOUSE_USAGE = ("Usage:\n/mouse - show cursor position\n/mouse move X Y\n/mouse moveby DX DY\n"
               "/mouse click [X Y]  (also: double, right, middle)\n/mouse scroll N  (positive = up)")

_MOUSE_OPS = {"move": "move", "moveto": "move", "moveby": "move_by", "move_by": "move_by", "rel": "move_by",
              "click": "click", "left": "click", "double": "double_click", "dblclick": "double_click",
              "double_click": "double_click", "right": "right_click", "rightclick": "right_click",
              "right_click": "right_click", "middle": "middle_click", "middle_click": "middle_click",
              "scroll": "scroll"}


# ------------------------------------------------------------------ arg parsing (pure, tested)
def raw_args(update: Update) -> str:
    """Text after the /command, keeping newlines and spacing (context.args loses them)."""
    text = update.effective_message.text or update.effective_message.caption or ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def parse_mouse_args(args: list[str]) -> dict:
    """['click', '10', '20'] -> {'action': 'click', 'x': 10, 'y': 20}. Raises ValueError."""
    if not args:
        raise ValueError("no mouse action")
    op = _MOUSE_OPS.get(args[0].lower())
    if op is None:
        raise ValueError(f"unknown mouse action {args[0]!r}")
    nums = [int(a) for a in args[1:]]
    if op == "scroll":
        if len(nums) != 1 or nums[0] == 0:
            raise ValueError("scroll needs one non-zero number")
        return {"action": op, "amount": nums[0]}
    if op in ("move", "move_by"):
        if len(nums) != 2:
            raise ValueError(f"{args[0]} needs X Y")
        return {"action": op, "x": nums[0], "y": nums[1]}
    if len(nums) not in (0, 2):
        raise ValueError("clicks take no coordinates or X Y")
    return {"action": op, **({"x": nums[0], "y": nums[1]} if nums else {})}


def parse_screenshot_args(args: list[str]) -> dict:
    kw: dict = {}
    for a in args:
        a = a.lower()
        if a in ("full", "fullres", "full_res", "png", "doc"):
            kw["full_res"] = True
        elif a.isdigit():
            kw["monitor"] = int(a)
        else:
            raise ValueError(f"unknown option {a!r} (use a monitor number and/or 'full')")
    return kw


def _preview(text: str, n: int = 80) -> str:
    one = text.replace("\n", "⏎")
    return one if len(one) <= n else one[:n] + "…"


# ------------------------------------------------------------------ helpers
async def _send_shot(context: ContextTypes.DEFAULT_TYPE, chat_id: int, result: ActionResult) -> None:
    """Screenshot photo gets Again/Full-res/Record buttons; anything else goes via send_result."""
    if not (result.ok and result.files and result.files[0].kind == "photo" and result.files[0].data):
        await send_result(context.bot, chat_id, result)
        return
    photo, rest = result.files[0], result.files[1:]
    await context.bot.send_photo(chat_id, io.BytesIO(photo.data), caption=result.text[:1024] or None,
                                 reply_markup=SHOT_KB)
    if rest:
        await send_result(context.bot, chat_id, ActionResult(files=rest))


async def _do_screenshot(context, chat_id: int, **kw) -> None:
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
    await _send_shot(context, chat_id, await run_action("screenshot", **kw))


async def _do_record(context, chat_id: int, seconds: int) -> None:
    seconds = max(1, min(60, seconds))
    await context.bot.send_message(chat_id, f"🎥 Recording the screen for {seconds}s…")
    result = await run_action("record_screen", seconds=seconds)
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
    await send_result(context.bot, chat_id, result)


# ------------------------------------------------------------------ handlers
@owner_only
async def cmd_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        kw = parse_screenshot_args(context.args or [])
    except ValueError as e:
        await update.effective_message.reply_text(f"⚠️ {e}")
        return
    await _do_screenshot(context, update.effective_chat.id, **kw)


@owner_only
async def cmd_record(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    try:
        seconds = int(args[0]) if args else 10
    except ValueError:
        await update.effective_message.reply_text("Usage: /record [seconds 1-60]")
        return
    await _do_record(context, update.effective_chat.id, seconds)


@owner_only
async def cmd_webcam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    kw = {"camera": int(args[0])} if args and args[0].isdigit() else {}
    await confirm_and_run(update, context, "webcam_photo", **kw,
                          summary=f"Turn on webcam {kw.get('camera', 0)} and take one photo")


@owner_only
async def cmd_clip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    parts = raw_args(update).split(maxsplit=1)
    sub = parts[0].lower() if parts else "get"
    payload = parts[1] if len(parts) > 1 else ""
    if sub in ("get", "show", "read"):
        await send_result(context.bot, chat_id, await run_action("clipboard_get"))
    elif sub in ("set", "copy", "put"):
        reply = update.effective_message.reply_to_message
        text = payload or ((reply.text or reply.caption or "") if reply else "")
        if not text:
            await update.effective_message.reply_text("Usage: /clip set <text> (or reply to a message with /clip set)")
            return
        await send_result(context.bot, chat_id, await run_action("clipboard_set", text=text))
    else:
        await update.effective_message.reply_text("Usage: /clip get | /clip set <text>")


@owner_only
async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = raw_args(update).strip()
    if not url:
        await update.effective_message.reply_text("Usage: /url https://example.com")
        return
    await send_result(context.bot, update.effective_chat.id, await run_action("open_url", url=url))


@owner_only
async def cmd_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = raw_args(update).strip()
    if not text:
        await update.effective_message.reply_text("Usage: /msg <text to show on the PC screen>")
        return
    await send_result(context.bot, update.effective_chat.id, await run_action("show_message", text=text))


@owner_only
async def cmd_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = raw_args(update)
    if not text.strip():
        await update.effective_message.reply_text("Usage: /type <text> (types into the focused window)")
        return
    await confirm_and_run(update, context, "type_text", text=text,
                          summary=f"Type {len(text)} chars on the PC: “{_preview(text)}”")


@owner_only
async def cmd_hotkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keys = raw_args(update).strip()
    if not keys:
        await update.effective_message.reply_text("Usage: /hotkey ctrl+c   (e.g. alt+tab, win+d, ctrl+shift+esc)")
        return
    await confirm_and_run(update, context, "press_hotkey", keys=keys, summary=f"Press {keys} on the PC")


@owner_only
async def cmd_mouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    chat_id = update.effective_chat.id
    if not args or args[0].lower() in ("pos", "position", "where"):
        res = await run_action("mouse_position")
        res.text += "\n\n" + MOUSE_USAGE
        await send_result(context.bot, chat_id, res)
        return
    try:
        kw = parse_mouse_args(args)
    except ValueError as e:
        await update.effective_message.reply_text(f"⚠️ {e}\n\n{MOUSE_USAGE}")
        return
    where = f" at {kw['x']},{kw['y']}" if "x" in kw else ""
    amount = f" by {kw['amount']}" if "amount" in kw else ""
    await confirm_and_run(update, context, "mouse_control", **kw,
                          summary=f"Mouse {kw['action'].replace('_', ' ')}{where}{amount}")


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    chat_id = update.effective_chat.id
    await q.answer()
    if parts[1:2] == ["shot"]:
        await _do_screenshot(context, chat_id)
    elif parts[1:2] == ["shotfull"]:
        await _do_screenshot(context, chat_id, full_res=True)
    elif parts[1:2] == ["rec"] and len(parts) == 3 and parts[2].isdigit():
        await _do_record(context, chat_id, int(parts[2]))


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["screenshot", "ss", "shot"], cmd_screenshot))
    app.add_handler(CommandHandler("record", cmd_record))
    app.add_handler(CommandHandler("webcam", cmd_webcam))
    app.add_handler(CommandHandler("clip", cmd_clip))
    app.add_handler(CommandHandler("url", cmd_url))
    app.add_handler(CommandHandler("msg", cmd_msg))
    app.add_handler(CommandHandler("type", cmd_type))
    app.add_handler(CommandHandler("hotkey", cmd_hotkey))
    app.add_handler(CommandHandler("mouse", cmd_mouse))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^desktop:"))
