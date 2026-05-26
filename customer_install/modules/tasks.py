"""
tasks module (per-user) — to-do items the user wants to track.

Simpler than reminders (no due time) — just an open/done flag.
"Add 'order more napkins' to my list." → task with status=open.

Task shape:
  {
    "id":     "12-char hex",
    "text":   "order more napkins",
    "status": "open" | "done",
    "tags":   ["supplies"],
    "ts":     <unix created>,
    "done_at":"2026-05-26T..."  # set when marked done
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
    return user_dir / "tasks.json"


def _load(user_dir: Path) -> list[dict]:
    p = _path(user_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_dir: Path, items: list[dict]) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def list_all(user_dir: Path, include_done: bool = False) -> list[dict]:
    with _LOCK:
        items = _load(user_dir)
    if not include_done:
        items = [t for t in items if t.get("status") != "done"]
    # Open first by creation order, done last
    return sorted(items, key=lambda t: (t.get("status") == "done", t.get("ts", 0)))


def add(user_dir: Path, text: str, tags: list[str] | None = None) -> dict:
    task = {
        "id":     uuid.uuid4().hex[:12],
        "text":   (text or "").strip(),
        "status": "open",
        "tags":   tags or [],
        "ts":     time.time(),
        "done_at": None,
    }
    with _LOCK:
        items = _load(user_dir)
        items.append(task)
        _save(user_dir, items)
    return task


def mark_done(user_dir: Path, task_id: str) -> bool:
    with _LOCK:
        items = _load(user_dir)
        for t in items:
            if t.get("id") == task_id:
                t["status"] = "done"
                t["done_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                _save(user_dir, items)
                return True
    return False


def remove(user_dir: Path, task_id: str) -> bool:
    with _LOCK:
        items = _load(user_dir)
        before = len(items)
        items = [t for t in items if t.get("id") != task_id]
        _save(user_dir, items)
        return len(items) < before


def search(user_dir: Path, query: str) -> list[dict]:
    q = (query or "").lower().strip()
    if not q:
        return list_all(user_dir, include_done=True)
    return [t for t in list_all(user_dir, include_done=True)
            if q in (t.get("text", "") or "").lower()
            or any(q in tag.lower() for tag in (t.get("tags") or []))]


def context_block(user_dir: Path, max_items: int = 10) -> str:
    items = list_all(user_dir)[:max_items]
    if not items:
        return ""
    lines = ["YOUR OPEN TASKS:"] + [f"  - {t.get('text','')}" for t in items]
    return "\n".join(lines)
