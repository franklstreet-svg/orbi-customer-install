"""
modules/invoices — contractor invoicing + receivables tracking.

Part of the Contractor module. Per Frank's blueprint Section 5.3:

  1. Orbi tracks each job's invoices and draws with due dates locally.
  2. Generates invoices from job + change-order data when a milestone /
     draw is due; GC approves; Orbi sends.
  3. Monitors payment status; on overdue, auto-sends polite escalating
     follow-up.
  4. Maintains an aging view surfaced in the morning brief.
  5. Tracks retainage held so it actually gets collected at closeout.

This module is the data layer + status machine. Invoice-PDF generation,
the email/Stripe send, and the follow-up scheduler live in orbi.py
handlers — same pattern as change_orders.

Invoice shape:
  {
    "id":               "12-char hex",
    "project_id":       "12-char hex",
    "invoice_number":   "INV-2026-0042",     # human-readable per-customer
    "draw_number":      3,                    # 1-indexed if this is a draw
    "line_items": [
       {"label": "Framing labor", "qty": 1, "rate": 4200.00, "amount": 4200.00},
       {"label": "Lumber",        "qty": 1, "rate": 1850.00, "amount": 1850.00}
    ],
    "subtotal":         6050.00,
    "retainage_pct":    10.0,                 # held back %, billed at closeout
    "retainage_held":   605.00,               # = subtotal * retainage_pct/100
    "amount_due":       5445.00,              # = subtotal - retainage_held
    "amount_paid":      0.00,
    "currency":         "USD",
    "memo":             "Progress draw #3 — framing complete",
    "issued_at":        1780000000,
    "sent_at":          null,
    "viewed_at":        null,
    "due_at":           1781000000,
    "paid_at":          null,
    "stripe_session_id":"",                   # if collected via Stripe
    "follow_up_count":  0,                    # how many nudges sent
    "last_follow_up_at":null,
    "status":           "draft" | "approved" | "sent" | "viewed" |
                        "partial" | "paid" | "overdue" | "void",
    "created_at":       1780000000,
    "updated_at":       1780000123
  }

Status machine:
   draft → approved → sent → viewed → paid
                          ↘ overdue (after due_at, status auto-promotes)
                          ↘ partial (if amount_paid > 0 but < amount_due)
   any → void (manual)

Retainage:
   retainage_held accumulates across all invoices on a project. When
   the project hits status='completed', a final "retainage release"
   invoice can be generated for the sum of retainage_held across the
   project's invoices.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()
FILE = "invoices.json"

VALID_STATUSES = {"draft", "approved", "sent", "viewed", "partial",
                   "paid", "overdue", "void"}


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


def _save(data_dir: Path, invs: list[dict]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(invs, indent=2), encoding="utf-8")
    tmp.replace(p)


def _next_invoice_number(invs: list[dict]) -> str:
    """Per-customer human-readable counter, format INV-YYYY-NNNN. Fine
    for v1; the formatting can be moved to a config setting later if a
    contractor wants a different style."""
    year = datetime.now(timezone.utc).year
    same_year = [i for i in invs if str(i.get("invoice_number", "")).startswith(f"INV-{year}-")]
    if not same_year:
        return f"INV-{year}-0001"
    nums = []
    for i in same_year:
        try:
            nums.append(int(str(i["invoice_number"]).rsplit("-", 1)[-1]))
        except (ValueError, KeyError, IndexError):
            continue
    n = (max(nums) + 1) if nums else len(same_year) + 1
    return f"INV-{year}-{n:04d}"


def _next_draw_for_project(invs: list[dict], project_id: str) -> int:
    nums = [i.get("draw_number", 0) for i in invs
             if i.get("project_id") == project_id and i.get("draw_number")]
    return (max(nums) + 1) if nums else 1


def add(data_dir: Path, *,
        project_id: str,
        line_items: list[dict] | None = None,
        subtotal: float | None = None,
        retainage_pct: float = 0.0,
        memo: str = "",
        due_at: int | None = None,
        is_draw: bool = False,
        status: str = "draft") -> dict:
    """Create an invoice. line_items each must have {label, amount}; qty
    and rate are optional. If subtotal is not given, computed from
    line_items. retainage_pct is the % held back; retainage_held =
    subtotal * retainage_pct / 100. amount_due = subtotal - retainage_held."""
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id required")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    items = list(line_items or [])
    for it in items:
        if "amount" not in it:
            raise ValueError("each line_item needs an 'amount'")
        it["amount"] = float(it.get("amount", 0))
        it.setdefault("qty", 1)
        it.setdefault("rate", it["amount"])
        it.setdefault("label", "")
    if subtotal is None:
        subtotal = sum(float(it["amount"]) for it in items)
    else:
        subtotal = float(subtotal)
    retainage_pct = float(retainage_pct or 0)
    retainage_held = round(subtotal * retainage_pct / 100, 2)
    amount_due = round(subtotal - retainage_held, 2)
    now = int(time.time())
    with _LOCK:
        invs = _load(data_dir)
        entry = {
            "id":                uuid.uuid4().hex[:12],
            "project_id":        project_id,
            "invoice_number":    _next_invoice_number(invs),
            "draw_number":       _next_draw_for_project(invs, project_id) if is_draw else None,
            "line_items":        items,
            "subtotal":          subtotal,
            "retainage_pct":     retainage_pct,
            "retainage_held":    retainage_held,
            "amount_due":        amount_due,
            "amount_paid":       0.0,
            "currency":          "USD",
            "memo":              (memo or "").strip(),
            "issued_at":         now,
            "sent_at":           None,
            "viewed_at":         None,
            "due_at":            due_at,
            "paid_at":           None,
            "stripe_session_id": "",
            "follow_up_count":   0,
            "last_follow_up_at": None,
            "status":            status,
            "created_at":        now,
            "updated_at":        now,
        }
        invs.append(entry)
        _save(data_dir, invs)
    return entry


def get(data_dir: Path, invoice_id: str) -> dict | None:
    for i in _load(data_dir):
        if i.get("id") == invoice_id:
            return i
    return None


def list_for_project(data_dir: Path, project_id: str) -> list[dict]:
    return sorted(
        [i for i in _load(data_dir) if i.get("project_id") == project_id],
        key=lambda i: i.get("issued_at", 0),
    )


def list_unpaid(data_dir: Path) -> list[dict]:
    """All invoices not in {paid, void} — i.e. money still owed."""
    return [i for i in _load(data_dir)
            if i.get("status") not in ("paid", "void")
            and i.get("status") != "draft"]


def list_overdue(data_dir: Path) -> list[dict]:
    """Invoices past their due_at that aren't paid. Promotes status to
    'overdue' if it hasn't been already. This is what the
    receivables-follow-up scheduler iterates."""
    now = int(time.time())
    overdue = []
    with _LOCK:
        invs = _load(data_dir)
        changed = False
        for i in invs:
            due = i.get("due_at")
            if not due:
                continue
            if i.get("status") in ("paid", "void", "draft"):
                continue
            if int(due) < now:
                if i.get("status") != "overdue":
                    i["status"] = "overdue"
                    i["updated_at"] = now
                    changed = True
                overdue.append(i)
        if changed:
            _save(data_dir, invs)
    return overdue


def aging_buckets(data_dir: Path) -> dict:
    """Group unpaid invoices into 0-30 / 31-60 / 61-90 / 90+ days
    past due. The receivables view in the morning brief uses this."""
    now = int(time.time())
    buckets = {"current": 0.0, "1-30": 0.0, "31-60": 0.0,
               "61-90": 0.0, "90+": 0.0}
    for i in _load(data_dir):
        if i.get("status") in ("paid", "void", "draft"):
            continue
        outstanding = float(i.get("amount_due", 0)) - float(i.get("amount_paid", 0))
        if outstanding <= 0:
            continue
        due = i.get("due_at")
        if not due:
            buckets["current"] += outstanding
            continue
        days_late = (now - int(due)) // 86400
        if days_late <= 0:
            buckets["current"] += outstanding
        elif days_late <= 30:
            buckets["1-30"] += outstanding
        elif days_late <= 60:
            buckets["31-60"] += outstanding
        elif days_late <= 90:
            buckets["61-90"] += outstanding
        else:
            buckets["90+"] += outstanding
    return {k: round(v, 2) for k, v in buckets.items()}


def update(data_dir: Path, invoice_id: str, **changes) -> dict | None:
    with _LOCK:
        invs = _load(data_dir)
        for i in invs:
            if i.get("id") == invoice_id:
                for k, v in changes.items():
                    if k in ("id", "created_at", "project_id", "invoice_number"):
                        continue
                    if k == "status" and v not in VALID_STATUSES:
                        raise ValueError(f"invalid status: {v}")
                    i[k] = v
                i["updated_at"] = int(time.time())
                _save(data_dir, invs)
                return i
    return None


def mark_sent(data_dir: Path, invoice_id: str) -> dict | None:
    return update(data_dir, invoice_id,
                   status="sent", sent_at=int(time.time()))


def mark_viewed(data_dir: Path, invoice_id: str) -> dict | None:
    """Called by a tracking pixel or Stripe-hosted-invoice view event.
    Only bumps status if currently 'sent' — doesn't downgrade paid."""
    inv = get(data_dir, invoice_id)
    if not inv:
        return None
    update_kwargs = {"viewed_at": int(time.time())}
    if inv.get("status") == "sent":
        update_kwargs["status"] = "viewed"
    return update(data_dir, invoice_id, **update_kwargs)


