"""
quick_capture (per-user) — single-entry-point for "just file this somewhere."

The owner says (or texts, or types): "remember to order napkins" or
"appointment Friday 2pm with Joe" and Orbi classifies it and writes it
into the right per-user module (notes / tasks / reminders / calendar /
contacts) without needing the owner to think about which one.

Classification is keyword + regex first (cheap, deterministic), with a
fallthrough capture that just files to notes/quick.json so nothing is lost.

Public API:
  capture(user_dir, text) -> {"kind": "task"|"reminder"|"calendar"|"contact"|"note",
                              "item": <the saved record>,
                              "summary": "Filed to your tasks"}
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import calendar as mod_calendar
from modules import contacts as mod_contacts
from modules import reminders as mod_reminders
from modules import tasks as mod_tasks

log = logging.getLogger("orbi.quick_capture")
_LOCK = threading.Lock()
QUICK_FILE = "quick_notes.json"


# ── Classifier regexes ──────────────────────────────────────────────────


# Require an explicit preposition (on / at / by / in) OR a known time phrase
# at end-of-string. Otherwise the non-greedy body would steal half the sentence.
_TIME_WORDS = (r"tomorrow|today|tonight|next\s+week|monday|tuesday|wednesday|"
               r"thursday|friday|saturday|sunday|in\s+\d+\s+(?:min|minute|hour|day|week)s?")
_REMIND_RE = re.compile(
    r"^(?:remind|nudge|ping)\s+me\s+(?:to\s+)?(?P<body>.+?)\s+"
    r"(?:on\s+|at\s+|by\s+|in\s+)(?P<when>.+)$",
    re.IGNORECASE,
)
_REMIND_END_TIME_RE = re.compile(
    r"^(?:remind|nudge|ping)\s+me\s+(?:to\s+)?(?P<body>.+?)\s+(?P<when>" + _TIME_WORDS + r")\s*$",
    re.IGNORECASE,
)
_REMIND_SIMPLE_RE = re.compile(
    r"^(?:remind|nudge)\s+me\s+(?:to\s+)?(.+)$", re.IGNORECASE,
)

_TASK_RE = re.compile(
    r"^(?:add|put)\s+(?:to\s+)?(?:my\s+)?(?:todo|to[\s-]?do|task)s?\s*(?:list)?[:\s]+(.+)$",
    re.IGNORECASE,
)
_TASK_SIMPLE_RE = re.compile(
    r"^(?:todo|task)[:\s]+(.+)$", re.IGNORECASE,
)

_APPT_RE = re.compile(
    r"^(?:appointment|meeting|event|book|schedule)\s+(.+?)\s+"
    r"(?:on\s+|at\s+|for\s+)(.+)$",
    re.IGNORECASE,
)

_CONTACT_RE = re.compile(
    r"^(?:add|save)\s+(?:contact|person)[:\s]+"
    r"(?P<name>[A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+){0,2})"
    r"(?:\s+(?P<rest>.+))?$",
)


# ── Public API ──────────────────────────────────────────────────────────


def capture(user_dir: Path, text: str) -> dict:
    """Classify and file a free-form snippet. Always returns a dict
    with kind/item/summary. Falls back to a generic note if nothing matches."""
    text = (text or "").strip()
    if not text:
        return {"kind": "noop", "item": None, "summary": "Nothing to capture."}

    # Try TASK
    m = _TASK_RE.match(text) or _TASK_SIMPLE_RE.match(text)
    if m:
        body = m.group(1).strip()
        item = mod_tasks.add(user_dir, body)
        return {"kind": "task", "item": item,
                "summary": f"Added to your tasks: \"{item['text']}\""}

    # Try REMINDER — try the strict patterns first so we don't truncate.
    # Order: explicit preposition > known time at end > simple fallback.
    for pattern in (_REMIND_RE, _REMIND_END_TIME_RE):
        m = pattern.match(text)
        if m:
            body = m.group("body").strip()
            when_phrase = m.group("when").strip()
            due_iso = _parse_when(when_phrase) or _default_tomorrow_9am()
            item = mod_reminders.add(user_dir, body, due_iso)
            return {"kind": "reminder", "item": item,
                    "summary": f"Reminder set: \"{item['text']}\" for {due_iso[:16].replace('T',' ')}"}
    m = _REMIND_SIMPLE_RE.match(text)
    if m:
        body = m.group(1).strip()
        item = mod_reminders.add(user_dir, body, _default_tomorrow_9am())
        return {"kind": "reminder", "item": item,
                "summary": f"Reminder set: \"{item['text']}\" for tomorrow 9am"}

    # Try APPOINTMENT
    m = _APPT_RE.match(text)
    if m:
        title = m.group(1).strip()
        when_phrase = m.group(2).strip()
        start_iso = _parse_when(when_phrase) or _default_tomorrow_9am()
        item = mod_calendar.add(user_dir, title, start_iso)
        return {"kind": "calendar", "item": item,
                "summary": f"Added to your calendar: \"{item['title']}\" at {start_iso[:16].replace('T',' ')}"}

    # Try CONTACT
    m = _CONTACT_RE.match(text)
    if m:
        name = m.group("name").strip()
        rest = (m.group("rest") or "").strip()
        phone = _extract_phone(rest)
        email = _extract_email(rest)
        item = mod_contacts.add(user_dir, name=name, phone=phone, email=email,
                                notes=rest, source="manual")
        return {"kind": "contact", "item": item,
                "summary": f"Saved contact: {item['name']}"}

    # Fallback — generic quick note
    item = _save_quick_note(user_dir, text)
    return {"kind": "note", "item": item,
            "summary": "Saved as a quick note. Tell me 'show my quick notes' to see them."}


def list_quick_notes(user_dir: Path) -> list[dict]:
    p = user_dir / QUICK_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return sorted(data if isinstance(data, list) else [],
                      key=lambda n: n.get("ts", 0), reverse=True)
    except (json.JSONDecodeError, OSError):
        return []


def remove_quick_note(user_dir: Path, note_id: str) -> bool:
    p = user_dir / QUICK_FILE
    if not p.exists():
        return False
    with _LOCK:
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or []
        except (json.JSONDecodeError, OSError):
            return False
        before = len(data)
        data = [n for n in data if n.get("id") != note_id]
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        return len(data) < before


# ── Internal helpers ────────────────────────────────────────────────────


def _save_quick_note(user_dir: Path, text: str) -> dict:
    p = user_dir / QUICK_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    note = {
        "id":   uuid.uuid4().hex[:12],
        "text": text,
        "ts":   time.time(),
        "at":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with _LOCK:
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        except (json.JSONDecodeError, OSError):
            data = []
        data.append(note)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    return note


def _parse_when(phrase: str) -> str | None:
    """Lightweight natural-language → ISO. Handles a handful of common patterns,
    bigger phrases fall through to None and the caller picks a default."""
    if not phrase:
        return None
    phrase = phrase.strip().lower().rstrip(".!?")
    now = datetime.now(timezone.utc)

    if phrase in ("tomorrow", "tomorrow morning"):
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    if phrase == "tonight":
        return now.replace(hour=20, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    if phrase in ("today", "later today"):
        return (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if phrase == "next week":
        return (now + timedelta(days=7)).replace(hour=9, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6}
    base = phrase.replace("on ", "").replace("next ", "").strip()
    if base in weekdays:
        target = weekdays[base]
        days_ahead = (target - now.weekday()) % 7 or 7
        return (now + timedelta(days=days_ahead)).replace(hour=9, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    return None


def _default_tomorrow_9am() -> str:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_phone(s: str) -> str:
    m = re.search(r"(\+?\d[\d\s\-\(\)\.]{7,})", s)
    return m.group(1).strip() if m else ""


def _extract_email(s: str) -> str:
    m = re.search(r"[\w\.\-]+@[\w\.\-]+\.\w+", s)
    return m.group(0).strip() if m else ""
