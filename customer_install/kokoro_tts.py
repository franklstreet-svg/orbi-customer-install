"""
kokoro_tts.py — Kokoro-82M TTS wrapper for Orbi.

Kokoro is the customer-tenant TTS engine. Apache 2.0 licensed, open weights,
runs entirely on Frank's server. No third-party API, no subscription, no
shutdown risk.

Model files live at customer_install/tts_models/kokoro/ (gitignored — too large
for the repo, downloaded once and cached on disk).

Public API:
    available_voices() -> list[dict]   — voices the customer can pick from
    render(text, voice, format="mp3")  — returns audio bytes
    is_available() -> bool             — quick check before falling back to
                                          edge_tts / Polly

Voice catalog exposed to customers:
    af              — Default Orbi blend (the main voice)
    af_bella        — Warm, conversational
    af_sarah        — Bright, energetic
    af_nicole       — Soft, intimate
    af_sky          — Smooth, professional
    am_michael      — Warm baritone (male)
    am_adam         — Authoritative (male)
    bm_george       — British distinguished (male)
    bm_lewis        — British polished (male)
"""

from __future__ import annotations

import io
import logging
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("kokoro_tts")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
MODEL_DIR = _HERE / "tts_models" / "kokoro"
MODEL_FILE = MODEL_DIR / "kokoro-v0_19.onnx"
VOICES_FILE = MODEL_DIR / "voices.bin"

# ---------------------------------------------------------------------------
# Voice catalog — what the customer sees + picks from
# ---------------------------------------------------------------------------

DEFAULT_VOICE = "af"

VOICE_CATALOG: list[dict] = [
    {"id": "af",         "label": "Orbi (default)",          "lang": "en-us", "gender": "female"},
    {"id": "af_bella",   "label": "Bella — warm",            "lang": "en-us", "gender": "female"},
    {"id": "af_sarah",   "label": "Sarah — bright",          "lang": "en-us", "gender": "female"},
    {"id": "af_nicole",  "label": "Nicole — soft",           "lang": "en-us", "gender": "female"},
    {"id": "af_sky",     "label": "Sky — professional",      "lang": "en-us", "gender": "female"},
    {"id": "am_michael", "label": "Michael — warm male",     "lang": "en-us", "gender": "male"},
    {"id": "am_adam",    "label": "Adam — authoritative",    "lang": "en-us", "gender": "male"},
    {"id": "bm_george",  "label": "George — British",        "lang": "en-gb", "gender": "male"},
    {"id": "bm_lewis",   "label": "Lewis — British polished","lang": "en-gb", "gender": "male"},
]

_VOICE_LOOKUP = {v["id"]: v for v in VOICE_CATALOG}

# ---------------------------------------------------------------------------
# Lazy model loader (thread-safe, loads once)
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_load_failed_reason: str | None = None


def _load_model():
    global _model, _load_failed_reason
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        if not MODEL_FILE.exists() or not VOICES_FILE.exists():
            _load_failed_reason = f"model files missing under {MODEL_DIR}"
            log.warning(f"kokoro_tts: {_load_failed_reason}")
            return None
        try:
            from kokoro_onnx import Kokoro
        except ImportError as e:
            _load_failed_reason = f"kokoro_onnx not installed: {e}"
            log.warning(f"kokoro_tts: {_load_failed_reason}")
            return None
        try:
            _model = Kokoro(str(MODEL_FILE), str(VOICES_FILE))
            log.info(f"kokoro_tts: model loaded from {MODEL_FILE}")
            return _model
        except Exception as e:
            _load_failed_reason = f"model load failed: {e}"
            log.error(f"kokoro_tts: {_load_failed_reason}")
            return None


def is_available() -> bool:
    """Quick check that doesn't force-load the model. True if the files exist
    AND the import works. The caller can use this to decide whether to attempt
    Kokoro vs. fall back to edge_tts."""
    if _model is not None:
        return True
    if not MODEL_FILE.exists() or not VOICES_FILE.exists():
        return False
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except ImportError:
        return False


def available_voices() -> list[dict]:
    """Catalog of voices the customer can pick from (for the dashboard
    dropdown)."""
    return list(VOICE_CATALOG)


