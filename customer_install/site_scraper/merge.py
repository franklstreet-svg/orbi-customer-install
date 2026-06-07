"""Fold every page's extraction into one unified business profile.

Design: lists accumulate + dedupe (services, menu_items, faq, specials).
Single-value fields prefer non-empty across pages, with source tracking
so we know which URL each fact came from. The merged profile is the
shape `business_info.json` expects, plus extras under `_pages` and
`_sources`.

NAME PICKING: a separate post-merge step scores all candidate names
(from per-page LLM extractions, plus org-shaped names mined out of the
description / other fields) and replaces the merged name with the
highest-scoring candidate. Earlier behavior was first-non-empty-wins,
which baked in marketing taglines like "SCS | The Builders Exchange"
when the real org name (Sierra Contractors Source) appeared further
into the page body.
"""
from __future__ import annotations

import re
from collections import OrderedDict


_NAME_SEPARATORS = (" | ", " — ", " - ", ": ")
_CORPORATE_SUFFIX_TOKENS = (
    "llc", "inc", "incorporated", "corp", "corporation", "co.", "co,",
    "ltd", "limited", "group", "holdings", "source", "services",
    "company", "associates", "partners", "enterprises", "industries",
)

# "Sierra Contractors Source, 'The Builders Exchange' is more than..."
# → grab "Sierra Contractors Source" — 2-5 capitalized words at the start
# of a sentence, followed by punctuation or a verb that introduces the
# business.
_NAME_FROM_SENTENCE_RE = re.compile(
    r"^([A-Z][A-Za-z'&\-]+(?:\s+(?:&|and|of|the|de|la|el)?\s*[A-Z][A-Za-z'&\-]+){1,5})"
    r"(?:[,.;]|\s+(?:is|are|was|were|provides|offers|specializes|has\s+been|"
    r"serves|started|founded|operates))",
    re.MULTILINE,
)


def _score_name_candidate(candidate: str, body_text: str) -> int:
    """Score a business-name candidate. Higher = more likely the legal name.
    Used to break ties when multiple candidate names appear across pages."""
    if not candidate or not candidate.strip():
        return -1000
    cand = candidate.strip()
    low = cand.lower()
    score = 0
    # Corporate suffix = strong signal this is a legal entity name.
    if any(f" {tok}" in f" {low}" or low.endswith(tok)
            for tok in _CORPORATE_SUFFIX_TOKENS):
        score += 50
    # Title with " | " or " - " separator = probably a page title, not a
    # legal name. Heavy penalty.
    if any(sep in cand for sep in _NAME_SEPARATORS):
        score -= 30
    # All-caps acronym (≤5 chars) = probably an abbreviation, not the
    # spelled-out legal name.
    if len(cand) <= 5 and cand.isupper():
        score -= 20
    # Occurrence count in body text = repetition boost (cap at +30).
    if body_text:
        count = body_text.lower().count(low)
        score += min(count * 5, 30)
    # Word count boost — legal names tend to be 2-5 words. Cap at +6.
    score += min(len(cand.split()), 6)
    return score


def _mine_name_candidates_from_text(text: str) -> list[str]:
    """Find org-shaped candidate names in body text (description, other)."""
    if not text:
        return []
    found: list[str] = []
    # Walk sentence by sentence so the start-of-sentence anchor works.
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        m = _NAME_FROM_SENTENCE_RE.match(sentence.strip())
        if m:
            found.append(m.group(1).strip())
    return found


def _pick_best_name(candidates: list[str], body_text: str) -> str:
    """From a list of candidate names, pick the highest-scoring one."""
    cleaned = [c.strip() for c in candidates if c and c.strip()]
    if not cleaned:
        return ""
    seen: set[str] = set()
    unique = []
    for c in cleaned:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)
    scored = [(c, _score_name_candidate(c, body_text)) for c in unique]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0]


