"""
modules/subcontractors — the GC's sub directory + assignment tracking.

Per Frank's blueprint Section 7 (Subcontractor Management). Stored as
shared business data — the office team, the foreman, and the GC all
work from the same sub list.

Sub shape:
  {
    "id":          "12-char hex",
    "name":        "Bob's Plumbing",
    "contact_name":"Bob Davis",
    "phone":       "+17751234567",
    "email":       "bob@bobsplumb.com",
    "trade":       "plumbing",     # lowercase canonical trade name
    "license":     "NV-123456",
    "insurance_expires": "2026-12-31",
    "rate":        "$85/hr",
    "notes":       "Slow on returns but reliable on rough-in",
    "rating":      4,              # 1-5 internal preference rating
    "active":      true,
    "created_at":  1780000000,
    "updated_at":  1780000123
  }

Assignment shape (which sub is on which job):
  {
    "id":          "12-char hex",
    "sub_id":      "12-char hex",
    "project_id":  "12-char hex",
    "scope":       "Rough plumbing through to walk-through",
    "scheduled":   "2026-06-05",
    "completed":   null,
    "created_at":  1780000000
  }

Subs and Assignments live in the same file for simplicity (small data
volumes per contractor — under a few hundred each).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()
FILE = "subcontractors.json"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


def _load(data_dir: Path) -> dict:
    p = _path(data_dir)
    if not p.exists():
        return {"subs": [], "assignments": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"subs": [], "assignments": []}
        data.setdefault("subs", [])
        data.setdefault("assignments", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"subs": [], "assignments": []}


def _save(data_dir: Path, data: dict) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def add_sub(data_dir: Path, *,
             name: str, trade: str = "",
             contact_name: str = "", phone: str = "", email: str = "",
             license: str = "", insurance_expires: str = "",
             rate: str = "", notes: str = "", rating: int = 0) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("sub name required")
    now = int(time.time())
    entry = {
        "id":                uuid.uuid4().hex[:12],
        "name":              name,
        "contact_name":      (contact_name or "").strip(),
        "phone":             (phone or "").strip(),
        "email":             (email or "").strip().lower(),
        "trade":             (trade or "").strip().lower(),
        "license":           (license or "").strip(),
        "insurance_expires": (insurance_expires or "").strip(),
        "rate":              (rate or "").strip(),
        "notes":             (notes or "").strip(),
        "rating":            int(rating or 0),
        "active":            True,
        "created_at":        now,
        "updated_at":        now,
    }
    with _LOCK:
        data = _load(data_dir)
        data["subs"].append(entry)
        _save(data_dir, data)
    return entry


def list_subs(data_dir: Path, trade: str = "",
               active_only: bool = True) -> list[dict]:
    data = _load(data_dir)
    subs = data.get("subs", [])
    if active_only:
        subs = [s for s in subs if s.get("active", True)]
    if trade:
        t = trade.strip().lower()
        subs = [s for s in subs if s.get("trade", "").lower() == t]
    subs.sort(key=lambda s: (-int(s.get("rating") or 0), s.get("name", "").lower()))
    return subs


def find_sub(data_dir: Path, query: str) -> list[dict]:
    """Fuzzy match by name or trade. Used when chat says 'assign Bob to Oak'."""
    q = (query or "").strip().lower()
    if not q:
        return []
    matches = []
    for s in _load(data_dir).get("subs", []):
        if not s.get("active", True):
            continue
        name = s.get("name", "").lower()
        contact = s.get("contact_name", "").lower()
        if q in name or q in contact or q == s.get("trade", "").lower():
            matches.append(s)
    return matches


def update_sub(data_dir: Path, sub_id: str, **changes) -> dict | None:
    with _LOCK:
        data = _load(data_dir)
        for s in data["subs"]:
            if s.get("id") == sub_id:
                for k, v in changes.items():
                    if k in ("id", "created_at"):
                        continue
                    s[k] = v
                s["updated_at"] = int(time.time())
                _save(data_dir, data)
                return s
    return None


def deactivate_sub(data_dir: Path, sub_id: str) -> bool:
    """Soft delete — sub stays in the file so historical assignments
    keep their reference but won't show up in active lists."""
    return update_sub(data_dir, sub_id, active=False) is not None


def assign(data_dir: Path, *,
            sub_id: str, project_id: str,
            scope: str = "", scheduled: str = "") -> dict:
    if not sub_id or not project_id:
        raise ValueError("sub_id + project_id required")
    now = int(time.time())
    entry = {
        "id":         uuid.uuid4().hex[:12],
        "sub_id":     sub_id,
        "project_id": project_id,
        "scope":      (scope or "").strip(),
        "scheduled":  (scheduled or "").strip(),
        "completed":  None,
        "created_at": now,
    }
    with _LOCK:
        data = _load(data_dir)
        data["assignments"].append(entry)
        _save(data_dir, data)
    return entry


def list_assignments_for_project(data_dir: Path, project_id: str) -> list[dict]:
    data = _load(data_dir)
    out = [a for a in data.get("assignments", [])
           if a.get("project_id") == project_id]
    out.sort(key=lambda a: a.get("created_at", 0), reverse=True)
    # Enrich with sub names
    sub_by_id = {s["id"]: s for s in data.get("subs", [])}
    for a in out:
        sub = sub_by_id.get(a.get("sub_id"))
        a["sub_name"] = sub.get("name") if sub else "(unknown)"
        a["sub_trade"] = sub.get("trade") if sub else ""
    return out


def list_assignments_for_sub(data_dir: Path, sub_id: str) -> list[dict]:
    data = _load(data_dir)
    return [a for a in data.get("assignments", [])
            if a.get("sub_id") == sub_id]


def mark_assignment_complete(data_dir: Path, assignment_id: str) -> dict | None:
    with _LOCK:
        data = _load(data_dir)
        for a in data.get("assignments", []):
            if a.get("id") == assignment_id:
                a["completed"] = int(time.time())
                _save(data_dir, data)
                return a
    return None


def insurance_expiring_soon(data_dir: Path, days: int = 30) -> list[dict]:
    """Find subs whose insurance expires within `days`. Used to feed the
    morning brief — 'Bob's Plumbing insurance expires in 5 days' is a
    real-money risk if Bob is on site without coverage."""
    from datetime import date, timedelta
    cutoff = date.today() + timedelta(days=days)
    out = []
    for s in _load(data_dir).get("subs", []):
        if not s.get("active", True):
            continue
        exp_str = (s.get("insurance_expires") or "").strip()
        if not exp_str:
            continue
        try:
            exp = date.fromisoformat(exp_str)
        except ValueError:
            continue
        if exp <= cutoff:
            out.append(s)
    out.sort(key=lambda s: s.get("insurance_expires", ""))
    return out