def record_payment(data_dir: Path, invoice_id: str, amount: float,
                    stripe_session_id: str = "") -> dict | None:
    """Apply a payment. amount is the dollars received this transaction.
    Status promotes to 'paid' if amount_paid >= amount_due, else 'partial'."""
    with _LOCK:
        invs = _load(data_dir)
        for i in invs:
            if i.get("id") == invoice_id:
                i["amount_paid"] = round(float(i.get("amount_paid", 0)) + float(amount), 2)
                if stripe_session_id:
                    i["stripe_session_id"] = stripe_session_id
                if i["amount_paid"] >= float(i.get("amount_due", 0)):
                    i["status"] = "paid"
                    i["paid_at"] = int(time.time())
                else:
                    i["status"] = "partial"
                i["updated_at"] = int(time.time())
                _save(data_dir, invs)
                return i
    return None


def mark_followed_up(data_dir: Path, invoice_id: str) -> dict | None:
    """Called by the receivables auto-follow-up scheduler after it
    sends a nudge. Bumps the counter so escalation logic knows whether
    to send the friendly nudge, the firm one, or the legal-warning one."""
    inv = get(data_dir, invoice_id)
    if not inv:
        return None
    return update(data_dir, invoice_id,
                   follow_up_count=int(inv.get("follow_up_count", 0)) + 1,
                   last_follow_up_at=int(time.time()))


def retainage_held_for_project(data_dir: Path, project_id: str) -> float:
    """Sum of retainage_held across all of a project's invoices that
    haven't already been released. Used to generate the closeout
    retainage release invoice when the project is marked completed."""
    total = 0.0
    for i in _load(data_dir):
        if i.get("project_id") != project_id:
            continue
        # Only count retainage from invoices that have actually been
        # collected (paid) — un-billed retainage doesn't get released.
        if i.get("status") not in ("paid", "partial"):
            continue
        total += float(i.get("retainage_held") or 0)
    return round(total, 2)


def summary(data_dir: Path) -> dict:
    """Top-level numbers for the reporting view + morning brief."""
    invs = _load(data_dir)
    total_billed = sum(float(i.get("amount_due", 0)) for i in invs
                        if i.get("status") not in ("draft", "void"))
    total_collected = sum(float(i.get("amount_paid", 0)) for i in invs)
    total_outstanding = round(total_billed - total_collected, 2)
    return {
        "total_invoices":    len(invs),
        "total_billed":      round(total_billed, 2),
        "total_collected":   round(total_collected, 2),
        "total_outstanding": total_outstanding,
        "aging":             aging_buckets(data_dir),
    }
