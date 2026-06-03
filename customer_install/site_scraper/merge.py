"""Fold every page's extraction into one unified business profile.

Design: lists accumulate + dedupe (services, menu_items, faq, specials).
Single-value fields prefer non-empty across pages, with source tracking
so we know which URL each fact came from. The merged profile is the
shape `business_info.json` expects, plus extras under `_pages` and
`_sources`.
"""
from __future__ import annotations

import re
from collections import OrderedDict


def merge_pages(extracts: list[dict]) -> dict:
    """`extracts` = [{url, signals, llm_data}, ...]. Returns a unified
    business profile dict."""
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
