"""
preferences — per-contact, per-customer preference notes.

"Mrs. Johnson always orders extra cheese." "Joe Smith prefers email over
phone." "The Reno location wants printed invoices, not PDF." These are
the small persistent details a good front-desk person remembers about
every customer they've ever served. Orbi stores them here, keyed by
contact ID, so they surface on every interaction with that contact.

Storage:   data/users/<username>/contact_preferences.json
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("orbi.modules.preferences")

_FILENAME = "contact_preferences.json"
_LOCK = threading.Lock()
_MAX_PREFS_PER_CONTACT = 30


def _path(user_dir: Path) -> Path:
    return user_dir / _FILENAME


def _load(user_dir: Path) -> dict:
    p = _path(user_dir)
    if not p.exists():
        return {"contacts": {}}
    with _LOCK:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"contacts": {}}


def _save(user_dir: Path, data: dict) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        tmp.replace(p)


def set_preference(user_dir: Path, contact_id: str,
                    note: str, source: str = "owner") -> dict:
    """Add a free-text preference note for a contact. Returns the
    persisted record. Duplicates (same exact note) are collapsed.

    `source` is "owner" when the owner typed it directly, "inferred"
    when Orbi picked it up from a conversation. Inferred entries surface
    with a "want me to remember this?" prompt before they become durable."""
    if not contact_id or not note.strip():
        return {}
    data = _load(user_dir)
    contacts = data.setdefault("contacts", {})
    bucket = contacts.setdefault(contact_id, {"prefs": []})
    for existing in bucket["prefs"]:
        if existing.get("note", "").strip().lower() == note.strip().lower():
            existing["last_confirmed_at"] = int(time.time())
            _save(user_dir, data)
            return existing
    rec = {
        "note":              note.strip()[:400],
        "source":            source,
        "added_at":          int(time.time()),
        "last_confirmed_at": int(time.time()),
    }
    bucket["prefs"].insert(0, rec)
    if len(bucket["prefs"]) > _MAX_PREFS_PER_CONTACT:
        bucket["prefs"] = bucket["prefs"][:_MAX_PREFS_PER_CONTACT]
    _save(user_dir, data)
    return rec


def get_preferences(user_dir: Path, contact_id: str) -> list[dict]:
    """Return every preference Orbi has for this contact, newest first."""
    data = _load(user_dir)
    bucket = (data.get("contacts") or {}).get(contact_id) or {}
    return list(bucket.get("prefs") or [])


def format_for_prompt(user_dir: Path, contact_id: str,
                       max_chars: int = 600) -> str:
    """Render preferences as a short block we can paste into the system
    prompt so Orbi 'remembers' the contact's quirks during a conversation.
    Returns an empty string if there are none — caller shouldn't pollute
    the prompt with a header when there's no content."""
    prefs = get_preferences(user_dir, contact_id)
    if not prefs:
        return ""
    lines = ["What we know about this contact's preferences:"]
    total = len(lines[0]) + 1
    for p in prefs:
        line = f"  - {p['note']}"
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def forget(user_dir: Path, contact_id: str,
            note_substring: str | None = None) -> int:
    """Remove preferences. If `note_substring` is given, only delete
    entries containing it (case-insensitive). Otherwise wipe ALL of
    this contact's preferences. Returns the number removed."""
    data = _load(user_dir)
    bucket = (data.get("contacts") or {}).get(contact_id)
    if not bucket:
        return 0
    prefs = bucket.get("prefs") or []
    if note_substring is None:
        removed = len(prefs)
        bucket["prefs"] = []
    else:
        needle = note_substring.lower()
        kept = [p for p in prefs if needle not in p.get("note", "").lower()]
        removed = len(prefs) - len(kept)
        bucket["prefs"] = kept
    _save(user_dir, data)
    return removed
