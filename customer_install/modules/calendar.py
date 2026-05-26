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


def list_all(user_dir: Path) -> list[dict]:
    with _LOCK:
        return _load(user_dir)


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
