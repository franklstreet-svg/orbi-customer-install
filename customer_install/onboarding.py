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


# ── Sales tax reference table ────────────────────────────────────────────
# State base rates (combined state+avg-local). Keyed by 2-letter state code.
# Source: Tax Foundation 2024 combined averages — good enough for a suggested
# default; owner always confirms or overrides during onboarding.

_STATE_TAX = {
    "AL": 0.0922, "AK": 0.0176, "AZ": 0.0840, "AR": 0.0947, "CA": 0.0873,
    "CO": 0.0777, "CT": 0.0635, "DE": 0.0000, "FL": 0.0701, "GA": 0.0732,
    "HI": 0.0444, "ID": 0.0602, "IL": 0.0882, "IN": 0.0700, "IA": 0.0694,
    "KS": 0.0868, "KY": 0.0600, "LA": 0.0945, "ME": 0.0550, "MD": 0.0600,
    "MA": 0.0625, "MI": 0.0600, "MN": 0.0749, "MS": 0.0707, "MO": 0.0822,
    "MT": 0.0000, "NE": 0.0694, "NV": 0.0823, "NH": 0.0000, "NJ": 0.0660,
    "NM": 0.0783, "NY": 0.0852, "NC": 0.0698, "ND": 0.0696, "OH": 0.0722,
    "OK": 0.0896, "OR": 0.0000, "PA": 0.0634, "RI": 0.0700, "SC": 0.0746,
    "SD": 0.0640, "TN": 0.0955, "TX": 0.0819, "UT": 0.0719, "VT": 0.0618,
    "VA": 0.0573, "WA": 0.0924, "WV": 0.0650, "WI": 0.0543, "WY": 0.0536,
    "DC": 0.0600,
}

# City/county overrides where the combined rate differs meaningfully from the
# state average. Keyed by (city_lower, state_upper).
_CITY_TAX: dict[tuple[str, str], float] = {
    # Nevada
    ("reno", "NV"):           0.08265,
    ("sparks", "NV"):         0.08265,
    ("las vegas", "NV"):      0.08375,
    ("henderson", "NV"):      0.08375,
    ("north las vegas", "NV"): 0.08375,
    # California
    ("los angeles", "CA"):    0.10250,
    ("san francisco", "CA"):  0.08625,
    ("san jose", "CA"):       0.09375,
    ("san diego", "CA"):      0.07750,
    ("sacramento", "CA"):     0.08750,
    ("fresno", "CA"):         0.08350,
    ("long beach", "CA"):     0.10250,
    # Texas
    ("houston", "TX"):        0.08250,
    ("dallas", "TX"):         0.08250,
    ("san antonio", "TX"):    0.08250,
    ("austin", "TX"):         0.08250,
    # Washington
    ("seattle", "WA"):        0.10250,
    ("spokane", "WA"):        0.08900,
    # New York
    ("new york", "NY"):       0.08875,
    ("new york city", "NY"):  0.08875,
    ("buffalo", "NY"):        0.08000,
    # Illinois
    ("chicago", "IL"):        0.10250,
    # Tennessee
    ("nashville", "TN"):      0.09250,
    ("memphis", "TN"):        0.09750,
    # Georgia
    ("atlanta", "GA"):        0.08900,
    # Colorado
    ("denver", "CO"):         0.08810,
    # Arizona
    ("phoenix", "AZ"):        0.08600,
    ("tucson", "AZ"):         0.08700,
    ("scottsdale", "AZ"):     0.07950,
    # Oregon/Montana/Delaware/New Hampshire — no sales tax
    ("portland", "OR"):       0.0000,
    ("eugene", "OR"):         0.0000,
    ("billings", "MT"):       0.0000,
    ("missoula", "MT"):       0.0000,
    ("dover", "DE"):          0.0000,
    ("manchester", "NH"):     0.0000,
}


def suggest_tax_rate(city: str, state: str) -> float | None:
    """Return a suggested combined sales tax rate (0-1) for a given
    city/state, or None if the state is unknown. City match is case-
    insensitive; state must be a 2-letter code."""
    state = (state or "").strip().upper()
    city  = (city  or "").strip().lower()
    if not state:
        return None
    key = (city, state)
    if key in _CITY_TAX:
        return _CITY_TAX[key]
    return _STATE_TAX.get(state)


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

    # Detect CMS platform from homepage HTML so widget install can pick the right recipe
    home_html = fetched.get("home", {}).get("text", "")
    try:
        from widget_installer import detect_platform
        draft["_platform"] = detect_platform(url, home_html)
    except Exception:
        draft["_platform"] = "unknown"

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


