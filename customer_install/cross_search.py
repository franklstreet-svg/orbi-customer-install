"""
cross_search — explicit "I actually looked" guarantee.

Adapted from orby_5050's _cross_search + _format_no_match_refusal
pattern. The point: when a visitor asks Orbi about something
business-specific, she should ACTUALLY search every data source
the owner has populated before claiming she doesn't know.

Currently /chat already runs catalog → workspace → web in cascade.
This module adds:

  1. A unified search that hits catalog + workspace + notes +
     business_info in one call, returning labeled matches.
  2. An honest-refusal formatter that names EVERY folder Orbi
     checked so the visitor sees the work (and the owner has
     evidence Orbi tried).
  3. A topic-fallback map (B2B version of orby_5050's
     _TOPIC_FALLBACK_MAP) that catches common phrasings the
     primary search might miss.

Why this matters for B2B:
  - LLM bluffing on a missing fact = lost customer trust.
  - Honest "I checked these 4 places and didn't find it — let me
    check with the owner and get back to you" preserves trust AND
    routes the question into the learning loop where it becomes
    permanent knowledge.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("orbi.cross_search")


# ── Topic-keyword fallback map (B2B version) ────────────────────────────
# When a user message contains any of these keywords, we hint to the LLM
# that the answer (if any) is most likely in the indicated source. This
# is the fallback layer beneath the primary catalog/workspace/notes
# checks — catches phrasing the primary check might miss.
_TOPIC_FALLBACK_MAP = [
    # keyword regex                                  source hint
    (r"\b(menu|item|product|sku|part|model|brand|stock|in stock|inventory)\b",  "catalog"),
    (r"\b(hours|open|closing|opening|operating|schedule)\b",                     "business_info.hours"),
    (r"\b(where|address|location|directions|drive|map|find you)\b",              "business_info.address"),
    (r"\b(phone|call|number|reach you|contact)\b",                               "business_info.phone"),
    (r"\b(email|e-mail|message us)\b",                                           "business_info.email"),
    (r"\b(website|web site|url|site)\b",                                         "business_info.website"),
    (r"\b(service|services|do you|offer|provide|sell)\b",                        "business_info.services"),
    (r"\b(owner|manager|founder|run by|run the|in charge)\b",                    "business_info.owner_name"),
    (r"\b(policy|policies|terms|refund|return|guarantee|warranty)\b",            "notes/business_info.policies"),
    (r"\b(faq|q and a|q&a|frequently asked|common question)\b",                  "notes"),
    (r"\b(special|deal|sale|discount|coupon|promotion|promo)\b",                 "workspace/notes"),
    (r"\b(price|cost|how much|fee)\b",                                           "catalog/notes"),
    (r"\b(appointment|booking|schedule a|reserve|reservation)\b",                "learning_loop"),
    (r"\b(emergency|urgent|asap|right now|right away)\b",                        "business_info.emergency"),
    (r"\b(insurance|insured|bonded|certified|licensed)\b",                       "business_info/notes"),
    (r"\b(history|founded|years|established|since)\b",                           "business_info/notes"),
]


# ── Public API ───────────────────────────────────────────────────────────


def search(data_dir: Path, query: str,
           include_workspace: bool = True) -> dict:
    """Multi-source search across every place the owner might have
    populated. Returns:

      {
        "matches": [ {"source": "catalog", "snippet": "...", "score": N}, ... ],
        "folders_searched": ["catalog", "notes", "business_info", ...],
        "topic_hints": ["catalog", "business_info.hours", ...],  # from fallback map
        "total_hits": N,
      }

    The caller (orbi.py /chat) decides what to do with this — usually
    inject the matches as authoritative context to the LLM, or call
    format_no_match_refusal() if total_hits == 0.
    """
    out = {
        "matches": [],
        "folders_searched": [],
        "topic_hints": [],
        "total_hits": 0,
    }
    if not query or not query.strip():
        return out

    q_lower = query.lower()

    # 1. Topic hints — fallback map
    for pattern, source_hint in _TOPIC_FALLBACK_MAP:
        if re.search(pattern, q_lower, re.IGNORECASE):
            out["topic_hints"].append(source_hint)

    # 2. Catalog
    out["folders_searched"].append("catalog")
    try:
        from modules import catalog as mod_catalog
        cat_hits = mod_catalog.search(data_dir, query, limit=5)
        for h in cat_hits:
            if h.get("score", 0) >= 5:
                bits = [h.get("name", "")]
                if h.get("sku"):     bits.append(f"part #{h['sku']}")
                if h.get("price") is not None: bits.append(f"${h['price']:.2f}")
                if h.get("stock") is not None: bits.append(f"{h['stock']} in stock")
                out["matches"].append({
                    "source": "catalog",
                    "snippet": " · ".join(b for b in bits if b),
                    "score": float(h.get("score", 0)),
                })
    except Exception as e:
        log.warning(f"catalog search failed: {e}")

    # 3. Notes — search note bodies
    out["folders_searched"].append("notes")
    try:
        notes_path = data_dir / "notes.json"
        if notes_path.exists():
            notes = json.loads(notes_path.read_text(encoding="utf-8")) or []
            for n in notes if isinstance(notes, list) else []:
                body = str(n.get("body") or n.get("text") or "")
                if body and _has_term_overlap(query, body):
                    out["matches"].append({
                        "source": "notes",
                        "snippet": body[:200],
                        "score": _term_overlap_score(query, body),
                    })
    except Exception as e:
        log.warning(f"notes search failed: {e}")

    # 4. Business info — single dict, search each field
    out["folders_searched"].append("business_info")
    try:
        biz_path = data_dir / "business_info.json"
        if biz_path.exists():
            biz = json.loads(biz_path.read_text(encoding="utf-8")) or {}
            for field, val in biz.items():
                if not val:
                    continue
                if isinstance(val, (dict, list)):
                    val_str = json.dumps(val)
                else:
                    val_str = str(val)
                if _has_term_overlap(query, val_str):
                    out["matches"].append({
                        "source": f"business_info.{field}",
                        "snippet": val_str[:160],
                        "score": _term_overlap_score(query, val_str) + 2,  # slight boost
                    })
    except Exception as e:
        log.warning(f"business_info search failed: {e}")

    # 5. Workspace (already indexed by workspace module)
    if include_workspace:
        out["folders_searched"].append("workspace")
        try:
            ws_index_path = data_dir / "workspace_index.json"
            if ws_index_path.exists():
                idx = json.loads(ws_index_path.read_text(encoding="utf-8")) or {}
                for file_entry in (idx.get("files") if isinstance(idx, dict) else []) or []:
                    content = str(file_entry.get("content", ""))
                    name = str(file_entry.get("name", ""))
                    if content and _has_term_overlap(query, content):
                        out["matches"].append({
                            "source": f"workspace/{name}",
                            "snippet": content[:200],
                            "score": _term_overlap_score(query, content),
                        })
        except Exception as e:
            log.warning(f"workspace search failed: {e}")

    # Sort matches by score (highest first), dedup by snippet
    seen = set()
    deduped = []
    for m in sorted(out["matches"], key=lambda x: x["score"], reverse=True):
        key = (m["source"], m["snippet"][:80])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)
    out["matches"] = deduped[:10]  # cap at top 10
    out["total_hits"] = len(out["matches"])
    return out


def format_no_match_refusal(query: str, folders_searched: list[str]) -> str:
    """When the cross-search returns 0 hits, this honest refusal tells
    the visitor exactly where Orbi looked. Used by /chat when the
    visitor's question reads like a real business question but no
    data source has the answer.

    The refusal then naturally flows into the learning-loop capture
    in /chat (visitor gets asked for contact info, owner gets paged)."""
    folder_list = ", ".join(folders_searched) if folders_searched else "every source we have"
    return (
        f"I'm honestly not sure about that one — I searched our "
        f"{folder_list} and didn't find anything that matches \"{query[:80]}\". "
        f"Let me check with the owner and get back to you. "
        f"Can I get your name and best way to reach you (text, call, or email)?"
    )


def format_topic_hint_for_llm(topic_hints: list[str]) -> str:
    """Format the topic-fallback hints as an LLM system-prompt addendum.
    Tells the LLM 'the user is probably asking about X — focus there.'"""
    if not topic_hints:
        return ""
    deduped = sorted(set(topic_hints))
    return (
        "TOPIC HINTS (the visitor's phrasing matched these source keywords — "
        "answer from these data sources if possible, don't invent):\n  - "
        + "\n  - ".join(deduped)
    )


# ── Internal helpers ────────────────────────────────────────────────────


_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "for", "to",
    "in", "on", "at", "with", "by", "from", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "i", "me", "my", "you", "your", "he", "she", "it", "we", "they",
    "what", "where", "when", "why", "how", "which", "who",
    "this", "that", "these", "those",
}


def _tokenize(s: str) -> set[str]:
    """Lowercase + word-split + drop stop-words. Returns the meaningful
    content terms from a query or document."""
    if not s:
        return set()
    s = s.lower()
    raw = re.findall(r"\b[\w\-/]{2,}\b", s)
    return {t for t in raw if t not in _STOP_WORDS}


def _has_term_overlap(query: str, document: str, min_terms: int = 1) -> bool:
    q_tokens = _tokenize(query)
    d_tokens = _tokenize(document)
    if not q_tokens or not d_tokens:
        return False
    return len(q_tokens & d_tokens) >= min_terms


def _term_overlap_score(query: str, document: str) -> float:
    q_tokens = _tokenize(query)
    d_tokens = _tokenize(document)
    if not q_tokens or not d_tokens:
        return 0.0
    overlap = q_tokens & d_tokens
    return float(len(overlap) * 2)
