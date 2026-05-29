"""
scheduler.py — meeting scheduling for Orbi customer installs.

A thin orchestration layer on top of modules/calendar.py (the per-user
calendar) and gcal.py (Google Calendar two-way sync). The flow is:

    1. owner asks Orbi for open slots in their working week
    2. Orbi computes free windows by walking calendar.upcoming()
    3. owner picks (or LLM picks 3 best) and Orbi drafts a polite email
       to the attendee proposing them
    4. owner sends via Gmail/Outlook connector (or via safe_send.py)
    5. when the attendee replies, parse_reschedule_request() tries to
       extract the new preferred time
    6. owner calls book_meeting() — adds to local calendar AND, if
       Google Calendar is connected, pushes to Google in one shot

DESIGN NOTES
------------
- No new state file. The calendar.json (modules/calendar.py) is the
  single source of truth. We never invent a side-store for proposed
  meetings — that lives in the email thread (owner's existing inbox).
- All datetimes are ISO-8601 strings ending in "Z" (UTC). Working hours
  are interpreted in UTC for simplicity; the owner can shift the tuple
  to match their wall-clock if needed.
- Lazy imports of llm_client and gcal so an install without those bits
  can still compute slots.

ROUTE SURFACE — see bottom of file. The orchestrator wires these into
orbi.py; this module does NOT touch orbi.py itself.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("orbi.scheduler")

# How many candidate slots to LLM-draft into a "propose" email.
_TOP_N_FOR_PROPOSAL = 3
# Skip weekends by default.
_SKIP_WEEKENDS = True


# ---------------------------------------------------------------------------
# Public API: find open slots
# ---------------------------------------------------------------------------

def find_open_slots(user_dir,
                    duration_minutes: int = 30,
                    days_ahead: int = 7,
                    working_hours: tuple[int, int] = (9, 17)) -> list[dict]:
    """
    Walk each working-hour day in the next `days_ahead` looking for free
    windows long enough for `duration_minutes`. Returns a list of dicts:

        {
          "start_iso":  "2026-05-28T14:00:00Z",
          "end_iso":    "2026-05-28T14:30:00Z",
          "day_label":  "Thursday May 28",
          "time_label": "2:00 PM",
        }

    Sorted earliest-first.
    """
    user_dir = Path(user_dir)
    duration_minutes = max(5, int(duration_minutes or 30))
    days_ahead       = max(1, int(days_ahead or 7))
    wh_start, wh_end = working_hours
    wh_start = max(0, min(23, int(wh_start)))
    wh_end   = max(wh_start + 1, min(24, int(wh_end)))

    # Lazy import — keeps this module bootable even if modules.calendar
    # has a problem during install.
    try:
        import modules.calendar as cal
    except Exception as exc:    # noqa: BLE001
        log.warning("scheduler.find_open_slots: calendar import failed: %s", exc)
        return []

    events = cal.upcoming(user_dir, days=days_ahead)
    busy = _events_to_busy_blocks(events)

    slots: list[dict] = []
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # Round "now" up to the next 15-min mark so we don't propose 2:03 PM.
    now = _round_up(now, minutes=15)

    for day_offset in range(days_ahead + 1):
        day_start_date = (now + timedelta(days=day_offset)).date()
        if _SKIP_WEEKENDS and day_start_date.weekday() >= 5:
            continue

        day_open  = datetime(day_start_date.year, day_start_date.month,
                             day_start_date.day, wh_start, 0,
                             tzinfo=timezone.utc)
        day_close = datetime(day_start_date.year, day_start_date.month,
                             day_start_date.day, wh_end, 0,
                             tzinfo=timezone.utc)

        cursor = max(day_open, now if day_offset == 0 else day_open)
        # Walk this day's busy blocks chronologically.
        days_busy = sorted(
            [(s, e) for (s, e) in busy if s.date() == day_start_date or
                                          e.date() == day_start_date],
            key=lambda x: x[0],
        )

        for (bs, be) in days_busy:
            if be <= cursor:
                continue            # busy block already behind us
            if bs >= day_close:
                break               # remaining busy blocks are after EOD
            # Free window: cursor .. bs (clamped to day_close)
            free_end = min(bs, day_close)
            slots.extend(_emit_slots_in(cursor, free_end, duration_minutes))
            cursor = max(cursor, be)

        # Final window of the day: cursor .. day_close
        if cursor < day_close:
            slots.extend(_emit_slots_in(cursor, day_close, duration_minutes))

    log.info("scheduler: found %d open slots (dur=%dm, days=%d)",
             len(slots), duration_minutes, days_ahead)
    return slots


# ---------------------------------------------------------------------------
# Public API: propose meeting
# ---------------------------------------------------------------------------

def propose_meeting(user_dir,
                    attendee_name: str,
                    attendee_email: str,
                    duration_minutes: int,
                    days_ahead: int = 7) -> dict:
    """
    Pick the 3 best open slots and LLM-draft a polite email proposing
    them. Returns:

        {
          "open_slots":       [<3 slot dicts>],
          "draft_email_text": "<the email body the owner can send>",
        }

    The owner is responsible for actually sending the email — wire this
    up to connectors.gmail.send_message / connectors.outlook.send_message
    or to safe_send.send_email().
    """
    slots = find_open_slots(user_dir, duration_minutes=duration_minutes,
                            days_ahead=days_ahead)
    top = slots[:_TOP_N_FOR_PROPOSAL]
    name = (attendee_name or "").strip() or "there"
    draft = _draft_proposal_email(name, attendee_email, duration_minutes, top, user_dir)
    return {"open_slots": top, "draft_email_text": draft}


# ---------------------------------------------------------------------------
# Public API: book meeting
# ---------------------------------------------------------------------------

def book_meeting(user_dir,
                 attendee_name: str,
                 attendee_email: str,
                 start_iso: str,
                 end_iso: str,
                 title: str | None = None,
                 notes: str = "") -> dict:
    """
    Add the event to the local calendar AND, if Google Calendar is
    connected, also push it up via gcal.push_to_google(). Returns the
    created event dict (Orbi event shape — id, start, end, ...) with
    an extra '_gcal_pushed' bool field.
    """
    user_dir = Path(user_dir)
    name = (attendee_name or "").strip()
    email = (attendee_email or "").strip()
    title = (title or f"Meeting with {name or email or 'guest'}").strip()
    attendees = [a for a in (name, email) if a]

    try:
        import modules.calendar as cal
    except Exception as exc:    # noqa: BLE001
        log.warning("scheduler.book_meeting: calendar import failed: %s", exc)
        return {"booked": False, "error": f"calendar import: {exc}"}

    event = cal.add(
        user_dir,
        title=title,
        start=start_iso,
        end=end_iso,
        notes=notes,
        with_=attendees,
    )
    log.info("scheduler: booked local event %s (%s -> %s) with %s",
             event.get("id"), start_iso, end_iso, email or name or "?")

    pushed = False
    push_error = ""
    try:
        import gcal
        if gcal.is_connected(user_dir):
            res = gcal.push_to_google(user_dir, event["id"])
            pushed = bool(res.get("pushed"))
            push_error = res.get("error", "") or ""
            if pushed:
                log.info("scheduler: pushed event %s to Google (%s)",
                         event["id"], res.get("gcal_id", ""))
            else:
                log.warning("scheduler: gcal push failed for %s: %s",
                            event["id"], push_error)
    except Exception as exc:    # noqa: BLE001 — gcal optional
        log.info("scheduler: gcal unavailable, local-only booking: %s", exc)
        push_error = str(exc)

    event["_gcal_pushed"] = pushed
    if push_error:
        event["_gcal_error"] = push_error
    event["booked"] = True
    return event


# ---------------------------------------------------------------------------
# Public API: parse a reschedule request from a customer reply
# ---------------------------------------------------------------------------

# Day-name / weekday tokens we recognise in free text.
_WEEKDAYS = {
    "monday":    0, "mon": 0,
    "tuesday":   1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday":  3, "thu": 3, "thurs": 3,
    "friday":    4, "fri": 4,
    "saturday":  5, "sat": 5,
    "sunday":    6, "sun": 6,
}

_TIME_RE = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm|AM|PM|a\.m\.|p\.m\.)?\b"
)

# "next Tuesday", "tuesday at 3pm", "tomorrow at 10", "wed 2 PM"
_RESCHED_HINTS = re.compile(
    r"(reschedul|push|move|different time|another time|"
    r"can(?:'t| not)\s+make|need to (?:reschedule|change)|"
    r"how about|what about|works better|prefer)", re.I,
)

_EVENT_ID_RE = re.compile(r"\b(event[_-]?id|orbi[_-]?id)[: ]+([a-f0-9]{8,12})\b", re.I)


def parse_reschedule_request(text: str) -> dict | None:
    """
    Extract a preferred new time from a customer's free-text reply.
    Returns {"new_start_iso": "...", "original_event_id": "..."} on
    success, or None when no time hint could be parsed.

    This is intentionally heuristic — it's a hint, not a commitment.
    The owner always confirms before book_meeting() is called.
    """
    if not text or not text.strip():
        return None

    looks_like_reschedule = bool(_RESCHED_HINTS.search(text))
    new_dt = _parse_natural_datetime(text)
    if not new_dt:
        return None

    # If the reply doesn't even mention rescheduling AND no day/weekday
    # token is present, fall back to None to avoid false positives on
    # casual "see you tomorrow!" sign-offs.
    if not looks_like_reschedule and not _has_day_token(text):
        return None

    out: dict = {"new_start_iso": _iso(new_dt)}
    m = _EVENT_ID_RE.search(text)
    if m:
        out["original_event_id"] = m.group(2).lower()
    return out


# ---------------------------------------------------------------------------
# Internal: busy-block extraction
# ---------------------------------------------------------------------------

def _events_to_busy_blocks(events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Convert calendar events into [(start_dt, end_dt), ...] in UTC. Ignores
    all-day events (they don't block working-hour slots)."""
    busy: list[tuple[datetime, datetime]] = []
    for e in events or []:
        if e.get("all_day"):
            continue
        s = _parse_iso(e.get("start", ""))
        en = _parse_iso(e.get("end", "") or e.get("start", ""))
        if not s or not en:
            continue
        if en <= s:
            en = s + timedelta(minutes=30)
        busy.append((s, en))
    return busy


