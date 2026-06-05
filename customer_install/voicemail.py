"""
voicemail.py — Voicemail capture, transcription and summary for the
Orbi phone receptionist.

WIRING (the Flask routes themselves live in orbi.py via voicemail.register()
or are added inline; this block documents the contract):

  POST /voice/voicemail_recording_callback
       PUBLIC — Twilio posts here when the <Record> verb finishes.
       Request is form-encoded: RecordingUrl, RecordingSid, From,
       RecordingDuration, CallSid. No auth check — but the handler
       SHOULD verify Twilio's X-Twilio-Signature header (see
       _verify_twilio_signature) before trusting the payload.

  GET  /api/owner/voicemails
       Owner-only. Returns the most recent voicemails for the dashboard.

  GET  /api/owner/voicemails/<id>
       Owner-only. Returns one voicemail record (full transcript + audio
       url + summary). If audio_local_path is set, the dashboard can
       stream it from disk; otherwise it links to the Twilio URL.

  POST /api/owner/voicemails/<id>/handled
       Owner-only. Marks the voicemail as handled (handled=true,
       handled_at=now).

  DELETE /api/owner/voicemails/<id>
       Owner-only. Deletes the JSON record and the local audio file
       (best-effort; Twilio's copy of the audio is not touched).


TRANSCRIPTION FALLBACK STRATEGY (process_recording tries each in order
and stops at the first success):

  (a) openai-whisper PYTHON PACKAGE installed locally
        `import whisper; model = whisper.load_model("base"); model.transcribe(path)`
      This is the preferred path because Frank's rule is LOCAL-FIRST,
      no recurring API spend. Whisper "base" is ~140 MB and runs on
      CPU. Only used if the `whisper` package is actually importable —
      we never auto-install.

  (b) OpenAI Whisper API (CONFIG.voicemail.whisper_api_key or
      CONFIG.openai.api_key). Cheap (~$0.006/min) but is a cloud call
      so it's tier 2.

  (c) Deepgram API (CONFIG.voicemail.deepgram_api_key). Also cheap and
      fast; included as an alternative in case the owner already has a
      Deepgram account.

  (d) LAST RESORT — store the audio URL + transcript="[transcription
      pending — play the recording from your dashboard]" so the owner
      can still listen to the message even if every transcriber failed.
      The audio is downloaded locally (audio_local_path) if Twilio
      auth creds are present, otherwise only the remote URL is kept.

Each tier wraps its work in try/except. A tier failing logs a warning
and falls through; we never crash the recording callback (Twilio would
retry and the caller's voicemail could be lost).


SUMMARY:
  After transcription, we ask the LLM to extract a JSON object:
    {caller, callback_number, reason, urgency}
  We parse defensively — first try json.loads on the LLM output, then
  fall back to regex/keyword extraction on the transcript itself. The
  summary text saved to the record is a human-readable one-line
  rephrase of the same fields.


THREADING / DURABILITY:
  - File writes are atomic (.tmp + os.replace).
  - All in-memory list access is guarded by _LOCK.
  - The recording callback returns a TwiML response immediately and
    runs the expensive download+transcribe+notify work in a daemon
    thread so Twilio doesn't time out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.voicemail")

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _vm_dir(data_dir: Path) -> Path:
    p = data_dir / "voicemails"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _record_path(data_dir: Path, vm_id: str) -> Path:
    return _vm_dir(data_dir) / f"{vm_id}.json"


def _audio_path(data_dir: Path, vm_id: str) -> Path:
    return _vm_dir(data_dir) / f"{vm_id}.mp3"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# TwiML — record-a-voicemail response
# ---------------------------------------------------------------------------

def record_voicemail_twiml(callback_url: str) -> str:
    """Return a TwiML <Response> that records the caller's voicemail.

    Twilio posts the recording metadata to `callback_url` once the
    caller hangs up or hits the maxLength. We pass `transcribe=false`
    because we run our own (better) transcription path in
    process_recording().
    """
    prompt = ("I couldn't connect you to a human right now. "
              "Please leave a message after the tone — up to sixty seconds. "
              "When you're done, just hang up.")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say voice="Polly.Joanna">{prompt}</Say>'
        f'<Record action="{callback_url}" method="POST" '
        'maxLength="60" playBeep="true" transcribe="false" '
        'finishOnKey="#" timeout="5"/>'
        '<Say voice="Polly.Joanna">Thanks. We will get back to you. Goodbye.</Say>'
        '<Hangup/>'
        '</Response>'
    )


# ---------------------------------------------------------------------------
# Twilio request signature verification (optional but recommended)
# ---------------------------------------------------------------------------

def verify_twilio_signature(config: dict, full_url: str,
                            form_params: dict, signature: str) -> bool:
    """Validate Twilio's X-Twilio-Signature header. Returns True on
    match. Returns False if no auth token is configured (so the route
    handler can decide whether to accept the request anyway in dev).

    Algorithm (per Twilio docs):
      1. concat the full URL with each form param k+v sorted by key
      2. HMAC-SHA1 with the auth token as the key
      3. base64-encode the digest; compare to the header.
    """
    token = (config.get("phone") or {}).get("twilio_auth_token", "")
    if not token or not signature:
        return False
    try:
        keys = sorted(form_params.keys())
        data = full_url + "".join(k + str(form_params[k]) for k in keys)
        mac = hmac.new(token.encode("utf-8"), data.encode("utf-8"),
                       hashlib.sha1).digest()
        expected = base64.b64encode(mac).decode("ascii")
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        log.warning(f"twilio signature verify error: {e}")
        return False


# ---------------------------------------------------------------------------
# Audio download
# ---------------------------------------------------------------------------

def _download_audio(audio_url: str, dest: Path, config: dict) -> bool:
    """Download Twilio recording to `dest`. Twilio recordings are
    auth-protected, so we use HTTP Basic with the account SID + auth
    token. Returns True on success.
    """
    if not audio_url:
        return False
    # Twilio recording URLs often need ".mp3" suffix to return MP3
    # (otherwise they redirect to WAV). Accept either.
    if not audio_url.endswith(".mp3") and "twilio.com" in audio_url:
        audio_url = audio_url + ".mp3"
    headers = {"User-Agent": "Orbi-Voicemail/1.0"}
    phone = config.get("phone") or {}
    sid, token = phone.get("twilio_account_sid"), phone.get("twilio_auth_token")
    if sid and token:
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"
    try:
        req = urllib.request.Request(audio_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        log.info(f"downloaded voicemail audio: {dest} ({len(data)} bytes)")
        return True
    except Exception as e:
        log.warning(f"audio download failed for {audio_url}: {e}")
        return False


# ---------------------------------------------------------------------------
# Transcription tiers
# ---------------------------------------------------------------------------

# Cached local Whisper model (loads slowly, ~5-15s)
_WHISPER_MODEL = None
_WHISPER_LOCK = threading.Lock()


def _transcribe_local_whisper(audio_path: Path) -> str | None:
    """Tier (a): openai-whisper Python package. Returns text or None."""
    global _WHISPER_MODEL
    try:
        import whisper  # type: ignore
    except ImportError:
        log.debug("whisper package not installed; skipping local tier")
        return None
    try:
        with _WHISPER_LOCK:
            if _WHISPER_MODEL is None:
                model_name = os.environ.get("ORBI_WHISPER_MODEL", "base")
                log.info(f"loading local whisper model: {model_name}")
                _WHISPER_MODEL = whisper.load_model(model_name)
        result = _WHISPER_MODEL.transcribe(str(audio_path), fp16=False)
        text = (result.get("text") or "").strip()
        if text:
            log.info(f"local-whisper transcribed {len(text)} chars")
            return text
    except Exception as e:
        log.warning(f"local-whisper failed: {e}")
    return None


def _transcribe_openai_api(audio_path: Path, api_key: str) -> str | None:
    """Tier (b): OpenAI Whisper REST API."""
    if not api_key:
        return None
    try:
        boundary = uuid.uuid4().hex
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            "whisper-1\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'
            "Content-Type: audio/mpeg\r\n\r\n"
        ).encode("utf-8") + audio_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = (data.get("text") or "").strip()
        if text:
            log.info(f"openai-whisper API transcribed {len(text)} chars")
            return text
    except Exception as e:
        log.warning(f"openai-whisper API failed: {e}")
    return None


def _transcribe_deepgram(audio_path: Path, api_key: str) -> str | None:
    """Tier (c): Deepgram REST API."""
    if not api_key:
        return None
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        req = urllib.request.Request(
            "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true",
            data=audio_bytes,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/mpeg",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        alts = (data.get("results", {})
                    .get("channels", [{}])[0]
                    .get("alternatives", []))
        text = (alts[0].get("transcript") if alts else "").strip()
        if text:
            log.info(f"deepgram transcribed {len(text)} chars")
            return text
    except Exception as e:
        log.warning(f"deepgram failed: {e}")
    return None


def _transcribe(audio_path: Path, config: dict) -> tuple[str, str]:
    """Run the fallback chain. Returns (transcript_text, tier_used).
    tier_used is one of: 'local', 'openai', 'deepgram', 'pending'.
    """
    vm_cfg = config.get("voicemail") or {}
    # (a) local whisper
    text = _transcribe_local_whisper(audio_path)
    if text:
        return text, "local"
    # (b) openai whisper API
    api_key = (vm_cfg.get("whisper_api_key")
               or (config.get("openai") or {}).get("api_key", ""))
    text = _transcribe_openai_api(audio_path, api_key)
    if text:
        return text, "openai"
    # (c) deepgram
    text = _transcribe_deepgram(audio_path, vm_cfg.get("deepgram_api_key", ""))
    if text:
        return text, "deepgram"
    # (d) give up — owner plays the audio from the dashboard
    return ("[transcription pending — play the recording from your dashboard]",
            "pending")


# ---------------------------------------------------------------------------
# Summary via LLM (defensive JSON parsing)
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(
    r"(?:\+?1[\s\-\.]?)?\(?(\d{3})\)?[\s\-\.]?(\d{3})[\s\-\.]?(\d{4})"
)
_NAME_RE = re.compile(
    r"\b(?:this is|my name is|it's|its|i am|i'm)\s+([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?)",
    re.IGNORECASE,
)
_URGENT_HINTS = ("urgent", "asap", "emergency", "right away", "right now",
                 "as soon as possible", "immediately")
_FYI_HINTS = ("just letting you know", "no rush", "whenever", "fyi",
              "no need to call back", "informational")


def _keyword_extract(transcript: str, caller_number: str) -> dict:
    """Cheap fallback when the LLM is unavailable or returns garbage."""
    name_match = _NAME_RE.search(transcript or "")
    caller = name_match.group(1).strip() if name_match else ""
    phone_match = _PHONE_RE.search(transcript or "")
    callback = ""
    if phone_match:
        callback = "-".join(phone_match.groups())
    elif caller_number:
        callback = caller_number
    low = (transcript or "").lower()
    urgency = "normal"
    if any(h in low for h in _URGENT_HINTS):
        urgency = "urgent"
    elif any(h in low for h in _FYI_HINTS):
        urgency = "fyi"
    # rough reason = first sentence trimmed to 140 chars
    reason = (transcript or "").strip().split(".")[0][:140] if transcript else ""
    return {
        "caller": caller,
        "callback_number": callback,
        "reason": reason,
        "urgency": urgency,
    }


def _parse_llm_json(raw: str) -> dict | None:
    """Find the first {...} block in `raw` and try to load it."""
    if not raw:
        return None
    # Strip markdown fences
    raw = re.sub(r"```(?:json)?", "", raw)
    raw = raw.replace("```", "")
    # First try whole-string
    try:
        obj = json.loads(raw.strip())
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # Then try first balanced {...}
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                blob = raw[start:i + 1]
                try:
                    obj = json.loads(blob)
                    if isinstance(obj, dict):
                        return obj
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _summarize(config: dict, transcript: str, caller_number: str) -> dict:
    """Returns {caller, callback_number, reason, urgency, summary}.
    Defensive: if the LLM call or parse fails, falls back to keyword
    extraction so the owner always sees SOMETHING useful.
    """
    fallback = _keyword_extract(transcript, caller_number)
    if not transcript or transcript.startswith("[transcription pending"):
        fallback["summary"] = (
            f"Voicemail from {caller_number or 'unknown'}. "
            "Audio is available — transcription pending."
        )
        return fallback

    try:
        import llm_client  # imported here to avoid a hard dep at module load
    except ImportError:
        fallback["summary"] = _compose_summary_text(fallback)
        return fallback

    system = (
        "You are a phone receptionist's assistant. The caller left a "
        "voicemail. Extract structured info. Return ONLY a JSON object "
        "with exactly these keys:\n"
        '  "caller": short name of the caller, or "" if not stated\n'
        '  "callback_number": phone number to call back, or "" if not given\n'
        '  "reason": one-sentence reason for the call (under 140 chars)\n'
        '  "urgency": one of "urgent" | "normal" | "fyi"\n'
        "Do NOT include any other text — JSON only."
    )
    user_msg = (
        f"Caller phone (from Twilio): {caller_number or 'unknown'}\n"
        f"Transcript:\n{transcript}"
    )
    try:
        resp = llm_client.generate(config, system, [
            {"role": "user", "content": user_msg}
        ])
        parsed = _parse_llm_json(resp.text or "") if resp else None
    except Exception as e:
        log.warning(f"summary LLM call failed: {e}")
        parsed = None

    if not parsed:
        result = fallback
    else:
        result = {
            "caller": str(parsed.get("caller") or fallback["caller"] or "").strip(),
            "callback_number": str(
                parsed.get("callback_number") or fallback["callback_number"] or ""
            ).strip(),
            "reason": str(parsed.get("reason") or fallback["reason"] or "").strip()[:200],
            "urgency": _normalize_urgency(parsed.get("urgency"), fallback["urgency"]),
        }
    result["summary"] = _compose_summary_text(result)
    return result


def _normalize_urgency(value, default: str) -> str:
    v = str(value or "").strip().lower()
    if v in ("urgent", "normal", "fyi"):
        return v
    if v in ("high", "asap", "emergency"):
        return "urgent"
    if v in ("low", "info", "informational"):
        return "fyi"
    return default if default in ("urgent", "normal", "fyi") else "normal"


def _compose_summary_text(d: dict) -> str:
    name = d.get("caller") or "Caller"
    reason = d.get("reason") or "left a message"
    cb = d.get("callback_number") or ""
    urgency = d.get("urgency", "normal")
    parts = [f"{name} — {reason}."]
    if cb:
        parts.append(f"Callback: {cb}.")
    if urgency == "urgent":
        parts.append("Marked URGENT.")
    elif urgency == "fyi":
        parts.append("FYI only.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------

def process_recording(audio_url: str, caller_number: str,
                       data_dir: Path, config: dict | None = None,
                       duration_seconds: int = 0) -> dict:
    """Called by Twilio's recording callback. Downloads audio,
    transcribes, summarizes, saves, and notifies the owner.

    Returns the saved voicemail record (also written to disk).
    """
    config = config or {}
    vm_id = uuid.uuid4().hex[:12]
    audio_dest = _audio_path(data_dir, vm_id)

    # 1. Download (best-effort — even if download fails, we still keep
    #    the remote URL so the owner can play it from Twilio).
    downloaded = _download_audio(audio_url, audio_dest, config)
    local_path = str(audio_dest) if downloaded else ""

    # 2. Transcribe (only if we have a local file)
    if downloaded:
        transcript, tier = _transcribe(audio_dest, config)
    else:
        transcript, tier = (
            "[transcription pending — audio download failed; "
            "play the recording from your dashboard]",
            "pending",
        )

    # 3. Summarize
    fields = _summarize(config, transcript, caller_number)

    # 4. Build record
    record = {
        "id": vm_id,
        "received_at": _now_iso(),
        "from": caller_number or "",
        "audio_url": audio_url or "",
        "audio_local_path": local_path,
        "duration_seconds": int(duration_seconds or 0),
        "transcript": transcript,
        "summary": fields.get("summary", ""),
        "caller_name": fields.get("caller", ""),
        "callback_number": fields.get("callback_number", ""),
        "reason": fields.get("reason", ""),
        "urgency": fields.get("urgency", "normal"),
        "transcription_tier": tier,
        "handled": False,
        "handled_at": "",
    }
    with _LOCK:
        _atomic_write_json(_record_path(data_dir, vm_id), record)

    # 5. Notify the owner (best-effort)
    try:
        import notifications as notify
        title = f"New voicemail from {caller_number or 'unknown'}"
        notify.send(
            config, data_dir,
            event="new_voicemail",
            title=title,
            body=record["summary"] or transcript[:300],
            url="/owner#voicemails",
        )
    except Exception as e:
        log.warning(f"voicemail owner notify failed: {e}")

    log.info(f"voicemail {vm_id} saved (tier={tier}, "
             f"from={caller_number}, dur={duration_seconds}s)")
    return record


# ---------------------------------------------------------------------------
# Dashboard read/write API
# ---------------------------------------------------------------------------

def list_voicemails(data_dir: Path, limit: int = 50) -> list[dict]:
    out: list[dict] = []
    with _LOCK:
        d = _vm_dir(data_dir)
        for p in d.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    out.sort(key=lambda r: r.get("received_at", ""), reverse=True)
    return out[:limit]


def get_voicemail(data_dir: Path, vm_id: str) -> dict | None:
    if not vm_id or not re.fullmatch(r"[0-9a-fA-F]{12}", vm_id):
        return None
    p = _record_path(data_dir, vm_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def mark_handled(data_dir: Path, vm_id: str) -> bool:
    with _LOCK:
        rec = get_voicemail(data_dir, vm_id)
        if not rec:
            return False
        rec["handled"] = True
        rec["handled_at"] = _now_iso()
        _atomic_write_json(_record_path(data_dir, vm_id), rec)
        return True


def delete(data_dir: Path, vm_id: str) -> bool:
    if not vm_id or not re.fullmatch(r"[0-9a-fA-F]{12}", vm_id):
        return False
    with _LOCK:
        jp = _record_path(data_dir, vm_id)
        ap = _audio_path(data_dir, vm_id)
        existed = jp.exists()
        try:
            if jp.exists():
                jp.unlink()
            if ap.exists():
                ap.unlink()
        except OSError as e:
            log.warning(f"voicemail delete error ({vm_id}): {e}")
            return False
        return existed
