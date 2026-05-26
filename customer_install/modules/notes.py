"""
notes module — free-form notes the owner adds for Orbi to remember.

Different from memory because notes are *authored* by the owner, not
extracted from conversation. ("Maria has nut allergy", "Tuesday delivery only".)
Notes always show up in Orbi's prompt when relevant.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()

def _path(data_dir: Path) -> Path:
    return data_dir / "notes.json"

def _load_raw(data_dir: Path) -> dict:
    p = _path(data_dir)
    if not p.exists():
        return {"notes": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"notes": []}

def _save_raw(data_dir: Path, data: dict) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)

def list_all(data_dir: Path) -> list[dict]:
    with _LOCK:
        return _load_raw(data_dir).get("notes", [])

def add(data_dir: Path, content: str, tags: list[str] | None = None) -> dict:
    note = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "content": content.strip(),
        "tags": tags or [],
    }
    with _LOCK:
        data = _load_raw(data_dir)
        data.setdefault("notes", []).append(note)
        _save_raw(data_dir, data)
    return note

def remove(data_dir: Path, note_id: str) -> bool:
    with _LOCK:
        data = _load_raw(data_dir)
        before = len(data.get("notes", []))
        data["notes"] = [n for n in data.get("notes", []) if n.get("id") != note_id]
        _save_raw(data_dir, data)
        return len(data["notes"]) < before

def search(data_dir: Path, query: str) -> list[dict]:
    q = (query or "").lower().strip()
    if not q:
        return list_all(data_dir)
    return [n for n in list_all(data_dir)
            if q in (n.get("content", "") or "").lower()
            or any(q in t.lower() for t in (n.get("tags") or []))]

def context_block(data_dir: Path, max_chars: int = 800) -> str:
    notes = list_all(data_dir)
    if not notes:
        return ""
    notes = sorted(notes, key=lambda n: n.get("ts", 0), reverse=True)[:15]
    lines = ["OWNER NOTES (always relevant):"] + [f"- {n.get('content','')}" for n in notes]
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars - 3] + "..."
    return out