def _emit_slots_in(window_start: datetime, window_end: datetime,
                   duration_minutes: int) -> list[dict]:
    """Yield one slot at each 15-min grid point that still fits the duration
    inside [window_start, window_end). Returns a list to keep this simple."""
    out: list[dict] = []
    cursor = _round_up(window_start, minutes=15)
    while cursor + timedelta(minutes=duration_minutes) <= window_end:
        end = cursor + timedelta(minutes=duration_minutes)
        out.append({
            "start_iso":  _iso(cursor),
            "end_iso":    _iso(end),
            "day_label":  cursor.strftime("%A %B %-d"),
            "time_label": _fmt_time(cursor),
        })
        cursor += timedelta(minutes=30)     # don't flood with overlapping slots
    return out


# ---------------------------------------------------------------------------
# Internal: LLM-drafted proposal email
# ---------------------------------------------------------------------------

def _draft_proposal_email(name: str, email: str, duration_minutes: int,
                          slots: list[dict], user_dir) -> str:
    """Build a polite proposal email. Tries LLM first; falls back to a
    static template if no LLM is reachable."""
    if not slots:
        return (f"Hi {name},\n\nI'd love to find a time that works — could "
                f"you share a few times in the next week that suit you? "
                f"Thanks!\n")

    slot_lines = "\n".join(
        f"  - {s['day_label']} at {s['time_label']}" for s in slots
    )

    fallback = (
        f"Hi {name},\n\n"
        f"Thanks for getting in touch. I'd love to set up a "
        f"{duration_minutes}-minute meeting. Any of these times work?\n\n"
        f"{slot_lines}\n\n"
        f"Just reply with the one that fits — happy to find another time "
        f"if none of these do.\n\n"
        f"Talk soon!\n"
    )

    # Try the LLM. If config isn't readily reachable, just return fallback.
    try:
        import json
        import llm_client
        config_path = Path(__file__).parent / "config.json"
        if not config_path.exists():
            return fallback
        config = json.loads(config_path.read_text(encoding="utf-8"))

        system = (
            "You write short, warm meeting-proposal emails for a business "
            "owner. Output ONLY the email body — no subject, no signature, no "
            "preamble like 'Sure, here is the email'. Keep it 3-5 short "
            "sentences. Friendly, plain English, no marketing-speak. "
            "List the proposed times verbatim as bullet points exactly as given."
        )
        user_msg = (
            f"Recipient name: {name}\n"
            f"Meeting duration: {duration_minutes} minutes\n"
            f"Times to propose (use these EXACTLY, do not invent others):\n"
            f"{slot_lines}\n\n"
            f"Ask them to reply with the one that works, and offer to find "
            f"another time if none do."
        )
        resp = llm_client.generate(config, system,
                                   [{"role": "user", "content": user_msg}])
        text = (resp.text or "").strip()
        if text and len(text) > 40:
            return text
    except Exception as exc:    # noqa: BLE001
        log.info("scheduler: LLM draft failed, using fallback: %s", exc)
    return fallback


