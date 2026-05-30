"""
modules/projects — contractor job/project tracking (shared business data).

Part of the Contractor module. Stores active and historical jobs for a
contractor's Orbi: addresses, contracted amounts, status, scheduled and
actual dates, foreman assignment, customer info. Per Frank's blueprint
Section 6, all data stored locally in the customer's Safe Folder
(data_dir) — never on Frank's servers.

Project shape:
  {
    "id":               "12-char hex",
    "address":          "123 Maple St, Reno NV 89501",
    "label":            "Kitchen Remodel",   # short label for chat refs
    "customer_name":    "Sarah Johnson",
    "customer_phone":   "+17751234567",
    "customer_email":   "sarah@example.com",
    "contract_amount":  18500.00,             # original signed total
    "contracted_at":    "2026-05-15T00:00:00Z",
    "started_at":       "2026-05-22T00:00:00Z",
    "est_complete":     "2026-06-30T00:00:00Z",
    "actual_complete":  null,
    "status":           "estimate" | "active" | "on_hold" | "completed" | "cancelled",
    "foreman":          "Mike",
    "stage":            "framing",            # current stage label (freeform)
    "notes":            "Customer wants quartz counters not granite",
    "created_at":       1780000000,
    "updated_at":       1780000123
  }

Running totals (change orders, invoices, paid amount) are derived from
the change_orders + invoices modules and not stored here, so we don't
get stale numbers.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()
FILE = "projects.json"

VALID_STATUSES = {"estimate", "active", "on_hold", "completed", "cancelled"}


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


def _load(data_dir: Path) -> list[dict]:
    p = _path(data_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(data_dir: Path, projects: list[dict]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(projects, indent=2), encoding="utf-8")
    tmp.replace(p)


def add(data_dir: Path, *,
        address: str, label: str = "",
        customer_name: str = "", customer_phone: str = "",
        customer_email: str = "", contract_amount: float = 0.0,
        contracted_at: str = "", started_at: str = "",
        est_complete: str = "", foreman: str = "",
        stage: str = "", notes: str = "",
        status: str = "estimate") -> dict:
    """Add a project. Address is the only required field — chat path
    only needs an address to start tracking; everything else can be
    backfilled as the job unfolds."""
    address = (address or "").strip()
    if not address:
        raise ValueError("address required")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    now = int(time.time())
    project = {
        "id":              uuid.uuid4().hex[:12],
        "address":         address,
        "label":           (label or "").strip(),
        "customer_name":   (customer_name or "").strip(),
        "customer_phone":  (customer_phone or "").strip(),
        "customer_email":  (customer_email or "").strip().lower(),
        "contract_amount": float(contract_amount or 0),
        "contracted_at":   contracted_at,
        "started_at":      started_at,
        "est_complete":    est_complete,
        "actual_complete": None,
        "status":          status,
        "foreman":         (foreman or "").strip(),
        "stage":           (stage or "").strip(),
        "notes":           (notes or "").strip(),
        "created_at":      now,
        "updated_at":      now,
    }
    with _LOCK:
        projects = _load(data_dir)
        projects.append(project)
        _save(data_dir, projects)
    return project


def get(data_dir: Path, project_id: str) -> dict | None:
    for p in _load(data_dir):
        if p.get("id") == project_id:
            return p
    return None


def list_all(data_dir: Path,
              statuses: list[str] | None = None,
              limit: int = 200) -> list[dict]:
    """Return projects, optionally filtered by status list. Newest first."""
    projects = _load(data_dir)
    if statuses:
        s = set(statuses)
        projects = [p for p in projects if p.get("status") in s]
    projects.sort(key=lambda p: p.get("updated_at", 0), reverse=True)
    return projects[:limit]


def list_active(data_dir: Path) -> list[dict]:
    """Active + on_hold = the jobs that aren't done and aren't speculative."""
    return list_all(data_dir, statuses=["active", "on_hold"])


def update(data_dir: Path, project_id: str, **changes) -> dict | None:
    """Update a project. Returns the new record or None if not found.
    Refuses to change id or created_at; bumps updated_at on success."""
    with _LOCK:
        projects = _load(data_dir)
        for p in projects:
            if p.get("id") == project_id:
                for k, v in changes.items():
                    if k in ("id", "created_at"):
                        continue
                    if k == "status" and v not in VALID_STATUSES:
                        raise ValueError(f"invalid status: {v}")
                    p[k] = v
                p["updated_at"] = int(time.time())
                _save(data_dir, projects)
                return p
    return None


def remove(data_dir: Path, project_id: str) -> bool:
    """Hard-delete a project. Almost always the wrong move — prefer
    status='cancelled' instead so the audit trail survives. Only here
    for genuine data-entry errors."""
    with _LOCK:
        projects = _load(data_dir)
        before = len(projects)
        projects = [p for p in projects if p.get("id") != project_id]
        if len(projects) < before:
            _save(data_dir, projects)
            return True
    return False


def find_by_address(data_dir: Path, address_fragment: str) -> list[dict]:
    """Fuzzy-match projects by address substring (case-insensitive).
    Used by the chat handler when the foreman says 'the Maple project'."""
    q = (address_fragment or "").strip().lower()
    if not q:
        return []
    matches = []
    for p in _load(data_dir):
        addr = (p.get("address") or "").lower()
        label = (p.get("label") or "").lower()
        if q in addr or q in label:
            matches.append(p)
    return matches


def summary(data_dir: Path) -> dict:
    """Top-level counts + dollar totals for the morning brief / dashboard.
    Doesn't pull from change_orders or invoices — call those modules
    separately for paid/owed totals."""
    projects = _load(data_dir)
    by_status: dict[str, int] = {}
    contracted_total = 0.0
    for p in projects:
        s = p.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
        contracted_total += float(p.get("contract_amount") or 0)
    return {
        "total_projects":   len(projects),
        "by_status":        by_status,
        "contracted_total": contracted_total,
    }