def gap_questions(draft: dict, enabled_modules: list | None = None) -> list[dict]:
    """Look at the draft and return a list of targeted questions for
    fields that are missing or low-confidence. The dashboard wizard
    walks the owner through these one at a time.
    enabled_modules: list of module keys from config (e.g. ['legal', 'calendar'])
    """
    qs = []
    enabled_modules = [str(m).lower() for m in (enabled_modules or [])]

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

    if not draft.get("tax_rate"):
        addr = draft.get("address", {}) or {}
        city  = addr.get("city", "")
        state = addr.get("state", "")
        suggested = suggest_tax_rate(city, state)
        if suggested is not None and suggested > 0:
            pct = round(suggested * 100, 3)
            hint = (f" In {city}, {state} the typical combined rate is "
                    f"{pct:.3g}% — enter that or your exact local rate."
                    if city and state else
                    f" In {state} the typical combined rate is {pct:.3g}% "
                    f"— enter that or your exact local rate."
                    if state else "")
            q = f"What is your local sales tax rate?{hint}"
        elif suggested == 0.0:
            state_name = state or "your state"
            q = (f"{state_name} has no sales tax — I'll skip the tax line on "
                 f"orders. If your city or county charges one anyway, enter "
                 f"the rate here (e.g. 2.5%). Otherwise just say 'none'.")
        else:
            q = ("What is your local sales tax rate? Enter a percentage "
                 "(e.g. 8.25%) or decimal (e.g. 0.0825). Say 'none' if you "
                 "don't charge sales tax.")
        qs.append({"field": "tax_rate", "question": q, "type": "text",
                   "suggested": suggested})

    # ── WIDGET INSTALL ─────────────────────────────────────────────────────────
    # Ask this only when the business has a website but the widget isn't installed yet.
    website = (draft.get("contact") or {}).get("website", "")
    widget_installed = draft.get("widget_installed", False)
    if website and not widget_installed:
        platform = draft.get("_platform", "unknown")
        try:
            from widget_installer import install_prompt, generate_embed_code
            prompt_text = install_prompt(platform, website)
        except Exception:
            prompt_text = (
                "I can install my chat widget on your website automatically. "
                "Would you like me to do that now?"
            )
        qs.append({
            "field": "widget_install",
            "question": prompt_text,
            "type": "widget_install",
            "platform": platform,
            "site_url": website,
        })

    # ── ATTORNEY / LEGAL MODULE ONBOARDING ────────────────────────────────────
    if "legal" in enabled_modules:
        legal = draft.get("legal") or {}

        if not legal.get("firm_name") and not draft.get("name"):
            qs.append({
                "field": "legal.firm_name",
                "question": "What is the name of your law firm? (e.g. Smith & Jones LLP)",
                "type": "text",
            })
        if not legal.get("attorney_name"):
            qs.append({
                "field": "legal.attorney_name",
                "question": "What is the lead attorney's full name and bar number? (e.g. Jane Smith, NV Bar #12345)",
                "type": "text",
            })
        if not legal.get("practice_areas"):
            qs.append({
                "field": "legal.practice_areas",
                "question": (
                    "What are your primary practice areas? List them, one per line. "
                    "(e.g. Personal Injury, Family Law, Criminal Defense, Business Law)"
                ),
                "type": "textarea",
            })
        if not legal.get("default_jurisdiction"):
            addr = draft.get("address") or {}
            state = addr.get("state", "")
            hint = f" (looks like {state} based on your address)" if state else ""
            qs.append({
                "field": "legal.default_jurisdiction",
                "question": f"What is your primary jurisdiction{hint}? (e.g. NV, California, Federal)",
                "type": "text",
            })
        if not legal.get("default_hourly_rate"):
            qs.append({
                "field": "legal.default_hourly_rate",
                "question": "What is your standard hourly billing rate? (e.g. 350 — I'll pre-fill it on every new matter)",
                "type": "text",
            })
        if not legal.get("contingency_available") and not legal.get("fee_structure"):
            qs.append({
                "field": "legal.fee_structure",
                "question": (
                    "How do you typically structure fees? "
                    "(e.g. Hourly, Flat fee, Contingency, or a mix — I'll know how to answer fee questions from potential clients)"
                ),
                "type": "text",
            })
        if not legal.get("consultation_fee"):
            qs.append({
                "field": "legal.consultation_fee",
                "question": (
                    "Do you charge for initial consultations? "
                    "If yes, how much? If free, say 'free'. "
                    "(Callers and website visitors will ask this.)"
                ),
                "type": "text",
            })
        if not legal.get("conflict_disclaimer"):
            qs.append({
                "field": "legal.conflict_disclaimer",
                "question": (
                    "When a caller asks if you can take their case, I'll tell them I need to run a conflict check first "
                    "before you can confirm. Would you like me to say anything else at that point? "
                    "(e.g. 'We typically respond within one business day' — or just say 'standard' to use that.)"
                ),
                "type": "text",
                "suggested": "standard",
            })

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
    if field == "tax_rate":
        return {"tax_rate": _parse_tax_rate(raw)}

    # Legal module fields — stored under draft["legal"]["field_name"]
    if field.startswith("legal."):
        subfield = field[len("legal."):]
        val: object = raw
        if subfield == "default_hourly_rate":
            try:
                val = float(raw.replace("$", "").replace(",", "").strip())
            except ValueError:
                val = raw
        elif subfield == "practice_areas":
            val = [l.strip(" -•·") for l in raw.splitlines() if l.strip()]
        elif subfield == "consultation_fee":
            lower = raw.lower()
            if lower in ("free", "no", "none", "0", "no charge"):
                val = "free"
        elif subfield == "conflict_disclaimer":
            if raw.lower() in ("standard", "yes", "ok", "default"):
                val = "We typically respond within one business day."
        return {"legal": {subfield: val}}

    # policies.* fields
    if field.startswith("policies."):
        subfield = field[len("policies."):]
        return {"policies": {subfield: raw}}

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


def _parse_tax_rate(raw: str) -> float:
    """Parse an owner's tax rate answer into a 0-1 float.
    Handles: '8.25%', '8.25', '0.0825', 'none', 'zero', '0'.
    Returns 0.0 for 'none'/'no'/'zero'.
    """
    raw = raw.strip().lower()
    if not raw or raw in ("none", "no", "zero", "n/a", "na", "0", "no tax",
                          "no sales tax", "exempt"):
        return 0.0
    raw = raw.replace("%", "").strip()
    try:
        val = float(raw)
    except ValueError:
        return 0.0
    # If > 1 assume it's a percentage (e.g. 8.25 → 0.0825)
    return round(val / 100.0, 6) if val > 1 else round(val, 6)


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