# ---------------------------------------------------------------------------
# Internal: free-text date/time parsing
# ---------------------------------------------------------------------------

def _has_day_token(text: str) -> bool:
    lower = text.lower()
    if any(w in lower for w in _WEEKDAYS):
        return True
    if "today" in lower or "tomorrow" in lower or "tonight" in lower:
        return True
    if re.search(r"\b\d{1,2}[/-]\d{1,2}\b", text):
        return True
    return False


def _parse_natural_datetime(text: str) -> datetime | None:
    """Best-effort parser. Recognises: today, tomorrow, weekday names
    (with optional 'next'), and a time like '3pm' or '14:30'. Returns
    a UTC datetime, or None."""
    if not text:
        return None
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    lower = text.lower()

    target_date = None
    if "tomorrow" in lower:
        target_date = (now + timedelta(days=1)).date()
    elif "today" in lower or "tonight" in lower:
        target_date = now.date()
    else:
        # weekday match
        for token, weekday in _WEEKDAYS.items():
            if re.search(rf"\b{re.escape(token)}\b", lower):
                days_ahead = (weekday - now.weekday()) % 7
                # "next <weekday>" => skip a week if it's the same day
                if days_ahead == 0:
                    days_ahead = 7
                if "next" in lower:
                    days_ahead = days_ahead if days_ahead >= 7 else days_ahead + 7
                target_date = (now + timedelta(days=days_ahead)).date()
                break

    # Time
    target_time = None
    for m in _TIME_RE.finditer(text):
        h = int(m.group("h"))
        mins = int(m.group("m") or 0)
        ampm = (m.group("ampm") or "").lower().replace(".", "")
        if h < 0 or h > 23 or mins < 0 or mins > 59:
            continue
        if ampm in ("pm", "p m") and h < 12:
            h += 12
        if ampm in ("am", "a m") and h == 12:
            h = 0
        # Skip "1pm tomorrow" wouldn't be confused; but skip ambiguous "10"
        # without ampm if h <= 7 (probably a list number, not a time).
        if not ampm and h <= 7 and mins == 0 and "at" not in lower:
            continue
        target_time = (h, mins)
        break

    if not target_date and not target_time:
        return None
    if not target_date:
        # Just a time — assume today or, if it's already past, tomorrow.
        candidate = now.replace(hour=target_time[0], minute=target_time[1])
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if not target_time:
        # Just a date — default to 10am local guess (UTC for simplicity).
        target_time = (10, 0)
    return datetime(target_date.year, target_date.month, target_date.day,
                    target_time[0], target_time[1], tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Internal: datetime helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")) \
                .astimezone(timezone.utc)
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0) \
        .isoformat().replace("+00:00", "Z")


