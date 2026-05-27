"""
voice_notes.py — Owner voice-memo capture for Orbi.

The owner holds a button, talks ("remind me Friday to call the
supplier", or "add task: order napkins"), releases — and Orbi:
  1. Transcribes the audio (reusing voicemail.py's whisper/openai/
     deepgram fallback chain, so we never duplicate that logic),
  2. Pipes the transcript through modules.quick_capture.capture()
     which classifies it into the right module (task / reminder /
     calendar / contact / note),
  3. Optionally saves the .mp3 + .txt under user_dir/voice_notes/
     so the owner can replay the original recording.

Result: no need to type. Voice is the input modality, the existing
quick-capture brain does the dispatching.

ROUTES (registered by orbi.py — leave a comment block here):

  POST /api/owner/voice_notes/process
       Multipart form-data with an audio file (field name "audio").
       Optional form field "hint" (string) — extra context the owner
       wants prepended to the transcript before quick_capture runs.
       Returns the process() dict.

  GET  /api/owner/voice_notes
       Lists saved recordings (newest first).

  DELETE /api/owner/voice_notes/<id>
       Deletes a saved recording (both .mp3 and .txt).
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.voice_notes")

_LOCK = threading.Lock()
VOICE_NOTES_DIRNAME = "voice_notes"


# ── transcribe ──────────────────────────────────────────────────────────


def transcribe(audio_bytes: bytes, config: dict) -> str:
    """Run audio through the same fallback chain voicemail uses:
    local whisper → openai whisper API → deepgram → "pending" stub.

    We delegate to voicemail._transcribe() which already encapsulates
    the tier-by-tier logic. It takes a Path, so we write `audio_bytes`
    to a temp file first.
    """
    if not audio_bytes:
        return ""
    try:
        import voicemail  # lazy — voicemail.py owns the tier logic
    except ImportError:
        log.error("voice_notes: voicemail module unavailable")
        return ""

    suffix = _guess_audio_suffix(audio_bytes)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_path = Path(f.name)
        text, tier = voicemail._transcribe(tmp_path, config or {})
        log.info(f"voice_notes: transcribed {len(text)} chars via tier={tier}")
        # If we got the "transcription pending" stub, return empty so
        # the caller can decide how to handle (vs. silently mis-filing
        # the stub string as a quick_note).
        if tier == "pending":
            return ""
        return text or ""
    except Exception as e:
        log.warning(f"voice_notes: transcribe failed: {e}")
        return ""
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ── process ─────────────────────────────────────────────────────────────


def process(config: dict, user_dir: Path, audio_bytes: bytes,
            hint: str = "") -> dict:
    """Transcribe + auto-file. Always returns a dict.

    Shape:
      {
        "transcript":      "...",
        "capture_kind":    "task"|"reminder"|"calendar"|"contact"|"note"|"noop",
        "capture_summary": "Reminder set: ..."
      }

    The audio file itself is also saved under user_dir/voice_notes/
    so the owner can play it back later.
    """
    from modules import quick_capture  # lazy

    transcript = transcribe(audio_bytes, config or {})
    if hint and hint.strip():
        # Hint gets prepended so the classifier sees it (e.g. owner taps
        # the "reminder" hint pill before speaking).
        feed = f"{hint.strip()} {transcript}".strip()
    else:
        feed = transcript

    result = {
        "transcript":      transcript,
        "capture_kind":    "noop",
        "capture_summary": "",
        "saved_id":        "",
    }

    if not feed:
        result["capture_summary"] = (
            "Couldn't transcribe the audio. Try recording again, "
            "or check your transcription provider settings."
        )
        return result

    try:
        captured = quick_capture.capture(user_dir, feed)
        result["capture_kind"]    = captured.get("kind", "note")
        result["capture_summary"] = captured.get("summary", "")
    except Exception as e:
        log.warning(f"voice_notes: quick_capture failed: {e}")
        result["capture_summary"] = "Saved the recording but couldn't auto-file it."

    # Best-effort save of the audio + transcript on disk.
    try:
        saved = save_recording(user_dir, audio_bytes, transcript)
        if saved:
            result["saved_id"] = saved.stem
    except Exception as e:
        log.warning(f"voice_notes: save_recording failed: {e}")

    return result


# ── save_recording / list / delete ──────────────────────────────────────


def _notes_dir(user_dir: Path) -> Path:
    p = Path(user_dir) / VOICE_NOTES_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_recording(user_dir: Path, audio_bytes: bytes, transcript: str) -> Path | None:
    """Save audio + transcript with a timestamped+random id.
    Returns the .mp3 Path (or None if audio_bytes is empty)."""
    if not audio_bytes:
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rand = uuid.uuid4().hex[:6]
    base = f"{ts}_{rand}"
    suffix = _guess_audio_suffix(audio_bytes)

    d = _notes_dir(user_dir)
    audio_path = d / f"{base}{suffix}"
    txt_path   = d / f"{base}.txt"
    meta_path  = d / f"{base}.json"

    with _LOCK:
        # Atomic-ish write: .tmp then rename.
        tmp_audio = audio_path.with_suffix(audio_path.suffix + ".tmp")
        tmp_audio.write_bytes(audio_bytes)
        tmp_audio.replace(audio_path)

        tmp_txt = txt_path.with_suffix(".txt.tmp")
        tmp_txt.write_text(transcript or "", encoding="utf-8")
        tmp_txt.replace(txt_path)

        meta = {
            "id":          base,
            "audio_file":  audio_path.name,
            "txt_file":    txt_path.name,
            "saved_at":    _now_iso(),
            "size_bytes":  len(audio_bytes),
            "transcript_chars": len(transcript or ""),
        }
        tmp_meta = meta_path.with_suffix(".json.tmp")
        tmp_meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        tmp_meta.replace(meta_path)

    log.info(f"voice_notes: saved {audio_path.name} ({len(audio_bytes)} bytes)")
    return audio_path


def list_recordings(user_dir: Path, limit: int = 100) -> list[dict]:
    """List saved recordings newest-first. Each: {id, saved_at,
    transcript, audio_file, size_bytes}."""
    d = _notes_dir(user_dir)
    out: list[dict] = []
    for meta_path in d.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        txt_path = d / meta.get("txt_file", "")
        transcript = ""
        if txt_path.exists():
            try:
                transcript = txt_path.read_text(encoding="utf-8")
            except OSError:
                transcript = ""
        out.append({
            "id":          meta.get("id", meta_path.stem),
            "saved_at":    meta.get("saved_at", ""),
            "transcript":  transcript,
            "audio_file":  meta.get("audio_file", ""),
            "size_bytes":  meta.get("size_bytes", 0),
        })
    out.sort(key=lambda r: r.get("saved_at", ""), reverse=True)
    return out[:limit]


def delete_recording(user_dir: Path, note_id: str) -> bool:
    """Delete the .mp3, .txt, and .json triplet for one recording id.
    Returns True if at least one file was removed."""
    if not note_id or "/" in note_id or "\\" in note_id or ".." in note_id:
        return False
    d = _notes_dir(user_dir)
    removed = False
    with _LOCK:
        # Match by id (the stem). There may be different extensions.
        for p in d.glob(f"{note_id}.*"):
            try:
                p.unlink()
                removed = True
            except OSError as e:
                log.warning(f"voice_notes: delete {p} failed: {e}")
    return removed


# ── Helpers ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _guess_audio_suffix(audio_bytes: bytes) -> str:
    """Cheap container sniff so the temp file gets the right extension
    (whisper/ffmpeg use the extension as a hint). Default .mp3."""
    if not audio_bytes or len(audio_bytes) < 12:
        return ".mp3"
    head = audio_bytes[:12]
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return ".wav"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head[4:8] == b"ftyp":
        # MP4 / M4A — webm-style container or iOS Voice Memos
        return ".m4a"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML — webm / matroska (Chrome MediaRecorder default)
        return ".webm"
    if head.startswith(b"ID3") or head[0:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if head.startswith(b"fLaC"):
        return ".flac"
    return ".mp3"
