"""Media actions: master volume / mute (pycaw), media keys (ctypes keybd_event), text-to-speech on the PC.

pycaw is COM-based: every call runs in a worker thread that does CoInitialize/CoUninitialize itself.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import queue
import threading
import time
from typing import Literal, Optional

from ..utils import run_blocking
from .registry import ActionResult, action

log = logging.getLogger("actions.media")

SAY_MAX_CHARS = 1000


# ================================================================== volume (pycaw)
def _get_endpoint():
    """IAudioEndpointVolume of the default speakers. Caller must have COM initialized."""
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def _with_endpoint(fn):
    """Run fn(endpoint) with COM initialized on the current (worker) thread."""
    import comtypes

    comtypes.CoInitialize()
    try:
        return fn(_get_endpoint())  # COM pointers die with this frame, before CoUninitialize
    finally:
        comtypes.CoUninitialize()


def _read(ep) -> tuple[int, bool]:
    return round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute())


def _volume_sync(level: int | None, change: int | None) -> tuple[int, bool]:
    def work(ep):
        if level is not None or change is not None:
            cur, _ = _read(ep)
            new = level if level is not None else cur + change
            new = max(0, min(100, new))
            ep.SetMasterVolumeLevelScalar(new / 100, None)
            if new > 0 and ep.GetMute():
                ep.SetMute(0, None)  # setting a volume implies "I want to hear it"
        return _read(ep)

    return _with_endpoint(work)


def _mute_sync(state: str) -> bool:
    def work(ep):
        muted = bool(ep.GetMute())
        want = (not muted) if state == "toggle" else (state == "on")
        ep.SetMute(1 if want else 0, None)
        return bool(ep.GetMute())

    return _with_endpoint(work)


def _vol_text(level: int, muted: bool) -> str:
    return f"🔇 Volume {level}% (muted)" if muted else f"🔊 Volume {level}%"


@action("volume", "Get or set the PC master volume. Give `level` (0-100) to set it, or `change` "
        "(e.g. +10 / -10) to adjust it; neither just reports it.", category="media",
        params={"level": "absolute volume 0-100", "change": "relative change in percent points, e.g. 10 or -10"})
async def volume(level: Optional[int] = None, change: Optional[int] = None) -> ActionResult:
    if level is not None and change is not None:
        return ActionResult(text="Give either level or change, not both.", ok=False)
    if level is not None and not 0 <= level <= 100:
        return ActionResult(text="level must be 0-100.", ok=False)
    lvl, muted = await run_blocking(_volume_sync, level, change)
    return ActionResult(text=_vol_text(lvl, muted), data={"level": lvl, "muted": muted})


@action("mute", "Mute, unmute or toggle mute of the PC speakers.", category="media",
        params={"state": "on = mute, off = unmute, toggle"})
async def mute(state: Literal["toggle", "on", "off"] = "toggle") -> ActionResult:
    muted = await run_blocking(_mute_sync, state)
    return ActionResult(text="🔇 Muted" if muted else "🔊 Unmuted", data={"muted": muted})


# ================================================================== media keys
VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
      "vol_up": 0xAF, "vol_down": 0xAE, "vol_mute": 0xAD}
KEY_LABELS = {"play_pause": "⏯ Play/Pause", "next": "⏭ Next track", "prev": "⏮ Previous track",
              "stop": "⏹ Stop", "vol_up": "🔊 Volume up", "vol_down": "🔉 Volume down", "vol_mute": "🔇 Mute toggle"}
MediaKey = Literal["play_pause", "next", "prev", "stop", "vol_up", "vol_down", "vol_mute"]


def _keybd_event(vk: int, flags: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, flags, 0)


def _press_media_key(vk: int, times: int) -> None:
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
    for _ in range(times):
        _keybd_event(vk, KEYEVENTF_EXTENDEDKEY)
        _keybd_event(vk, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)
        time.sleep(0.02)


@action("media_key", "Press a media key on the PC (play/pause, next, previous, stop, volume up/down, mute).",
        category="media",
        params={"key": "play_pause | next | prev | stop | vol_up | vol_down | vol_mute",
                "times": "how many presses (vol_up/vol_down move ~2% each), 1-25"})
async def media_key(key: MediaKey, times: int = 1) -> ActionResult:
    times = max(1, min(25, times))
    await run_blocking(_press_media_key, VK[key], times)
    text = KEY_LABELS[key] + (f" ×{times}" if times > 1 else "")
    data: dict = {"key": key}
    if key.startswith("vol_"):
        try:
            await asyncio.sleep(0.15)  # let the shell apply the key before we read the level
            lvl, muted = await run_blocking(_volume_sync, None, None)
            text += f" → {_vol_text(lvl, muted)}"
            data.update(level=lvl, muted=muted)
        except Exception:  # noqa: BLE001 - level readout is best-effort
            log.debug("volume readout failed", exc_info=True)
    return ActionResult(text=text, data=data)


# ================================================================== text-to-speech on the PC
def _speak_sync(text: str) -> None:
    """Speak text through the PC speakers with SAPI5 (pyttsx3). Runs on the speech thread."""
    import comtypes
    import pyttsx3

    comtypes.CoInitialize()
    try:
        engine = pyttsx3.init()
        try:
            engine.say(text)
            engine.runAndWait()
        finally:
            engine.stop()
            del engine
    finally:
        comtypes.CoUninitialize()


_say_queue: "queue.Queue[str]" = queue.Queue(maxsize=20)
_say_thread: threading.Thread | None = None
_say_thread_lock = threading.Lock()


def _say_worker() -> None:
    while True:
        text = _say_queue.get()
        try:
            _speak_sync(text)
        except Exception:  # noqa: BLE001 - keep the speech thread alive
            log.exception("text-to-speech failed")
        finally:
            _say_queue.task_done()


def _ensure_say_thread() -> None:
    global _say_thread
    with _say_thread_lock:
        if _say_thread is None or not _say_thread.is_alive():
            _say_thread = threading.Thread(target=_say_worker, name="pc-tts", daemon=True)
            _say_thread.start()


@action("say", "Speak text out loud through the PC speakers (text-to-speech). Returns immediately.",
        category="media", params={"text": "what to say"})
async def say(text: str) -> ActionResult:
    text = " ".join(text.split())
    if not text:
        return ActionResult(text="Nothing to say.", ok=False)
    if len(text) > SAY_MAX_CHARS:
        return ActionResult(text=f"Too long (max {SAY_MAX_CHARS} chars).", ok=False)
    _ensure_say_thread()
    try:
        _say_queue.put_nowait(text)
    except queue.Full:
        return ActionResult(text="Speech queue is full - try again in a moment.", ok=False)
    return ActionResult(text=f"🗣 Speaking on the PC: “{text[:200]}{'…' if len(text) > 200 else ''}”")
