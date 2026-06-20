"""
thread_tone — per-customer-thread tone overrides.

The owner talks differently to different people. Mrs. Henderson gets the
"yes ma'am" voice. The Reno construction site gets clipped + practical.
Cousin Mike gets jokes. When Orbi answers on behalf of the owner, she
should match the existing tone of the relationship instead of reverting
to a single dashboard-wide default.

This module stores a per-contact (or per-thread) tone preference + an
optional sample-text bank Orbi can use as a few-shot prompt seed. The
prompts module reads from here when composing replies.

Storage:   data/users/<username>/thread_tone.json
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("orbi.modules.thread_tone")

_FILENAME = "thread_tone.json"
_LOCK = threading.Lock()
_MAX_SAMPLES_PER_THREAD = 6

# Tone shapes Orbi understands. Free-form notes are still allowed —
# these are just the menu the dashboard offers as quick picks.
KNOWN_TONES = [
    "warm",        # "Hi Linda — great to hear from you!"
    "formal",      # "Dear Mr. Henderson, thank you for your inquiry."
    "casual",      # "Hey, got your message"
    "brief",       # 1-2 sentences, no small talk
    "playful",     # light banter okay
    "professional",  # default
]


def _path(user_dir: Path) -> Path:
    return user_dir / _FILENAME


def _load(user_dir: Path) -> dict:
    p = _path(user_dir)
    if not p.exists():
        return {"threads": {}}
    with _LOCK:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"threads": {}}


def _save(user_dir: Path, data: dict) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        tmp.replace(p)


def set_tone(user_dir: Path, thread_id: str,
              tone: str, note: str = "") -> dict:
    """Set or update the tone for a thread (usually a contact_id, but
    can also be a phone number or an email-thread id). `tone` should
    be from KNOWN_TONES; free-form is accepted too."""
    if not thread_id:
        return {}
    data = _load(user_dir)
    threads = data.setdefault("threads", {})
    bucket = threads.setdefault(thread_id, {"tone": "", "note": "", "samples": []})
    bucket["tone"]       = tone.strip().lower()
    bucket["note"]       = note.strip()[:300]
    bucket["updated_at"] = int(time.time())
    _save(user_dir, data)
    return bucket


def add_sample(user_dir: Path, thread_id: str, sample_text: str) -> int:
    """Stash a short snippet from an actual past message in this thread
    so Orbi has a real example of the owner's voice for this person.
    Returns the number of samples now on file."""
    if not thread_id or not sample_text.strip():
        return 0
    data = _load(user_dir)
    threads = data.setdefault("threads", {})
    bucket = threads.setdefault(thread_id, {"tone": "", "note": "", "samples": []})
    samples = bucket.setdefault("samples", [])
    snippet = sample_text.strip()[:400]
    if snippet in samples:
        return len(samples)
    samples.insert(0, snippet)
    if len(samples) > _MAX_SAMPLES_PER_THREAD:
        samples[:] = samples[:_MAX_SAMPLES_PER_THREAD]
    _save(user_dir, data)
    return len(samples)


def get_thread(user_dir: Path, thread_id: str) -> dict | None:
    return (_load(user_dir).get("threads") or {}).get(thread_id)


def format_for_prompt(user_dir: Path, thread_id: str,
                       max_chars: int = 700) -> str:
    """Render the tone + sample text into a short block the prompt
    builder can paste into the system prompt when Orbi is writing on
    behalf of the owner to this thread."""
    bucket = get_thread(user_dir, thread_id) or {}
    if not bucket:
        return ""
    tone = bucket.get("tone") or ""
    note = bucket.get("note") or ""
    samples = bucket.get("samples") or []
    lines = []
    if tone:
        lines.append(f"Tone for this contact: {tone}.")
    if note:
        lines.append(f"Notes: {note}")
    if samples:
        lines.append("Recent examples of how the owner has written to "
                     "them — match this voice:")
        total = sum(len(l) + 1 for l in lines)
        for s in samples:
            line = f"  \"{s}\""
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
    return "\n".join(lines)


def forget(user_dir: Path, thread_id: str) -> bool:
    data = _load(user_dir)
    if thread_id not in (data.get("threads") or {}):
        return False
    data["threads"].pop(thread_id)
    _save(user_dir, data)
    return True
