"""
reminders module (per-user) — one-shot timed reminders.

"Remind me Friday to call the supplier." Stored as reminders.json under
data/users/<username>/. A background worker (or the watchdog tick)
checks for due reminders and fires the notification.

Reminder shape:
  {
    "id":       "12-char hex",
    "text":     "call the supplier",
    "due":      "2026-05-30T09:00:00Z",
    "status":   "pending" | "fired" | "snoozed" | "done",
    "fired_at": "2026-05-30T09:00:01Z" | None,
    "channel":  "sms" | "email" | "in_app",
    "ts":       <unix when created>
  }
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


def _path(user_dir: Path) -> Path:
    return user_dir / "reminders.json"


def _load(user_dir: Path) -> list[dict]:
    p = _path(user_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_dir: Path, reminders: list[dict]) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reminders, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def list_all(user_dir: Path, include_done: bool = False) -> list[dict]:
    with _LOCK:
        rem = _load(user_dir)
    if not include_done:
        rem = [r for r in rem if r.get("status") not in ("done", "fired")]
    return sorted(rem, key=lambda r: r.get("due", ""))


def add(user_dir: Path, text: str, due: str, channel: str = "in_app") -> dict:
    """due is ISO-8601 ("2026-05-30T09:00:00Z"). channel = sms / email / in_app."""
    reminder = {
        "id":      uuid.uuid4().hex[:12],
        "text":    (text or "").strip(),
        "due":     due,
        "status":  "pending",
        "fired_at": None,
        "channel": channel,
        "ts":      time.time(),
    }
    with _LOCK:
        rem = _load(user_dir)
        rem.append(reminder)
        _save(user_dir, rem)
    return reminder


def mark_done(user_dir: Path, reminder_id: str) -> bool:
    return _set_status(user_dir, reminder_id, "done")


def snooze(user_dir: Path, reminder_id: str, new_due: str) -> bool:
    with _LOCK:
        rem = _load(user_dir)
        for r in rem:
            if r.get("id") == reminder_id:
                r["due"] = new_due
                r["status"] = "snoozed"
                _save(user_dir, rem)
                return True
    return False


def due_now(user_dir: Path) -> list[dict]:
    """Pending reminders whose due timestamp is ≤ now. Used by the firing worker."""
    now = datetime.now(timezone.utc)
    out = []
    for r in list_all(user_dir):
        if r.get("status") != "pending":
            continue
        try:
            due_dt = datetime.strptime(r.get("due", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if due_dt <= now:
            out.append(r)
    return out


def mark_fired(user_dir: Path, reminder_id: str) -> bool:
    return _set_status(user_dir, reminder_id, "fired",
                       extra={"fired_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})


def _set_status(user_dir: Path, reminder_id: str, status: str, extra: dict | None = None) -> bool:
    with _LOCK:
        rem = _load(user_dir)
        for r in rem:
            if r.get("id") == reminder_id:
                r["status"] = status
                if extra:
                    r.update(extra)
                _save(user_dir, rem)
                return True
    return False


def context_block(user_dir: Path, max_items: int = 6) -> str:
    items = list_all(user_dir)[:max_items]
    if not items:
        return ""
    lines = ["YOUR PENDING REMINDERS:"]
    for r in items:
        when = r.get("due", "")[:16].replace("T", " ")
        lines.append(f"  - {when}  {r.get('text','')}")
    return "\n".join(lines)
