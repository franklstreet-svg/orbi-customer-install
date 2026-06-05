"""
memory module — four tiers.

  short_term: cleared after 30 days
  mid_term:   cleared after 365 days
  long_term:  never cleared
  concerns:   things the user is worried about. Decays after 90 days
              of no reinforcement (e.g. user mentions it again, or it
              stays relevant). Surfaces with more weight than facts.

Stored as a single JSON file. Each entry has timestamp + content + optional
metadata. Cleanup runs lazily on every save.

Concerns tier is the infrastructure for personal-Orby's emotional
intelligence — companion_mode == "personal" only. The owner-chat path
gates writes on the setting; this module just stores/retrieves.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()

SHORT_TTL    = 30 * 86400
MID_TTL      = 365 * 86400
CONCERNS_TTL = 90 * 86400  # decay if not reinforced for 90 days

def _path(data_dir: Path) -> Path:
    return data_dir / "memory.json"

def _empty() -> dict:
    return {"short_term": [], "mid_term": [], "long_term": [], "concerns": []}

def _load_raw(data_dir: Path) -> dict:
    p = _path(data_dir)
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # Backfill concerns key for files written before the tier existed
        if "concerns" not in data:
            data["concerns"] = []
        return data
    except (json.JSONDecodeError, OSError):
        return _empty()

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
    # Concerns: use last_reinforced_at (set when the concern is reaffirmed)
    # rather than original ts, so reaffirming a concern keeps it alive.
    data["concerns"] = [e for e in data.get("concerns", [])
                        if now - e.get("last_reinforced_at", e.get("ts", now)) < CONCERNS_TTL]
    return data

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "and", "or", "of", "to",
    "in", "on", "at", "for", "with", "his", "her", "their", "user", "users",
    "this", "that", "it", "its", "i", "you", "me", "my", "your", "our",
    "be", "been", "as", "by", "from", "but", "if", "so", "do", "does",
    "did", "has", "have", "had", "will", "would", "should", "can", "could",
}


def _content_tokens(s: str) -> set[str]:
    """Lowercase + stopword-strip the fact for similarity comparison.
    "User's main supplier for cleaning chemicals is ChemPro of Sparks NV"
    → {'main', 'supplier', 'cleaning', 'chemicals', 'chempro', 'sparks', 'nv'}
    """
    import re as _re
    tokens = _re.findall(r"[a-z0-9]+", (s or "").lower())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def _is_near_duplicate(a: str, b: str, threshold: float = 0.70) -> bool:
    """Jaccard similarity of content tokens >= threshold → duplicate.
    PLUS: if one fact's tokens are a strict subset of the other, also
    treat as duplicate (the longer wins). The subset rule catches
    "User's wife is Cathleen" ≡ "User's wife's name is Cathleen"
    (Jaccard 0.67, just below threshold) without lowering the threshold
    enough to cause false positives elsewhere."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return False
    # Subset short-circuit (one is fully contained in the other)
    if ta <= tb or tb <= ta:
        return True
    return (len(ta & tb) / len(ta | tb)) >= threshold


def remember(data_dir: Path, content: str, tier: str = "short_term",
             meta: dict | None = None) -> None:
    """Append a fact, but only if it's not already known. Dedup checks
    ALL tiers (not just the target tier) — facts shouldn't be stored
    twice just because they were extracted into different tiers. When a
    near-duplicate exists, the NEWER content replaces the older one IF
    the newer one is more specific (longer); otherwise the older entry
    is left alone and the new write is skipped."""
    if tier not in ("short_term", "mid_term", "long_term"):
        raise ValueError(f"unknown tier: {tier}")
    content = (content or "").strip()
    if not content:
        return
    with _LOCK:
        data = _load_raw(data_dir)
        # Scan every tier for a near-duplicate
        for t in ("short_term", "mid_term", "long_term"):
            for i, existing in enumerate(data.get(t, []) or []):
                if _is_near_duplicate(existing.get("content", ""), content):
                    # Newer + more specific → replace; otherwise skip
                    if len(content) > len(existing.get("content", "")):
                        existing["content"] = content
                        existing["ts"] = time.time()
                        if meta:
                            existing["meta"] = meta
                    _prune(data)
                    _save_raw(data_dir, data)
                    return
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
    # Read-side dedup so older dirty data (pre-write-side-dedup) doesn't
    # surface duplicates in chat. When two facts are near-duplicates,
    # keep the MORE SPECIFIC one (longer content) — "ChemPro of Sparks
    # NV" wins over "ChemPro of Sparks". Order is then re-sorted by ts.
    deduped: list = []
    for e in flat:
        c = e.get("content", "")
        replaced = False
        for i, k in enumerate(deduped):
            if _is_near_duplicate(c, k.get("content", "")):
                # If incoming has more detail, swap it in
                if len(c) > len(k.get("content", "")):
                    deduped[i] = e
                replaced = True
                break
        if not replaced:
            deduped.append(e)
    deduped.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return deduped[:limit]

def add_concern(data_dir: Path, content: str,
                 weight: float = 0.6, meta: dict | None = None) -> None:
    """Record something the user is worried about. If a near-duplicate
    already exists, REINFORCE it (bump weight + reset last_reinforced_at)
    rather than adding a second entry. This is how concerns 'matter
    more' over time — the more they come up, the more weight they
    carry."""
    content = (content or "").strip()
    if not content:
        return
    with _LOCK:
        data = _load_raw(data_dir)
        now = time.time()
        concerns = data.setdefault("concerns", [])
        for c in concerns:
            if _is_near_duplicate(c.get("content", ""), content):
                # Reinforce: bump weight (capped at 1.0), refresh decay clock
                c["weight"] = min(1.0, c.get("weight", weight) + 0.15)
                c["last_reinforced_at"] = now
                c["reinforce_count"] = c.get("reinforce_count", 1) + 1
                if meta:
                    c.setdefault("meta", {}).update(meta)
                _prune(data)
                _save_raw(data_dir, data)
                return
        concerns.append({
            "ts":                    now,
            "last_reinforced_at":    now,
            "reinforce_count":       1,
            "content":               content,
            "weight":                float(weight),
            "resolved":              False,
            "meta":                  meta or {},
        })
        _prune(data)
        _save_raw(data_dir, data)


def recall_concerns(data_dir: Path, limit: int = 10) -> list[dict]:
    """Return active (unresolved) concerns, heaviest first.
    Reinforced concerns rank above one-off mentions."""
    with _LOCK:
        data = _prune(_load_raw(data_dir))
    items = [c for c in data.get("concerns", [])
             if not c.get("resolved")]
    items.sort(key=lambda c: (
        c.get("weight", 0),
        c.get("last_reinforced_at", c.get("ts", 0)),
    ), reverse=True)
    return items[:limit]


def resolve_concern(data_dir: Path, query: str) -> int:
    """Mark concerns matching `query` as resolved. Returns how many
    were touched. The owner triggers this by saying things like
    "the ChemPro thing is sorted" / "stop worrying about X"."""
    q = (query or "").strip().lower()
    if not q:
        return 0
    count = 0
    with _LOCK:
        data = _load_raw(data_dir)
        for c in data.get("concerns", []):
            if q in (c.get("content") or "").lower() and not c.get("resolved"):
                c["resolved"] = True
                c["resolved_at"] = time.time()
                count += 1
        if count:
            _save_raw(data_dir, data)
    return count


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
