"""Per-page LLM extraction. Sends the readable text of one page to the
brain and asks for structured business data.

Key design: the prompt is INTENTIONALLY broad. We're not asking the LLM
to fill specific named fields — we're asking it to pull out anything a
human receptionist might want to know about this business, in a flexible
shape. The merger downstream decides what becomes canonical.

If the brain isn't available (offline, rate-limited, etc.), this falls
back to regex extraction (phone, email, zip, prices) — incomplete but
better than zero.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("orbi.site_scraper.llm")

# Text cap per page. Anything longer gets truncated — the brain doesn't
# need a 50,000-char menu page to extract structured items.
MAX_PROMPT_CHARS = 8_000


_EXTRACTION_SYSTEM = """You are an AI receptionist's intake assistant. \
You're reading one page from a small business's website. Extract anything \
on this page that could matter to a customer calling or chatting in — \
even small details. Don't filter for "is this important enough" — pull \
everything and the receptionist will pick what to surface.

Return ONE JSON object (no prose, no markdown fence) with these keys, \
omitting keys that have no findings on this page:

{
  "name":         "<business name if mentioned>",
  "tagline":      "<short positioning line>",
  "description":  "<what the business does, 1-3 sentences>",
  "owner":        {"name":"<owner/founder full name>", "role":"<Owner|Founder|President|CEO|...>", "bio":"<short bio if available>"},
  "founded_year": "<year established>",
  "ownership":    "<family-owned, employee-owned, LLC, sole proprietor, franchise, etc.>",
  "address":      {"street":"...", "city":"...", "state":"...", "zip":"..."},
  "phone":        "...",
  "email":        "...",
  "website":      "...",
  "social":       {"facebook":"...", "instagram":"...", "yelp":"...", "google":"..."},
  "hours":        {"monday":{"open":"HH:MM","close":"HH:MM"}, ...},
  "service_area": "...",
  "services":     [{"name":"...", "price":"...", "description":"...", "category":"..."}, ...],
  "menu_items":   [{"name":"...", "price":"...", "description":"...", "category":"...", "modifiers":[...]}, ...],
  "faq":          [{"q":"...", "a":"..."}, ...],
  "policies":     {"cancellation":"...", "refunds":"...", "warranty":"...", "payment_methods":"...", "delivery":"...", "tipping":"...", "allergens":"...", "dress_code":"..."},
  "pricing_notes":"<deposit, minimums, fees etc.>",
  "booking":      {"how_to_book":"...", "lead_time":"...", "deposit":"..."},
  "specials":     ["<active promotion or seasonal>"],
  "events":       [{"name":"...", "when":"...", "details":"..."}],
  "staff":        [{"name":"...", "role":"...", "bio":"..."}],
  "parking":      "...",
  "accessibility":"...",
  "languages":    ["..."],
  "credentials":  ["<licenses, certifications, awards>"],
  "other":        ["<anything notable that doesn't fit above>"]
}

RULES:
- If a field isn't mentioned on this page, OMIT it entirely. Don't invent.
- LOOK HARD FOR OWNERSHIP INFO. Owner / founder names tend to live in:
  * About / About Us / Our Story / Meet the Team pages
  * Footer copyright lines ("© 2026 Frank Smith dba J's Bakery")
  * Contact pages ("Owner: Jane Doe, jane@biz.com")
  * Press releases or news sections
  * Phrases like "founded by", "started by", "owned by", "operated by",
    "our founder", "our owner", "President / CEO is"
  Pull the owner name into BOTH the "owner" object AND the "staff" array.
- For prices, keep them as the page wrote them (e.g. "$11.95" or "From $4.95").
- **For menu items, the description field is CRITICAL.** Pull EVERY adjacent
  line of detail into it: ingredients/toppings, sizes, flavors, sauces,
  spice levels, what it's served with, dressings. If the item is "Chicken
  Wings" and the next lines say "Mild, Medium, Hot or Nitro" and "Served
  with celery and ranch", BOTH belong in the description. Variants like
  flavor options or size options ALSO go in the "modifiers" array as
  `{"group":"flavor","options":["Mild","Medium","Hot","Nitro"]}` or
  `{"group":"size","options":["12 Wings","18 Wings"]}`. Never extract
  just the bare item name when the page lists details around it.
- If the page is mostly navigation / fluff / 404, return {} (empty object).
- Output ONLY the JSON. No code fence, no explanation."""


def extract_from_page(page_url: str, page_text: str,
                       brain_call=None) -> dict:
    """Call the brain with the page's text and parse out structured data.
    `brain_call` is a callable (system, messages) -> response_text. If
    None, falls back to regex-only extraction."""
    if not page_text or len(page_text.strip()) < 30:
        return {}

    # Always do the regex pass — even if the LLM works, regex finds give us
    # high-confidence corroboration.
    regex_data = _regex_extract(page_text)

    if not brain_call:
        return regex_data

    truncated = page_text[:MAX_PROMPT_CHARS]
    user_msg = f"PAGE URL: {page_url}\n\nPAGE TEXT:\n{truncated}"
    try:
        raw = brain_call(_EXTRACTION_SYSTEM, [{"role": "user", "content": user_msg}])
    except Exception as e:
        log.warning(f"brain call failed on {page_url}: {e}")
        return regex_data

    llm_data = _parse_json_lenient(raw)
    if not isinstance(llm_data, dict):
        return regex_data

    # Merge: LLM wins for most fields, but regex-found contacts always
    # get checked against LLM's (in case LLM hallucinated a number).
    merged = dict(llm_data)
    for key in ("phone", "email", "address"):
        if key in regex_data and not merged.get(key):
            merged[key] = regex_data[key]
    return merged


# ── Regex fallback ─────────────────────────────────────────────────────


_PHONE_RE = re.compile(r"\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_ADDRESS_RE = re.compile(
    r"(\d{1,6}\s+[A-Za-z0-9.\-\s]+(?:Street|St|Avenue|Ave|Boulevard|Blvd|"
    r"Road|Rd|Lane|Ln|Drive|Dr|Way|Court|Ct|Place|Pl)\b[,\s]+"
    r"[A-Za-z\s]+,?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)
_HOURS_LINE_RE = re.compile(
    r"((?:mon|tue|wed|thu|fri|sat|sun)(?:day)?[^\n]{0,80}?"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)[^\n]{0,40}?(?:\d{1,2}(?::\d{2})?\s*(?:am|pm))?)",
    re.IGNORECASE,
)


def _regex_extract(text: str) -> dict:
    out: dict = {}

    # Phone — take the first plausible US-format match
    pm = _PHONE_RE.search(text)
    if pm:
        out["phone"] = f"({pm.group(1)}) {pm.group(2)}-{pm.group(3)}"

    # Email
    em = _EMAIL_RE.search(text)
    if em:
        out["email"] = em.group(1)

    # Address (full street + city + state + zip)
    am = _ADDRESS_RE.search(text)
    if am:
        out["address_raw"] = am.group(1)

    # Hours lines (raw — merger will normalize)
    hours_lines = _HOURS_LINE_RE.findall(text)
    if hours_lines:
        out["hours_raw"] = hours_lines[:7]
    return out


def _parse_json_lenient(raw: str) -> dict:
    """Pull a JSON object out of LLM output, even if it wrapped it in a
    code fence or added prose."""
    if not raw:
        return {}
    s = raw.strip()
    # Strip markdown fences
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("\n", 1)[0] if "\n" in s else s[:-3]
    # If the LLM added prose, find the first { and last } and try that
    if "{" in s and "}" in s:
        start = s.find("{")
        end = s.rfind("}") + 1
        candidate = s[start:end]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}
