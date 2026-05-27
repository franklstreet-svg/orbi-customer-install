"""
onboarding — first-time setup for a new business owner.

The flow Frank wants:
    1. Owner installs Orbi → first run sees an empty business_info.json
    2. Orbi asks: "What's your website?"
    3. Orbi scrapes:
         - homepage
         - about / about-us
         - contact / contact-us
         - services / menu / products
    4. From the scraped HTML she extracts everything she can:
         - business name (legal entity from JSON-LD / footer / multi-line picker)
         - tagline / description (og:description / meta description / "about" prose)
         - address (regex over the contact page)
         - phone + email (regex)
         - hours ("Monday-Friday 9am-5pm" patterns)
         - services / menu items (page nav text + heading list)
         - social handles (links to facebook/instagram/etc.)
    5. She presents a "found this, what's right/wrong" view + a list of
       gaps she couldn't fill.
    6. She asks the owner targeted questions for each gap, one at a time.
    7. She saves the result to business_info.json.
    8. Done — she now knows the business.

This module exposes:
    discover_from_url(url) -> dict  # scrape + extract, no save
    apply_to_business(data_dir, draft, overwrite=False) -> dict  # save
    gap_questions(draft) -> list[dict]  # questions to ask the owner
    explain_flow() -> str  # text Orbi can paste into chat when prospects ask

Important: this module is intentionally local-first. The owner's website
URL goes through url_fetch (which uses our 3-tier fallback) — no third
party sees the business data after extraction.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path

log = logging.getLogger("orbi.onboarding")


# ── Page-discovery patterns ─────────────────────────────────────────────


_PAGE_HINTS = [
    ("about",    ["about", "about-us", "about_us", "our-story", "our-company",
                  "who-we-are"]),
    ("contact",  ["contact", "contact-us", "contact_us", "reach-us", "get-in-touch"]),
    ("services", ["services", "menu", "products", "offerings", "what-we-do",
                  "what-we-offer"]),
    ("hours",    ["hours", "schedule", "open-hours"]),
    ("pricing",  ["pricing", "rates", "plans"]),
]


# ── Public API ──────────────────────────────────────────────────────────


def discover_from_url(url: str) -> dict:
    """Scrape + extract everything we can from a business's website.
    Returns a draft business_info-shaped dict plus an `inputs` field
    that lists which pages were fetched (so the dashboard can show the
    owner where each value came from)."""
    from tools import url_fetch
    url = _normalize_url(url)

    draft = {
        "name": "",
        "tagline": "",
        "description": "",
        "address": {"street": "", "city": "", "state": "", "zip": ""},
        "contact": {"phone": "", "email": "", "website": url},
        "hours": {},
        "services": [],
        "faq": [],
        "social": {},
        "_sources": {},  # field -> URL the value came from
    }

    fetched: dict[str, dict] = {}

    # 1. Homepage
    home = url_fetch.fetch_or_search(url, timeout=12)
    fetched["home"] = home
    if home.get("ok"):
        _merge_identity(draft, home, source=url)
        _merge_from_text(draft, home.get("text", ""), source=url)

    # 2. Sub-pages we can guess by URL
    base = _base_url(url)
    candidates = _candidate_subpages(url, fetched.get("home", {}))
    for kind, sub_url in candidates.items():
        if not sub_url or sub_url == url:
            continue
        try:
            sub = url_fetch.fetch_or_search(sub_url, timeout=10)
        except Exception as e:
            log.debug(f"sub-page {sub_url} failed: {e}")
            continue
        fetched[kind] = sub
        if sub.get("ok"):
            _merge_identity(draft, sub, source=sub_url)
            _merge_from_text(draft, sub.get("text", ""), source=sub_url)

    draft["_inputs"] = {k: v.get("url", "") for k, v in fetched.items()}
    draft["_confidence"] = _score_confidence(draft)
    return draft


def apply_to_business(data_dir: Path, draft: dict, overwrite: bool = False) -> dict:
    """Merge a draft into business_info.json. If overwrite=False, only
    fills empty fields. Returns the saved business dict."""
    from modules import business_info as mod_business
    current = mod_business.load(data_dir)
    merged = _deep_merge(current, draft, overwrite=overwrite)
    # Strip internal fields before saving
    for k in ("_inputs", "_sources", "_confidence"):
        merged.pop(k, None)
    mod_business.save(data_dir, merged)
    return merged


def gap_questions(draft: dict) -> list[dict]:
    """Look at the draft and return a list of targeted questions for
    fields that are missing or low-confidence. The dashboard wizard
    walks the owner through these one at a time."""
    qs = []

    if not draft.get("name"):
        qs.append({"field": "name", "question": "What's your business's name?",
                   "type": "text"})
    if not (draft.get("tagline") or draft.get("description")):
        qs.append({"field": "tagline",
                   "question": "Describe your business in one short sentence.",
                   "type": "text"})

    addr = draft.get("address", {}) or {}
    if not (addr.get("street") and addr.get("city")):
        qs.append({"field": "address",
                   "question": "What's your business address? (street, city, state, zip)",
                   "type": "text"})

    contact = draft.get("contact", {}) or {}
    if not contact.get("phone"):
        qs.append({"field": "contact.phone",
                   "question": "What phone number should customers reach you at?",
                   "type": "tel"})
    if not contact.get("email"):
        qs.append({"field": "contact.email",
                   "question": "What email address do you check for customer messages?",
                   "type": "email"})

    if not draft.get("hours"):
        qs.append({"field": "hours",
                   "question": ("What are your business hours? "
                                "(For each day Mon-Sun, say 'open 9-5' or 'closed')"),
                   "type": "textarea"})

    if not draft.get("services"):
        qs.append({"field": "services",
                   "question": ("List your top services or products, one per line. "
                                "Add a price if you have one."),
                   "type": "textarea"})

    if not draft.get("faq"):
        qs.append({"field": "faq",
                   "question": ("Top 3 questions customers ask. "
                                "Format: 'Q: ...' on one line, 'A: ...' on the next."),
                   "type": "textarea"})

    return qs


def explain_flow() -> str:
    """Text Orbi pastes when a prospect or customer asks how she learns
    about their business. Drop-in answer."""
    return (
        "Here's how I get to know a new business when I'm installed:\n\n"
        "1. The owner pastes their website URL into my setup wizard.\n"
        "2. I scrape the homepage, About, Contact, and Services pages — "
        "looking for the business name, tagline, address, phone, email, "
        "hours, services, and FAQs.\n"
        "3. I show the owner everything I found and let them correct anything "
        "I got wrong.\n"
        "4. For anything I COULDN'T find on the website, I ask the owner "
        "directly — one focused question at a time.\n"
        "5. I save the result locally. From that point on I can answer "
        "every customer question about the business using real facts, "
        "not guesses.\n\n"
        "Everything happens on the owner's own computer — the business "
        "data never leaves their machine. The website fetch uses three "
        "fallback paths so even sites that block bots (Cloudflare, etc.) "
        "still work via the Internet Archive."
    )


def parse_answer(field: str, raw: str) -> dict:
    """Turn the owner's free-form answer into the structured shape the
    business_info JSON expects."""
    raw = (raw or "").strip()
    if not raw:
        return {}

    if field == "name":
        return {"name": raw}
    if field == "tagline":
        return {"tagline": raw[:200]}
    if field == "address":
        return {"address": _parse_address(raw)}
    if field == "contact.phone":
        return {"contact": {"phone": raw}}
    if field == "contact.email":
        return {"contact": {"email": raw}}
    if field == "hours":
        return {"hours": _parse_hours(raw)}
    if field == "services":
        return {"services": _parse_services(raw)}
    if field == "faq":
        return {"faq": _parse_faq(raw)}
    return {field: raw}


# ── Internal: identity + text merging ───────────────────────────────────


_PHONE_RE = re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})\b"
)
_EMAIL_RE = re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w\-.]+\b")
_ADDRESS_RE = re.compile(
    r"(\d{1,6}[^\n,]{1,40}(?:Street|St\.?|Ave|Avenue|Road|Rd\.?|"
    r"Blvd|Boulevard|Lane|Ln\.?|Drive|Dr\.?|Way|Court|Ct\.?|Place|Pl\.?|"
    r"Parkway|Pkwy)[\s,]+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?,\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)
_HOURS_RE = re.compile(
    r"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]{0,7}"
    r"(?:\s*[-–—]\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]{0,7})?)"
    r"\s*:?\s*"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?\s*[-–—]\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)",
    re.IGNORECASE,
)


def _merge_identity(draft: dict, fetched: dict, source: str) -> None:
    """Take a url_fetch result and merge its structured identity into draft."""
    if not draft.get("name") and fetched.get("official_name"):
        draft["name"] = fetched["official_name"]
        draft["_sources"]["name"] = source
    # Tagline from the og:description / meta description
    if not draft.get("tagline") and fetched.get("tagline"):
        draft["tagline"] = fetched["tagline"][:200]
        draft["_sources"]["tagline"] = source


def _merge_from_text(draft: dict, text: str, source: str) -> None:
    """Pull phone, email, address, hours from page text via regex."""
    if not text:
        return
    text = text[:30_000]  # keep regex work bounded

    # Phone
    if not draft.get("contact", {}).get("phone"):
        m = _PHONE_RE.search(text)
        if m:
            draft["contact"]["phone"] = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
            draft["_sources"]["contact.phone"] = source

    # Email — prefer one that doesn't look like a vendor (mailchimp, etc.)
    if not draft.get("contact", {}).get("email"):
        for m in _EMAIL_RE.finditer(text):
            email = m.group(0).lower()
            if any(b in email for b in ("@mailchimp", "@sentry", "@example.com",
                                         "@wix", "@squarespace", "noreply", "no-reply")):
                continue
            draft["contact"]["email"] = email
            draft["_sources"]["contact.email"] = source
            break

    # Address
    addr = draft.get("address", {})
    if not (addr.get("street") and addr.get("city")):
        m = _ADDRESS_RE.search(text)
        if m:
            parsed = _parse_address(m.group(0))
            for k, v in parsed.items():
                if v and not addr.get(k):
                    addr[k] = v
            draft["_sources"]["address"] = source

    # Hours
    if not draft.get("hours"):
        hours = {}
        for m in _HOURS_RE.finditer(text):
            day_part = m.group(1).lower()
            time_part = m.group(2)
            for day in _expand_day_range(day_part):
                if day not in hours:
                    hours[day] = _parse_time_range(time_part)
        if hours:
            draft["hours"] = hours
            draft["_sources"]["hours"] = source

    # Description — fall back to first non-trivial paragraph from About-style page
    if not draft.get("description") and "about" in source.lower():
        # First 400 chars of clean text
        para = text.strip()[:400]
        if len(para) > 60:
            draft["description"] = para
            draft["_sources"]["description"] = source


def _candidate_subpages(home_url: str, home_result: dict) -> dict:
    """Discover sub-page URLs to scrape next. Prefers actual <a href="..."> links
    over guessed paths."""
    out = {}
    base = _base_url(home_url)
    html = home_result.get("text", "")

    # Try to find real links in the homepage text/HTML
    link_re = re.compile(r"https?://[^\s<>\"']+|/[\w\-/]+", re.IGNORECASE)
    found_links = set(link_re.findall(html))

    for kind, hints in _PAGE_HINTS:
        if kind in out:
            continue
        for link in found_links:
            link_l = link.lower().rstrip("/")
            if any(h in link_l for h in hints):
                out[kind] = link if link.startswith("http") else urllib.parse.urljoin(base, link)
                break
        if kind not in out:
            # Fall back to a guess
            out[kind] = urllib.parse.urljoin(base, f"/{hints[0]}")
    return out


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url


def _base_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _expand_day_range(s: str) -> list[str]:
    """e.g. 'mon-fri' → ['monday','tuesday','wednesday','thursday','friday']."""
    days_order = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    short = {"mon":"monday","tue":"tuesday","wed":"wednesday","thu":"thursday",
             "fri":"friday","sat":"saturday","sun":"sunday"}
    s = s.lower()
    parts = re.split(r"\s*[-–—]\s*", s)
    if len(parts) == 1:
        start = end = short.get(parts[0][:3], "")
    else:
        start = short.get(parts[0][:3], "")
        end = short.get(parts[1][:3], "")
    if not start:
        return []
    if not end or start == end:
        return [start]
    si, ei = days_order.index(start), days_order.index(end)
    if ei < si:
        ei += 7
    return [days_order[i % 7] for i in range(si, ei + 1)]


def _parse_time_range(s: str) -> dict:
    """'9am-5pm' / '9:00 - 17:00' → {open: '09:00', close: '17:00', closed: False}"""
    parts = re.split(r"\s*[-–—]\s*", s.strip())
    if len(parts) != 2:
        return {"open": "09:00", "close": "17:00", "closed": False}
    return {
        "open":   _normalize_clock(parts[0]),
        "close":  _normalize_clock(parts[1]),
        "closed": False,
    }


def _normalize_clock(s: str) -> str:
    s = s.strip().lower()
    pm = "pm" in s
    am = "am" in s
    s = s.replace("am", "").replace("pm", "").strip()
    if ":" in s:
        h, m = s.split(":", 1)
        m = m[:2].zfill(2)
    else:
        h, m = s, "00"
    h = int(re.sub(r"\D", "", h) or "9")
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    return f"{h:02d}:{m}"


def _parse_address(raw: str) -> dict:
    """Try to break a free-form address into {street, city, state, zip}."""
    m = re.search(
        r"^(?P<street>[\w .,#'\-]+?),\s*(?P<city>[\w .\-]+?),\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5}(?:-\d{4})?)?",
        raw.strip(),
    )
    if m:
        return {
            "street": m.group("street").strip(),
            "city":   m.group("city").strip(),
            "state":  m.group("state").strip(),
            "zip":    (m.group("zip") or "").strip(),
        }
    # Best-effort fallback
    return {"street": raw.strip(), "city": "", "state": "", "zip": ""}


def _parse_hours(raw: str) -> dict:
    """Parse owner's free-form hours answer back into structured form."""
    out = {}
    for line in raw.splitlines():
        m = _HOURS_RE.search(line)
        if m:
            day_part = m.group(1).lower()
            time_part = m.group(2)
            for day in _expand_day_range(day_part):
                out[day] = _parse_time_range(time_part)
        elif "close" in line.lower():
            for day_short, day in (("mon","monday"),("tue","tuesday"),
                                    ("wed","wednesday"),("thu","thursday"),
                                    ("fri","friday"),("sat","saturday"),("sun","sunday")):
                if day_short in line.lower():
                    out[day] = {"open": "00:00", "close": "00:00", "closed": True}
    return out


