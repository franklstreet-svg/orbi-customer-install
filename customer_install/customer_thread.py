"""
customer_thread — one unified timeline per contact.

Given a contact (by id, email, phone, or name), walks every reachable
source (messages, calendar, notes, Stripe, Gmail, Outlook) and returns a
single newest-first list of events plus a one-line human summary.

This is the "tap on Joe Smith, see everything you've ever done together"
view — the small-business equivalent of opening a CRM card.

DESIGN NOTES
------------
* Contact resolution is *cascading*: try ID, then exact email, then
  exact phone (normalized to digits), then case-insensitive exact name,
  then fuzzy (difflib) name. First hit wins. This keeps "joe" -> "Joe
  Smith" predictable while still finding "John smith" when the owner
  types "john smith ".

* Every source is wrapped in its own try/except. One blown source (a
  bad Stripe key, an expired Gmail token, a corrupt notes.json) NEVER
  takes down the timeline. Failures are logged and the corresponding
  bucket comes back empty.

* Connectors only run if `inst.is_connected()` to avoid round-trips and
  unauthorized log spam.

* Each event normalizes to {kind, ts, title, snippet, source} with the
  same shape regardless of origin, so the dashboard renders one row
  type. `ts` is ISO-8601 UTC where possible; for items that only have
  a unix timestamp we convert.

* `list_top_contacts` powers the dashboard "recent customers" rail —
  it's just contacts sorted by last_contact, with an interaction count
  computed from messages.
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.customer_thread")


# ── Public API ──────────────────────────────────────────────────────────


def build_thread(config: dict, data_dir: Path, user_dir: Path,
                 contact_id_or_name_or_email: str) -> dict:
    """Build a unified timeline for one contact.

    Returns:
        {
          "contact": {...} | None,
          "events":  [ {kind, ts, title, snippet, source}, ... ],   # newest first
          "summary": "Joe Smith — 14 interactions, last contact 2 days ago, $1,250 paid total"
        }
    """
    contact = _resolve_contact(user_dir, contact_id_or_name_or_email)
    if not contact:
        return {
            "contact": None,
            "events":  [],
            "summary": f"No contact matched '{contact_id_or_name_or_email}'.",
        }

    events: list[dict] = []
    payment_total = 0.0

    # Each source independently — one failure must not kill the thread.
    try:
        events.extend(_from_messages(data_dir, contact))
    except Exception as e:
        log.warning("customer_thread.messages failed: %s", e)

    try:
        events.extend(_from_calendar(user_dir, contact))
    except Exception as e:
        log.warning("customer_thread.calendar failed: %s", e)

    try:
        events.extend(_from_notes(data_dir, contact))
    except Exception as e:
        log.warning("customer_thread.notes failed: %s", e)

    try:
        pay_events, payment_total = _from_stripe(config, user_dir, contact)
        events.extend(pay_events)
    except Exception as e:
        log.warning("customer_thread.stripe failed: %s", e)

    try:
        events.extend(_from_gmail(config, user_dir, contact))
    except Exception as e:
        log.warning("customer_thread.gmail failed: %s", e)

    try:
        events.extend(_from_outlook(config, user_dir, contact))
    except Exception as e:
        log.warning("customer_thread.outlook failed: %s", e)

    # Newest first — string ISO sort works for ISO-8601 in UTC.
    events.sort(key=lambda e: e.get("ts", "") or "", reverse=True)

    return {
        "contact": contact,
        "events":  events,
        "summary": _build_summary(contact, events, payment_total),
    }


def list_top_contacts(config: dict, data_dir: Path, user_dir: Path,
                      limit: int = 10) -> list[dict]:
    """Most-recently-interacted contacts with an interaction count.

    Returns list of {id, name, phone, email, company, last_contact,
    interaction_count} — sorted newest-first by last_contact.

    Interaction count is a cheap heuristic: number of messages from
    that contact. (Gmail/Outlook/Stripe aren't counted because they'd
    require a round-trip per contact — too slow for the dashboard.)
    """
    try:
        from modules import contacts as mod_contacts
        contacts = mod_contacts.list_all(user_dir) or []
    except Exception as e:
        log.warning("list_top_contacts: contacts load failed: %s", e)
        return []

    # Pull all messages once and bucket by contact.
    msg_counts: dict[str, int] = {}
    try:
        from modules import messages as mod_messages
        for m in mod_messages.list_all(data_dir, limit=1000) or []:
            key = _msg_match_key(m)
            if key:
                msg_counts[key] = msg_counts.get(key, 0) + 1
    except Exception as e:
        log.info("list_top_contacts: messages count failed: %s", e)

    out = []
    for c in contacts:
        count = 0
        for key in _contact_keys(c):
            count += msg_counts.get(key, 0)
        out.append({
            "id":                c.get("id", ""),
            "name":              c.get("name", ""),
            "phone":             c.get("phone", ""),
            "email":             c.get("email", ""),
            "company":           c.get("company", ""),
            "last_contact":      c.get("last_contact", ""),
            "interaction_count": count,
        })

    # Sort by last_contact desc (empty strings sort last).
    out.sort(key=lambda c: c.get("last_contact", "") or "", reverse=True)
    return out[:max(1, int(limit or 10))]


# ── Contact resolution ─────────────────────────────────────────────────


def _resolve_contact(user_dir: Path, needle: str) -> dict | None:
    """ID -> email -> phone -> exact name -> fuzzy name. First hit wins."""
    if not needle or not str(needle).strip():
        return None
    n = str(needle).strip()

    try:
        from modules import contacts as mod_contacts
        contacts = mod_contacts.list_all(user_dir) or []
    except Exception as e:
        log.warning("_resolve_contact: contacts load failed: %s", e)
        return None

    # 1. ID match (exact).
    for c in contacts:
        if c.get("id") == n:
            return c

    # 2. Email match (case-insensitive exact).
    nl = n.lower()
    if "@" in n:
        for c in contacts:
            if (c.get("email") or "").lower() == nl:
                return c

    # 3. Phone match (digits-only compare).
    n_digits = _digits_only(n)
    if n_digits and len(n_digits) >= 7:
        for c in contacts:
            cdig = _digits_only(c.get("phone", ""))
            if cdig and (cdig == n_digits or cdig.endswith(n_digits)
                         or n_digits.endswith(cdig)):
                return c

    # 4. Exact name (case-insensitive).
    for c in contacts:
        if (c.get("name") or "").lower() == nl:
            return c

    # 5. Fuzzy name — difflib ratio against name, cutoff 0.72. Prefer
    # contacts whose name CONTAINS the needle as a substring first
    # (that's how a human would expect "joe" -> "Joe Smith" to behave).
    for c in contacts:
        cname = (c.get("name") or "").lower()
        if cname and nl in cname:
            return c

    names_lower = [(c, (c.get("name") or "").lower()) for c in contacts]
    best = None
    best_score = 0.0
    for c, cname in names_lower:
        if not cname:
            continue
        score = difflib.SequenceMatcher(None, nl, cname).ratio()
        if score > best_score:
            best_score = score
            best = c
    if best and best_score >= 0.72:
        return best

    return None


# ── Per-source loaders ─────────────────────────────────────────────────


def _from_messages(data_dir: Path, contact: dict) -> list[dict]:
    from modules import messages as mod_messages
    out = []
    keys = _contact_keys(contact)
    name_lc = (contact.get("name") or "").lower().strip()
    for m in mod_messages.list_all(data_dir, limit=2000) or []:
        if _msg_match_key(m) in keys or (name_lc and
                _matches_name(m.get("from_name"), name_lc)):
            body = m.get("body") or ""
            mtype = (m.get("type") or "message").lower()
            kind = "call" if mtype in ("voicemail", "callback") else "message"
            out.append({
                "kind":    kind,
                "ts":      _unix_to_iso(m.get("timestamp")),
                "title":   _msg_title(m),
                "snippet": body[:240],
                "source":  "messages",
            })
    return out


def _from_calendar(user_dir: Path, contact: dict) -> list[dict]:
    from modules import calendar as mod_calendar
    name_lc = (contact.get("name") or "").lower().strip()
    if not name_lc:
        return []
    out = []
    for e in mod_calendar.list_all(user_dir) or []:
        attendees = e.get("with") or []
        if any(name_lc in (a or "").lower() for a in attendees):
            out.append({
                "kind":    "calendar",
                "ts":      e.get("start", "") or "",
                "title":   e.get("title", "") or "(untitled event)",
                "snippet": (e.get("notes") or
                            e.get("location") or "")[:240],
                "source":  "calendar",
            })
    return out


def _from_notes(data_dir: Path, contact: dict) -> list[dict]:
    from modules import notes as mod_notes
    name_lc = (contact.get("name") or "").lower().strip()
    if not name_lc:
        return []
    out = []
    for n in mod_notes.list_all(data_dir) or []:
        content = (n.get("content") or "")
        if name_lc in content.lower():
            out.append({
                "kind":    "note",
                "ts":      _unix_to_iso(n.get("ts")),
                "title":   content[:80],
                "snippet": content[:240],
                "source":  "notes",
            })
    return out


def _from_stripe(config: dict, user_dir: Path, contact: dict) -> tuple[list[dict], float]:
    """Past payments. Returns (events, total_paid_dollars)."""
    email = (contact.get("email") or "").strip()
    if not email:
        return [], 0.0

    inst = _connector_instance("stripe", config, user_dir)
    if inst is None:
        return [], 0.0

    customers = inst.find_customer(email) or []
    if not customers:
        return [], 0.0

    out: list[dict] = []
    total = 0.0
    # Pull a window of recent payments and bucket those matching our
    # customer email — simpler than per-customer charge listing and
    # mirrors how universal_search uses the connector.
    try:
        payments = inst.list_recent_payments(limit=100) or []
    except Exception as e:
        log.info("stripe.list_recent_payments failed: %s", e)
        payments = []

    email_lc = email.lower()
    for p in payments:
        pemail = (p.get("customer_email") or "").lower()
        if pemail and pemail == email_lc:
            amt = float(p.get("amount_dollars", 0) or 0)
            total += amt
            out.append({
                "kind":    "payment",
                "ts":      p.get("created_iso", "") or "",
                "title":   f"Paid ${amt:,.2f}",
                "snippet": (p.get("description") or "")[:240],
                "source":  "stripe",
            })
    return out, round(total, 2)


def _from_gmail(config: dict, user_dir: Path, contact: dict) -> list[dict]:
    email = (contact.get("email") or "").strip()
    if not email:
        return []
    inst = _connector_instance("gmail", config, user_dir)
    if inst is None:
        return []
    msgs = inst.search(email, limit=30) or []
    out = []
    for m in msgs:
        out.append({
            "kind":    "email",
            "ts":      m.get("date", "") or "",
            "title":   (m.get("subject") or "(no subject)")[:120],
            "snippet": (m.get("snippet") or m.get("from") or "")[:240],
            "source":  "gmail",
        })
    return out


def _from_outlook(config: dict, user_dir: Path, contact: dict) -> list[dict]:
    email = (contact.get("email") or "").strip()
    if not email:
        return []
    inst = _connector_instance("outlook", config, user_dir)
    if inst is None:
        return []
    msgs = inst.search(email, limit=30) or []
    out = []
    for m in msgs:
        out.append({
            "kind":    "email",
            "ts":      m.get("received_iso", "") or m.get("date", "") or "",
            "title":   (m.get("subject") or "(no subject)")[:120],
            "snippet": (m.get("snippet") or m.get("bodyPreview")
                        or m.get("from") or "")[:240],
            "source":  "outlook",
        })
    return out


# ── Connector access ───────────────────────────────────────────────────


def _connector_instance(connector_id: str, config: dict, user_dir: Path):
    """Return a connected connector or None. Tolerates missing modules."""
    try:
        from connectors.base import get_instance, import_all
        import_all()
        inst = get_instance(connector_id, config, user_dir)
    except Exception as e:
        log.info("connector %s instantiate failed: %s", connector_id, e)
        return None
    if inst is None:
        return None
    try:
        if not inst.is_connected():
            return None
    except Exception:
        return None
    return inst


# ── Helpers ────────────────────────────────────────────────────────────


def _digits_only(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def _contact_keys(contact: dict) -> set[str]:
    """All match keys for a contact, used for fast set lookup against
    incoming messages."""
    keys: set[str] = set()
    name = (contact.get("name") or "").lower().strip()
    if name:
        keys.add(f"name:{name}")
    email = (contact.get("email") or "").lower().strip()
    if email:
        keys.add(f"email:{email}")
    phone = _digits_only(contact.get("phone") or "")
    if phone:
        keys.add(f"phone:{phone}")
    return keys


def _msg_match_key(m: dict) -> str:
    """Pick the strongest identifier on a message (email > phone > name)."""
    email = (m.get("from_email") or "").lower().strip()
    if email:
        return f"email:{email}"
    phone = _digits_only(m.get("from_phone") or "")
    if phone:
        return f"phone:{phone}"
    name = (m.get("from_name") or "").lower().strip()
    if name:
        return f"name:{name}"
    return ""


def _matches_name(msg_name: str | None, name_lc: str) -> bool:
    if not msg_name:
        return False
    mn = msg_name.lower().strip()
    if not mn:
        return False
    return name_lc == mn or name_lc in mn or mn in name_lc


def _msg_title(m: dict) -> str:
    mtype = (m.get("type") or "message").title()
    who   = m.get("from_name") or m.get("from_email") or m.get("from_phone") or "Unknown"
    return f"{mtype} from {who}"


def _unix_to_iso(ts) -> str:
    """Convert a unix float/int (or already-ISO string) to ISO-8601 UTC."""
    if not ts:
        return ""
    if isinstance(ts, str):
        return ts
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _build_summary(contact: dict, events: list[dict], payment_total: float) -> str:
    name = contact.get("name") or contact.get("email") or contact.get("phone") or "Contact"
    count = len(events)

    # "last contact" — newest event ts (events are already sorted desc).
    last_ts = events[0]["ts"] if events else (contact.get("last_contact") or "")
    rel = _relative_days(last_ts)

    parts = [f"{name} — {count} interaction{'s' if count != 1 else ''}"]
    if rel:
        parts.append(f"last contact {rel}")
    if payment_total > 0:
        parts.append(f"${payment_total:,.2f} paid total")
    return ", ".join(parts)


def _relative_days(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    try:
        # Accept both ISO with Z and full ISO.
        s = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt
    days = delta.days
    if days < 0:
        # Future event — describe as "in N days".
        d = abs(days)
        if d == 0:
            return "later today"
        if d == 1:
            return "tomorrow"
        return f"in {d} days"
    if days == 0:
        hours = int(delta.total_seconds() // 3600)
        if hours <= 0:
            return "just now"
        if hours == 1:
            return "1 hour ago"
        return f"{hours} hours ago"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. config is the loaded config.json.
#
#   GET  /api/owner/customer_thread/<contact_id>
#       calls:  build_thread(config, data_dir, user_dir, contact_id)
#       returns:{contact, events:[...], summary}
#       errors: 404 if contact not found
#
#   GET  /api/owner/customer_thread?email=joe@example.com
#       calls:  build_thread(config, data_dir, user_dir, request.args["email"])
#       returns:{contact, events, summary}
#
#   GET  /api/owner/customer_thread?name=Joe%20Smith
#       calls:  build_thread(config, data_dir, user_dir, request.args["name"])
#       returns:{contact, events, summary}
#       (Falls through to fuzzy match when exact name misses.)
#
#   GET  /api/owner/customer_thread/top?limit=10
#       calls:  list_top_contacts(config, data_dir, user_dir, limit)
#       returns:[{id, name, phone, email, company, last_contact,
#                 interaction_count}, ...]
#
# ---------------------------------------------------------------------------
