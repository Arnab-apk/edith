"""Desktop actions: screen capture/recording, webcam, clipboard, URL, popup, keyboard/mouse input.

Heavy/OS libraries (mss, PyAV, opencv, pyautogui, pyperclip) are imported lazily inside the
sync helpers so this module always imports, and tests can monkeypatch the small `_xxx` helpers.
Input actions (type_text, press_hotkey, mouse_control) and webcam_photo are dangerous=True: callers
confirm first.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import io
import logging
import os
import re
import tempfile
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional
from urllib.parse import urlsplit

from ..utils import run_blocking
from .registry import ActionFile, ActionResult, action

log = logging.getLogger("actions.desktop")

PHOTO_MAX_SIDE = 2560        # Telegram photos get recompressed anyway; keep uploads small
RECORD_MAX_SECONDS = 60
RECORD_MAX_WIDTH = 1280      # downscale recordings to keep mp4 size well under 50 MB
TYPE_MAX_CHARS = 2000
POPUP_MAX_CHARS = 2000
CLIP_MAX_CHARS = 100_000

_record_lock = threading.Lock()
_webcam_lock = threading.Lock()


# ================================================================== screen capture
def _grab(monitor: int):
    """Capture one monitor (0 = all monitors combined). Returns (PIL.Image RGB, monitor dict, n_monitors)."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        mons = sct.monitors
        if not 0 <= monitor < len(mons):
            raise IndexError(f"monitor must be 0 (all) .. {len(mons) - 1}")
        mon = mons[monitor]
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img, dict(mon), len(mons) - 1


def _cursor_pos() -> tuple[int, int] | None:
    """Virtual-screen cursor position (None if unavailable)."""
    try:
        pt = ctypes.wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return pt.x, pt.y
    except Exception:  # noqa: BLE001 - cursor overlay is cosmetic
        pass
    return None


