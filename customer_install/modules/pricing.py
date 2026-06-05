"""
modules/pricing — unit-price catalog for common contractor line items.

The contractor sets up their go-to prices ONCE: outlet install, recessed
light install, drywall patch, etc. Then when the foreman says "3 plugs
and 3 lights" in a change order, Orby can look up the per-unit price,
multiply by quantity, and pre-fill the CO total. Owner can override the
amount before signing.

If no price catalog is set up, the line items still get captured — just
without dollar amounts. Owner fills the total manually.

Storage: data/pricing.json
Each entry:
  {
    "id":           "12-char hex",
    "label":        "Electrical outlet install",  ← canonical display label
    "aliases":      ["plug", "outlet", "receptacle"],  ← what the foreman says
    "unit_price":   85.00,
    "unit":         "each",
    "notes":        "Standard 15A duplex, includes wire + box",
    "created_at":   1780000000,
    "updated_at":   1780000123
  }

The matching is done by lowercase-substring against aliases + label.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()
FILE = "pricing.json"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


def _load(data_dir: Path) -> list[dict]:
    p = _path(data_dir)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8") or "[]")
    except (json.JSONDecodeError, OSError):
        return []


def _save(data_dir: Path, items: list[dict]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(p)


# ── CRUD ─────────────────────────────────────────────────────────────────


def add(data_dir: Path, *, label: str, unit_price: float,
        aliases: list[str] | None = None, unit: str = "each",
        notes: str = "") -> dict:
    label = (label or "").strip()
    if not label:
        raise ValueError("label required")
    now = int(time.time())
    item = {
        "id":         uuid.uuid4().hex[:12],
        "label":      label,
        "aliases":    [a.strip().lower() for a in (aliases or []) if a.strip()],
        "unit_price": round(float(unit_price), 2),
        "unit":       (unit or "each").strip().lower(),
        "notes":      (notes or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    with _LOCK:
        items = _load(data_dir)
        items.append(item)
        _save(data_dir, items)
    return item


def list_all(data_dir: Path) -> list[dict]:
    return sorted(_load(data_dir), key=lambda i: (i.get("label") or "").lower())


def get(data_dir: Path, item_id: str) -> dict | None:
    for i in _load(data_dir):
        if i.get("id") == item_id:
            return i
    return None


def update(data_dir: Path, item_id: str, **changes) -> dict | None:
    with _LOCK:
        items = _load(data_dir)
        for i in items:
            if i.get("id") == item_id:
                for k, v in changes.items():
                    if k in ("id", "created_at"):
                        continue
                    if k == "aliases" and isinstance(v, list):
                        v = [a.strip().lower() for a in v if a.strip()]
                    if k == "unit_price":
                        v = round(float(v), 2)
                    i[k] = v
                i["updated_at"] = int(time.time())
                _save(data_dir, items)
                return i
    return None


def remove(data_dir: Path, item_id: str) -> bool:
    with _LOCK:
        items = _load(data_dir)
        before = len(items)
        items = [i for i in items if i.get("id") != item_id]
        _save(data_dir, items)
        return len(items) < before


# ── Matching ─────────────────────────────────────────────────────────────


def find_price(data_dir: Path, item_text: str) -> dict | None:
    """Given a free-text item description ('plugs', 'recessed light',
    'drywall patch'), return the best-matching pricing record or None.
    Matches against label + aliases via case-insensitive substring."""
    q = (item_text or "").strip().lower()
    if not q:
        return None
    items = _load(data_dir)

    # Exact alias match first
    for i in items:
        for alias in i.get("aliases", []) or []:
            if alias == q:
                return i

    # Singular ↔ plural tolerance (strip trailing 's' on the query)
    q_sing = q.rstrip("s") if len(q) > 3 else q
    for i in items:
        for alias in i.get("aliases", []) or []:
            if alias == q_sing or alias.rstrip("s") == q_sing:
                return i

    # Substring match against label or aliases (longest match wins so
    # "recessed light" beats "light")
    best: tuple[int, dict] | None = None
    for i in items:
        hay = " ".join([
            (i.get("label") or "").lower(),
            *[(a or "").lower() for a in i.get("aliases", []) or []]
        ])
        score = 0
        for token in re.split(r"\W+", q_sing):
            if len(token) >= 3 and token in hay:
                score += len(token)
        if score > 0 and (best is None or score > best[0]):
            best = (score, i)
    return best[1] if best else None


def summary(data_dir: Path) -> dict:
    items = _load(data_dir)
    return {
        "total": len(items),
        "by_unit": {u: sum(1 for i in items if (i.get("unit") or "") == u)
                    for u in {(i.get("unit") or "") for i in items}},
    }
