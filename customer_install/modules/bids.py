"""
modules/bids — outstanding bids + win/loss tracking for the contractor.

Per Frank's blueprint Section 4 (Estimating & Bidding). Every contractor
has bids sitting on a prospect's desk that never converted because nobody
followed up. This module tracks the outstanding bids and lets Orby nudge
the GC when one's been silent too long.

Bid shape:
  {
    "id":               "12-char hex",
    "customer_name":    "Sarah Johnson",
    "customer_phone":   "+17755551234",
    "customer_email":   "sarah@example.com",
    "project_address":  "555 Oak Avenue, Reno NV",
    "project_type":     "Kitchen Remodel",
    "scope_summary":    "Full kitchen — cabinets, counters, floor, paint",
    "amount":           48500.00,
    "sent_at":          1780000000,
    "expires_at":       null,                # bid validity if quoted
    "status":           "sent" | "won" | "lost" | "expired" | "withdrawn",
    "won_at":           null,                 # if status=won
    "won_project_id":   "",                   # link to the converted project
    "lost_at":          null,
    "lost_reason":      "",                   # captured for win/loss analysis
    "follow_up_count":  0,
    "last_followup_at": null,
    "notes":            "",
    "created_at":       1780000000,
    "updated_at":       1780000000
  }

Pattern matches modules/invoices for status machine + follow-up tracking
so the same scheduler logic (in orbi.py) can sweep both.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()
FILE = "bids.json"

VALID_STATUSES = {"sent", "won", "lost", "expired", "withdrawn"}


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


def _save(data_dir: Path, bids: list[dict]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bids, indent=2), encoding="utf-8")
    tmp.replace(p)


def add(data_dir: Path, *,
        customer_name: str, amount: float,
        customer_phone: str = "", customer_email: str = "",
        project_address: str = "", project_type: str = "",
        scope_summary: str = "", notes: str = "",
        status: str = "sent") -> dict:
    name = (customer_name or "").strip()
    if not name:
        raise ValueError("customer_name required")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    now = int(time.time())
    entry = {
        "id":               uuid.uuid4().hex[:12],
        "customer_name":    name,
        "customer_phone":   (customer_phone or "").strip(),
        "customer_email":   (customer_email or "").strip().lower(),
        "project_address":  (project_address or "").strip(),
        "project_type":     (project_type or "").strip(),
        "scope_summary":    (scope_summary or "").strip(),
        "amount":           float(amount or 0),
        "sent_at":          now if status == "sent" else None,
        "expires_at":       None,
        "status":           status,
        "won_at":           None,
        "won_project_id":   "",
        "lost_at":          None,
        "lost_reason":      "",
        "follow_up_count":  0,
        "last_followup_at": None,
        "notes":            (notes or "").strip(),
        "created_at":       now,
        "updated_at":       now,
    }
    with _LOCK:
        bids = _load(data_dir)
        bids.append(entry)
        _save(data_dir, bids)
    return entry


def get(data_dir: Path, bid_id: str) -> dict | None:
    for b in _load(data_dir):
        if b.get("id") == bid_id:
            return b
    return None


def list_open(data_dir: Path) -> list[dict]:
    """Outstanding bids still waiting on a decision."""
    return [b for b in _load(data_dir) if b.get("status") == "sent"]


def list_all(data_dir: Path, statuses: list[str] | None = None,
              limit: int = 200) -> list[dict]:
    bids = _load(data_dir)
    if statuses:
        s = set(statuses)
        bids = [b for b in bids if b.get("status") in s]
    bids.sort(key=lambda b: b.get("updated_at", 0), reverse=True)
    return bids[:limit]


def find_by_customer(data_dir: Path, query: str) -> list[dict]:
    """Fuzzy match by customer name or project address."""
    q = (query or "").strip().lower()
    if not q:
        return []
    matches = []
    for b in _load(data_dir):
        if (q in (b.get("customer_name") or "").lower()
                or q in (b.get("project_address") or "").lower()
                or q in (b.get("project_type") or "").lower()):
            matches.append(b)
    return matches


def update(data_dir: Path, bid_id: str, **changes) -> dict | None:
    with _LOCK:
        bids = _load(data_dir)
        for b in bids:
            if b.get("id") == bid_id:
                for k, v in changes.items():
                    if k in ("id", "created_at"):
                        continue
                    if k == "status" and v not in VALID_STATUSES:
                        raise ValueError(f"invalid status: {v}")
                    b[k] = v
                b["updated_at"] = int(time.time())
                _save(data_dir, bids)
                return b
    return None


def mark_won(data_dir: Path, bid_id: str,
              project_id: str = "") -> dict | None:
    return update(data_dir, bid_id, status="won",
                   won_at=int(time.time()),
                   won_project_id=(project_id or "").strip())


def mark_lost(data_dir: Path, bid_id: str, reason: str = "") -> dict | None:
    return update(data_dir, bid_id, status="lost",
                   lost_at=int(time.time()),
                   lost_reason=(reason or "").strip())


def mark_followed_up(data_dir: Path, bid_id: str) -> dict | None:
    b = get(data_dir, bid_id)
    if not b:
        return None
    return update(data_dir, bid_id,
                   follow_up_count=int(b.get("follow_up_count", 0)) + 1,
                   last_followup_at=int(time.time()))


def open_needing_follow_up(data_dir: Path, days: int = 7) -> list[dict]:
    """Bids in 'sent' status that haven't been followed up in `days` days.
    Drives the bid-side nightly nudge sweep (same pattern as receivables)."""
    now = int(time.time())
    threshold = days * 86400
    out = []
    for b in list_open(data_dir):
        last = b.get("last_followup_at") or b.get("sent_at") or 0
        if now - int(last) >= threshold:
            out.append(b)
    return out


def summary(data_dir: Path) -> dict:
    """Top-level numbers for the money report + morning brief."""
    bids = _load(data_dir)
    open_bids = [b for b in bids if b.get("status") == "sent"]
    won = [b for b in bids if b.get("status") == "won"]
    lost = [b for b in bids if b.get("status") == "lost"]
    total_open_value = sum(float(b.get("amount") or 0) for b in open_bids)
    total_won_value = sum(float(b.get("amount") or 0) for b in won)
    total_lost_value = sum(float(b.get("amount") or 0) for b in lost)
    decided = len(won) + len(lost)
    win_rate = (len(won) / decided * 100) if decided else 0.0
    avg_won = (total_won_value / len(won)) if won else 0.0
    return {
        "open_count":       len(open_bids),
        "open_value":       total_open_value,
        "won_count":        len(won),
        "won_value":        total_won_value,
        "lost_count":       len(lost),
        "lost_value":       total_lost_value,
        "win_rate_pct":     round(win_rate, 1),
        "avg_won_amount":   round(avg_won, 2),
    }