def _parse_services(raw: str) -> list:
    """Each non-empty line becomes a service. If a line has '$' or 'starts at',
    try to split into {name, price}."""
    out = []
    for line in raw.splitlines():
        line = line.strip(" -•*")
        if not line:
            continue
        m = re.search(r"(.+?)\s*[-–—]\s*\$?(\d[\d,]*(?:\.\d{1,2})?)", line)
        if m:
            out.append({"name": m.group(1).strip(),
                        "price": "$" + m.group(2).strip()})
        else:
            out.append({"name": line, "price": ""})
    return out


def _parse_faq(raw: str) -> list:
    """Pull out alternating Q:/A: lines into a list of {q, a} dicts."""
    out = []
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^q\s*[:.\-]", line, re.IGNORECASE):
            q = re.sub(r"^q\s*[:.\-]\s*", "", line, flags=re.IGNORECASE)
            a = ""
            if i + 1 < len(lines) and re.match(r"^a\s*[:.\-]", lines[i+1], re.IGNORECASE):
                a = re.sub(r"^a\s*[:.\-]\s*", "", lines[i+1], flags=re.IGNORECASE)
                i += 1
            out.append({"q": q, "a": a})
        i += 1
    return out


def _score_confidence(draft: dict) -> dict:
    """Score each field so the UI can mark green/yellow/red."""
    s = {}
    s["name"] = "high" if draft.get("name") else "missing"
    s["tagline"] = "high" if draft.get("tagline") or draft.get("description") else "missing"
    addr = draft.get("address", {})
    s["address"] = "high" if (addr.get("street") and addr.get("city")) else "missing"
    contact = draft.get("contact", {})
    s["phone"] = "high" if contact.get("phone") else "missing"
    s["email"] = "high" if contact.get("email") else "missing"
    s["hours"] = "high" if draft.get("hours") else "missing"
    s["services"] = "high" if draft.get("services") else "missing"
    return s


def _deep_merge(base: dict, overlay: dict, overwrite: bool = False) -> dict:
    """Merge overlay into base. If overwrite=False, only fills empties."""
    out = dict(base)
    for k, v in overlay.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v, overwrite=overwrite)
        elif isinstance(v, list) and not v:
            continue
        elif v:
            if overwrite or not out.get(k):
                out[k] = v
    return out
