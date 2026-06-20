"""
glossary — owner-defined vocabulary substitutions.

Every business has its own words. "Invoices" might be "bills" at one
shop, "tickets" at another, "billables" at a third. "Customers" might
be "patients", "clients", "patrons", "members". When the owner teaches
Orbi the local vocabulary, every reply Orbi writes — chat responses,
emails, voice scripts — passes through this filter so she sounds like
she works there.

Bidirectional:
    OWNER     ←→     ORBI
    "bills"   ←→    "invoices"

When the owner SAYS "bills", Orbi reads it as "invoices" so her code
path works. When Orbi WRITES, she renders "invoices" as "bills" so
she sounds local.

Storage:   data/users/<username>/glossary.json
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("orbi.modules.glossary")

_FILENAME = "glossary.json"
_LOCK = threading.Lock()


def _path(user_dir: Path) -> Path:
    return user_dir / _FILENAME


def _load(user_dir: Path) -> dict:
    p = _path(user_dir)
    if not p.exists():
        return {"terms": {}}
    with _LOCK:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"terms": {}}


def _save(user_dir: Path, data: dict) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        tmp.replace(p)


def set_term(user_dir: Path, canonical: str, local: str,
              note: str = "") -> dict:
    """Map Orbi's internal word (`canonical`, lowercased) to the
    owner's word (`local`, kept-as-typed for casing). Replaces any
    existing entry for that canonical term."""
    if not canonical.strip() or not local.strip():
        return {}
    key = canonical.strip().lower()
    rec = {
        "canonical":  key,
        "local":      local.strip(),
        "note":       note.strip()[:200],
        "updated_at": int(time.time()),
    }
    data = _load(user_dir)
    data.setdefault("terms", {})[key] = rec
    _save(user_dir, data)
    return rec


def remove_term(user_dir: Path, canonical: str) -> bool:
    data = _load(user_dir)
    key = canonical.strip().lower()
    if key not in (data.get("terms") or {}):
        return False
    data["terms"].pop(key)
    _save(user_dir, data)
    return True


def localize(user_dir: Path, text: str) -> str:
    """Translate Orbi's internal phrasing into the owner's local
    vocabulary. Word-boundary safe so 'invoices' becomes 'bills' but
    'invoice the customer' becomes 'bill the customer' (and
    'unsubsidized' stays 'unsubsidized' even if 'sub' is mapped)."""
    if not text:
        return text
    terms = (_load(user_dir).get("terms") or {})
    if not terms:
        return text
    out = text
    # Apply longest canonical first so multi-word terms beat single-word
    # subsets ("change order" before "order").
    for canonical in sorted(terms.keys(), key=len, reverse=True):
        local = terms[canonical]["local"]
        pattern = r"\b" + re.escape(canonical) + r"\b"
        out = re.sub(pattern, local, out, flags=re.IGNORECASE)
    return out


def canonicalize(user_dir: Path, text: str) -> str:
    """Inverse direction: the owner said 'bills', we read as 'invoices'
    so internal code paths that match on canonical keywords still fire."""
    if not text:
        return text
    terms = (_load(user_dir).get("terms") or {})
    if not terms:
        return text
    out = text
    for canonical, rec in sorted(terms.items(),
                                  key=lambda kv: len(kv[1]["local"]),
                                  reverse=True):
        local = rec["local"]
        pattern = r"\b" + re.escape(local) + r"\b"
        out = re.sub(pattern, canonical, out, flags=re.IGNORECASE)
    return out


def list_all(user_dir: Path) -> list[dict]:
    return list((_load(user_dir).get("terms") or {}).values())
