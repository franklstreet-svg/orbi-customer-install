"""
contextual_reminders — extract promises from conversation text and turn
them into reminders automatically.

When the owner replies "I'll call you Tuesday" or "I'll send the quote
Friday", Orbi catches that, computes the due date, and drops a reminder
into modules/reminders.json so the promise actually gets kept.

DESIGN NOTES
------------
* Two-stage extraction: regex fast-path first (cheap, deterministic,
  handles the common phrasings), then LLM fallback ONLY when the text
  smells "promise-y" but regex caught nothing. This keeps the local-only
  promise out of the cold path — most chats are handled with zero LLM
  cost.

* Vague phrases like "I'll call you sometime soon" or "I'll get back to
  you" are intentionally NOT created as reminders without a due time —
  we'd rather miss a vague promise than spam the owner with hourly
  guesses. The LLM step can produce one with a suggested_due_iso of
  "" (empty) and the auto_create step will skip empty-due entries.

* Suggested due times default to 9 AM in the user's local-ish window
  (we use UTC here because the rest of the codebase does — the owner's
  TZ adjustment happens at display time). The matcher computes a date
  relative to "now" so "Tuesday" always means *next* Tuesday if today
  is past that day, never today.

* Source context goes in meta.source_context so the dashboard can show
  "Reminder created from chat with Joe Smith on Tuesday" — the owner
  sees WHY this reminder exists.

* scan_recent_chat_for_promises only looks at the OWNER's replies
  (role=assistant in the chat history shape used by the owner-chat).
  Customer-side promises ("the customer said they'd call back") aren't
  meaningful reminders for the owner — Orbi shouldn't book the
  customer's calendar.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("orbi.contextual_reminders")


# ── Regex patterns ─────────────────────────────────────────────────────

# Verbs/phrasings that signal a promise.
_PROMISE_VERBS = (
    r"call\s+(?:you|them|him|her|back)?",
    r"text\s+(?:you|them|him|her|back)?",
    r"email\s+(?:you|them|him|her|back)?",
    r"send\s+(?:you|them|him|her|over\s+)?(?:the\s+)?\w*",
    r"get\s+back\s+to\s+(?:you|them|him|her)",
    r"follow\s+up(?:\s+with\s+(?:you|them|him|her))?",
    r"check\s+(?:in|back)(?:\s+with\s+(?:you|them|him|her))?",
    r"drop\s+(?:you|them)\s+a\s+\w+",
    r"reach\s+out",
)
_VERB_PATTERN = "|".join(_PROMISE_VERBS)

# Time anchors. Order matters — match longest first so "next tuesday"
# beats "tuesday".
_DUE_PATTERNS = [
    # next/this <weekday>
    r"(?:next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
    # by/on <weekday>
    r"(?:by|on)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
    # bare weekday after a verb (covered separately, captured)
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    # next week / this week
    r"\bnext\s+week\b",
    r"\bthis\s+week\b",
    # tomorrow / today / tonight
    r"\btomorrow\s+morning\b",
    r"\btomorrow\s+afternoon\b",
    r"\btomorrow\s+evening\b",
    r"\btomorrow\b",
    r"\btonight\b",
    r"\btoday\b",
    # in N days / hours / weeks
    r"\bin\s+(\d+)\s+(day|days|hour|hours|week|weeks)\b",
    # at <time>
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    # by end of day/week
    r"\bby\s+end\s+of\s+(day|week)\b",
    # first thing tomorrow / first thing monday etc.
    r"\bfirst\s+thing\s+(tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
]

# Full promise sentence: "I'll <verb> <stuff> <due-phrase>".
# We capture the whole sentence (up to next . ! ? or newline).
_PROMISE_SENTENCE_RE = re.compile(
    rf"((?:i(?:'|’)?ll|i\s+will|let\s+me|we(?:'|’)?ll|we\s+will)"
    rf"\s+(?:{_VERB_PATTERN})"
    rf"[^.!?\n]{{0,200}}?"
    rf"(?:{'|'.join(_DUE_PATTERNS)})"
    rf"[^.!?\n]{{0,80}})"
    rf"(?:[.!?\n]|$)",
    re.IGNORECASE,
)

# "Remind me to X (by/on) Y"
_REMIND_RE = re.compile(
    rf"(remind\s+me\s+to\s+[^.!?\n]{{1,200}}?"
    rf"(?:{'|'.join(_DUE_PATTERNS)})"
    rf"[^.!?\n]{{0,80}})"
    rf"(?:[.!?\n]|$)",
    re.IGNORECASE,
)

# Looks-promise-y but might lack a due clause — used to decide if the
# LLM fallback should fire when regex caught nothing.
_PROMISEY_RE = re.compile(
    rf"(?:i(?:'|’)?ll|i\s+will|let\s+me|we(?:'|’)?ll)"
    rf"\s+(?:{_VERB_PATTERN})",
    re.IGNORECASE,
)


# ── Public API ──────────────────────────────────────────────────────────


def extract_promises(text: str) -> list[dict]:
    """Find promises in `text`. Returns a list of:
        {promise_text, due_phrase, suggested_due_iso}

    Empty suggested_due_iso means "promise detected but no concrete time"
    — caller should NOT auto-create (would just be a vague nag).
    """
    if not text or not str(text).strip():
        return []
    src = str(text)
    seen_spans: list[tuple[int, int]] = []
    out: list[dict] = []

    # 1. Promise sentences ("I'll call you Tuesday").
    for m in _PROMISE_SENTENCE_RE.finditer(src):
        span = m.span(1)
        if _overlaps(span, seen_spans):
            continue
        seen_spans.append(span)
        sentence = m.group(1).strip()
        due_phrase, due_iso = _find_due(sentence)
        out.append({
            "promise_text":       sentence,
            "due_phrase":         due_phrase,
            "suggested_due_iso":  due_iso,
        })

    # 2. "Remind me to..."
    for m in _REMIND_RE.finditer(src):
        span = m.span(1)
        if _overlaps(span, seen_spans):
            continue
        seen_spans.append(span)
        sentence = m.group(1).strip()
        due_phrase, due_iso = _find_due(sentence)
        out.append({
            "promise_text":       sentence,
            "due_phrase":         due_phrase,
            "suggested_due_iso":  due_iso,
        })

    # 3. LLM fallback — only if regex caught nothing AND text smells
    # promise-y. We never call the LLM "just to be sure" because the
    # regex captures the common cases at zero cost.
    if not out and _PROMISEY_RE.search(src):
        llm_hits = _llm_extract(src)
        out.extend(llm_hits)

    return out


def auto_create_reminders(user_dir: Path, source_context: str,
                          source_id: str = "") -> list[dict]:
    """Extract promises from `source_context` and create reminders.

    Returns the list of created reminder dicts. Promises without a
    concrete due time are silently skipped — we'd rather miss a vague
    promise than create useless "remind me sometime" entries.
    """
    promises = extract_promises(source_context)
    if not promises:
        return []

    try:
        from modules import reminders as mod_reminders
    except Exception as e:
        log.warning("auto_create_reminders: reminders module import failed: %s", e)
        return []

    created: list[dict] = []
    for p in promises:
        due = (p.get("suggested_due_iso") or "").strip()
        if not due:
            # Vague promise — skip without nagging the owner.
            log.info("auto_create_reminders: skipping vague promise: %r",
                     p.get("promise_text", "")[:80])
            continue
        text = (p.get("promise_text") or "").strip()
        if not text:
            continue
        try:
            rem = mod_reminders.add(user_dir, text, due)
        except Exception as e:
            log.warning("auto_create_reminders: reminders.add failed: %s", e)
            continue

        # Attach source context as meta. mod_reminders.add doesn't
        # support meta directly so we write it post-hoc by re-reading
        # and patching the file — small surface, no public API needed.
        try:
            _attach_meta(user_dir, rem.get("id", ""), {
                "source_context": source_context[:500],
                "source_id":      source_id,
                "due_phrase":     p.get("due_phrase", ""),
            })
        except Exception as e:
            log.info("auto_create_reminders: meta attach failed: %s", e)
        created.append(rem)
    return created


def scan_recent_chat_for_promises(data_dir: Path, user_dir: Path,
                                  chat_history: list[dict]) -> list[dict]:
    """Scan the owner's recent replies in `chat_history` and auto-create
    reminders for any promises found.

    `chat_history` is a list of {role, content, ts?} dicts in chat order.
    Only role=="assistant" entries are scanned (those are the OWNER's
    replies in the owner-chat shape — Orbi replied on the owner's behalf
    or as the owner).

    Returns the list of created reminder dicts.
    """
    if not chat_history:
        return []
    # Look at the last ~5 assistant turns — older promises were likely
    # already processed.
    assistant_turns = [m for m in chat_history
                       if (m.get("role") or "").lower() == "assistant"][-5:]
    if not assistant_turns:
        return []

    created: list[dict] = []
    for turn in assistant_turns:
        content = turn.get("content") or ""
        if not content.strip():
            continue
        source_id = turn.get("id", "") or turn.get("ts", "") or ""
        context_header = "from chat reply"
        # If we can identify the customer the chat is with, include it.
        customer_name = _find_customer_in_history(chat_history)
        if customer_name:
            context_header = f"from chat with {customer_name}"
        ctx = f"{context_header}: {content}"
        new_reminders = auto_create_reminders(user_dir, ctx, str(source_id))
        created.extend(new_reminders)
    return created


# ── Due-phrase resolution ──────────────────────────────────────────────


_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday",
                  "friday", "saturday", "sunday")


def _find_due(sentence: str) -> tuple[str, str]:
    """Walk the due-phrase patterns in priority order, return the first
    matched (phrase, iso). Returns ("", "") if nothing concrete found."""
    sl = sentence.lower()
    now = _now()

    # next/this <weekday>
    m = re.search(r"(?:next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", sl)
    if m:
        wd = m.group(1)
        is_next = "next" in m.group(0)
        return m.group(0), _weekday_iso(now, wd, force_next_week=is_next)

    # by/on <weekday>
    m = re.search(r"(?:by|on)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", sl)
    if m:
        return m.group(0), _weekday_iso(now, m.group(1))

    # first thing <weekday>/tomorrow
    m = re.search(r"first\s+thing\s+(tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)", sl)
    if m:
        when = m.group(1)
        if when == "tomorrow":
            return m.group(0), _shift_iso(now + timedelta(days=1), hour=8)
        return m.group(0), _weekday_iso(now, when, hour=8)

    # tomorrow morning/afternoon/evening
    for tag, hr in (("tomorrow morning", 9), ("tomorrow afternoon", 14),
                    ("tomorrow evening", 18), ("tomorrow", 9)):
        if tag in sl:
            return tag, _shift_iso(now + timedelta(days=1), hour=hr)

    if "tonight" in sl:
        return "tonight", _shift_iso(now, hour=19)
    if "today" in sl:
        return "today", _shift_iso(now, hour=now.hour + 1 if now.hour < 17 else 17)

    # in N days/hours/weeks
    m = re.search(r"in\s+(\d+)\s+(day|days|hour|hours|week|weeks)", sl)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("hour"):
            return m.group(0), _shift_iso(now + timedelta(hours=n))
        if unit.startswith("week"):
            return m.group(0), _shift_iso(now + timedelta(weeks=n), hour=9)
        return m.group(0), _shift_iso(now + timedelta(days=n), hour=9)

    # by end of day/week
    m = re.search(r"by\s+end\s+of\s+(day|week)", sl)
    if m:
        if m.group(1) == "day":
            return m.group(0), _shift_iso(now, hour=17)
        # End of week = Friday 5pm.
        return m.group(0), _weekday_iso(now, "friday", hour=17)

    # next week
    if "next week" in sl:
        # Default to next Monday 9 AM.
        return "next week", _weekday_iso(now, "monday", force_next_week=True)

    if "this week" in sl:
        # Default to Friday this week if not yet Friday, else next Monday.
        return "this week", _weekday_iso(now, "friday")

    # at <time>
    m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", sl)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        # Time-of-day with no date = today (or tomorrow if already past).
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return m.group(0), target.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Bare weekday (must come last so "next tuesday" wins).
    m = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", sl)
    if m:
        return m.group(0), _weekday_iso(now, m.group(1))

    return "", ""


def _weekday_iso(now: datetime, weekday: str, hour: int = 9,
                 force_next_week: bool = False) -> str:
    """Next occurrence of `weekday` after `now`. If today IS that
    weekday, return today + 7 days (we never schedule a same-day
    reminder for a bare weekday — too ambiguous)."""
    weekday = weekday.lower()
    if weekday not in _WEEKDAY_NAMES:
        return ""
    target_idx = _WEEKDAY_NAMES.index(weekday)
    today_idx = now.weekday()  # Monday = 0
    days_ahead = (target_idx - today_idx) % 7
    if days_ahead == 0 or force_next_week:
        days_ahead = 7 if force_next_week else 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    return target.strftime("%Y-%m-%dT%H:%M:%SZ")


def _shift_iso(dt: datetime, hour: int | None = None) -> str:
    if hour is not None:
        hour = max(0, min(int(hour), 23))
        dt = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── LLM fallback ───────────────────────────────────────────────────────


def _llm_extract(text: str) -> list[dict]:
    """Ask the local LLM to extract promises when regex missed. Lazy
    import — and if the LLM is unavailable we just return []."""
    try:
        import llm_client
    except Exception as e:
        log.info("llm fallback unavailable: %s", e)
        return []

    system = (
        "You extract PROMISES from short text — sentences where the speaker "
        "commits to doing something by a specific time. Only return promises "
        "with a concrete time anchor (a weekday, 'tomorrow', 'tonight', "
        "'next week', a specific time, etc.). SKIP vague phrases like "
        "'sometime soon', 'eventually', 'when I get a chance'. "
        "Reply with one promise per line in EXACTLY this format:\n"
        "PROMISE | DUE_PHRASE\n"
        "If no concrete promises, reply NONE."
    )
    user = f"TEXT:\n{text}\n\nList the concrete promises:"

    try:
        resp = llm_client.generate({}, system, [{"role": "user", "content": user}])
    except Exception as e:
        log.info("llm extract failed: %s", e)
        return []

    body = (getattr(resp, "text", "") or "").strip()
    if not body or body.upper().startswith("NONE"):
        return []

    now = _now()
    out: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        promise, _, due_phrase = line.partition("|")
        promise = promise.strip().lstrip("-* ").strip()
        due_phrase = due_phrase.strip()
        if not promise:
            continue
        # Use our own date math on the LLM's due_phrase — don't trust
        # the LLM to compute ISO dates.
        _, due_iso = _find_due(due_phrase or promise)
        out.append({
            "promise_text":       promise,
            "due_phrase":         due_phrase,
            "suggested_due_iso":  due_iso,
        })
    # Drop LLM-emitted promises with no concrete time.
    return [p for p in out if p["suggested_due_iso"]]


# ── Reminder meta attachment ───────────────────────────────────────────


def _attach_meta(user_dir: Path, reminder_id: str, meta: dict) -> None:
    """Patch a reminder record to add a meta dict. Done out-of-band
    because modules.reminders.add doesn't accept meta directly."""
    if not reminder_id:
        return
    import json
    p = Path(user_dir) / "reminders.json"
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, list):
        return
    changed = False
    for r in data:
        if r.get("id") == reminder_id:
            existing = r.get("meta") or {}
            if not isinstance(existing, dict):
                existing = {}
            existing.update(meta)
            r["meta"] = existing
            changed = True
            break
    if not changed:
        return
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)


