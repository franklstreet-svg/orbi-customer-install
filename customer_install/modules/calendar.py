"""
calendar module (per-user) — appointments and time-blocked events.

Each user has their own calendar.json under data/users/<username>/.
Visitors see only "next availability" via the public booking helper;
the owner sees everything, staff see their own.

Event shape:
  {
    "id":      "12-char hex",
    "title":   "Dentist - Dr. Smith",
    "start":   "2026-05-28T14:00:00Z",
    "end":     "2026-05-28T15:00:00Z",
    "all_day": false,
    "notes":   "Bring insurance card",
    "with":    ["Dr. Smith"],     # optional people
    "location":"123 Main St",     # optional
    "ts":      <unix when created>
  }
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOCK = threading.Lock()


def _path(user_dir: Path) -> Path:
    return user_dir / "calendar.json"


def _load(user_dir: Path) -> list[dict]:
    p = _path(user_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_dir: Path, events: list[dict]) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _dedup_event_key(e: dict) -> tuple[str, str]:
    """Normalized title + same 30-min bucket of start = same event.
    Collapses pre-existing duplicate events on read so old dirty data
    (from before the write-side dedup landed in quick_capture) doesn't
    surface as multiple lines in chat."""
    # Strip trailing time/day debris that old write paths left in titles
    # ("with Joe Friday 10am" → "with joe")
    import re as _re
    raw = (e.get("title") or "").lower().strip()
    raw = _re.sub(r"\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*$", "", raw)
    raw = _re.sub(
        r"\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*$",
        "", raw,
    )
    title = " ".join(raw.split())
    start = (e.get("start") or "")[:16]
    try:
        mm = int(start[14:16])
        bucket = (mm // 30) * 30
        start = f"{start[:14]}{bucket:02d}"
    except (ValueError, IndexError):
        pass
    return (title, start)


def _clean_title_on_read(t: str) -> str:
    """Strip trailing weekday word + trailing clock-time debris from
    titles that older write paths left dirty (e.g. "with Joe Friday 10am"
    → "with Joe"). Read-only — leaves the stored field untouched so
    nothing downstream that expects the original title breaks."""
    import re as _re
    out = (t or "").strip()
    out = _re.sub(r"\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*$", "", out, flags=_re.IGNORECASE)
    out = _re.sub(
        r"\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*$",
        "", out, flags=_re.IGNORECASE,
    )
    return out.strip()


def list_all(user_dir: Path) -> list[dict]:
    with _LOCK:
        events = _load(user_dir)
    seen: set = set()
    deduped: list = []
    for e in sorted(events, key=lambda x: x.get("ts", 0)):
        k = _dedup_event_key(e)
        if k in seen:
            continue
        seen.add(k)
        # Display-clean the title without mutating storage
        copy = dict(e)
        copy["title"] = _clean_title_on_read(copy.get("title", ""))
        deduped.append(copy)
    return deduped


def add(user_dir: Path, title: str, start: str, end: str | None = None,
        all_day: bool = False, notes: str = "",
        with_: list[str] | None = None, location: str = "") -> dict:
    """start/end are ISO-8601 strings ("2026-05-28T14:00:00Z" or "2026-05-28")."""
    event = {
        "id":       uuid.uuid4().hex[:12],
        "title":    (title or "").strip(),
        "start":    start,
        "end":      end or start,
        "all_day":  all_day,
        "notes":    notes.strip(),
        "with":     with_ or [],
        "location": location.strip(),
        "ts":       time.time(),
    }
    with _LOCK:
        events = _load(user_dir)
        events.append(event)
        _save(user_dir, events)
    return event


def remove(user_dir: Path, event_id: str) -> bool:
    with _LOCK:
        events = _load(user_dir)
        before = len(events)
        events = [e for e in events if e.get("id") != event_id]
        _save(user_dir, events)
        return len(events) < before


def update(user_dir: Path, event_id: str, **changes) -> dict | None:
    with _LOCK:
        events = _load(user_dir)
        for e in events:
            if e.get("id") == event_id:
                for k, v in changes.items():
                    if k in ("id", "ts"):
                        continue
                    e[k] = v
                _save(user_dir, events)
                return e
    return None


def today(user_dir: Path) -> list[dict]:
    """Events whose start is today (UTC). Sorted chronologically."""
    return _range(user_dir, _now().date(), _now().date())


def upcoming(user_dir: Path, days: int = 7) -> list[dict]:
    return _range(user_dir, _now().date(), (_now() + timedelta(days=days)).date())


def _range(user_dir: Path, start_date, end_date) -> list[dict]:
    events = list_all(user_dir)
    out = []
    for e in events:
        start_str = e.get("start", "")
        try:
            d = datetime.strptime(start_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if start_date <= d <= end_date:
            out.append(e)
    return sorted(out, key=lambda x: x.get("start", ""))


def context_block(user_dir: Path, max_events: int = 8) -> str:
    """Format upcoming events as an LLM system-prompt addendum."""
    events = upcoming(user_dir, days=7)[:max_events]
    if not events:
        return ""
    lines = ["YOUR CALENDAR — next 7 days:"]
    for e in events:
        when = e.get("start", "")[:16].replace("T", " ")
        title = e.get("title", "")
        loc = f" @ {e['location']}" if e.get("location") else ""
        lines.append(f"  - {when}  {title}{loc}")
    return "\n".join(lines)


def _now() -> datetime:
    return datetime.now(timezone.utc)