def resolve_voice(voice_id: str | None) -> str:
    """Normalize a possibly-unknown voice id to a known one. Falls back to
    DEFAULT_VOICE if the input isn't recognized."""
    if voice_id and voice_id in _VOICE_LOOKUP:
        return voice_id
    return DEFAULT_VOICE


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(text: str, voice: str | None = None, format: str = "mp3",
           speed: float = 1.0) -> bytes | None:
    """Render `text` to audio bytes using the given Kokoro voice.

    Returns the audio bytes on success, None on any failure (caller should
    fall back to edge_tts or another engine).

    `format` may be 'mp3' (default, browser-friendly + small) or 'wav'.
    MP3 requires ffmpeg on PATH for transcoding from Kokoro's WAV output.
    """
    text = (text or "").strip()
    if not text:
        return None

    model = _load_model()
    if model is None:
        return None

    voice_id = resolve_voice(voice)
    voice_meta = _VOICE_LOOKUP[voice_id]
    lang = voice_meta.get("lang", "en-us")

    try:
        samples, sample_rate = model.create(text, voice=voice_id,
                                            speed=speed, lang=lang)
    except Exception as e:
        log.error(f"kokoro_tts.render: synthesis failed for voice={voice_id!r}: {e}")
        return None

    # Pack WAV in memory
    try:
        import soundfile as sf
    except ImportError:
        log.error("kokoro_tts.render: soundfile not installed — cannot pack audio")
        return None

    wav_buf = io.BytesIO()
    try:
        sf.write(wav_buf, samples, sample_rate, format="WAV")
    except Exception as e:
        log.error(f"kokoro_tts.render: WAV pack failed: {e}")
        return None

    wav_bytes = wav_buf.getvalue()

    if format.lower() == "wav":
        return wav_bytes

    # Transcode WAV → MP3 via ffmpeg (smaller payload, browser-friendly)
    mp3_bytes = _wav_to_mp3(wav_bytes)
    if mp3_bytes is not None:
        return mp3_bytes

    # ffmpeg unavailable — fall back to WAV. Browser will still play it.
    log.warning("kokoro_tts.render: ffmpeg unavailable, returning WAV")
    return wav_bytes


def _wav_to_mp3(wav_bytes: bytes) -> bytes | None:
    """Transcode WAV to MP3 using ffmpeg from PATH or the bundled bin/ffmpeg."""
    ffmpeg_bin = _find_ffmpeg()
    if ffmpeg_bin is None:
        return None
    try:
        proc = subprocess.run(
            [str(ffmpeg_bin), "-loglevel", "error", "-y",
             "-f", "wav", "-i", "pipe:0",
             "-c:a", "libmp3lame", "-b:a", "96k",
             "-f", "mp3", "pipe:1"],
            input=wav_bytes,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return proc.stdout
        log.warning(f"kokoro_tts: ffmpeg returned {proc.returncode}: "
                    f"{proc.stderr.decode(errors='replace')[:200]}")
        return None
    except Exception as e:
        log.warning(f"kokoro_tts: ffmpeg transcode failed: {e}")
        return None


def _find_ffmpeg() -> Path | None:
    bundled = _HERE / "bin" / ("ffmpeg.exe" if _is_windows() else "ffmpeg")
    if bundled.exists():
        return bundled
    import shutil
    p = shutil.which("ffmpeg")
    return Path(p) if p else None


def _is_windows() -> bool:
    import platform
    return platform.system() == "Windows"


# ---------------------------------------------------------------------------
# Diagnostic helper (mainly for the /health endpoint and dev sanity)
# ---------------------------------------------------------------------------

def diagnostic() -> dict:
    """Return a small dict describing Kokoro's current state — handy for
    /health endpoints and 'why is voice failing' debugging."""
    return {
        "model_file_present": MODEL_FILE.exists(),
        "voices_file_present": VOICES_FILE.exists(),
        "model_loaded": _model is not None,
        "load_failed_reason": _load_failed_reason,
        "kokoro_onnx_importable": is_available(),
        "default_voice": DEFAULT_VOICE,
        "voice_count": len(VOICE_CATALOG),
        "ffmpeg_path": str(_find_ffmpeg()) if _find_ffmpeg() else None,
    }
