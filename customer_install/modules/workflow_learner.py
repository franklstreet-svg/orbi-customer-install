"""
workflow_learner — Orbi notices when the owner does the same thing on a
schedule (every Friday at 5pm she runs the weekly report; every month-end
she sends the same invoice reminder) and offers to automate it.

Same shape as web_agent.learner: observe, store, recall when relevant.
Each per-user workflow record captures the action signature + the
recurrence pattern. After the third occurrence inside the expected
window, Orbi surfaces a "want me to do this automatically next time?"
suggestion in the dashboard.

Storage:   data/users/<username>/workflows.json
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("orbi.modules.workflow_learner")

_FILENAME = "workflows.json"
_LOCK = threading.Lock()
_MIN_OBSERVATIONS_TO_SUGGEST = 3
_SAME_WINDOW_MINUTES = 90    # observations within 90 min of the same time-of-day count


def _path(user_dir: Path) -> Path:
    return user_dir / _FILENAME


def _load(user_dir: Path) -> dict:
    p = _path(user_dir)
    if not p.exists():
        return {"workflows": []}
    with _LOCK:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"workflows": []}


def _save(user_dir: Path, data: dict) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        tmp.replace(p)


def _signature(action: str) -> str:
    """Normalize an action description into a stable fingerprint so
    'draft weekly report for Joe' and 'draft this week's report for joe'
    collapse onto the same workflow."""
    norm = re.sub(r"[^\w\s]", " ", action.lower())
    tokens = sorted({t for t in norm.split() if len(t) > 2})
    return "|".join(tokens)


def record(user_dir: Path, action_description: str,
            now: datetime | None = None) -> dict:
    """Log one occurrence of an action. Returns the workflow record
    (new or updated) so the caller can decide whether to ask the owner
    'want me to schedule this?'"""
    now = now or datetime.now()
    sig = _signature(action_description)
    if not sig:
        return {}
    data = _load(user_dir)
    workflows = data.get("workflows") or []
    rec = next((w for w in workflows if w.get("signature") == sig), None)
    obs = {"ts": int(now.timestamp()),
            "minute_of_day": now.hour * 60 + now.minute,
            "weekday":       now.weekday()}
    if rec is None:
        rec = {
            "id":              f"wf_{int(time.time())}_{secrets.token_hex(3)}",
            "signature":       sig,
            "first_seen":      action_description[:200],
            "observations":    [obs],
            "automated":       False,
        }
        workflows.append(rec)
    else:
        observations = rec.get("observations") or []
        observations.append(obs)
        if len(observations) > 30:
            observations = observations[-30:]
        rec["observations"] = observations
    data["workflows"] = workflows
    _save(user_dir, data)
    return rec


def suggestion_for(user_dir: Path, action_description: str) -> dict | None:
    """Should we offer to automate this? Returns a suggestion dict if
    the same signature has been observed N times within a stable
    time-of-day window. None otherwise."""
    sig = _signature(action_description)
    if not sig:
        return None
    data = _load(user_dir)
    rec = next((w for w in data.get("workflows") or []
                 if w.get("signature") == sig), None)
    if not rec or rec.get("automated"):
        return None
    obs = rec.get("observations") or []
    if len(obs) < _MIN_OBSERVATIONS_TO_SUGGEST:
        return None
    # Are they clustering around the same minute-of-day?
    minutes = [o["minute_of_day"] for o in obs]
    median = sorted(minutes)[len(minutes) // 2]
    near = sum(1 for m in minutes
                if abs(m - median) <= _SAME_WINDOW_MINUTES)
    if near < _MIN_OBSERVATIONS_TO_SUGGEST:
        return None
    weekdays = [o["weekday"] for o in obs]
    common_weekday = max(set(weekdays), key=weekdays.count)
    return {
        "id":              rec["id"],
        "what":            rec["first_seen"],
        "usual_time":      f"{median // 60:02d}:{median % 60:02d}",
        "usual_weekday":   common_weekday,
        "observation_n":   len(obs),
    }


def mark_automated(user_dir: Path, workflow_id: str) -> bool:
    """The owner accepted the automation. Stop suggesting it."""
    data = _load(user_dir)
    for w in data.get("workflows") or []:
        if w.get("id") == workflow_id:
            w["automated"] = True
            w["automated_at"] = int(time.time())
            _save(user_dir, data)
            return True
    return False


def list_all(user_dir: Path) -> list[dict]:
    return list((_load(user_dir).get("workflows") or []))


def forget(user_dir: Path, workflow_id: str) -> bool:
    data = _load(user_dir)
    before = len(data.get("workflows") or [])
    data["workflows"] = [w for w in (data.get("workflows") or [])
                          if w.get("id") != workflow_id]
    if len(data["workflows"]) == before:
        return False
    _save(user_dir, data)
    return True
