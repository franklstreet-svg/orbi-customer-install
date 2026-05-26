"""
memory module — three tiers.

  short_term: cleared after 30 days
  mid_term:   cleared after 365 days
  long_term:  never cleared

Stored as a single JSON file. Each entry has timestamp + content + optional metadata.
Cleanup runs lazily on every save.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()

SHORT_TTL = 30 * 86400
MID_TTL   = 365 * 86400

def _path(data_dir: Path) -> Path:
    return data_dir / "memory.json"

def _load_raw(data_dir: Path) -> dict:
    p = _path(data_dir)
    if not p.exists():
        return {"short_term": [], "mid_term": [], "long_term": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"short_term": [], "mid_term": [], "long_term": []}

def _save_raw(data_dir: Path, data: dict) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)

def _prune(data: dict) -> dict:
    now = time.time()
    data["short_term"] = [e for e in data.get("short_term", [])
                          if now - e.get("ts", now) < SHORT_TTL]
    data["mid_term"] = [e for e in data.get("mid_term", [])
                        if now - e.get("ts", now) < MID_TTL]
    return data

def remember(data_dir: Path, content: str, tier: str = "short_term",
             meta: dict | None = None) -> None:
    if tier not in ("short_term", "mid_term", "long_term"):
        raise ValueError(f"unknown tier: {tier}")
    with _LOCK:
        data = _load_raw(data_dir)
        data.setdefault(tier, []).append({
            "ts": time.time(),
            "content": content,
            "meta": meta or {},
        })
        _prune(data)
        _save_raw(data_dir, data)

def recall(data_dir: Path, query: str | None = None, tier: str | None = None,
           limit: int = 50) -> list[dict]:
    with _LOCK:
        data = _prune(_load_raw(data_dir))
    pools = []
    if tier:
        pools.append(data.get(tier, []))
    else:
        pools.extend([data.get("short_term", []), data.get("mid_term", []), data.get("long_term", [])])
    flat = [e for pool in pools for e in pool]
    flat.sort(key=lambda e: e.get("ts", 0), reverse=True)
    if query:
        q = query.lower()
        flat = [e for e in flat if q in (e.get("content", "") or "").lower()]
    return flat[:limit]

def context_block(data_dir: Path, max_chars: int = 1500) -> str:
    """Builds a short context block to prepend to LLM prompts. Long-term first."""
    with _LOCK:
        data = _prune(_load_raw(data_dir))
    chunks = []
    for tier in ("long_term", "mid_term", "short_term"):
        for e in data.get(tier, [])[-10:]:
            chunks.append(f"- {e.get('content','')}")
    if not chunks:
        return ""
    out = "REMEMBERED CONTEXT (use only when relevant):\n" + "\n".join(chunks)
    if len(out) > max_chars:
        out = out[:max_chars - 3] + "..."
    return out
