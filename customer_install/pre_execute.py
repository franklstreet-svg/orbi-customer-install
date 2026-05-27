"""
pre_execute — fast local-answer pre-classifier.

Adapted from my_orby/website_server.py's _pre_execute() pattern. Same
idea, stripped to the B2B Orbi modules: catalog, business_info,
messages, notes. No personal-Orby modules (calendar/recipes/finance/
my_people/etc.).

Why: most chat questions a visitor asks have a LOCAL deterministic
answer. "What are your hours?" doesn't need to round-trip through the
LLM. We answer locally in milliseconds, save HF Inference cost, and
sound more reliable (no LLM hallucination risk on simple facts).

For LOCAL-ANSWERABLE queries the function returns (response, "direct")
and orbi.py /chat short-circuits the LLM entirely — just returns the
text to the visitor.

For DATA-LOOKUP queries the function returns (data_string, "data:<key>")
and orbi.py injects the data into the LLM context as authoritative so
the LLM only has to compose natural wording around real facts (109
tokens instead of 6,302 — the my_orby gain).

Unrecognized queries return (None, None) and /chat continues with
the normal catalog → workspace → web-search → LLM cascade.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Tuple

log = logging.getLogger("orbi.pre_execute")


# ── Public API ──────────────────────────────────────────────────────────


def pre_execute(message: str, data_dir: Path,
                business: dict | None = None) -> Tuple[str | None, str | None]:
    """Try to answer a chat message locally before any LLM call.

    Returns:
      (text, "direct")   → text is the final visitor-facing reply, no LLM needed
      (text, "data:<k>") → text is data to inject as LLM context (LLM composes)
      (None, None)       → no match, fall through to normal /chat flow
    """
    if not message:
        return (None, None)
    msg = message.strip()
    msg_l = msg.lower().rstrip("?!. ")
    business = business or {}

    # ── GREETINGS → direct reply ──────────────────────────────────────
    if _GREETING_RE.match(msg_l):
        biz_name = business.get("name", "") or "us"
        return (f"Hi! Thanks for reaching out to {biz_name}. What can I help with?",
                "direct")

    # ── TIME / DATE / DAY-OF-WEEK → direct reply ──────────────────────
    if _TIME_RE.search(msg_l):
        return (f"It's {datetime.now().strftime('%-I:%M %p')}.", "direct") \
               if hasattr(datetime, "strftime") else (None, None)
    if _DATE_RE.search(msg_l):
        return (f"Today is {datetime.now().strftime('%A, %B %-d, %Y')}.", "direct")
    if _DAY_OF_WEEK_RE.search(msg_l):
        return (f"Today is {datetime.now().strftime('%A')}.", "direct")

    # ── BUSINESS INFO direct answers (only if owner has filled in the field) ──
    # business_info has nested shapes — address is a dict, contact is a dict.
    # Flatten safely so a missing/partial dict doesn't crash.
    def _flat_addr(b):
        a = b.get("address")
        if isinstance(a, str):
            return a.strip()
        if isinstance(a, dict):
            parts = [a.get("street", ""), a.get("city", ""),
                     a.get("state", ""), a.get("zip", "")]
            return ", ".join(p.strip() for p in parts if p and p.strip())
        return ""
    def _flat_str(b, *keys):
        """Look up keys.0 directly OR nested under 'contact'.{key}."""
        for k in keys:
            v = b.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        contact = b.get("contact") or {}
        if isinstance(contact, dict):
            for k in keys:
                v = contact.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    biz_addr    = _flat_addr(business)
    biz_phone   = _flat_str(business, "phone")
    biz_email   = _flat_str(business, "email")
    biz_website = _flat_str(business, "website")

    if biz_addr and _ADDRESS_RE.search(msg_l):
        return (f"We're at {biz_addr}.", "direct")
    if biz_phone and _PHONE_RE.search(msg_l):
        return (f"You can reach us at {biz_phone}.", "direct")
    if biz_email and _EMAIL_RE.search(msg_l):
        return (f"You can email us at {biz_email}.", "direct")
    if biz_website and _WEBSITE_RE.search(msg_l):
        return (f"Our website is {biz_website}.", "direct")

    # ── HOURS → data lookup (LLM composes natural reply) ──────────────
    if _HOURS_RE.search(msg_l):
        hours = business.get("hours")
        if hours:
            return (_format_hours(hours), "data:hours")

    # ── SERVICES → data lookup ────────────────────────────────────────
    if _SERVICES_RE.search(msg_l):
        services = business.get("services")
        if services:
            if isinstance(services, list):
                return ("Services we offer: " + ", ".join(str(s) for s in services), "data:services")
            return (f"Services: {services}", "data:services")

    # ── CATALOG ITEM COUNT → data lookup from catalog index ───────────
    if _CATALOG_COUNT_RE.search(msg_l):
        try:
            meta = _read_json(data_dir / "Catalog" / ".index" / "meta.json", {})
            count = meta.get("item_count", 0)
            src = meta.get("source_file", "")
            if count:
                return (f"We have {count} items in our catalog (from {src}).",
                        "direct")
        except Exception as e:
            log.warning(f"catalog count lookup failed: {e}")

    # ── No local match — fall through to normal /chat flow ────────────
    return (None, None)


# ── Internal helpers ────────────────────────────────────────────────────


def _format_hours(hours) -> str:
    """Hours can be a dict {monday: '9-5', ...} or a string or a list of strings."""
    if isinstance(hours, str):
        return f"Hours: {hours}"
    if isinstance(hours, list):
        return "Hours:\n" + "\n".join(f"  {h}" for h in hours)
    if isinstance(hours, dict):
        return "Hours:\n" + "\n".join(f"  {day.title()}: {val}"
                                       for day, val in hours.items())
    return f"Hours: {hours}"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


# ── Intent regexes — kept liberal so common variations all match ────────


_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|hiya|howdy|yo|greetings|"
    r"good (?:morning|afternoon|evening|day))"
    r"(?:\s+(?:there|everyone|team|guys|all|y'?all|folks|orbi))?"
    r"[\s!.,]*$",
    re.IGNORECASE,
)

_TIME_RE = re.compile(
    r"\bwhat(?:'s|s| is) the (?:current |right )?time\b|"
    r"\bwhat time is it\b|"
    r"\bdo you (?:have|know) the time\b",
    re.IGNORECASE,
)

_DATE_RE = re.compile(
    r"\bwhat(?:'s|s| is) (?:today'?s? |the )?date\b|"
    r"\btoday'?s? date\b|"
    r"\bwhat is the date today\b",
    re.IGNORECASE,
)

_DAY_OF_WEEK_RE = re.compile(
    r"\bwhat day (?:is it|is today|of the week)\b|"
    r"\bwhat'?s? today\b",
    re.IGNORECASE,
)

_ADDRESS_RE = re.compile(
    r"\bwhere (?:are you (?:located|at)?|do you (?:guys|all)? (?:work|operate))\b|"
    r"\bwhat(?:'s|s| is) (?:your |the )?address\b|"
    r"\baddress(?:\?|$)|"
    r"\bhow do i find you\b|"
    r"\bwhere can i find you\b",
    re.IGNORECASE,
)

_PHONE_RE = re.compile(
    r"\bwhat(?:'s|s| is) (?:your |the )?(?:phone |phone number|number)\b|"
    r"\bhow (?:do|can) i (?:call|phone|reach) you\b|"
    r"\bphone number(?:\?|$)|"
    r"\byour number(?:\?|$)",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(
    r"\bwhat(?:'s|s| is) (?:your |the )?email(?: address)?\b|"
    r"\bhow do i email you\b|"
    r"\bemail (?:address )?(?:\?|$)",
    re.IGNORECASE,
)

_WEBSITE_RE = re.compile(
    r"\bwhat(?:'s|s| is) (?:your |the )?(?:website|web site|url)\b|"
    r"\bwhat(?:'s|s| is) (?:your |the )?web address\b",
    re.IGNORECASE,
)

_HOURS_RE = re.compile(
    r"\bwhat (?:are |is )?(?:your |the )?(?:business |open(?:ing)? |store )?hours\b|"
    r"\bwhen (?:are|do) you (?:open|close|opens|closes)\b|"
    r"\bare you open\b|"
    r"\bhours of operation\b|"
    r"\bopen (?:today|now|tomorrow|on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.IGNORECASE,
)

_SERVICES_RE = re.compile(
    r"\bwhat (?:services |stuff |things )?(?:do you (?:offer|provide|do)|"
    r"are (?:available|offered))\b|"
    r"\bwhat do you (?:do|sell|offer)\b|"
    r"\bservices(?:\?|$)|"
    r"\bwhat can you do for me\b",
    re.IGNORECASE,
)

_CATALOG_COUNT_RE = re.compile(
    r"\bhow many (?:items|products|parts|things) (?:do you have|are in|in your)\b|"
    r"\bhow big is your (?:catalog|inventory|store)\b|"
    r"\bsize of your (?:catalog|inventory)\b|"
    r"\bcatalog count\b",
    re.IGNORECASE,
)
