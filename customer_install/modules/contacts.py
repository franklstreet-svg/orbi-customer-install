"""
contacts module (per-user) — people the user knows.

Each contact has standard fields plus a free-form notes field and
last_contact timestamp so Orbi can answer "when did I last talk to Joe."

Contacts are auto-created from leads (when a website visitor leaves their
info, it lands in the owner's contacts as source='lead'). Staff can add
their own contacts manually.

Contact shape:
  {
    "id":          "12-char hex",
    "name":        "Joe Smith",
    "phone":       "+17751234567",
    "email":       "joe@example.com",
    "notes":       "Wants estimate on bathroom remodel",
    "tags":        ["plumbing", "lead"],
    "source":      "manual" | "lead" | "import",
    "company":     "Smith Construction",
    "ts":          <unix when created>,
    "last_contact":"2026-05-26T14:00:00Z"
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
    return user_dir / "contacts.json"


def _load(user_dir: Path) -> list[dict]:
    p = _path(user_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_dir: Path, contacts: list[dict]) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(contacts, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def list_all(user_dir: Path) -> list[dict]:
    with _LOCK:
        return sorted(_load(user_dir), key=lambda c: (c.get("name", "") or "").lower())


def add(user_dir: Path, name: str, phone: str = "", email: str = "",
        notes: str = "", tags: list[str] | None = None,
        source: str = "manual", company: str = "") -> dict:
    contact = {
        "id":           uuid.uuid4().hex[:12],
        "name":         (name or "").strip(),
        "phone":        (phone or "").strip(),
        "email":        (email or "").strip(),
        "notes":        (notes or "").strip(),
        "tags":         tags or [],
        "source":       source,
        "company":      (company or "").strip(),
        "ts":           time.time(),
        "last_contact": _now_iso(),
    }
    with _LOCK:
        contacts = _load(user_dir)
        contacts.append(contact)
        _save(user_dir, contacts)
    return contact


def update(user_dir: Path, contact_id: str, **changes) -> dict | None:
    with _LOCK:
        contacts = _load(user_dir)
        for c in contacts:
            if c.get("id") == contact_id:
                for k, v in changes.items():
                    if k in ("id", "ts"):
                        continue
                    c[k] = v
                _save(user_dir, contacts)
                return c
    return None


def remove(user_dir: Path, contact_id: str) -> bool:
    with _LOCK:
        contacts = _load(user_dir)
        before = len(contacts)
        contacts = [c for c in contacts if c.get("id") != contact_id]
        _save(user_dir, contacts)
        return len(contacts) < before


def touch_last_contact(user_dir: Path, contact_id: str) -> bool:
    """Update last_contact timestamp — called when there's a fresh interaction."""
    with _LOCK:
        contacts = _load(user_dir)
        for c in contacts:
            if c.get("id") == contact_id:
                c["last_contact"] = _now_iso()
                _save(user_dir, contacts)
                return True
    return False


def append_personal_note(user_dir: Path, contact_id: str,
                          note: str, source: str = "chat") -> dict | None:
    """Append a timestamped personal fact to a contact's record.

    contact.personal_notes = [
        {"note": "daughter Maria just graduated", "ts": 1780000000, "source": "chat"},
        {"note": "loves jazz, especially Miles Davis", "ts": 1780100000, "source": "chat"},
        ...
    ]

    Why: every fact you learn about a client is leverage for the next
    interaction. CRMs have a 'notes' field nobody uses; this one fills
    itself in from chat automatically.

    Idempotency: skips if an identical note (case-insensitive) already
    exists. Newest notes first; auto-prunes to 40 max per contact.
    """
    note = (note or "").strip()
    if not note or len(note) < 5:
        return None
    note_lower = note.lower()
    with _LOCK:
        contacts = _load(user_dir)
        for c in contacts:
            if c.get("id") == contact_id:
                existing = c.setdefault("personal_notes", [])
                # Dedupe — don't store the same fact twice
                if any((n.get("note", "").lower() == note_lower) for n in existing):
                    return c
                existing.insert(0, {
                    "note":   note[:300],
                    "ts":     int(time.time()),
                    "source": source,
                })
                # Cap at 40 notes per contact — prevents runaway growth
                c["personal_notes"] = existing[:40]
                _save(user_dir, contacts)
                return c
    return None


def find_by_name(user_dir: Path, name: str) -> dict | None:
    """Find a contact by case-insensitive name match. First-name-only OK."""
    if not name:
        return None
    name_lower = name.strip().lower()
    contacts = list_all(user_dir)
    # Exact full-name match first
    for c in contacts:
        if c.get("name", "").lower() == name_lower:
            return c
    # First-name match — only return if unambiguous
    matches = [c for c in contacts
               if c.get("name", "").lower().split()[:1] == name_lower.split()[:1]]
    if len(matches) == 1:
        return matches[0]
    return None


def personal_notes_for(user_dir: Path, contact_id: str,
                       limit: int = 8) -> list[dict]:
    """Return the personal_notes for a contact, newest first."""
    contacts = list_all(user_dir)
    for c in contacts:
        if c.get("id") == contact_id:
            notes = c.get("personal_notes", []) or []
            return notes[:limit]
    return []


def search(user_dir: Path, query: str) -> list[dict]:
    q = (query or "").lower().strip()
    if not q:
        return list_all(user_dir)
    hits = []
    for c in list_all(user_dir):
        haystack = " ".join([
            c.get("name", ""), c.get("phone", ""), c.get("email", ""),
            c.get("company", ""), c.get("notes", ""),
            " ".join(c.get("tags", []) or []),
            " ".join(
                str(n.get("note", ""))
                for n in (c.get("personal_notes", []) or [])
                if isinstance(n, dict)
            ),
        ]).lower()
        if q in haystack:
            hits.append(c)
    return hits


def context_block(user_dir: Path, max_items: int = 5) -> str:
    """Most-recently-contacted people, for chat context. Not the full book."""
    contacts = sorted(list_all(user_dir),
                      key=lambda c: c.get("last_contact", ""), reverse=True)[:max_items]
    if not contacts:
        return ""
    lines = ["YOUR RECENT CONTACTS:"]
    for c in contacts:
        bits = [c.get("name", "")]
        if c.get("company"):
            bits.append(f"({c['company']})")
        if c.get("phone"):
            bits.append(c["phone"])
        lines.append("  - " + " ".join(bits))
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
