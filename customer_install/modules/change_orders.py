"""
modules/change_orders — the hero feature of the Contractor module.

A change order (CO) is extra work the client agrees to beyond the
original contract. Per Frank's blueprint Section 5.2, the workflow is:

  1. Foreman tells Orbi a change was agreed on a job.
  2. Orbi drafts the CO document from a template, pulling job + client
     context from local memory.
  3. The GC (human-in-the-loop) reviews + approves the draft.
  4. Orbi sends to the client for e-signature.
  5. On client signature, Orbi logs the CO to the project, updates the
     contract total, files the signed document, and notifies the office.
  6. Leak alarm — if work gets logged on a project with no matching
     signed CO, Orbi flags it BEFORE it becomes unbillable. The whole
     point of the module.

This module is just the data layer + status machine. The drafting
template, the GC-approval UI, and the e-sign integration live one
layer up (in orbi.py handlers + the dashboard) — kept here so this
file stays testable in isolation.

ChangeOrder shape:
  {
    "id":               "12-char hex",
    "project_id":       "12-char hex",
    "co_number":        2,                    # 1-indexed per project
    "description":      "Upgrade counters from laminate to quartz",
    "amount":           1200.00,              # positive = client owes more
                                              # negative = credit / reduction
    "scope_detail":     "Remove laminate; supply + install 28 sf quartz...",
    "draft_text":       "...",                # LLM-drafted document body
    "gc_approved_at":   1780000123,           # owner approved the draft
    "gc_approved_by":   "frank",
    "sent_at":          1780000200,           # sent to client for sig
    "sent_via":         "email" | "sms" | "in_person",
    "client_signer":    "Sarah Johnson",
    "client_signed_at": 1780100000,
    "signed_doc_path":  "/data/.../co_abc123.pdf",
    "status":           "draft" | "awaiting_approval" | "approved" |
                        "sent_for_signature" | "signed" | "rejected" |
                        "cancelled",
    "rejected_reason":  "",                   # if status == rejected
    "created_at":       1780000000,
    "updated_at":       1780000123
  }

The "leak alarm" works by checking, periodically:
   projects.list_active() →
   for each: total amount of work logged this week (from daily logs or
   chat mentions in a future build) vs (project.contract_amount +
   sum(co.amount where status='signed')).
If logged > signed_total + tolerance, flag it.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()
FILE = "change_orders.json"

VALID_STATUSES = {
    "draft",                # foreman raised it, Orbi composed it, GC hasn't seen it
    "awaiting_approval",    # GC has it in their dashboard queue
    "approved",              # GC said go, but not sent to client yet
    "sent_for_signature",    # at the client for e-signature
    "signed",                # signed by client — counts toward contract total
    "rejected",              # client declined to sign
    "cancelled",             # GC withdrew it
}


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


def _save(data_dir: Path, cos: list[dict]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cos, indent=2), encoding="utf-8")
    tmp.replace(p)


def _next_co_number(cos: list[dict], project_id: str) -> int:
    existing = [c.get("co_number", 0) for c in cos
                 if c.get("project_id") == project_id]
    return (max(existing) + 1) if existing else 1


def add(data_dir: Path, *,
        project_id: str, description: str,
        amount: float, scope_detail: str = "",
        draft_text: str = "",
        status: str = "draft") -> dict:
    """Create a CO record. Initial status is usually 'draft' (set by
    Orbi when the foreman raises it) or 'awaiting_approval' if the
    draft is being sent straight to the GC's queue."""
    project_id = (project_id or "").strip()
    description = (description or "").strip()
    if not project_id:
        raise ValueError("project_id required")
    if not description:
        raise ValueError("description required")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    now = int(time.time())
    with _LOCK:
        cos = _load(data_dir)
        entry = {
            "id":                uuid.uuid4().hex[:12],
            "project_id":        project_id,
            "co_number":         _next_co_number(cos, project_id),
            "description":       description,
            "amount":            float(amount),
            "scope_detail":      (scope_detail or "").strip(),
            "draft_text":        draft_text or "",
            "gc_approved_at":    None,
            "gc_approved_by":    "",
            "sent_at":           None,
            "sent_via":          "",
            "client_signer":     "",
            "client_signed_at":  None,
            "signed_doc_path":   "",
            "status":            status,
            "rejected_reason":   "",
            "created_at":        now,
            "updated_at":        now,
        }
        cos.append(entry)
        _save(data_dir, cos)
    return entry


