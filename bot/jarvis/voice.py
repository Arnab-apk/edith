"""Edith voice: speech-to-text (faster-whisper, local) and text-to-speech (edge-tts).

    text = await transcribe(ogg_bytes)        # Telegram voice note (.oga/OGG-Opus) or any audio
    ogg  = await synthesize("Hello there")    # OGG/Opus bytes ready for bot.send_voice

Env:
  WHISPER_MODEL    faster-whisper model name/path (default "small")
  WHISPER_DEVICE   "auto" (default: CUDA float16 if usable, else CPU int8), "cuda" or "cpu"
  WHISPER_LANGUAGE optional language code (e.g. "en"); empty = auto-detect
  TTS_VOICE        edge-tts voice (default "en-GB-SoniaNeural")
  TTS_RATE         edge-tts rate, e.g. "+5%" (default "+0%")

All decoding/encoding and Whisper inference are blocking, so they go through run_blocking.
The Whisper model loads once, lazily, on the first transcription.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
import re
import threading
from typing import Any

import av
import numpy as np

from ..utils import run_blocking

log = logging.getLogger("jarvis.voice")

SAMPLE_RATE = 16000          # Whisper input rate
OPUS_RATE = 48000            # Telegram voice notes: OGG/Opus 48 kHz mono
MAX_TTS_CHARS = 1500         # longer replies are spoken truncated (full text is still sent)
MAX_AUDIO_SECONDS = 10 * 60  # refuse to transcribe absurdly long audio

_model: Any = None
_model_desc = ""
_model_lock = threading.Lock()


class VoiceError(RuntimeError):
    """Expected, user-presentable voice failure."""


# ====================================================================== STT
def decode_audio(data: bytes, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any audio container PyAV understands (Telegram .oga, mp3, m4a...) to
    mono float32 PCM in [-1, 1] at `sample_rate`."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
    chunks: list[np.ndarray] = []
    limit, total = MAX_AUDIO_SECONDS * sample_rate, 0
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.audio:
                raise VoiceError("that file has no audio track")
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
                    total += chunks[-1].size
                if total > limit:  # stop early: 20 MB of Opus can be hours (GBs of PCM)
                    raise VoiceError(f"audio is longer than {MAX_AUDIO_SECONDS // 60} minutes")
            for out in resampler.resample(None):  # flush
                chunks.append(out.to_ndarray().reshape(-1))
    except av.error.FFmpegError as e:
        raise VoiceError(f"could not decode audio: {e}") from None
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    pcm = np.concatenate(chunks).astype(np.float32) / 32768.0
    if pcm.size > MAX_AUDIO_SECONDS * sample_rate:
        raise VoiceError(f"audio is longer than {MAX_AUDIO_SECONDS // 60} minutes")
    return pcm


def _add_nvidia_dll_dirs() -> None:
    """Make pip-installed CUDA libs (requirements-gpu.txt: nvidia-cublas-cu12, nvidia-cudnn-cu12)
    loadable by ctranslate2 on Windows. No-op when they are not installed."""
    if os.name != "nt":
        return
    import site
    import sys
    roots = [Path(p) for p in site.getsitepackages()] + [Path(sys.prefix) / "Lib" / "site-packages"]
    for root in roots:
        for bin_dir in sorted((root / "nvidia").glob("*/bin")):
            d = str(bin_dir)
            if d not in os.environ.get("PATH", ""):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass


def _cuda_available() -> bool:
    _add_nvidia_dll_dirs()
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def _load_model() -> Any:
    """Load faster-whisper once: CUDA float16 first (RTX 3050), CPU int8 fallback."""
    global _model, _model_desc
    with _model_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel  # heavy import, only when first needed

        name = os.getenv("WHISPER_MODEL", "small").strip() or "small"
        pref = os.getenv("WHISPER_DEVICE", "auto").strip().lower() or "auto"
        attempts: list[tuple[str, str]] = []
        if pref in ("auto", "cuda") and _cuda_available():
            attempts.append(("cuda", "float16"))
        if pref != "cuda" or not attempts:
            attempts.append(("cpu", "int8"))
        last: Exception | None = None
        for device, compute in attempts:
            try:
                m = WhisperModel(name, device=device, compute_type=compute)
                if device == "cuda":
                    # Missing cuBLAS/cuDNN DLLs usually only fail at first inference: probe now.
                    segs, _ = m.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), beam_size=1)
                    list(segs)
                _model, _model_desc = m, f"{name} on {device}/{compute}"
                log.info("Whisper loaded: %s", _model_desc)
                return _model
            except Exception as e:  # noqa: BLE001 - try the next device
                log.warning("Whisper %s on %s/%s failed: %s", name, device, compute, e)
                last = e
        raise VoiceError(f"could not load Whisper model {name!r}: {last}")


def _transcribe_sync(data: bytes) -> str:
    pcm = decode_audio(data)
    if pcm.size < SAMPLE_RATE // 4:  # < 0.25 s
        return ""
    model = _load_model()
    lang = os.getenv("WHISPER_LANGUAGE", "").strip() or None
    segments, _info = model.transcribe(pcm, language=lang, beam_size=5, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(data: bytes) -> str:
    """Speech -> text. Returns "" when nothing intelligible was said."""
    return await run_blocking(_transcribe_sync, bytes(data))


def model_info() -> str:
    return _model_desc or "not loaded yet"


# ====================================================================== TTS
_MD_RE = [
    (re.compile(r"```.*?```", re.S), " (code omitted) "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
    (re.compile(r"https?://\S+"), "the link"),
    (re.compile(r"[*_#>|~]+"), ""),
    (re.compile(r"\s+"), " "),
]


def speakable(text: str) -> str:
    """Strip markdown/URLs and cap length so TTS reads naturally."""
    for rx, rep in _MD_RE:
        text = rx.sub(rep, text)
    text = text.strip()
    if len(text) > MAX_TTS_CHARS:
        cut = text[:MAX_TTS_CHARS]
        cut = cut[: max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), MAX_TTS_CHARS // 2) + 1]
        text = cut.strip() + " The rest is in the text message."
    return text


async def _edge_tts_mp3(text: str, voice: str, rate: str) -> bytes:
    import edge_tts

    comm = edge_tts.Communicate(text, voice, rate=rate)
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            buf += chunk["data"]
    return bytes(buf)


def to_ogg_opus(data: bytes) -> bytes:
    """Re-encode any audio (edge-tts mp3) to OGG/Opus mono 48 kHz, fully in memory."""
    out_buf = io.BytesIO()
    resampler = av.AudioResampler(format="s16", layout="mono", rate=OPUS_RATE)
    try:
        with av.open(io.BytesIO(data), mode="r") as src, av.open(out_buf, mode="w", format="ogg") as dst:
            stream = dst.add_stream("libopus", rate=OPUS_RATE)
            stream.layout = "mono"
            stream.format = "s16"
            stream.bit_rate = 48000
            for frame in src.decode(audio=0):
                for rf in resampler.resample(frame):
                    rf.pts = None
                    for pkt in stream.encode(rf):
                        dst.mux(pkt)
            for rf in resampler.resample(None):
                rf.pts = None
                for pkt in stream.encode(rf):
                    dst.mux(pkt)
            for pkt in stream.encode(None):  # flush encoder
                dst.mux(pkt)
    except av.error.FFmpegError as e:
        raise VoiceError(f"could not encode voice reply: {e}") from None
    return out_buf.getvalue()


async def synthesize(text: str, *, voice: str | None = None) -> bytes:
    """Text -> OGG/Opus bytes for bot.send_voice. Raises VoiceError on failure."""
    text = speakable(text)
    if not text:
        raise VoiceError("nothing to say")
    voice = voice or os.getenv("TTS_VOICE", "en-GB-SoniaNeural").strip() or "en-GB-SoniaNeural"
    rate = os.getenv("TTS_RATE", "+0%").strip() or "+0%"
    try:
        mp3 = await _edge_tts_mp3(text, voice, rate)
    except Exception as e:  # noqa: BLE001 - network/service errors from edge-tts
        raise VoiceError(f"text-to-speech failed: {type(e).__name__}: {e}") from None
    if not mp3:
        raise VoiceError("text-to-speech returned no audio")
    return await run_blocking(to_ogg_opus, mp3)
