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


def _dedup_key(r: dict) -> tuple[str, str]:
    """Two reminders are "same enough" if they share normalized text AND
    a due timestamp within the same 15-minute bucket. Collapses the
    8-copies-of-the-same-reminder problem without merging genuinely
    distinct alerts scheduled close together."""
    text = " ".join((r.get("text") or "").lower().strip().split())
    due  = (r.get("due") or "")[:16]
    try:
        mm = int(due[14:16])
        bucket = (mm // 15) * 15
        due = f"{due[:14]}{bucket:02d}"
    except (ValueError, IndexError):
        pass
    return (text, due)


def list_all(user_dir: Path, include_done: bool = False) -> list[dict]:
    with _LOCK:
        rem = _load(user_dir)
    if not include_done:
        rem = [r for r in rem if r.get("status") not in ("done", "fired")]
    # Dedup near-duplicates on read so old dirty data shows clean. Keep
    # the OLDEST instance of each (text, due-bucket) group so downstream
    # snooze/done operations target a stable id.
    seen: set = set()
    deduped: list = []
    for r in sorted(rem, key=lambda x: x.get("ts", 0)):
        k = _dedup_key(r)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return sorted(deduped, key=lambda r: r.get("due", ""))


def add(user_dir: Path, text: str, due: str, channel: str = "in_app") -> dict:
    """due is ISO-8601 ("2026-05-30T09:00:00Z"). channel = sms / email / in_app.
    If a pending reminder with the same text+due-bucket already exists,
    returns it instead of creating a duplicate — prevents the 8× dup
    issue Frank hit during testing."""
    new_text = (text or "").strip()
    incoming_key = _dedup_key({"text": new_text, "due": due})
    with _LOCK:
        rem = _load(user_dir)
        for existing in rem:
            if existing.get("status") in ("done", "fired"):
                continue
            if _dedup_key(existing) == incoming_key:
                return existing
        reminder = {
            "id":      uuid.uuid4().hex[:12],
            "text":    new_text,
            "due":     due,
            "status":  "pending",
            "fired_at": None,
            "channel": channel,
            "ts":      time.time(),
        }
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
    """Mark a reminder as fired — AND mark every duplicate twin in the
    same dedup bucket fired too. Without this, the firing worker would
    fire one duplicate per tick (every 60s) until the cluster drained,
    creating a "buzz the user every minute for 8 minutes" loop when
    historical duplicates exist in the file. Pre-write-side-dedup data
    routinely has these clusters."""
    fired_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _LOCK:
        rem = _load(user_dir)
        target = next((r for r in rem if r.get("id") == reminder_id), None)
        if not target:
            return False
        target_key = _dedup_key(target)
        count = 0
        for r in rem:
            if r.get("status") in ("done", "fired"):
                continue
            if _dedup_key(r) == target_key:
                r["status"] = "fired"
                r["fired_at"] = fired_at
                count += 1
        if count:
            _save(user_dir, rem)
        return count > 0


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
