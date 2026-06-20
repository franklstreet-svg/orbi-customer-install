"""
schedule_patterns — Orbi learns the owner's day-shape.

Watches calendar entries + recurring blocks over time and forms a
soft model of when the owner usually does what. "You usually take lunch
at 12:30." "Your weekly review is Friday afternoon." "Your gym block
is Mon/Wed/Fri 6-7am." When the owner asks Orbi to schedule something
new, she protects these usual times by default; when an unfamiliar
visitor wants to book, she avoids them.

This is not a hard calendar — it's a HEURISTIC the rest of the system
consults. The actual calendar entries (in modules.calendar) remain the
source of truth.

Storage:   data/users/<username>/schedule_patterns.json
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("orbi.modules.schedule_patterns")

_FILENAME = "schedule_patterns.json"
_LOCK = threading.Lock()

# A pattern is "established" once we've seen the same kind of block at
# roughly the same time-of-day on the same weekday this many times.
_MIN_OCCURRENCES = 3
_TIME_TOLERANCE_MIN = 45     # ±45 min from the median to count as "same time"


def _path(user_dir: Path) -> Path:
    return user_dir / _FILENAME


def _load(user_dir: Path) -> dict:
    p = _path(user_dir)
    if not p.exists():
        return {"events": [], "patterns": []}
    with _LOCK:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"events": [], "patterns": []}


def _save(user_dir: Path, data: dict) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        tmp.replace(p)


def record_event(user_dir: Path, *,
                  kind: str,
                  start: datetime,
                  end: datetime | None = None) -> None:
    """Log one calendar occurrence — usually called from a calendar
    after-create hook. `kind` is a short label ('lunch', 'gym',
    'weekly_review', 'one_on_one'). End defaults to start + 30 min."""
    end = end or (start + timedelta(minutes=30))
    data = _load(user_dir)
    events = data.get("events") or []
    events.append({
        "kind":     kind.strip().lower(),
        "start_ts": int(start.timestamp()),
        "minute":   start.hour * 60 + start.minute,
        "weekday":  start.weekday(),
        "duration": int((end - start).total_seconds() / 60),
    })
    if len(events) > 1000:
        events = events[-1000:]
    data["events"] = events
    data["patterns"] = _derive_patterns(events)
    _save(user_dir, data)


def _derive_patterns(events: list[dict]) -> list[dict]:
    """From the raw event log, fold occurrences into stable patterns.
    A pattern is a (kind, weekday, ±tolerance time-of-day) cluster
    with at least _MIN_OCCURRENCES hits in the last 90 days."""
    cutoff = int(time.time()) - 90 * 86400
    by_kind_weekday: dict[tuple[str, int], list[dict]] = {}
    for e in events:
        if e.get("start_ts", 0) < cutoff:
            continue
        key = (e.get("kind", ""), int(e.get("weekday", -1)))
        by_kind_weekday.setdefault(key, []).append(e)

    patterns = []
    for (kind, weekday), evs in by_kind_weekday.items():
        if len(evs) < _MIN_OCCURRENCES:
            continue
        minutes = sorted(e["minute"] for e in evs)
        median = minutes[len(minutes) // 2]
        in_window = [m for m in minutes
                      if abs(m - median) <= _TIME_TOLERANCE_MIN]
        if len(in_window) < _MIN_OCCURRENCES:
            continue
        durations = [e.get("duration", 30) for e in evs]
        median_dur = sorted(durations)[len(durations) // 2]
        patterns.append({
            "kind":           kind,
            "weekday":        weekday,
            "usual_minute":   median,
            "usual_duration": median_dur,
            "n_observations": len(in_window),
            "confidence":     min(1.0, len(in_window) / 10.0),
        })
    return patterns


def is_blocked(user_dir: Path, *,
                when: datetime,
                duration_minutes: int = 30) -> dict | None:
    """Does the proposed time clash with an established pattern? Returns
    the matching pattern dict or None. Used by booking + scheduling
    modules to avoid stomping on the owner's usual blocks."""
    proposed_minute = when.hour * 60 + when.minute
    weekday = when.weekday()
    data = _load(user_dir)
    for p in data.get("patterns") or []:
        if int(p.get("weekday", -1)) != weekday:
            continue
        usual_minute = int(p.get("usual_minute", -1))
        if usual_minute < 0:
            continue
        # The proposed block clashes if it overlaps the usual one.
        proposed_end = proposed_minute + duration_minutes
        usual_end = usual_minute + int(p.get("usual_duration", 30))
        if not (proposed_end <= usual_minute - _TIME_TOLERANCE_MIN
                or proposed_minute >= usual_end + _TIME_TOLERANCE_MIN):
            return p
    return None


def list_patterns(user_dir: Path) -> list[dict]:
    return list((_load(user_dir).get("patterns") or []))


def forget(user_dir: Path, kind: str, weekday: int) -> bool:
    """Remove a learned pattern at the owner's request — 'no, I'm not
    going to gym at 6am on Mondays anymore.'"""
    data = _load(user_dir)
    before = len(data.get("patterns") or [])
    data["patterns"] = [p for p in (data.get("patterns") or [])
                         if not (p.get("kind") == kind.strip().lower()
                                  and int(p.get("weekday", -1)) == weekday)]
    if len(data["patterns"]) == before:
        return False
    _save(user_dir, data)
    return True