# ── Helpers ────────────────────────────────────────────────────────────


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    a0, a1 = span
    for b0, b1 in spans:
        if a0 < b1 and b0 < a1:
            return True
    return False


def _find_customer_in_history(history: list[dict]) -> str:
    """Best-effort: look for a customer name in user-role messages. Used
    to make the source_context label more informative ('from chat with
    Joe Smith'). Returns "" if nothing obvious."""
    for m in history:
        if (m.get("role") or "").lower() != "user":
            continue
        # Some chat shapes carry the customer name in meta or speaker.
        for key in ("from_name", "speaker", "customer_name", "name"):
            v = m.get(key)
            if v and isinstance(v, str) and v.strip():
                return v.strip()
    return ""


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder.
#
#   POST /api/owner/promises/extract
#       body:   {"text": "...conversation text..."}
#       calls:  extract_promises(text)
#       returns:{"promises": [{promise_text, due_phrase,
#                              suggested_due_iso}, ...]}
#       errors: 400 if text missing
#
#   POST /api/owner/promises/auto_create
#       body:   {"text": "...", "source_id": "optional"}
#       calls:  auto_create_reminders(user_dir, text, source_id)
#       returns:{"created": [{id, text, due, status, ...}, ...]}
#       errors: 400 if text missing
#
# ---------------------------------------------------------------------------
