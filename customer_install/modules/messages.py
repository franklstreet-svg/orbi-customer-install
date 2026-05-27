"""
messages module — captured leads, voicemails, orders, callback requests.

Anything the public-facing Orbi captures goes here. Owner reviews in the
dashboard. Each entry has a type, contact info, body, and read state.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()

VALID_TYPES = {"lead", "order", "voicemail", "callback", "question", "message"}

def _path(data_dir: Path) -> Path:
    return data_dir / "messages.json"

def _load_raw(data_dir: Path) -> dict:
    p = _path(data_dir)
    if not p.exists():
        return {"messages": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"messages": []}

def _save_raw(data_dir: Path, data: dict) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)

def list_all(data_dir: Path, limit: int = 200) -> list[dict]:
    with _LOCK:
        msgs = _load_raw(data_dir).get("messages", [])
    msgs = sorted(msgs, key=lambda m: m.get("timestamp", 0), reverse=True)
    return msgs[:limit]

def capture(data_dir: Path, *, msg_type: str, from_name: str | None,
            from_phone: str | None, from_email: str | None,
            body: str, source: str = "chat", meta: dict | None = None,
            config: dict | None = None) -> dict:
    if msg_type not in VALID_TYPES:
        msg_type = "message"
    msg = {
        "id": uuid.uuid4().hex[:12],
        "type": msg_type,
        "from_name": (from_name or "").strip() or None,
        "from_phone": (from_phone or "").strip() or None,
        "from_email": (from_email or "").strip() or None,
        "body": (body or "").strip(),
        "source": source,        # "chat" | "phone" | "sms"
        "timestamp": time.time(),
        "read": False,
        "meta": meta or {},
    }
    # Auto-categorize on capture. Lazy import so messages.py stays
    # usable from setup scripts that don't have an LLM configured.
    try:
        import auto_categorize
        msg["tags"] = auto_categorize.categorize(
            config or {}, msg["body"],
            hints={"from_name": msg["from_name"],
                   "from_email": msg["from_email"],
                   "from_phone": msg["from_phone"],
                   "type": msg["type"]},
        )
    except Exception:
        msg["tags"] = ["other"]
    with _LOCK:
        data = _load_raw(data_dir)
        data.setdefault("messages", []).append(msg)
        _save_raw(data_dir, data)
    return msg


def retag_all(data_dir: Path, config: dict) -> int:
    """Re-run categorization across the whole message list. Owner-triggered
    from the dashboard (e.g. after tweaking the classifier prompt). Returns
    the number of messages updated."""
    import auto_categorize
    with _LOCK:
        data = _load_raw(data_dir)
        msgs = data.get("messages", [])
        for m in msgs:
            try:
                m["tags"] = auto_categorize.categorize(
                    config, m.get("body", ""),
                    hints={"from_name": m.get("from_name"),
                           "from_email": m.get("from_email"),
                           "from_phone": m.get("from_phone"),
                           "type": m.get("type")},
                )
            except Exception:
                m["tags"] = m.get("tags") or ["other"]
        _save_raw(data_dir, data)
        return len(msgs)


def update_tags(data_dir: Path, message_id: str, tags: list[str]) -> bool:
    """Manual tag override from the dashboard."""
    with _LOCK:
        data = _load_raw(data_dir)
        for m in data.get("messages", []):
            if m.get("id") == message_id:
                m["tags"] = list(tags)
                _save_raw(data_dir, data)
                return True
    return False

def mark_read(data_dir: Path, message_id: str) -> bool:
    with _LOCK:
        data = _load_raw(data_dir)
        for m in data.get("messages", []):
            if m.get("id") == message_id:
                m["read"] = True
                _save_raw(data_dir, data)
                return True
    return False

def delete(data_dir: Path, message_id: str) -> bool:
    with _LOCK:
        data = _load_raw(data_dir)
        before = len(data.get("messages", []))
        data["messages"] = [m for m in data.get("messages", []) if m.get("id") != message_id]
        _save_raw(data_dir, data)
        return len(data["messages"]) < before

def unread_count(data_dir: Path) -> int:
    return sum(1 for m in list_all(data_dir, limit=1000) if not m.get("read"))
