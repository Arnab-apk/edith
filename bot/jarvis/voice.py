"""Edith voice: speech-to-text (faster-whisper, local) and text-to-speech (edge-tts).

    text = await transcribe(ogg_bytes)        # Telegram voice note (.oga/OGG-Opus) or any audio
    ogg  = await synthesize("Hello there")    # OGG/Opus bytes ready for bot.send_voice

Works on any laptop: with a usable NVIDIA GPU it runs CUDA float16, otherwise CPU int8 with
settings tuned for short spoken commands (greedy decoding, physical-core threads, VAD). Measured
on a 4-core Ryzen laptop CPU, a 5 s command takes ~2 s with "small" and ~1 s with "base".

Env:
  WHISPER_MODEL    "auto" (default: small on GPU or a >=4-core CPU, base on smaller CPUs)
                   or any faster-whisper name/path (tiny, base, small, base.en, ...)
  WHISPER_DEVICE   "auto" (default: CUDA float16 if usable, else CPU int8), "cuda" or "cpu"
  WHISPER_LANGUAGE "auto" (default, detect) or a code such as "en" (faster and more accurate)
  WHISPER_BEAM     beam size (default 5 on GPU, 1 on CPU)
  WHISPER_PROMPT   vocabulary hint for short commands (default mentions "Edith" + command words)
  TTS_VOICE        edge-tts voice (default "en-GB-SoniaNeural")
  TTS_RATE         edge-tts rate, e.g. "+5%" (default "+0%")

All decoding/encoding and Whisper inference are blocking, so they go through run_blocking.
The model loads once; warmup() (called in the background at bot startup) hides the load and
any first-run download, so the first voice note is as fast as the rest.
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
MAX_TTS_CHARS = 220          # spoken answers stay short; the full text is always sent too
MAX_AUDIO_SECONDS = 10 * 60  # refuse to transcribe absurdly long audio
DEFAULT_PROMPT = ("Edith, screenshot, CPU, RAM, battery, processes, volume, mute, lock, "
                  "shut down, restart, open Chrome, YouTube, remind me.")
STICKY_LANG_PROB = 0.8       # auto mode: reuse a confidently detected language (saves an encoder pass)
RECHECK_LOGPROB = -0.9       # ...and re-detect when a forced-language result looks poor

_model: Any = None
_model_desc = ""
_model_device = ""
_model_lock = threading.Lock()
_sticky_lang: str | None = None  # last confidently detected language (WHISPER_LANGUAGE=auto)


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


def _physical_cores() -> int:
    try:
        import psutil
        return psutil.cpu_count(logical=False) or max(1, (os.cpu_count() or 2) // 2)
    except Exception:  # noqa: BLE001
        return max(1, (os.cpu_count() or 2) // 2)


def _ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / 2**30
    except Exception:  # noqa: BLE001
        return 8.0


def pick_model(device: str) -> str:
    """WHISPER_MODEL, or for "auto" the best model that stays fast on this hardware."""
    name = os.getenv("WHISPER_MODEL", "auto").strip() or "auto"
    if name.lower() != "auto":
        return name
    if device == "cuda":
        return "small"
    # CPU: "small" is ~2 s per command on 4 cores; on 2-core / low-RAM laptops "base" (~2x faster).
    return "small" if _physical_cores() >= 4 and _ram_gb() >= 6 else "base"


def language() -> str | None:
    lang = os.getenv("WHISPER_LANGUAGE", "auto").strip().lower()
    return None if lang in ("", "auto") else lang


def _load_model() -> Any:
    """Load faster-whisper once: CUDA float16 when a GPU really works, else CPU int8."""
    global _model, _model_desc, _model_device
    with _model_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel  # heavy import, only when first needed

        pref = os.getenv("WHISPER_DEVICE", "auto").strip().lower() or "auto"
        attempts: list[tuple[str, str]] = []
        if pref in ("auto", "cuda") and _cuda_available():
            attempts.append(("cuda", "float16"))
        if pref != "cuda" or not attempts:
            attempts.append(("cpu", "int8"))
        last: Exception | None = None
        for device, compute in attempts:
            name = pick_model(device)
            try:
                kw: dict[str, Any] = {}
                if device == "cpu":
                    kw["cpu_threads"] = min(8, _physical_cores())
                m = WhisperModel(name, device=device, compute_type=compute, **kw)
                if device == "cuda":
                    # Missing cuBLAS/cuDNN DLLs usually only fail at first inference: probe now.
                    segs, _ = m.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), beam_size=1)
                    list(segs)
                _model, _model_device = m, device
                _model_desc = f"{name} on {device}/{compute}"
                log.info("Whisper loaded: %s", _model_desc)
                return _model
            except Exception as e:  # noqa: BLE001 - try the next device
                log.warning("Whisper %s on %s/%s failed: %s", name, device, compute, e)
                last = e
        raise VoiceError(f"could not load the speech model: {last}")


def _is_prompt_echo(text: str, prompt: str) -> bool:
    """Whisper sometimes 'hears' its own initial_prompt in noise. Never treat that as a command."""
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower()).split()  # noqa: E731
    t, p = norm(text), norm(prompt)
    if len(t) < 4:
        return False
    joined = " ".join(p)
    return " ".join(t) in joined  # every word, in order, comes straight from the prompt


def _transcribe_sync(data: bytes) -> str:
    pcm = decode_audio(data)
    if pcm.size < SAMPLE_RATE // 4:  # < 0.25 s
        return ""
    global _sticky_lang
    model = _load_model()
    beam = int(os.getenv("WHISPER_BEAM", "0") or 0) or (5 if _model_device == "cuda" else 1)
    prompt = os.getenv("WHISPER_PROMPT", DEFAULT_PROMPT).strip()
    fixed = language()

    def run(lang: str | None) -> tuple[str, float, Any]:
        segments, info = model.transcribe(
            pcm, language=lang, beam_size=beam, vad_filter=True,
            initial_prompt=prompt or None, condition_on_previous_text=False, without_timestamps=True)
        segs = [s for s in segments
                if not (getattr(s, "no_speech_prob", 0.0) > 0.6 and getattr(s, "avg_logprob", 0.0) < -1.0)]
        lp = min((getattr(s, "avg_logprob", 0.0) for s in segs), default=0.0)
        return " ".join(s.text.strip() for s in segs if s.text.strip()).strip(), lp, info

    if fixed:
        text, _lp, _info = run(fixed)
    else:
        guess = _sticky_lang
        text, lp, info = run(guess)
        if guess and (not text or lp < RECHECK_LOGPROB):  # maybe a different language now
            text, lp, info = run(None)
            guess = None
        if guess is None:
            prob = getattr(info, "language_probability", 0.0) or 0.0
            _sticky_lang = info.language if (text and prob >= STICKY_LANG_PROB) else None
    if prompt and _is_prompt_echo(text, prompt):
        log.info("dropping transcript that only echoes the vocabulary prompt: %r", text)
        return ""
    return text


# ------------------------------------------------------------------ NVIDIA hosted Whisper (Riva gRPC)
NVIDIA_STT_URI = "grpc.nvcf.nvidia.com:443"
NVIDIA_STT_FUNCTION_ID = "b702f636-f60c-4a3d-a6f4-f3568c13bd7d"  # openai/whisper-large-v3 on build.nvidia.com
_last_engine = ""


def _nvidia_key() -> str:
    for name in ("NVIDIA_API_KEY", "LLM_API_KEY"):
        v = os.getenv(name, "").strip()
        if v.startswith("nvapi-"):
            return v
    return ""


def _groq_key() -> str:
    for name in ("GROQ_API_KEY", "LLM_API_KEY"):
        v = os.getenv(name, "").strip()
        if v.startswith("gsk_"):
            return v
    return ""


def stt_provider() -> str:
    """"groq" or "nvidia" (hosted whisper-large-v3) or "local" (faster-whisper).
    STT_PROVIDER=auto (default) picks groq, then nvidia, by which key is configured."""
    pref = os.getenv("STT_PROVIDER", "auto").strip().lower() or "auto"
    if pref in ("local", "groq", "nvidia"):
        if pref == "groq" and not _groq_key() or pref == "nvidia" and not _nvidia_key():
            return "local"
        return pref
    if _groq_key():
        return "groq"
    return "nvidia" if _nvidia_key() else "local"


GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_PROMPT = "Edith, open, close, lock, screenshot, volume, mute, record, Brave, Chrome, WhatsApp."


def _transcribe_groq_sync(data: bytes) -> str:
    import httpx

    pcm = decode_audio(data)
    if pcm.size < SAMPLE_RATE // 5:
        return ""
    form = {"model": os.getenv("GROQ_STT_MODEL", "").strip() or "whisper-large-v3",
            "response_format": "json", "temperature": "0", "prompt": GROQ_STT_PROMPT}
    lang = language()
    if lang:
        form["language"] = lang
    r = httpx.post(GROQ_STT_URL, headers={"Authorization": "Bearer " + _groq_key()}, data=form,
                   files={"file": ("voice.wav", _wav_bytes(pcm), "audio/wav")}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Groq speech-to-text HTTP {r.status_code}")
    text = (r.json().get("text") or "").strip()
    return "" if _is_prompt_echo(text, GROQ_STT_PROMPT) else text


def _wav_bytes(pcm: np.ndarray) -> bytes:
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def _transcribe_nvidia_sync(data: bytes) -> str:
    import riva.client  # small gRPC client (nvidia-riva-client); no model download

    pcm = decode_audio(data)
    if pcm.size < SAMPLE_RATE // 5:
        return ""
    auth = riva.client.Auth(uri=NVIDIA_STT_URI, use_ssl=True, metadata_args=[
        ["function-id", os.getenv("NVIDIA_STT_FUNCTION_ID", "").strip() or NVIDIA_STT_FUNCTION_ID],
        ["authorization", "Bearer " + _nvidia_key()]])
    cfg = riva.client.RecognitionConfig(language_code=language() or "en", max_alternatives=1,
                                        enable_automatic_punctuation=True)
    resp = riva.client.ASRService(auth).offline_recognize(_wav_bytes(pcm), cfg)
    return " ".join(r.alternatives[0].transcript.strip() for r in resp.results if r.alternatives).strip()


async def transcribe(data: bytes) -> str:
    """Speech -> text. Returns "" when nothing intelligible was said.
    Hosted NVIDIA Whisper first when configured; the local model is the automatic fallback."""
    global _last_engine
    data = bytes(data)
    engine = stt_provider()
    if engine in ("groq", "nvidia"):
        try:
            import asyncio
            fn = _transcribe_groq_sync if engine == "groq" else _transcribe_nvidia_sync
            text = await asyncio.wait_for(run_blocking(fn, data), timeout=35)
            _last_engine = f"whisper-large-v3 on {engine.capitalize() if engine == 'groq' else 'NVIDIA'}"
            return text
        except VoiceError:
            raise
        except Exception as e:  # noqa: BLE001 - network/quota problems must not kill voice control
            log.warning("NVIDIA speech-to-text failed (%s) - using the local model", type(e).__name__)
    text = await run_blocking(_transcribe_sync, data)
    _last_engine = _model_desc or "local Whisper"
    return text


def warmup() -> str:
    """Load the model (downloading it on first run) and run one tiny inference. Blocking."""
    if stt_provider() != "local":
        return f"whisper-large-v3 on {'Groq' if stt_provider() == 'groq' else 'NVIDIA'} (hosted, nothing to load)"
    model = _load_model()
    segs, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), beam_size=1,
                               language=language() or "en", without_timestamps=True)
    list(segs)
    return _model_desc


def model_info() -> str:

    if stt_provider() != "local":
        host = "Groq" if stt_provider() == "groq" else "NVIDIA"
        return f"whisper-large-v3 on {host} (hosted); local fallback: " + (_model_desc or "not loaded")
    return _model_desc or "loading…"


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
    text = re.sub(r"<think>.*?(</think>|$)", " ", text or "", flags=re.S | re.I)
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