def merge_pages(extracts: list[dict]) -> dict:
    """`extracts` = [{url, signals, llm_data}, ...]. Returns a unified
    business profile dict."""
    # Collected per-page name candidates (with source URLs). Used in the
    # post-merge name-picking pass below.
    name_candidates: list[tuple[str, str]] = []  # (candidate_name, source_url)

    out: dict = {
        "name": "",
        "tagline": "",
        "description": "",
        "owner": {"name": "", "role": "", "bio": ""},
        "founded_year": "",
        "ownership": "",
        "address": {"street": "", "city": "", "state": "", "zip": ""},
        "contact": {"phone": "", "email": "", "website": ""},
        "social": {},
        "hours": {},
        "service_area": "",
        "services": [],
        "menu_items": [],
        "faq": [],
        "policies": {},
        "pricing_notes": "",
        "specials": [],
        "events": [],
        "staff": [],
        "parking": "",
        "accessibility": "",
        "credentials": [],
        "other": [],
        "_sources": {},   # field path → URL where the value came from
        "_pages": [],     # list of {url, title, signals} for everything we read
    }

    for entry in extracts:
        url = entry.get("url", "")
        data = entry.get("llm_data") or {}
        signals = entry.get("signals") or {}
        title = entry.get("title", "")
        out["_pages"].append({"url": url, "title": title, "signals": signals})

        # ── String fields: prefer first non-empty value, record source
        for field in ("name", "tagline", "description", "service_area",
                       "pricing_notes", "parking", "accessibility",
                       "founded_year", "ownership"):
            val = (data.get(field) or "").strip() if isinstance(data.get(field), str) else ""
            if val and not out[field]:
                out[field] = val
                out["_sources"][field] = url
            # ALSO track every page's name candidate for the post-merge
            # scoring pass (even if the field is already set on `out`).
            if field == "name" and val:
                name_candidates.append((val, url))

        # ── Owner — merge piecemeal (different pages may have name vs role
        # vs bio); first non-empty per sub-key wins.
        owner = data.get("owner") or {}
        if isinstance(owner, dict):
            for k in ("name", "role", "bio"):
                v = (owner.get(k) or "").strip() if isinstance(owner.get(k), str) else ""
                if v and not out["owner"][k]:
                    out["owner"][k] = v
                    out["_sources"][f"owner.{k}"] = url

        # ── Address — merge piecemeal (each page might only have part)
        addr = data.get("address") or {}
        if isinstance(addr, dict):
            for k in ("street", "city", "state", "zip"):
                v = (addr.get(k) or "").strip() if isinstance(addr.get(k), str) else ""
                if v and not out["address"][k]:
                    out["address"][k] = v
                    out["_sources"][f"address.{k}"] = url

        # ── Contact (phone / email / website)
        for k in ("phone", "email", "website"):
            v = (data.get(k) or "").strip() if isinstance(data.get(k), str) else ""
            if v and not out["contact"][k]:
                out["contact"][k] = v
                out["_sources"][f"contact.{k}"] = url

        # ── Social — merge dicts
        social = data.get("social") or {}
        if isinstance(social, dict):
            for k, v in social.items():
                v = (v or "").strip() if isinstance(v, str) else ""
                if v and k not in out["social"]:
                    out["social"][k] = v
                    out["_sources"][f"social.{k}"] = url

        # ── Hours — first page that has any wins; later pages can fill gaps
        hours = data.get("hours") or {}
        if isinstance(hours, dict):
            for day, hrs in hours.items():
                day_l = day.lower()
                if day_l not in out["hours"] and isinstance(hrs, dict):
                    out["hours"][day_l] = hrs
                    out["_sources"][f"hours.{day_l}"] = url

        # ── Policies — first non-empty per key
        policies = data.get("policies") or {}
        if isinstance(policies, dict):
            for k, v in policies.items():
                v = (v or "").strip() if isinstance(v, str) else ""
                if v and not out["policies"].get(k):
                    out["policies"][k] = v
                    out["_sources"][f"policies.{k}"] = url

        # ── Lists — accumulate + dedupe by name (for items) or text
        for field in ("services", "menu_items"):
            for item in (data.get(field) or []):
                if not isinstance(item, dict):
                    continue
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                # Dedupe by lowered name
                key = name.lower()
                if any((i.get("name") or "").strip().lower() == key
                        for i in out[field]):
                    continue
                item["_source_url"] = url
                out[field].append(item)

        # FAQ — dedupe by question
        for q in (data.get("faq") or []):
            if not isinstance(q, dict):
                continue
            qtext = (q.get("q") or "").strip()
            atext = (q.get("a") or "").strip()
            if not (qtext and atext):
                continue
            if any((f.get("q") or "").strip().lower() == qtext.lower()
                    for f in out["faq"]):
                continue
            q["_source_url"] = url
            out["faq"].append(q)

        # Free-form lists — append + dedupe by lowercased value
        for field in ("specials", "credentials", "other"):
            for v in (data.get(field) or []):
                v = (v or "").strip() if isinstance(v, str) else ""
                if not v: continue
                if v.lower() in (x.lower() if isinstance(x, str) else ""
                                  for x in out[field]):
                    continue
                out[field].append(v)

        # Events + staff — dedupe by name
        for field in ("events", "staff"):
            for item in (data.get(field) or []):
                if not isinstance(item, dict):
                    continue
                name = (item.get("name") or "").strip()
                if not name: continue
                if any((i.get("name") or "").strip().lower() == name.lower()
                        for i in out[field]):
                    continue
                item["_source_url"] = url
                out[field].append(item)

        # ── Regex-fallback singles (when LLM was off)
        # Phone/email/address_raw from llm_extract._regex_extract
        for k in ("phone", "email"):
            v = data.get(k)
            if isinstance(v, str) and v.strip() and not out["contact"][k]:
                out["contact"][k] = v.strip()
                out["_sources"][f"contact.{k}"] = url
        if isinstance(data.get("address_raw"), str) and not out["address"]["street"]:
            # Best-effort split: "<street>, <city>, <ST> <zip>"
            raw = data["address_raw"]
            m = re.match(r"^(.+?),\s*(.+?),?\s+([A-Z]{2})\s+(\d{5})", raw)
            if m:
                out["address"] = {
                    "street": m.group(1).strip(),
                    "city":   m.group(2).strip(),
                    "state":  m.group(3),
                    "zip":    m.group(4),
                }
                out["_sources"]["address"] = url

    # ── Post-merge NAME pick ──────────────────────────────────────────
    # The first-non-empty-wins logic above can lock in a marketing
    # tagline (e.g. page title "SCS | The Builders Exchange") when the
    # real legal name (e.g. "Sierra Contractors Source") is in the
    # description or repeated in the body. Re-score all candidates +
    # candidates mined from the merged description/other fields, then
    # pick the best one.
    body_text_parts = [out.get("description", "") or ""]
    other_list = out.get("other") or []
    if isinstance(other_list, list):
        body_text_parts.extend([x for x in other_list if isinstance(x, str)])
    body_text = " ".join(body_text_parts)

    # Add candidates mined from description + other (org-style name patterns).
    mined = _mine_name_candidates_from_text(body_text)
    for m_name in mined:
        name_candidates.append((m_name, "(mined from description/other)"))

    # Also split any title-shaped candidates on separators so the parts
    # get scored individually.
    expanded: list[tuple[str, str]] = list(name_candidates)
    for cand, src in name_candidates:
        for sep in _NAME_SEPARATORS:
            if sep in cand:
                for half in cand.split(sep):
                    half = half.strip()
                    if half:
                        expanded.append((half, src))

    if expanded:
        cand_strs = [c for c, _ in expanded]
        best = _pick_best_name(cand_strs, body_text)
        if best and best.lower() != (out.get("name") or "").lower():
            old_name = out.get("name", "")
            # Record source of the new name (mined or per-page extraction)
            best_src = next(
                (src for c, src in expanded if c.strip().lower() == best.lower()),
                out["_sources"].get("name", "(post-merge name pick)")
            )
            out["name"] = best
            out["_sources"]["name"] = best_src
            # If the displaced name was a title with a separator, promote
            # the non-name half into tagline (unless tagline already set).
            if old_name and not out.get("tagline"):
                for sep in _NAME_SEPARATORS:
                    if sep in old_name:
                        halves = [h.strip() for h in old_name.split(sep, 1)]
                        non_best = [h for h in halves if h.lower() != best.lower()]
                        if non_best:
                            out["tagline"] = non_best[0]
                            out["_sources"]["tagline"] = out["_sources"].get(
                                "name", "(promoted from displaced name)")
                        break

    return out