def _draw_cursor(img, mon: dict) -> None:
    """mss does not capture the mouse pointer - draw a visible ring where it is."""
    pos = _cursor_pos()
    if pos is None:
        return
    x, y = pos[0] - mon.get("left", 0), pos[1] - mon.get("top", 0)
    if not (0 <= x < img.width and 0 <= y < img.height):
        return
    from PIL import ImageDraw

    r = max(6, img.width // 200)
    d = ImageDraw.Draw(img)
    d.ellipse((x - r, y - r, x + r, y + r), outline=(255, 0, 0), width=max(2, r // 3))


def _screenshot_sync(monitor: int, full_res: bool) -> tuple[bytes, bytes | None, tuple[int, int], int]:
    img, mon, n = _grab(monitor)
    _draw_cursor(img, mon)
    size = img.size
    full = None
    if full_res:
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=3)
        full = buf.getvalue()
    small = img.copy()
    small.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    return buf.getvalue(), full, size, n


@action("screenshot", "Take a screenshot of the PC screen (all monitors by default) and send it as a photo.",
        category="desktop",
        params={"monitor": "0 = all monitors combined, 1..N = a single monitor",
                "full_res": "also send the lossless full-resolution PNG as a document"})
async def screenshot(monitor: int = 0, full_res: bool = False) -> ActionResult:
    try:
        jpeg, png, (w, h), n = await run_blocking(_screenshot_sync, monitor, full_res)
    except IndexError as e:
        return ActionResult(text=f"Bad monitor: {e}", ok=False)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    where = "all monitors" if monitor == 0 else f"monitor {monitor}"
    text = f"🖥 Screenshot {w}×{h} ({where}, {n} display{'s' if n != 1 else ''})"
    files = [ActionFile(data=jpeg, filename=f"screenshot_{stamp}.jpg", kind="photo")]
    if png is not None:
        files.append(ActionFile(data=png, filename=f"screenshot_{stamp}.png", kind="document",
                                caption="Full resolution"))
    return ActionResult(text=text, files=files, data={"width": w, "height": h, "monitors": n})


# ================================================================== screen recording
def _even(n: int) -> int:
    return max(2, n - (n % 2))


def _target_size(w: int, h: int, max_w: int = RECORD_MAX_WIDTH) -> tuple[int, int]:
    if w > max_w:
        h = round(h * max_w / w)
        w = max_w
    return _even(w), _even(h)


def _encode_mp4(frames: Iterable[tuple[float, "object"]], path: Path, fps: int) -> int:
    """Encode (seconds_since_start, PIL.Image) frames into an H.264 mp4. Returns frames written.
    Uses timestamp-based PTS so the video plays in real time even if capture lags."""
    import av

    tb = Fraction(1, fps)
    container = av.open(str(path), mode="w", options={"movflags": "faststart"})
    stream = None
    size = (0, 0)
    written, last_pts = 0, -1
    try:
        for t, img in frames:
            if stream is None:
                size = _target_size(*img.size, max_w=10_000)
                stream = _add_video_stream(container, size, fps)
            if img.size != size:
                img = img.resize(size)
            pts = int(round(t * fps))
            if pts <= last_pts:
                continue  # captured faster than fps - drop the duplicate slot
            frame = av.VideoFrame.from_image(img)
            frame.pts, frame.time_base = pts, tb
            for pkt in stream.encode(frame):
                container.mux(pkt)
            last_pts, written = pts, written + 1
        if stream is not None:
            for pkt in stream.encode(None):
                container.mux(pkt)
    finally:
        container.close()
    return written


def _add_video_stream(container, size: tuple[int, int], fps: int):
    last_err: Exception | None = None
    for codec, opts in (("libx264", {"crf": "28", "preset": "veryfast"}), ("h264_mf", {}), ("mpeg4", {})):
        try:
            stream = container.add_stream(codec, rate=fps, options=opts)
        except Exception as e:  # noqa: BLE001 - try the next encoder
            last_err = e
            continue
        stream.width, stream.height = size
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = Fraction(1, fps)
        return stream
    raise RuntimeError(f"no usable video encoder: {last_err}")


def _capture_frames(monitor: int, seconds: float, fps: int) -> Iterator[tuple[float, "object"]]:
    import mss
    from PIL import Image

    with mss.mss() as sct:
        mons = sct.monitors
        if not 0 <= monitor < len(mons):
            raise IndexError(f"monitor must be 0 (all) .. {len(mons) - 1}")
        mon = mons[monitor]
        size = _target_size(mon["width"], mon["height"])
        start = time.perf_counter()
        i = 0
        while (t := time.perf_counter() - start) < seconds:
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            _draw_cursor(img, mon)
            if img.size != size:
                img = img.resize(size)
            yield t, img
            i += 1
            delay = i / fps - (time.perf_counter() - start)
            if delay > 0:
                time.sleep(delay)


def _record_sync(monitor: int, seconds: int, fps: int) -> tuple[Path, int]:
    if not _record_lock.acquire(blocking=False):
        raise RuntimeError("a screen recording is already running")
    fd, name = tempfile.mkstemp(prefix="screenrec_", suffix=".mp4")
    os.close(fd)
    path = Path(name)
    try:
        n = _encode_mp4(_capture_frames(monitor, seconds, fps), path, fps)
        return path, n
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        _record_lock.release()


@action("record_screen", "Record the PC screen for N seconds (max 60) and send it as an mp4 video.",
        category="desktop",
        params={"seconds": "duration in seconds, 1-60", "monitor": "0 = all monitors, 1..N = one monitor",
                "fps": "frames per second, 1-30"})
async def record_screen(seconds: int = 10, monitor: int = 0, fps: int = 10) -> ActionResult:
    seconds = max(1, min(RECORD_MAX_SECONDS, seconds))
    fps = max(1, min(30, fps))
    try:
        path, frames = await run_blocking(_record_sync, monitor, seconds, fps)
    except (IndexError, RuntimeError) as e:
        return ActionResult(text=f"Recording failed: {e}", ok=False)
    mb = path.stat().st_size / 1_048_576
    if mb > 49:
        path.unlink(missing_ok=True)
        return ActionResult(text=f"Recording too large for Telegram ({mb:.0f} MB) - try fewer seconds/fps.",
                            ok=False)
    return ActionResult(
        text=f"🎥 Screen recording: {seconds}s, {frames} frames, {mb:.1f} MB",
        files=[ActionFile(path=path, filename=f"screen_{time.strftime('%Y%m%d_%H%M%S')}.mp4",
                          kind="video", delete_after_send=True)],
        data={"seconds": seconds, "frames": frames})


# ================================================================== webcam
def _webcam_sync(camera: int) -> bytes:
    """Grab ONE frame and release the camera immediately (the webcam LED goes off right away)."""
    import cv2

    if not _webcam_lock.acquire(blocking=False):
        raise RuntimeError("the webcam is busy with another capture")
    cap = None
    try:
        cap = cv2.VideoCapture(camera, cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise RuntimeError(f"camera {camera} not available")
        frame = None
        for _ in range(8):  # first frames are often dark while auto-exposure settles
            ok, f = cap.read()
            if ok:
                frame = f
        if frame is None:
            raise RuntimeError("camera returned no image")
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return buf.tobytes()
    finally:
        if cap is not None:
            cap.release()
        _webcam_lock.release()


# dangerous=True (reviewer decision): turning a camera on is a privacy action, so it is ALWAYS
# confirmed - from /webcam, from Edith and inside automations - unlike screenshots, which only
# show the owner their own screen.
@action("webcam_photo", "Take ONE photo with the PC webcam (only when the owner explicitly asks). "
        "The camera is released immediately after.", dangerous=True, category="desktop",
        params={"camera": "camera index, 0 = default"})
async def webcam_photo(camera: int = 0) -> ActionResult:
    try:
        jpeg = await run_blocking(_webcam_sync, camera)
    except RuntimeError as e:
        return ActionResult(text=f"Webcam: {e}", ok=False)
    return ActionResult(text="📷 Webcam photo",
                        files=[ActionFile(data=jpeg, filename=f"webcam_{time.strftime('%Y%m%d_%H%M%S')}.jpg",
                                          kind="photo")])


# ================================================================== clipboard
def _clip_paste() -> str:
    import pyperclip
    return pyperclip.paste() or ""


def _clip_copy(text: str) -> None:
    import pyperclip
    pyperclip.copy(text)


@action("clipboard_get", "Read the current text on the PC clipboard.", category="desktop")
async def clipboard_get() -> ActionResult:
    text = await run_blocking(_clip_paste)
    if not text:
        return ActionResult(text="📋 Clipboard is empty (or holds no text).", data={"text": ""})
    return ActionResult(text=f"📋 Clipboard ({len(text)} chars):\n{text}", data={"text": text})


@action("clipboard_set", "Put text on the PC clipboard.", category="desktop",
        params={"text": "the text to copy"})
async def clipboard_set(text: str) -> ActionResult:
    if len(text) > CLIP_MAX_CHARS:
        return ActionResult(text=f"Text too long (max {CLIP_MAX_CHARS} chars).", ok=False)
    await run_blocking(_clip_copy, text)
    return ActionResult(text=f"📋 Copied {len(text)} chars to the PC clipboard.")


# ================================================================== URL
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-\[\]:]+$")


def normalize_url(url: str) -> str:
    """Return a safe http(s) URL or raise ValueError. Bare domains get https://."""
    url = url.strip()
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("URL is empty or contains spaces/control characters")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("only http:// and https:// URLs are allowed")
    host = parts.hostname or ""
    if not host or not _HOST_RE.match(host):
        raise ValueError("URL has no valid host")
    try:
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        raise ValueError("URL has an invalid port") from None
    return url


def _open_os(url: str) -> None:
    os.startfile(url)  # type: ignore[attr-defined]  # opens the default browser for http(s)


@action("open_url", "Open a web page (http/https only) in the PC's default browser.", category="desktop",
        params={"url": "the http(s) URL, e.g. https://example.com"})
async def open_url(url: str) -> ActionResult:
    try:
        clean = normalize_url(url)
    except ValueError as e:
        return ActionResult(text=f"Not opened: {e}", ok=False)
    await run_blocking(_open_os, clean)
    return ActionResult(text=f"🌐 Opened {clean}")


# ================================================================== popup message
_MB_OK, _MB_ICONINFORMATION, _MB_SETFOREGROUND, _MB_TOPMOST = 0x0, 0x40, 0x10000, 0x40000


def _message_box(text: str, title: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title,
                                     _MB_OK | _MB_ICONINFORMATION | _MB_SETFOREGROUND | _MB_TOPMOST)


@action("show_message", "Show a visible popup message box on the PC screen (returns immediately).",
        category="desktop", params={"text": "message to show", "title": "window title"})
async def show_message(text: str, title: str = "Message from Telegram") -> ActionResult:
    text = text.strip()
    if not text:
        return ActionResult(text="Nothing to show.", ok=False)
    text, title = text[:POPUP_MAX_CHARS], (title.strip() or "Message")[:100]
    # Own daemon thread: MessageBoxW blocks until dismissed, so it must not hold a pool worker.
    threading.Thread(target=_message_box, args=(text, title), name="popup", daemon=True).start()
    return ActionResult(text="💬 Popup shown on the PC.")


# ================================================================== keyboard / mouse (DANGEROUS)
def _pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = True  # owner at the PC can abort by slamming the mouse into a corner
    return pyautogui


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):  # only here so the INPUT union has the right size
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _send_unicode(text: str) -> None:
    """Type arbitrary Unicode (e.g. non-Latin scripts, emoji) via SendInput KEYEVENTF_UNICODE."""
    INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, VK_RETURN = 1, 0x2, 0x4, 0x0D
    send = ctypes.windll.user32.SendInput
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            events = [(VK_RETURN, 0, 0), (VK_RETURN, 0, KEYEVENTF_KEYUP)]
        else:
            data = ch.encode("utf-16-le")
            events = []
            for i in range(0, len(data), 2):  # surrogate pairs -> two code units
                unit = int.from_bytes(data[i:i + 2], "little")
                events += [(0, unit, KEYEVENTF_UNICODE), (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
        arr = (_INPUT * len(events))()
        for i, (vk, scan, flags) in enumerate(events):
            arr[i].type = INPUT_KEYBOARD
            arr[i].u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        send(len(events), arr, ctypes.sizeof(_INPUT))
        time.sleep(0.005)


def _type_sync(text: str) -> None:
    if text.isascii():
        _pyautogui().write(text, interval=0.01)
    else:
        _send_unicode(text)


@action("type_text", "Type text on the PC keyboard into the focused window (newlines press Enter).",
        dangerous=True, category="desktop", params={"text": "text to type"})
async def type_text(text: str) -> ActionResult:
    if not text:
        return ActionResult(text="Nothing to type.", ok=False)
    if len(text) > TYPE_MAX_CHARS:
        return ActionResult(text=f"Too long (max {TYPE_MAX_CHARS} chars).", ok=False)
    await run_blocking(_type_sync, text)
    return ActionResult(text=f"⌨️ Typed {len(text)} chars.")


_KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "windows": "win", "super": "win", "cmd": "win", "meta": "win",
    "escape": "esc", "return": "enter", "del": "delete", "ins": "insert", "pgup": "pageup",
    "pgdn": "pagedown", "pagedn": "pagedown", "prtsc": "printscreen", "printscr": "printscreen",
    "caps": "capslock", "option": "alt", "spacebar": "space", "bksp": "backspace",
}


def parse_keys(keys: str) -> list[str]:
    """'Ctrl+Shift+Esc' / 'ctrl shift esc' -> ['ctrl', 'shift', 'esc']. Raises ValueError on unknown keys."""
    raw = [k for k in re.split(r"[+\s]+", keys.strip().lower()) if k]
    if keys.strip().endswith("++"):  # 'ctrl++' means ctrl and the plus key
        raw.append("+")
    if not raw:
        raise ValueError("no keys given")
    if len(raw) > 6:
        raise ValueError("at most 6 keys in one hotkey")
    valid = set(_pyautogui().KEYBOARD_KEYS)
    out = []
    for k in raw:
        k = _KEY_ALIASES.get(k, k)
        if k not in valid:
            raise ValueError(f"unknown key {k!r}")
        out.append(k)
    return out


@action("press_hotkey", "Press a key or key combination on the PC, e.g. 'ctrl+c', 'alt+tab', 'win+d', 'enter'.",
        dangerous=True, category="desktop", params={"keys": "keys joined by '+', e.g. ctrl+shift+esc"})
async def press_hotkey(keys: str) -> ActionResult:
    try:
        combo = parse_keys(keys)
    except ValueError as e:
        return ActionResult(text=f"Bad hotkey: {e}", ok=False)
    await run_blocking(lambda: _pyautogui().hotkey(*combo, interval=0.05))
    return ActionResult(text=f"⌨️ Pressed {'+'.join(combo)}")


MouseOp = Literal["move", "move_by", "click", "double_click", "right_click", "middle_click", "scroll"]


def _mouse_sync(op: str, x: int | None, y: int | None, amount: int | None) -> tuple[int, int]:
    pg = _pyautogui()
    if op == "move":
        pg.moveTo(x, y, duration=0.2)
    elif op == "move_by":
        pg.moveRel(x or 0, y or 0, duration=0.2)
    elif op == "scroll":
        pg.scroll(amount)
    else:
        button = {"click": "left", "double_click": "left", "right_click": "right", "middle_click": "middle"}[op]
        clicks = 2 if op == "double_click" else 1
        if x is not None and y is not None:
            pg.click(x, y, clicks=clicks, interval=0.1, button=button)
        else:
            pg.click(clicks=clicks, interval=0.1, button=button)
    pos = pg.position()
    return int(pos[0]), int(pos[1])


@action("mouse_control", "Move, click or scroll the PC mouse. move: x,y absolute; move_by: x,y relative; "
        "clicks: optional x,y to click at; scroll: amount (positive = up).",
        dangerous=True, category="desktop",
        params={"action": "move | move_by | click | double_click | right_click | middle_click | scroll",
                "x": "x pixel (absolute for move/click, delta for move_by)",
                "y": "y pixel (absolute for move/click, delta for move_by)",
                "amount": "scroll clicks, positive = up, negative = down"})
async def mouse_control(action: MouseOp, x: Optional[int] = None, y: Optional[int] = None,
                        amount: Optional[int] = None) -> ActionResult:
    if action in ("move", "move_by") and (x is None or y is None):
        return ActionResult(text=f"{action} needs x and y.", ok=False)
    if action == "scroll" and not amount:
        return ActionResult(text="scroll needs a non-zero amount.", ok=False)
    if (x is None) != (y is None):
        return ActionResult(text="give both x and y, or neither.", ok=False)
    pos = await run_blocking(_mouse_sync, action, x, y, amount)
    return ActionResult(text=f"🖱 {action.replace('_', ' ')} done - cursor at {pos[0]},{pos[1]}",
                        data={"x": pos[0], "y": pos[1]})


def _mouse_pos_sync() -> tuple[tuple[int, int], tuple[int, int]]:
    pg = _pyautogui()
    p, s = pg.position(), pg.size()
    return (int(p[0]), int(p[1])), (int(s[0]), int(s[1]))


@action("mouse_position", "Get the current mouse cursor position and primary screen size.", category="desktop")
async def mouse_position() -> ActionResult:
    (x, y), (w, h) = await run_blocking(_mouse_pos_sync)
    return ActionResult(text=f"🖱 Cursor at {x},{y} (primary screen {w}×{h})",
                        data={"x": x, "y": y, "width": w, "height": h})