def _round_up(dt: datetime, minutes: int = 15) -> datetime:
    """Round a datetime UP to the next multiple of `minutes`."""
    delta = minutes * 60
    epoch = int(dt.timestamp())
    rem = epoch % delta
    if rem == 0:
        return dt
    return dt + timedelta(seconds=(delta - rem))


def _fmt_time(dt: datetime) -> str:
    """'2:00 PM' style. strftime %-I isn't portable so we hand-format."""
    h24 = dt.hour
    h12 = h24 % 12 or 12
    ampm = "AM" if h24 < 12 else "PM"
    return f"{h12}:{dt.minute:02d} {ampm}"


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir is the logged-in owner's
# per-user data folder.
#
#   POST /api/owner/scheduler/find_slots
#       body:    { "duration_minutes": 30, "days_ahead": 7,
#                  "working_hours": [9, 17] }   # working_hours optional
#       calls:   find_open_slots(user_dir, duration_minutes, days_ahead,
#                                working_hours or (9,17))
#       returns: { "slots": [ {start_iso, end_iso, day_label, time_label}, ... ] }
#
#   POST /api/owner/scheduler/propose
#       body:    { "attendee_name": "...", "attendee_email": "...",
#                  "duration_minutes": 30, "days_ahead": 7 }
#       calls:   propose_meeting(user_dir, attendee_name, attendee_email,
#                                duration_minutes, days_ahead)
#       returns: { "open_slots": [...3...], "draft_email_text": "..." }
#
#   POST /api/owner/scheduler/book
#       body:    { "attendee_name": "...", "attendee_email": "...",
#                  "start_iso": "...", "end_iso": "...",
#                  "title": "...?", "notes": "...?" }
#       calls:   book_meeting(user_dir, attendee_name, attendee_email,
#                             start_iso, end_iso, title, notes)
#       returns: created event dict (with id, _gcal_pushed bool)
#
#   POST /api/owner/scheduler/parse_reply        (optional helper)
#       body:    { "text": "..." }
#       calls:   parse_reschedule_request(text)
#       returns: { "new_start_iso": "...", "original_event_id": "..." } or null
#
# ---------------------------------------------------------------------------
