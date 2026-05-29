"""
auto_categorize — tag incoming customer messages/leads/reviews so the
owner can scan a long inbox and instantly know what's worth opening first.

Light wrapper around the LLM client. Returns a small list of standardized
tags so dashboard chips and filters stay consistent across sources.

Tag taxonomy (kept tight on purpose — too many tags = noisy UI):
    urgent      — needs reply within a few hours
    lead        — potential new customer
    customer    — existing customer (paying / been served before)
    question    — they're asking how to / what / where / when / why
    complaint   — frustrated tone, negative experience
    compliment  — positive feedback, thank-you, kind words
    supplier    — vendors / suppliers / contractors reaching out
    personal    — friend / family / non-business
    spam        — bulk / sales pitch / phishing
    other       — fallback when nothing fits

Two callsites:
    - modules/messages.py capture() — every captured lead/voicemail/callback
    - dashboard on-demand — owner can re-categorize a batch from the UI
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable

log = logging.getLogger("orbi.auto_categorize")

VALID_TAGS = {
    "urgent", "lead", "customer", "question", "complaint",
    "compliment", "supplier", "personal", "spam", "other",
}


# ── Public API ──────────────────────────────────────────────────────────


def categorize(config: dict, text: str, hints: dict | None = None) -> list[str]:
    """Return 1-3 tags for the given text. Never raises — falls back to
    keyword heuristics if the LLM is unreachable, then to ['other']."""
    text = (text or "").strip()
    if not text:
        return ["other"]
    hints = hints or {}

    # Cheap fast-path FIRST — kills the obvious cases without an LLM call.
    cheap = _heuristic(text, hints)
    if cheap:
        log.debug(f"heuristic match: {cheap}")
        return cheap

    # LLM-driven classification
    try:
        return _llm(config, text, hints) or ["other"]
    except Exception as e:
        log.warning(f"LLM categorize failed, using heuristics: {e}")
        return _heuristic_fallback(text, hints)


def categorize_batch(config: dict, items: Iterable[dict],
                     text_field: str = "body") -> list[dict]:
    """Tag a list of items in-place (adds a 'tags' field). Used by the
    dashboard 'recategorize' bulk action. Returns the same list with tags
    populated."""
    out = []
    for item in items:
        text = (item.get(text_field) or "").strip()
        tags = categorize(config, text, hints={
            "from_name":  item.get("from_name"),
            "from_email": item.get("from_email"),
            "from_phone": item.get("from_phone"),
            "type":       item.get("type"),
        })
        item["tags"] = tags
        out.append(item)
    return out


# ── Heuristic fast-path (no LLM cost on the obvious cases) ──────────────


_URGENT_PATTERNS = re.compile(
    r"\b(urgent|asap|emergency|right away|today|broken|leak|flooding|"
    r"fire|water everywhere|no heat|no power|stuck|stranded)\b",
    re.IGNORECASE,
)
_COMPLAINT_PATTERNS = re.compile(
    r"\b(terrible|awful|worst|disappointed|refund|never again|complain|"
    r"angry|frustrated|unhappy|rude|unprofessional|scam)\b",
    re.IGNORECASE,
)
_COMPLIMENT_PATTERNS = re.compile(
    r"\b(thank you|thanks so much|amazing|wonderful|fantastic|excellent|"
    r"loved|great job|best ever|highly recommend|appreciate)\b",
    re.IGNORECASE,
)
_QUESTION_PATTERNS = re.compile(
    r"\?|\b(how|what|when|where|why|do you|can you|could you|"
    r"is it|are you)\b",
    re.IGNORECASE,
)
_SPAM_PATTERNS = re.compile(
    r"\b(boost your seo|guaranteed traffic|click here to claim|"
    r"crypto investment|nigerian prince|won the lottery|free vacation|"
    r"limited time offer|act now)\b",
    re.IGNORECASE,
)


def _heuristic(text: str, hints: dict) -> list[str] | None:
    """Returns tags only if at least one strong signal fires — otherwise
    returns None so the LLM gets a swing. We DON'T fast-path everything
    because the LLM is better at nuance for the gray middle."""
    tags = []
    if _SPAM_PATTERNS.search(text):
        return ["spam"]
    if _URGENT_PATTERNS.search(text):
        tags.append("urgent")
    if _COMPLAINT_PATTERNS.search(text):
        tags.append("complaint")
    if _COMPLIMENT_PATTERNS.search(text):
        tags.append("compliment")
    # If exactly ONE strong category fired and no question mark, return it
    if len(tags) == 1 and not _QUESTION_PATTERNS.search(text):
        return tags
    return None  # let the LLM decide


def _heuristic_fallback(text: str, hints: dict) -> list[str]:
    """Always-returns-something fallback when the LLM is unreachable."""
    tags = []
    if _URGENT_PATTERNS.search(text):    tags.append("urgent")
    if _COMPLAINT_PATTERNS.search(text): tags.append("complaint")
    if _COMPLIMENT_PATTERNS.search(text):tags.append("compliment")
    if _QUESTION_PATTERNS.search(text):  tags.append("question")
    if _SPAM_PATTERNS.search(text):      tags.append("spam")
    if hints.get("type") == "lead":      tags.append("lead")
    if not tags:
        tags = ["other"]
    return tags[:3]


# ── LLM classification (the smart path) ────────────────────────────────


_SYSTEM_PROMPT = """You are a message-classifier for a business inbox.

Classify the message into 1 to 3 tags from this EXACT list (do not invent new ones):
  urgent     - needs reply within a few hours
  lead       - potential new customer asking about services / pricing
  customer   - existing customer (paying, returning, been served before)
  question   - asking how/what/when/where/why
  complaint  - frustrated tone, negative experience
  compliment - positive feedback, thank-you, kind words
  supplier   - vendor / contractor / B2B outreach
  personal   - friend, family, non-business
  spam       - bulk sales pitch, phishing, irrelevant
  other      - none of the above fits

Output ONLY a JSON array of tag strings, e.g. ["urgent","question"].
No prose, no explanation, no preamble. Just the array."""


def _llm(config: dict, text: str, hints: dict) -> list[str]:
    import llm_client
    context = text[:1500]
    if hints.get("from_name"):
        context = f"From: {hints['from_name']}\n\n{context}"
    if hints.get("type"):
        context = f"Source type: {hints['type']}\n{context}"
    resp = llm_client.generate(config, _SYSTEM_PROMPT,
                                [{"role": "user", "content": context}])
    raw = (resp.text or "").strip()
    return _parse_tags(raw)


def _parse_tags(s: str) -> list[str]:
    if not s:
        return ["other"]
    # Try JSON first
    try:
        # Find the FIRST [...] block in case the LLM wrapped it in prose
        m = re.search(r"\[[^\]]*\]", s, re.DOTALL)
        if m:
            arr = json.loads(m.group(0))
            tags = [t.strip().lower() for t in arr if isinstance(t, str)]
            tags = [t for t in tags if t in VALID_TAGS]
            return tags[:3] if tags else ["other"]
    except (json.JSONDecodeError, ValueError):
        pass
    # Last resort: scan for any of our valid tags as words in the response
    found = [t for t in VALID_TAGS if re.search(r"\b" + t + r"\b", s, re.IGNORECASE)]
    return found[:3] if found else ["other"]