def confidence(profile: dict) -> dict:
    """For each important field, classify as 'high' / 'low' / 'missing'.
    Drives the gap-fill onboarding wizard later."""
    def has(v):
        if v is None: return False
        if isinstance(v, str): return bool(v.strip())
        if isinstance(v, (list, dict)): return len(v) > 0
        return bool(v)

    return {
        "name":         "high" if has(profile.get("name")) else "missing",
        "tagline":      "high" if has(profile.get("tagline")) else "missing",
        "description":  "high" if has(profile.get("description")) else "missing",
        "address":      "high" if has(profile.get("address", {}).get("street")) else "missing",
        "phone":        "high" if has(profile.get("contact", {}).get("phone")) else "missing",
        "email":        "high" if has(profile.get("contact", {}).get("email")) else "missing",
        "hours":        "high" if len(profile.get("hours", {})) >= 5 else (
                        "low" if has(profile.get("hours")) else "missing"),
        "services":     "high" if len(profile.get("services", [])) >= 3 else (
                        "low" if has(profile.get("services")) else "missing"),
        "menu_items":   "high" if len(profile.get("menu_items", [])) >= 3 else (
                        "low" if has(profile.get("menu_items")) else "missing"),
        "faq":          "high" if has(profile.get("faq")) else "missing",
        "policies":     "high" if has(profile.get("policies")) else "missing",
    }
