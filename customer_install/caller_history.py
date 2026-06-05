"""
caller_history.py — per-business per-caller order history.

When a customer calls a restaurant for the second time, Orby looks up their
phone number in this store and remembers them by name + recalls their last
order. That's the "Oh hi Frank, thanks for calling again — want the usual?"
moment that makes Orby feel less like a robot and more like a familiar face.

Storage:
    data/customer_profiles/<business_slug>/callers/<phone_digits>.json

Each caller file looks like:
    {
      "phone_e164": "+17755280574",
      "phone_display": "775-528-0574",
      "first_seen": "2026-06-01T15:42:00",
      "last_seen":  "2026-06-04T18:30:00",
      "call_count": 3,
      "remembered_name": "Frank",
      "orders": [
        {
          "date": "2026-06-01T15:42:00",
          "order_id": "Order #abc123",
          "cart": [...],
          "summary": "1x Truckee Italian (12-inch, toasted, extra meat,
                      no onion), 1x House Cookie Box",
          "pickup_time": "in 15 minutes",
          "total": "$30.20"
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("orbi.caller_history")

_MAX_ORDERS_KEPT = 20  # most recent N orders per caller


def _digits_only(phone: str) -> str:
    """Normalize +17755280574 → 7755280574 for use as filename."""
    if not phone:
        return ""
    d = re.sub(r"\D", "", phone)
    if d.startswith("1") and len(d) == 11:
        d = d[1:]
    return d


def _callers_dir(data_dir: Path, business_slug: str) -> Path:
    p = Path(data_dir) / "customer_profiles" / business_slug / "callers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _caller_path(data_dir: Path, business_slug: str, phone: str) -> Optional[Path]:
    digits = _digits_only(phone)
    if not digits:
        return None
    return _callers_dir(data_dir, business_slug) / f"{digits}.json"


def load(data_dir: Path, business_slug: str, phone: str) -> Optional[dict]:
    """Return the caller record if it exists, else None."""
    fpath = _caller_path(data_dir, business_slug, phone)
    if fpath is None or not fpath.exists():
        return None
    try:
        return json.loads(fpath.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"load failed for {fpath}: {e}")
        return None


def upsert_order(data_dir: Path, business_slug: str, *,
                  caller_phone: str, caller_name: str,
                  order_id: str, cart: list[dict],
                  pickup_time: str, total: str,
                  summary: str = "") -> dict:
    """Record this order against the caller's history. Creates the file
    if it doesn't exist, appends the order, updates last_seen + count.
    Trims orders list to the most recent N."""
    fpath = _caller_path(data_dir, business_slug, caller_phone)
    if fpath is None:
        return {}

    now_iso = datetime.utcnow().replace(microsecond=0).isoformat()
    record = load(data_dir, business_slug, caller_phone) or {
        "phone_e164": caller_phone,
        "phone_display": _format_us(caller_phone),
        "first_seen": now_iso,
        "call_count": 0,
        "orders": [],
    }

    record["last_seen"] = now_iso
    record["call_count"] = int(record.get("call_count", 0)) + 1
    if caller_name and not record.get("remembered_name"):
        record["remembered_name"] = caller_name
    elif caller_name:
        # If they gave a different name this time, prefer the most recent
        record["remembered_name"] = caller_name

    order_entry = {
        "date": now_iso,
        "order_id": order_id or "",
        "cart": cart or [],
        "summary": summary or "",
        "pickup_time": pickup_time or "",
        "total": total or "",
    }
    record.setdefault("orders", []).append(order_entry)
    # Trim to most recent N
    if len(record["orders"]) > _MAX_ORDERS_KEPT:
        record["orders"] = record["orders"][-_MAX_ORDERS_KEPT:]

    try:
        tmp = fpath.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp.replace(fpath)
    except Exception as e:
        log.warning(f"write failed for {fpath}: {e}")
    return record


def _format_us(phone: str) -> str:
    d = _digits_only(phone)
    if len(d) == 10:
        return f"{d[0:3]}-{d[3:6]}-{d[6:10]}"
    return phone or ""


def last_order_summary(record: dict) -> str:
    """Pull the most recent order summary for prompt injection."""
    orders = (record or {}).get("orders") or []
    if not orders:
        return ""
    return orders[-1].get("summary", "")


def build_greeting_for_returning_caller(business_name: str,
                                            record: dict) -> Optional[str]:
    """If this caller has history, produce a warm personalized greeting.
    Returns None if we should use the default greeting instead."""
    if not record:
        return None
    name = record.get("remembered_name")
    if not name:
        return None
    last = last_order_summary(record)
    if last:
        return (f"Hi, this is Orby at {business_name}. Is this {name}? "
                "Want me to start your usual?")
    return f"Hi, this is Orby at {business_name}. Is this {name}? How can I help?"


def build_prompt_context_for_caller(record: dict) -> str:
    """Inject into the LLM system prompt so Orby knows who's calling
    and what they ordered last time. Empty string if no record."""
    if not record:
        return ""
    name = record.get("remembered_name", "")
    prior_calls = int(record.get("call_count", 0))
    # The current call hasn't been counted yet (upsert_order happens at the
    # end of THIS call). So "this is their Nth call" = prior_calls + 1.
    this_call_number = prior_calls + 1
    last = last_order_summary(record)
    lines = ["RETURNING CALLER CONTEXT:"]
    if name:
        lines.append(f"- This caller's name is {name}. Use it naturally.")
    lines.append(f"- This is their {_ordinal(this_call_number)} call to this business.")
    if last:
        lines.append(f"- Their last order was: {last}")
        lines.append(
            "- If they say 'the usual' or 'same as last time', it means the "
            "above order. Re-confirm details before submitting."
        )
    lines.append("- Don't make a big production of remembering them — "
                 "be natural, like a regular at a deli would expect.")
    return "\n".join(lines)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
