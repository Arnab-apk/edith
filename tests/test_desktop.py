"""Tests for the desktop + media slice. Every OS side effect is monkeypatched:
nothing is typed, clicked, opened, spoken, captured from the real camera, or changed in volume."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from bot.actions import desktop as D
from bot.actions import media as M
from bot.actions.registry import REGISTRY, run_action
from bot.config import settings
from bot.modules import desktop as MD
from bot.modules import media as MM

DESKTOP_ACTIONS = {"screenshot": False, "record_screen": False, "webcam_photo": True,  # reviewer: camera = always confirmed
                   "clipboard_get": False,
                   "clipboard_set": False, "open_url": False, "show_message": False, "type_text": True,
                   "press_hotkey": True, "mouse_control": True, "mouse_position": False}
MEDIA_ACTIONS = {"volume": False, "mute": False, "media_key": False, "say": False}


# ================================================================== registry
def test_actions_registered_with_flags_and_schemas():
    for name, dangerous in {**DESKTOP_ACTIONS, **MEDIA_ACTIONS}.items():
        act = REGISTRY[name]
        assert act.dangerous is dangerous, name
        sch = act.input_schema
        assert sch["type"] == "object" and sch["additionalProperties"] is False
        assert set(sch["required"]) <= set(sch["properties"])
        tool = act.tool_schema()
        assert tool["name"] == name and tool["description"]
        assert ("DANGEROUS" in tool["description"]) is dangerous
    assert REGISTRY["mouse_control"].input_schema["properties"]["action"]["enum"][0] == "move"
    assert "vol_up" in REGISTRY["media_key"].input_schema["properties"]["key"]["enum"]


# ================================================================== screenshot / recording
@pytest.fixture
def fake_screen(monkeypatch):
    def grab(monitor):
        if monitor > 1:
            raise IndexError("monitor must be 0 (all) .. 1")
        return Image.new("RGB", (3000, 1000), (10, 120, 200)), {"left": 0, "top": 0}, 1
    monkeypatch.setattr(D, "_grab", grab)
    monkeypatch.setattr(D, "_cursor_pos", lambda: (50, 50))


async def test_screenshot_photo_and_full_res(fake_screen):
    res = await run_action("screenshot")
    assert res.ok and len(res.files) == 1
    photo = res.files[0]
    assert photo.kind == "photo" and photo.data[:2] == b"\xff\xd8"  # JPEG
    import io
    assert max(Image.open(io.BytesIO(photo.data)).size) <= D.PHOTO_MAX_SIDE
    assert "3000×1000" in res.text

    res = await run_action("screenshot", full_res=True)
    assert [f.kind for f in res.files] == ["photo", "document"]
    assert res.files[1].data[:4] == b"\x89PNG" and res.files[1].filename.endswith(".png")


async def test_screenshot_bad_monitor(fake_screen):
    res = await run_action("screenshot", monitor=5)
    assert not res.ok and "monitor" in res.text


def test_encode_mp4_produces_playable_h264(tmp_path):
    import av
    frames = [(i / 10, Image.new("RGB", (101, 61), (i * 20 % 255, 0, 0))) for i in range(15)]
    frames.insert(3, (0.21, frames[2][1]))  # duplicate pts slot gets dropped
    out = tmp_path / "x.mp4"
    n = D._encode_mp4(frames, out, fps=10)
    assert n == 15 and out.stat().st_size > 0
    with av.open(str(out)) as c:
        s = c.streams.video[0]
        assert s.codec_context.name in ("h264", "mpeg4")
        assert (s.width, s.height) == (100, 60)  # forced even for yuv420p
        assert sum(1 for _ in c.decode(video=0)) == 15


async def test_record_screen_returns_temp_video(monkeypatch):
    def frames(monitor, seconds, fps):
        for i in range(seconds * fps):
            yield i / fps, Image.new("RGB", (64, 48), (0, i * 10 % 255, 0))
    monkeypatch.setattr(D, "_capture_frames", frames)
    res = await run_action("record_screen", seconds=1, fps=5)
    assert res.ok, res.text
    f = res.files[0]
    try:
        assert f.kind == "video" and f.delete_after_send and f.path.exists() and f.path.suffix == ".mp4"
        assert res.data["frames"] == 5
    finally:
        f.path.unlink(missing_ok=True)


async def test_record_screen_clamps_and_cleans_up(monkeypatch):
    seen = {}

    def fake_record(monitor, seconds, fps):
        seen.update(seconds=seconds, fps=fps)
        raise IndexError("monitor must be 0 (all) .. 1")
    monkeypatch.setattr(D, "_record_sync", fake_record)
    res = await run_action("record_screen", seconds=999, fps=500)
    assert not res.ok and seen == {"seconds": 60, "fps": 30}


def test_record_sync_removes_temp_file_on_error(monkeypatch, tmp_path):
    made = []
    real_mkstemp = D.tempfile.mkstemp

    def mkstemp(**kw):
        fd, name = real_mkstemp(dir=tmp_path, **kw)
        made.append(Path(name))
        return fd, name

    def boom(monitor, seconds, fps):
        raise IndexError("bad monitor")
        yield  # pragma: no cover
    monkeypatch.setattr(D.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(D, "_capture_frames", boom)
    with pytest.raises(IndexError):
        D._record_sync(9, 1, 5)
    assert made and not made[0].exists()
    assert not D._record_lock.locked()


# ================================================================== webcam
class FakeCap:
    instances: list["FakeCap"] = []

    def __init__(self, index, api=None, opened=True):
        self.index, self.opened, self.released = index, opened, False
        FakeCap.instances.append(self)

    def isOpened(self):
        return self.opened

    def read(self):
        import numpy as np
        return True, np.zeros((48, 64, 3), dtype="uint8")

    def release(self):
        self.released = True


async def test_webcam_releases_camera(monkeypatch):
    import cv2
    FakeCap.instances.clear()
    monkeypatch.setattr(cv2, "VideoCapture", FakeCap)
    res = await run_action("webcam_photo")
    assert res.ok and res.files[0].kind == "photo" and res.files[0].data[:2] == b"\xff\xd8"
    assert FakeCap.instances[-1].released

    monkeypatch.setattr(cv2, "VideoCapture", lambda i, api=None: FakeCap(i, opened=False))
    res = await run_action("webcam_photo", camera=3)
    assert not res.ok and "camera 3" in res.text
    assert FakeCap.instances[-1].released and not D._webcam_lock.locked()


# ================================================================== clipboard / url / popup
async def test_clipboard(monkeypatch):
    store = {"v": ""}
    monkeypatch.setattr(D, "_clip_paste", lambda: store["v"])
    monkeypatch.setattr(D, "_clip_copy", lambda t: store.update(v=t))
    assert "empty" in (await run_action("clipboard_get")).text
    assert (await run_action("clipboard_set", text="héllo")).ok
    res = await run_action("clipboard_get")
    assert res.data["text"] == "héllo" and "héllo" in res.text


@pytest.mark.parametrize("url,expected", [
    ("example.com", "https://example.com"),
    ("http://example.com/a?b=1", "http://example.com/a?b=1"),
    ("HTTPS://Example.com:8443/x", "HTTPS://Example.com:8443/x"),
])
def test_normalize_url_ok(url, expected):
    assert D.normalize_url(url) == expected


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///C:/Windows/system32/calc.exe", "ftp://x.com",
                                 "http://", "", "http://exa mple.com", "ms-settings://x", "https://x.com:99999",
                                 "https://a\x00b.com", "\\\\server\\share"])
def test_normalize_url_rejects(url):
    with pytest.raises(ValueError):
        D.normalize_url(url)


async def test_open_url_only_opens_valid(monkeypatch):
    opened = []
    monkeypatch.setattr(D, "_open_os", opened.append)
    assert not (await run_action("open_url", url="file:///C:/x")).ok
    assert (await run_action("open_url", url="example.org")).ok
    assert opened == ["https://example.org"]


async def test_show_message_is_non_blocking(monkeypatch):
    release, shown = threading.Event(), []

    def box(text, title):
        shown.append((text, title))
        release.wait(5)  # a real MessageBoxW blocks until dismissed
    monkeypatch.setattr(D, "_message_box", box)
    res = await asyncio.wait_for(run_action("show_message", text="hi there"), timeout=2)
    assert res.ok
    for _ in range(100):
        if shown:
            break
        await asyncio.sleep(0.01)
    release.set()
    assert shown == [("hi there", "Message from Telegram")]
    assert not (await run_action("show_message", text="   ")).ok


# ================================================================== keyboard / mouse
class FakePG:
    KEYBOARD_KEYS = ["ctrl", "shift", "alt", "esc", "win", "c", "v", "tab", "enter", "delete", "+", "f4"]

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(*a, **kw):
            self.calls.append((name, a, kw))
            return (11, 22) if name in ("position", "size") else None
        return rec


@pytest.fixture
def fake_pg(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(D, "_pyautogui", lambda: pg)
    return pg


async def test_type_text_ascii_and_unicode(monkeypatch, fake_pg):
    uni = []
    monkeypatch.setattr(D, "_send_unicode", uni.append)
    assert (await run_action("type_text", text="hello")).ok
    assert fake_pg.calls[0][0] == "write" and fake_pg.calls[0][1] == ("hello",)
    assert (await run_action("type_text", text="নমস্কার 👋")).ok
    assert uni == ["নমস্কার 👋"]
    assert not (await run_action("type_text", text="x" * (D.TYPE_MAX_CHARS + 1))).ok
    assert len(fake_pg.calls) == 1 and len(uni) == 1


def test_parse_keys(fake_pg):
    assert D.parse_keys("Ctrl+Shift+Escape") == ["ctrl", "shift", "esc"]
    assert D.parse_keys("control c") == ["ctrl", "c"]
    assert D.parse_keys("windows+del") == ["win", "delete"]
    assert D.parse_keys("ctrl++") == ["ctrl", "+"]
    for bad in ("", "ctrl+nope", "a+b+c+d+e+f+g"):
        with pytest.raises(ValueError):
            D.parse_keys(bad)


async def test_press_hotkey(fake_pg):
    res = await run_action("press_hotkey", keys="alt+f4")
    assert res.ok and fake_pg.calls[-1][0] == "hotkey" and fake_pg.calls[-1][1] == ("alt", "f4")
    assert not (await run_action("press_hotkey", keys="alt+bogus")).ok


async def test_mouse_control(fake_pg):
    assert not (await run_action("mouse_control", action="move")).ok
    assert not (await run_action("mouse_control", action="click", x=5)).ok
    assert not (await run_action("mouse_control", action="scroll")).ok
    assert not (await run_action("mouse_control", action="teleport")).ok
    fake_pg.calls.clear()
    assert (await run_action("mouse_control", action="move", x=100, y=200)).ok
    assert (await run_action("mouse_control", action="right_click", x=1, y=2)).ok
    assert (await run_action("mouse_control", action="double_click")).ok
    assert (await run_action("mouse_control", action="scroll", amount=-3)).ok
    names = [c[0] for c in fake_pg.calls if c[0] != "position"]
    assert names == ["moveTo", "click", "click", "scroll"]
    clicks = [c for c in fake_pg.calls if c[0] == "click"]
    assert clicks[0][1] == (1, 2) and clicks[0][2]["button"] == "right"
    assert clicks[1][2]["clicks"] == 2
    res = await run_action("mouse_position")
    assert res.data == {"x": 11, "y": 22, "width": 11, "height": 22}


# ================================================================== volume / media keys / say
class FakeEndpoint:
    def __init__(self, level=0.5, muted=0):
        self.level, self.muted = level, muted

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, v, ctx):
        self.level = v

    def GetMute(self):
        return self.muted

    def SetMute(self, m, ctx):
        self.muted = m


@pytest.fixture
def fake_ep(monkeypatch):
    ep = FakeEndpoint()
    monkeypatch.setattr(M, "_get_endpoint", lambda: ep)
    return ep


async def test_volume(fake_ep):
    res = await run_action("volume")
    assert res.data == {"level": 50, "muted": False}
    assert (await run_action("volume", level=30)).data["level"] == 30
    assert (await run_action("volume", change="+80")).data["level"] == 100  # clamped, str coerced
    assert (await run_action("volume", change=-150)).data["level"] == 0
    assert not (await run_action("volume", level=101)).ok
    assert not (await run_action("volume", level=10, change=5)).ok
    fake_ep.muted = 1
    res = await run_action("volume", level=40)
    assert res.data == {"level": 40, "muted": False}  # setting a level unmutes


async def test_mute(fake_ep):
    assert (await run_action("mute")).data["muted"] is True
    assert (await run_action("mute")).data["muted"] is False
    assert (await run_action("mute", state="on")).data["muted"] is True
    assert (await run_action("mute", state="on")).data["muted"] is True
    assert (await run_action("mute", state="off")).data["muted"] is False


async def test_media_key(monkeypatch):
    events = []
    monkeypatch.setattr(M, "_keybd_event", lambda vk, flags: events.append((vk, flags)))
    monkeypatch.setattr(M, "_volume_sync", lambda level, change: (42, False))
    res = await run_action("media_key", key="play_pause")
    assert res.ok and events == [(0xB3, 1), (0xB3, 3)]
    res = await run_action("media_key", key="vol_up", times=3)
    assert len(events) == 2 + 6 and "42%" in res.text and res.data["level"] == 42
    assert not (await run_action("media_key", key="eject")).ok


async def test_say_runs_in_background(monkeypatch):
    spoken, done = [], threading.Event()

    def speak(text):
        spoken.append(text)
        done.set()
    monkeypatch.setattr(M, "_speak_sync", speak)
    res = await run_action("say", text="  hello\n  world ")
    assert res.ok
    assert await asyncio.to_thread(done.wait, 5)
    assert spoken == ["hello world"]
    assert not (await run_action("say", text=" ")).ok
    assert not (await run_action("say", text="a" * (M.SAY_MAX_CHARS + 1))).ok


# ================================================================== Telegram modules
class FakeApp:
    def __init__(self):
        self.handlers = []

    def add_handler(self, h):
        self.handlers.append(h)


@pytest.mark.parametrize("mod,prefix", [(MD, "desktop:"), (MM, "media:")])
def test_modules_register_owner_only_handlers(mod, prefix):
    from telegram.ext import CallbackQueryHandler, CommandHandler
    app = FakeApp()
    mod.register(app)
    cmds = set()
    for h in app.handlers:
        assert hasattr(h.callback, "__wrapped__"), f"{h.callback} not wrapped with owner_only"
        if isinstance(h, CommandHandler):
            cmds |= set(h.commands)
        elif isinstance(h, CallbackQueryHandler):
            assert h.pattern.pattern == f"^{prefix}"
    assert {c for c, _ in mod.COMMANDS} <= cmds


def test_parse_helpers():
    assert MD.parse_mouse_args(["click"]) == {"action": "click"}
    assert MD.parse_mouse_args(["right", "10", "20"]) == {"action": "right_click", "x": 10, "y": 20}
    assert MD.parse_mouse_args(["moveby", "-5", "7"]) == {"action": "move_by", "x": -5, "y": 7}
    assert MD.parse_mouse_args(["scroll", "-3"]) == {"action": "scroll", "amount": -3}
    for bad in ([], ["fly"], ["move", "1"], ["scroll", "0"], ["click", "1"], ["move", "a", "b"]):
        with pytest.raises(ValueError):
            MD.parse_mouse_args(bad)
    assert MD.parse_screenshot_args(["2", "full"]) == {"monitor": 2, "full_res": True}
    with pytest.raises(ValueError):
        MD.parse_screenshot_args(["huh"])
    assert MM.parse_volume_arg("") == {}
    assert MM.parse_volume_arg("40%") == {"level": 40}
    assert MM.parse_volume_arg("+10") == {"change": 10}
    assert MM.parse_volume_arg("-5") == {"change": -5}
    assert MM.parse_volume_arg("up") == {"change": 10}
    for bad in ("150", "loud", "+x"):
        with pytest.raises(ValueError):
            MM.parse_volume_arg(bad)


def _update(text: str, user_id: int):
    msg = SimpleNamespace(text=text, caption=None, reply_text=AsyncMock(), reply_to_message=None)
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="u"), effective_message=msg,
                           effective_chat=SimpleNamespace(id=42, type="private"), callback_query=None)


def _context(args):
    return SimpleNamespace(args=args, bot=SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(),
                                                          send_chat_action=AsyncMock()))


OWNER = next(iter(settings.allowed_user_ids))


async def test_dangerous_commands_go_through_confirm(monkeypatch):
    calls = []

    async def fake_confirm(update, context, name, /, *, summary=None, **kw):
        calls.append((name, kw, summary))
    monkeypatch.setattr(MD, "confirm_and_run", fake_confirm)

    await MD.cmd_type(_update("/type line one\n  line two", OWNER), _context(["line", "one", "line", "two"]))
    await MD.cmd_hotkey(_update("/hotkey ctrl+c", OWNER), _context(["ctrl+c"]))
    await MD.cmd_mouse(_update("/mouse click 5 6", OWNER), _context(["click", "5", "6"]))
    assert calls[0][:2] == ("type_text", {"text": "line one\n  line two"})  # newlines preserved
    assert calls[1][:2] == ("press_hotkey", {"keys": "ctrl+c"})
    assert calls[2][:2] == ("mouse_control", {"action": "click", "x": 5, "y": 6})
    assert all(summary for *_, summary in calls)


async def test_strangers_are_ignored(monkeypatch):
    calls = []

    async def fake_confirm(*a, **kw):
        calls.append(a)
    monkeypatch.setattr(MD, "confirm_and_run", fake_confirm)
    monkeypatch.setattr(MM, "run_action", AsyncMock())
    upd = _update("/type evil", user_id=999)
    await MD.cmd_type(upd, _context(["evil"]))
    await MM.cmd_volume(_update("/volume 100", 999), _context(["100"]))
    assert calls == [] and not MM.run_action.called
    upd.effective_message.reply_text.assert_not_called()


async def test_volume_command_shows_panel(monkeypatch, fake_ep):
    upd = _update("/volume 35", OWNER)
    await MM.cmd_volume(upd, _context(["35"]))
    args, kwargs = upd.effective_message.reply_text.call_args
    assert "35%" in args[0] and kwargs["reply_markup"] is MM.PANEL_KB
    assert round(fake_ep.level * 100) == 35
