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
# Reversed order: "remind me at 5:45 to call Bill"
_REMIND_AT_FIRST_RE = re.compile(
    r"^(?:remind|nudge|ping)\s+me\s+"
    r"(?:on\s+|at\s+|by\s+|in\s+|tomorrow\s+at\s+|today\s+at\s+)?(?P<when>.+?)\s+"
    r"(?:to\s+)(?P<body>.+)$",
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
    # Order: time-first ("remind me at 5:45 to call") > explicit preposition
    # > known time at end > simple fallback.
    for pattern in (_REMIND_AT_FIRST_RE, _REMIND_RE, _REMIND_END_TIME_RE):
        m = pattern.match(text)
        if m:
            body = m.group("body").strip()
            when_phrase = m.group("when").strip()
            # Body sometimes absorbs the day word: "call John tomorrow" + "1:00 p.m."
            # Move trailing day-word from body into the front of when_phrase.
            for day_word in ("tomorrow", "today", "tonight"):
                if body.lower().endswith(" " + day_word):
                    body = body[: -(len(day_word) + 1)].strip()
                    when_phrase = day_word + " at " + when_phrase
                    break
            due_iso = _parse_when(when_phrase) or _default_tomorrow_9am()
            item = mod_reminders.add(user_dir, body, due_iso)
            return {"kind": "reminder", "item": item,
                    "summary": f"Reminder set: \"{item['text']}\" for {_iso_to_local_display(due_iso)}"}
    m = _REMIND_SIMPLE_RE.match(text)
    if m:
        body = m.group(1).strip()
        # Catch incomplete requests like "remind me to call Jeffrey at" —
        # the user pressed send before typing the time. We only treat "at"
        # and "on" as dangling because "in"/"to"/"by" are also natural at
        # the end of verb phrases ("check in", "talk to", "drive by").
        # Also bail when the body is empty or is only a stub preposition
        # ("remind me to" gets parsed as body='to').
        if (not body
                or re.fullmatch(r"(to|at|on|in|by|for)", body, re.IGNORECASE)
                or re.search(r"\b(at|on)\s*$", body, re.IGNORECASE)):
            cleaned = re.sub(r"\b(at|on)\s*$", "", body, flags=re.IGNORECASE).strip()
            if cleaned:
                ask = f"Sure — what time should I remind you to {cleaned}?"
            else:
                ask = "What should I remind you about, and when?"
            return {
                "kind":    "reminder_needs_time",
                "item":    None,
                "summary": ask + (" (try 'at 4:45 today', 'tomorrow at 9 AM',"
                                  " or 'in 30 minutes')"),
            }
        due_iso = _default_tomorrow_9am()
        item = mod_reminders.add(user_dir, body, due_iso)
        return {"kind": "reminder", "item": item,
                "summary": f"Reminder set: \"{item['text']}\" for {_iso_to_local_display(due_iso)}"}

    # Try APPOINTMENT
    m = _APPT_RE.match(text)
    if m:
        title = m.group(1).strip()
        when_phrase = m.group(2).strip()
        start_iso = _parse_when(when_phrase) or _default_tomorrow_9am()
        item = mod_calendar.add(user_dir, title, start_iso)
        return {"kind": "calendar", "item": item,
                "summary": f"Added to your calendar: \"{item['title']}\" at {_iso_to_local_display(start_iso)}"}

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


_TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|am|pm)?\s*$",
    re.IGNORECASE,
)


def _parse_clock(token: str, default_pm_if_low: bool = True) -> tuple[int, int] | None:
    """Parse '5:45', '1:00 p.m.', '4:45 pm', '13:30', '5' into (hour, minute) 24h.
    If no am/pm given and the hour is 1-7, assume PM (matches owner intent —
    nobody says 'remind me at 5' meaning 5am). 8-12 with no suffix → AM."""
    m = _TIME_RE.match(token or "")
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower().replace(".", "")
    if hour > 23 or minute > 59:
        return None
    if suffix in ("am",):
        if hour == 12: hour = 0
    elif suffix in ("pm",):
        if hour < 12: hour += 12
    elif suffix == "" and hour < 12:
        # No suffix — assume PM for hours 1-7 (typical business-day reminders).
        if 1 <= hour <= 7 and default_pm_if_low:
            hour += 12
    return hour, minute


