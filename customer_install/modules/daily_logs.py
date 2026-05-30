"""
modules/daily_logs — contractor field daily logs.

Part of the Contractor module. Per Frank's blueprint Section 6 (Field
Operations) and Section 8 v2 upgrade list. Daily logs are GOLD in a
dispute — who was on site, what got done, weather, deliveries — and
the data input that powers the unsigned-work leak alarm.

A foreman dictates the day; Orby structures and files it. Each log
entry is anchored to a project + a date.

LogEntry shape:
  {
    "id":         "12-char hex",
    "project_id": "12-char hex",
    "date":       "2026-05-30",          # ISO date (local)
    "crew":       ["Mike", "Jose", "Tony"],
    "work_done":  "Framing complete on south wall. Started rough plumbing.",
    "hours":      8.0,
    "weather":    "Clear, 72°F, light wind",
    "deliveries": ["lumber - 2x4s from 84 Lumber", "rebar - 12 bundles"],
    "subs_on_site": ["Bob's Plumbing - 2 guys"],
    "materials_used": ["28 sheets of plywood", "5 gal of nails"],
    "delays":     "",                     # delay note if any
    "scope_changes_mentioned": ["client wants quartz upgrade"],  # FLAGGED for CO check
    "photos":     ["/data/.../oak-2026-05-30-1.jpg"],
    "notes":      "Free-form. Anything else worth recording.",
    "logged_by":  "frank",
    "created_at": 1780000000,
  }

The scope_changes_mentioned field is the input to the leak alarm:
when foreman mentions "client wanted X" or "added Y" or "extra Z" in
the log, those phrases get extracted into this list. After save, the
alarm scanner checks whether a signed CO exists covering those items;
if not, it flags the GC.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, date
from pathlib import Path

_LOCK = threading.Lock()
FILE = "daily_logs.json"

# Keywords that suggest a scope addition the client agreed to but might
# not have a signed CO covering. Foreman naturally drops these into the
# day's log; the alarm uses them to nudge.
_SCOPE_ADD_PATTERNS = [
    re.compile(r"client\s+(?:wanted|asked|requested|approved|wants)\s+(.{5,80})", re.IGNORECASE),
    re.compile(r"(?:extra|additional|added)\s+(.{5,80})", re.IGNORECASE),
    re.compile(r"out\s+of\s+scope[:\s]+(.{5,80})", re.IGNORECASE),
    re.compile(r"(?:upgrade|change[d]?)\s+(?:to\s+)?(.{5,80})", re.IGNORECASE),
    re.compile(r"(?:owner|homeowner)\s+(?:wanted|asked|approved)\s+(.{5,80})", re.IGNORECASE),
]


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


def _load(data_dir: Path) -> list[dict]:
    p = _path(data_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(data_dir: Path, logs: list[dict]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(logs, indent=2), encoding="utf-8")
    tmp.replace(p)


def extract_scope_changes(text: str) -> list[str]:
    """Pull anything-that-sounds-like-a-scope-change out of free text.
    Used at log-creation time to populate scope_changes_mentioned, which
    then drives the unsigned-work leak alarm."""
    out: list[str] = []
    seen: set[str] = set()
    for pat in _SCOPE_ADD_PATTERNS:
        for m in pat.finditer(text or ""):
            phrase = m.group(1).strip().rstrip(".,;!?")
            phrase_l = phrase.lower()
            if phrase_l not in seen and len(phrase_l) >= 5:
                seen.add(phrase_l)
                out.append(phrase)
    return out


def add(data_dir: Path, *,
        project_id: str,
        work_done: str = "",
        date_iso: str = "",
        crew: list | None = None,
        hours: float = 0.0,
        weather: str = "",
        deliveries: list | None = None,
        subs_on_site: list | None = None,
        materials_used: list | None = None,
        delays: str = "",
        photos: list | None = None,
        notes: str = "",
        logged_by: str = "") -> dict:
    """Add a daily log entry. project_id required; everything else
    optional. work_done text is scanned for scope-change keywords to
    populate scope_changes_mentioned automatically."""
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id required")
    date_iso = date_iso or datetime.now().strftime("%Y-%m-%d")
    full_text = " ".join(filter(None, [work_done, notes, delays]))
    scope_changes = extract_scope_changes(full_text)
    now = int(time.time())
    entry = {
        "id":                       uuid.uuid4().hex[:12],
        "project_id":               project_id,
        "date":                     date_iso,
        "crew":                     list(crew or []),
        "work_done":                (work_done or "").strip(),
        "hours":                    float(hours or 0),
        "weather":                  (weather or "").strip(),
        "deliveries":               list(deliveries or []),
        "subs_on_site":             list(subs_on_site or []),
        "materials_used":           list(materials_used or []),
        "delays":                   (delays or "").strip(),
        "scope_changes_mentioned":  scope_changes,
        "photos":                   list(photos or []),
        "notes":                    (notes or "").strip(),
        "logged_by":                (logged_by or "").strip(),
        "created_at":               now,
    }
    with _LOCK:
        logs = _load(data_dir)
        logs.append(entry)
        _save(data_dir, logs)
    return entry


def get(data_dir: Path, log_id: str) -> dict | None:
    for l in _load(data_dir):
        if l.get("id") == log_id:
            return l
    return None


def list_for_project(data_dir: Path, project_id: str,
                      limit: int = 100) -> list[dict]:
    logs = [l for l in _load(data_dir) if l.get("project_id") == project_id]
    logs.sort(key=lambda l: (l.get("date", ""), l.get("created_at", 0)), reverse=True)
    return logs[:limit]


def list_for_date(data_dir: Path, date_iso: str) -> list[dict]:
    return [l for l in _load(data_dir) if l.get("date") == date_iso]


def list_for_week(data_dir: Path, end_date: str = "") -> list[dict]:
    """Logs for the 7-day window ending on end_date (inclusive).
    Defaults to today."""
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    try:
        end = date.fromisoformat(end_date)
    except ValueError:
        return []
    from datetime import timedelta
    start = end - timedelta(days=6)
    out = []
    for l in _load(data_dir):
        try:
            d = date.fromisoformat(l.get("date", ""))
        except ValueError:
            continue
        if start <= d <= end:
            out.append(l)
    out.sort(key=lambda l: (l.get("date", ""), l.get("created_at", 0)))
    return out


def find_unmatched_scope_changes(data_dir: Path,
                                   recent_days: int = 14) -> list[dict]:
    """The leak alarm. Walks recent daily logs, pulls every
    scope_changes_mentioned entry, and checks whether a signed CO on
    that same project mentions matching keywords. Returns the list of
    UNMATCHED scope-change mentions (the things the foreman flagged
    but no signed CO covers).

    Returns: [{log_id, project_id, date, phrase, ...}, ...]

    Heuristic match: if the scope_change phrase shares a meaningful
    word (>=4 chars, not stopword) with any signed CO description on
    that project, count it as matched.
    """
    from datetime import timedelta
    cutoff = (datetime.now().date() - timedelta(days=recent_days)).isoformat()
    logs = [l for l in _load(data_dir) if l.get("date", "") >= cutoff]
    # Lazy import to avoid circular at module-load time
    from modules import change_orders as _co_mod
    cos = _co_mod._load(data_dir)
    out = []
    stopwords = {"the", "and", "for", "with", "from", "that", "this",
                  "have", "want", "wants", "wanted", "asked", "client",
                  "homeowner", "extra", "additional", "added", "change"}
    for log in logs:
        for phrase in log.get("scope_changes_mentioned", []):
            phrase_words = {w.lower().strip(".,;:") for w in phrase.split()
                             if len(w) >= 4 and w.lower() not in stopwords}
            if not phrase_words:
                continue
            matched = False
            for c in cos:
                if c.get("project_id") != log.get("project_id"):
                    continue
                if c.get("status") not in ("signed", "approved", "sent_for_signature"):
                    continue
                co_text = (c.get("description", "") + " " + c.get("scope_detail", "")).lower()
                co_words = {w.lower().strip(".,;:") for w in co_text.split()
                              if len(w) >= 4}
                if phrase_words & co_words:
                    matched = True
                    break
            if not matched:
                out.append({
                    "log_id":     log["id"],
                    "project_id": log["project_id"],
                    "date":       log["date"],
                    "phrase":     phrase,
                    "log_excerpt": (log.get("work_done") or log.get("notes") or "")[:200],
                })
    return out