def get(data_dir: Path, co_id: str) -> dict | None:
    for c in _load(data_dir):
        if c.get("id") == co_id:
            return c
    return None


def list_for_project(data_dir: Path, project_id: str,
                      statuses: list[str] | None = None) -> list[dict]:
    cos = [c for c in _load(data_dir) if c.get("project_id") == project_id]
    if statuses:
        s = set(statuses)
        cos = [c for c in cos if c.get("status") in s]
    cos.sort(key=lambda c: c.get("co_number", 0))
    return cos


def list_pending_approval(data_dir: Path) -> list[dict]:
    """COs sitting in the GC's queue waiting for them to approve the
    draft. Surfaced in the morning brief so they don't get forgotten."""
    return [c for c in _load(data_dir) if c.get("status") == "awaiting_approval"]


def list_awaiting_signature(data_dir: Path) -> list[dict]:
    """Sent to client for e-sign but not yet signed. Surfaced so we can
    nudge slow signers."""
    return [c for c in _load(data_dir) if c.get("status") == "sent_for_signature"]


def update(data_dir: Path, co_id: str, **changes) -> dict | None:
    with _LOCK:
        cos = _load(data_dir)
        for c in cos:
            if c.get("id") == co_id:
                for k, v in changes.items():
                    if k in ("id", "created_at", "project_id", "co_number"):
                        continue
                    if k == "status" and v not in VALID_STATUSES:
                        raise ValueError(f"invalid status: {v}")
                    c[k] = v
                c["updated_at"] = int(time.time())
                _save(data_dir, cos)
                return c
    return None


def mark_approved(data_dir: Path, co_id: str, gc_username: str) -> dict | None:
    """GC approved the draft — ready to send to client. The send action
    itself is a separate call (mark_sent_for_signature) because the GC
    might approve now but send later."""
    return update(data_dir, co_id,
                   status="approved",
                   gc_approved_at=int(time.time()),
                   gc_approved_by=gc_username)


def mark_sent_for_signature(data_dir: Path, co_id: str,
                              via: str = "email") -> dict | None:
    return update(data_dir, co_id,
                   status="sent_for_signature",
                   sent_at=int(time.time()),
                   sent_via=via)


def mark_signed(data_dir: Path, co_id: str, *,
                 signer_name: str, signed_doc_path: str = "") -> dict | None:
    """Client signed — this is the moment the CO counts toward the
    project's contract total. The handler that calls this should ALSO
    trigger the notification to the office + the audit log entry."""
    return update(data_dir, co_id,
                   status="signed",
                   client_signer=signer_name,
                   client_signed_at=int(time.time()),
                   signed_doc_path=signed_doc_path)


def mark_rejected(data_dir: Path, co_id: str, reason: str = "") -> dict | None:
    return update(data_dir, co_id,
                   status="rejected",
                   rejected_reason=(reason or "").strip())


def signed_total_for_project(data_dir: Path, project_id: str) -> float:
    """Sum of amounts on COs the client actually signed. This is what
    you add to the project's original contract_amount to get the
    current authorized total."""
    total = 0.0
    for c in _load(data_dir):
        if c.get("project_id") == project_id and c.get("status") == "signed":
            total += float(c.get("amount") or 0)
    return total


def summary(data_dir: Path) -> dict:
    """For the reporting view (Section 5.4) — the "look what I saved
    you" screen. Captured = signed CO dollars Orbi has helped land."""
    cos = _load(data_dir)
    captured = sum(float(c.get("amount") or 0)
                    for c in cos if c.get("status") == "signed")
    pending = sum(float(c.get("amount") or 0)
                   for c in cos if c.get("status") in
                   ("awaiting_approval", "approved", "sent_for_signature"))
    rejected = sum(float(c.get("amount") or 0)
                    for c in cos if c.get("status") == "rejected")
    by_status: dict[str, int] = {}
    for c in cos:
        s = c.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "total_cos":         len(cos),
        "by_status":         by_status,
        "captured_dollars":  captured,
        "pending_dollars":   pending,
        "rejected_dollars":  rejected,
    }