def _local_at(days_offset: int, hour: int, minute: int = 0) -> str:
    """Build a UTC ISO timestamp from a LOCAL hour:minute. Critical for
    natural-language reminders: 'tomorrow at 9' means 9am where the user
    lives, not 9am UTC. Uses Python's OS-local timezone."""
    now_local = datetime.now().astimezone()
    target_local = (now_local + timedelta(days=days_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return target_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_when(phrase: str) -> str | None:
    """Lightweight natural-language → ISO. Returns None when nothing matches
    so the caller can pick a default. All clock times are interpreted in
    LOCAL time; storage is UTC."""
    if not phrase:
        return None
    phrase = phrase.strip().lower().rstrip(".!?")
    now = datetime.now(timezone.utc)

    if phrase in ("tomorrow", "tomorrow morning"):
        return _local_at(1, 9)
    if phrase == "tonight":
        return _local_at(0, 20)
    if phrase in ("today", "later today"):
        return (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if phrase == "next week":
        return _local_at(7, 9)

    # "in N minutes/hours/days/weeks" — offset from now (no TZ involved)
    m_in = re.match(r"^(?:in\s+)?(\d+)\s*(min|minute|hour|hr|day|week)s?$", phrase)
    if m_in:
        n = int(m_in.group(1))
        unit = m_in.group(2)
        delta = {"min": timedelta(minutes=n), "minute": timedelta(minutes=n),
                 "hour": timedelta(hours=n), "hr": timedelta(hours=n),
                 "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
        return (now + delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6}
    base = phrase.replace("on ", "").replace("next ", "").strip()
    if base in weekdays:
        target = weekdays[base]
        now_local = datetime.now().astimezone()
        days_ahead = (target - now_local.weekday()) % 7 or 7
        return _local_at(days_ahead, 9)

    # Time-of-day phrases — "5:45", "1:00 p.m.", "4:45 today", "1pm tomorrow",
    # "tomorrow at 1:00 p.m.", "today at 4:45". Crucially these are parsed in
    # LOCAL time (the user said "1pm" meaning 1pm where they live), then
    # converted to UTC for storage. Without this the reminder lands 7-8 hours
    # off in the wrong direction.
    day_offset = None  # None = decide from time-of-day (past → tomorrow, future → today)
    time_part = phrase
    # Strip leading/trailing day-words
    for word, off in (("today ", 0), ("tomorrow ", 1), ("tonight ", 0)):
        if time_part.startswith(word):
            day_offset = off
            time_part = time_part[len(word):].strip()
            break
    for word, off in ((" today", 0), (" tomorrow", 1), (" tonight", 0)):
        if time_part.endswith(word):
            day_offset = off
            time_part = time_part[:-len(word)].strip()
            break
    # Allow "at 5:45" / "at 1:00 p.m."
    if time_part.startswith("at "):
        time_part = time_part[3:].strip()

    clock = _parse_clock(time_part)
    if clock is not None:
        hour, minute = clock
        now_local = datetime.now().astimezone()
        if day_offset is None:
            target_today_local = now_local.replace(
                hour=hour, minute=minute, second=0, microsecond=0)
            day_offset = 0 if target_today_local > now_local + timedelta(minutes=2) else 1
        target_local = (now_local + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        target_utc = target_local.astimezone(timezone.utc)
        return target_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    return None


def _default_tomorrow_9am() -> str:
    """9 AM the user's local time, tomorrow. Stored as UTC."""
    return _local_at(1, 9)


def _iso_to_local_display(due_iso: str) -> str:
    """Convert a UTC ISO timestamp ('2026-05-27T22:40:00Z') into a local-time
    display string ('2026-05-27 3:40 PM'). Storage stays UTC; only the text
    shown to the user is in local time.

    Uses the OS local timezone (Python's astimezone() with no arg picks up
    /etc/timezone). If parsing fails for any reason, falls back to the raw
    ISO so the reminder is never silently dropped from the summary.
    """
    if not due_iso:
        return ""
    try:
        s = due_iso.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone()  # OS-local tz
        now_local = datetime.now().astimezone()
        # Same day?  show just "3:40 PM today"
        # Tomorrow?  show "3:40 PM tomorrow"
        # Else        "Fri May 30 at 3:40 PM"
        same_day = dt_local.date() == now_local.date()
        tomorrow = dt_local.date() == (now_local + timedelta(days=1)).date()
        time_str = dt_local.strftime("%-I:%M %p") if hasattr(dt_local, "strftime") else ""
        # %-I works on Linux/Mac; fall back if needed
        try:
            time_str = dt_local.strftime("%-I:%M %p")
        except (ValueError, OSError):
            time_str = dt_local.strftime("%I:%M %p").lstrip("0")
        if same_day:
            return f"{time_str} today"
        if tomorrow:
            return f"{time_str} tomorrow"
        return dt_local.strftime("%a %b %d") + " at " + time_str
    except (ValueError, TypeError) as e:
        log.warning(f"_iso_to_local_display failed for {due_iso!r}: {e}")
        return due_iso[:16].replace("T", " ")


def _extract_phone(s: str) -> str:
    m = re.search(r"(\+?\d[\d\s\-\(\)\.]{7,})", s)
    return m.group(1).strip() if m else ""


def _extract_email(s: str) -> str:
    m = re.search(r"[\w\.\-]+@[\w\.\-]+\.\w+", s)
    return m.group(0).strip() if m else ""
