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
from modules import notes as mod_notes
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
# Reversed order: "remind me at 5:45 to call Bill" /
# "remind me tomorrow at 5pm to send the schedule".
# Day-words ('tomorrow', weekday names) MUST be left in the `when`
# capture so they reach `_parse_when()` — previously 'tomorrow at' was
# being eaten by the optional prefix and the reminder fell back to
# today.
_REMIND_AT_FIRST_RE = re.compile(
    r"^(?:remind|nudge|ping)\s+me\s+"
    r"(?:on\s+|at\s+|by\s+|in\s+)?(?P<when>.+?)\s+"
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
# Does the message body contain ANY hint of when the reminder should fire?
# If not, we ASK instead of silently defaulting to tomorrow 9am. Hints:
#   - any digit (5, 5:45, 4pm, in 30 min)
#   - day-words: today, tomorrow, tonight, monday..sunday, weekend, weekday
#   - time-of-day words: morning, noon, afternoon, evening, midnight
#   - relative: later, soon, asap, now, in N units (already digit-matched above)
#   - quarters / next-N
_HAS_TIME_SIGNAL_RE = re.compile(
    r"\d"
    r"|\b(?:today|tomorrow|tonight|tomorrow\s+morning|tomorrow\s+night|"
    r"this\s+(?:morning|afternoon|evening|weekend)|"
    r"next\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening|noon|midnight|"
    r"later|soon|asap|right\s+now|in\s+a\s+(?:minute|sec|second|moment|bit|while)|"
    r"first\s+thing|end\s+of\s+(?:the\s+)?(?:day|week|month))\b",
    re.IGNORECASE,
)

_TASK_RE = re.compile(
    r"^(?:add|put)\s+(?:to\s+)?(?:my\s+)?(?:todo|to[\s-]?do|task)s?\s*(?:list)?[:\s]+(.+)$",
    re.IGNORECASE,
)
_TASK_SIMPLE_RE = re.compile(
    r"^(?:todo|task)[:\s]+(.+)$", re.IGNORECASE,
)

_APPT_RE = re.compile(
    r"^(?:appointment|meeting|event|book|schedule|"
    # Frank 2026-06-23: extended so "lunch with Sam Friday at noon",
    # "dinner Wednesday at 7pm", "coffee with Mike Mon 9am", "block off
    # Thursday for vacation", "call Bill tomorrow at 3pm" all classify
    # as calendar entries (previously fell through to quick-note).
    r"lunch|dinner|breakfast|coffee|drinks|"
    r"call|meeting|"
    r"block\s+off)\s+(.+?)\s+"
    r"(?:on\s+|at\s+|for\s+)(.+)$",
    re.IGNORECASE,
)

# Frank 2026-06-23: separate pattern for the "block off all day X" /
# "block off X for vacation" all-day form, which doesn't have an "at <time>"
# clause. Captured as an all-day calendar event.
_BLOCK_OFF_ALLDAY_RE = re.compile(
    r"^block\s+off\s+(?:all\s+day\s+)?"
    r"(?P<day>monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"tomorrow|today|next\s+\w+)"
    r"(?:\s+(?:for|to)\s+(?P<reason>.+))?$",
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
            # Frank 2026-06-23: extended to weekday names too — was leaking
            # 'pay rent Friday' as the reminder body with time 'tomorrow 9am'
            # instead of 'pay rent' on Friday at 9am.
            for day_word in ("tomorrow", "today", "tonight",
                              "monday", "tuesday", "wednesday", "thursday",
                              "friday", "saturday", "sunday"):
                if body.lower().endswith(" " + day_word):
                    body = body[: -(len(day_word) + 1)].strip()
                    when_phrase = day_word + " at " + when_phrase
                    break
            due_iso = _parse_when(when_phrase) or _default_tomorrow_9am()
            item = mod_reminders.add(user_dir, body, due_iso)
            note = _rollover_note(when_phrase, due_iso)
            return {"kind": "reminder", "item": item,
                    "summary": f"Reminder set: \"{item['text']}\" for {_iso_to_local_display(due_iso)}{note}"}
    m = _REMIND_SIMPLE_RE.match(text)
    if m:
        body = m.group(1).strip()
        # Catch incomplete requests:
        #   - 'remind me to call Jeffrey at' (dangling preposition)
        #   - 'remind me to' (stub)
        #   - 'remind me to call Bob' (no time signal AT ALL)
        # Don't default to tomorrow 9am — the user almost never means that
        # and silent defaults make Orby look like she invented a time.
        has_time_signal = bool(_HAS_TIME_SIGNAL_RE.search(body))
        is_stub        = bool(re.fullmatch(r"(to|at|on|in|by|for)", body, re.IGNORECASE))
        is_dangling    = bool(re.search(r"\b(at|on)\s*$", body, re.IGNORECASE))
        if not body or is_stub or is_dangling or not has_time_signal:
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

    # Try ALL-DAY BLOCK ("block off Thursday for vacation")
    m = _BLOCK_OFF_ALLDAY_RE.match(text)
    if m:
        day_phrase = m.group("day").strip().lower()
        reason = (m.group("reason") or "").strip() or "blocked"
        start_iso = _parse_when(day_phrase) or _default_tomorrow_9am()
        title = f"{reason}" if reason != "blocked" else "blocked off"
        item = mod_calendar.add(user_dir, title, start_iso, all_day=True)
        return {"kind": "calendar", "item": item,
                "summary": f"Added to your calendar (all day): \"{title}\" on {_iso_to_local_display(start_iso)[:10]}"}

    # Try APPOINTMENT
    m = _APPT_RE.match(text)
    if m:
        title = m.group(1).strip()
        when_phrase = m.group(2).strip()
        # Move trailing day-word out of title into the front of when_phrase.
        # Tolerate both relative ('tomorrow') AND weekday names ('friday'),
        # because "appointment with Joe Friday at 2pm" parses with
        # title='with Joe Friday' / when='2pm' and we want title='with Joe'
        # and when='friday at 2pm'.
        day_words = ("tomorrow", "today", "tonight",
                     "monday", "tuesday", "wednesday", "thursday",
                     "friday", "saturday", "sunday")
        title_low = title.lower()
        for day_word in day_words:
            if title_low.endswith(" " + day_word):
                title = title[: -(len(day_word) + 1)].strip()
                when_phrase = day_word + " at " + when_phrase
                break
        # Also absorb a trailing clock time the regex left in the title:
        # 'with Joe Friday 10am' should become title='with Joe' +
        # when_phrase='friday at 10am'. Done AFTER the day-word pass so
        # they stack.
        time_tail = re.search(
            r"\s+(?P<t>\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*$",
            title, re.IGNORECASE,
        )
        if time_tail:
            title = title[: time_tail.start()].strip()
            when_phrase = (when_phrase + " " + time_tail.group("t")).strip()
        # Drop leading article 'a/an/the' the regex includes
        title = re.sub(r"^(?:a|an|the)\s+", "", title, flags=re.IGNORECASE)
        # Calendar event dedup: same title + same start within 30 min →
        # return the existing event instead of stacking duplicates.
        start_iso = _parse_when(when_phrase) or _default_tomorrow_9am()
        existing_event = _find_duplicate_event(user_dir, title, start_iso)
        if existing_event:
            return {"kind": "calendar", "item": existing_event,
                    "summary": (
                        f"You already have \"{existing_event['title']}\" "
                        f"at {_iso_to_local_display(existing_event['start'])}. "
                        f"No duplicate created.")}
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

    # Frank 2026-06-23: if the user explicitly said "take a note: X" or
    # "note to self: X", route to the regular notes module so it shows up
    # in the same notes Orby reads from when answering "what notes do I
    # have about X". Quick-notes are a separate bucket she doesn't search.
    note_match = re.match(
        r"^(?:take\s+a\s+note|note\s+to\s+self|note)\s*:?\s+(.+)$",
        text, re.IGNORECASE)
    if note_match:
        # The user_dir here is the user's per-user folder. mod_notes.add
        # writes to data_dir (the SHARED data root) so all users / Orby
        # can see the same note. user_dir's parent.parent IS data_dir.
        try:
            data_dir = user_dir.parent.parent  # users/<name>/ → data root
        except Exception:
            data_dir = user_dir
        item = mod_notes.add(data_dir, content=note_match.group(1).strip())
        return {"kind": "note", "item": item,
                "summary": f"Got it — noted: \"{item.get('content','')[:80]}\""}

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


def _find_duplicate_event(user_dir: Path, title: str, start_iso: str) -> dict | None:
    """Return an existing calendar event with the same normalized title
    and a start within 30 minutes of `start_iso`, or None if no near-
    duplicate exists. Prevents the "add appointment with Joe Friday at
    2pm" tap from stacking duplicates when accidentally said twice."""
    norm_title = " ".join((title or "").lower().strip().split())
    if not norm_title or not start_iso:
        return None
    try:
        target = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    try:
        events = mod_calendar.list_all(user_dir) or []
    except Exception:
        return None
    for e in events:
        existing_title = " ".join((e.get("title") or "").lower().strip().split())
        if existing_title != norm_title:
            continue
        try:
            es = datetime.fromisoformat((e.get("start") or "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if abs((es - target).total_seconds()) <= 30 * 60:
            return e
    return None


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

    # "in N seconds/minutes/hours/days/weeks/months" — offset from now
    m_in = re.match(
        r"^(?:in\s+)?(\d+)\s*"
        r"(sec|second|min|minute|hour|hr|day|week|month|mo)s?$",
        phrase,
    )
    if m_in:
        n = int(m_in.group(1))
        unit = m_in.group(2)
        delta = {"sec": timedelta(seconds=n), "second": timedelta(seconds=n),
                 "min": timedelta(minutes=n), "minute": timedelta(minutes=n),
                 "hour": timedelta(hours=n), "hr": timedelta(hours=n),
                 "day": timedelta(days=n), "week": timedelta(weeks=n),
                 "month": timedelta(days=30 * n), "mo": timedelta(days=30 * n)}[unit]
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
    # Frank 2026-06-23: "Friday at 10am" was falling through to default
    # 9am-tomorrow because the day-strip only knew today/tomorrow/tonight.
    # Extend to weekday names so "Friday 10am" → day_offset=days-to-Fri,
    # time=10am. Same for trailing form ("10am Friday").
    if day_offset is None:
        for day_name, weekday_num in (("monday", 0), ("tuesday", 1),
                                       ("wednesday", 2), ("thursday", 3),
                                       ("friday", 4), ("saturday", 5),
                                       ("sunday", 6)):
            now_local = datetime.now().astimezone()
            days_ahead = (weekday_num - now_local.weekday()) % 7 or 7
            if time_part.startswith(day_name + " "):
                day_offset = days_ahead
                time_part = time_part[len(day_name) + 1:].strip()
                break
            if time_part.endswith(" " + day_name):
                day_offset = days_ahead
                time_part = time_part[:-len(day_name) - 1].strip()
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


def _rollover_note(when_phrase: str, due_iso: str) -> str:
    """Returns a short parenthetical to append to the reminder summary IF
    we silently rolled a bare time-of-day to tomorrow because the time had
    already passed today. Without this note, "remind me at 3:37pm" at 5pm
    becomes "Reminder set for 3:37 PM tomorrow" and the user thinks Orby
    invented "tomorrow" out of nowhere.
    """
    if not when_phrase or not due_iso:
        return ""
    wp_lower = when_phrase.lower()
    # Skip if user explicitly said tomorrow / today / a weekday — no surprise.
    explicit_day = ("tomorrow", "today", "tonight", "monday", "tuesday",
                    "wednesday", "thursday", "friday", "saturday", "sunday",
                    "next ", "in ")
    if any(w in wp_lower for w in explicit_day):
        return ""
    try:
        s = due_iso.replace("Z", "+00:00")
        due_dt = datetime.fromisoformat(s).astimezone()
        now_local = datetime.now().astimezone()
        # Is the due date tomorrow (or later) instead of today?
        if due_dt.date() > now_local.date():
            time_str = due_dt.strftime("%-I:%M %p").lower() if hasattr(due_dt, 'strftime') else ""
            return f" (since {time_str} already passed today — say so if you meant today)"
    except (ValueError, OSError):
        pass
    return ""


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
