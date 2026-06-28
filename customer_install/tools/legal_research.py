"""
tools/legal_research.py — real legal database search for the paralegal module.

Sources hit in order:
  1. CourtListener API  — free, covers federal + most state courts
  2. Caselaw Access Project (Harvard) — digitized US case law, free API
  3. Cornell LII (DuckDuckGo targeted) — statutes, CFR, constitution
  4. Justia (DuckDuckGo targeted) — state + federal case law, codes
  5. Google Scholar (DuckDuckGo targeted) — case law fallback

Returns a structured dict with cases, statutes, and secondary sources
that gets injected into the LLM research prompt so citations are REAL.
"""

import json
import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_TIMEOUT = 8  # seconds per external request


# ---------------------------------------------------------------------------
# CourtListener — free REST API, no key required for basic searches
# ---------------------------------------------------------------------------

def _courtlistener_search(query: str, jurisdiction: str = "",
                           limit: int = 8) -> list[dict]:
    """Search CourtListener for case law. Returns list of case dicts."""
    params = {
        "q": query,
        "type": "o",        # opinions
        "order_by": "score desc",
        "format": "json",
        "page_size": limit,
        "stat_Precedential": "on",
    }
    if jurisdiction:
        jx_lower = jurisdiction.lower()
        # Map common jurisdiction strings to CourtListener court IDs
        _JX_MAP = {
            "9th circuit": "ca9", "ninth circuit": "ca9",
            "5th circuit": "ca5", "fifth circuit": "ca5",
            "federal": "scotus,ca1,ca2,ca3,ca4,ca5,ca6,ca7,ca8,ca9,ca10,ca11",
            "supreme court": "scotus",
            "nevada": "nvd,nvsupct", "nv": "nvd,nvsupct",
            "california": "calctapp,casc", "ca": "calctapp,casc",
            "texas": "txs,txsupct", "tx": "txs,txsupct",
            "new york": "nyed,nysd,nywd,nynd,nysupct", "ny": "nyed,nysd,nywd,nynd",
            "florida": "flmd,flnd,flsd,flsupct", "fl": "flmd,flnd,flsd,flsupct",
        }
        for key, court_id in _JX_MAP.items():
            if key in jx_lower:
                params["court"] = court_id
                break

    url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "myOrby Legal Research / educational use",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read())
        results = []
        for hit in (data.get("results") or [])[:limit]:
            case_name = hit.get("caseName") or hit.get("case_name") or ""
            citation = (hit.get("citation") or [{}])
            if isinstance(citation, list) and citation:
                cite_str = citation[0] if isinstance(citation[0], str) else str(citation[0])
            else:
                cite_str = ""
            court = hit.get("court_id") or hit.get("court") or ""
            date = (hit.get("dateFiled") or hit.get("date_filed") or "")[:10]
            snippet = (hit.get("snippet") or "").strip()
            url_path = hit.get("absolute_url") or ""
            full_url = f"https://www.courtlistener.com{url_path}" if url_path else ""
            if case_name:
                results.append({
                    "type": "case",
                    "name": case_name,
                    "citation": cite_str,
                    "court": court,
                    "date": date,
                    "snippet": snippet[:400],
                    "url": full_url,
                    "source": "CourtListener",
                })
        log.info(f"CourtListener: {len(results)} results for {query!r}")
        return results
    except Exception as e:
        log.warning(f"CourtListener search failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Caselaw Access Project (Harvard) — digitized case law, free API
# ---------------------------------------------------------------------------

def _cap_search(query: str, jurisdiction: str = "", limit: int = 5) -> list[dict]:
    """Search Harvard's Caselaw Access Project."""
    params = {
        "search": query,
        "page_size": limit,
        "ordering": "-decision_date",
        "full_case": "false",
    }
    if jurisdiction:
        jx_lower = jurisdiction.lower()
        _CAP_JX = {
            "nevada": "nev", "nv": "nev",
            "california": "cal", "ca": "cal",
            "new york": "n.y.", "ny": "n.y.",
            "texas": "tex.", "tx": "tex.",
            "federal": "us",
            "supreme court": "us",
            "florida": "fla.", "fl": "fla.",
        }
        for key, jx_code in _CAP_JX.items():
            if key in jx_lower:
                params["jurisdiction"] = jx_code
                break

    url = "https://api.case.law/v1/cases/?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "myOrby Legal Research / educational use",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read())
        results = []
        for hit in (data.get("results") or [])[:limit]:
            name = hit.get("name_abbreviation") or hit.get("name") or ""
            citations = hit.get("citations") or []
            cite_str = citations[0].get("cite", "") if citations else ""
            court = (hit.get("court") or {}).get("name_abbreviation", "")
            date = (hit.get("decision_date") or "")[:10]
            url_val = hit.get("url", "")
            if name:
                results.append({
                    "type": "case",
                    "name": name,
                    "citation": cite_str,
                    "court": court,
                    "date": date,
                    "snippet": "",
                    "url": url_val,
                    "source": "Caselaw Access Project (Harvard)",
                })
        log.info(f"CAP: {len(results)} results for {query!r}")
        return results
    except Exception as e:
        log.warning(f"CAP search failed: {e}")
        return []


# ---------------------------------------------------------------------------
# DuckDuckGo targeted at legal databases
# ---------------------------------------------------------------------------

def _ddg_legal(query: str, site: str = "", limit: int = 5) -> list[dict]:
    """DuckDuckGo search targeted at a legal site."""
    full_query = f"site:{site} {query}" if site else query
    encoded = urllib.parse.urlencode({"q": full_query, "kl": "us-en"})
    url = "https://html.duckduckgo.com/html/"
    try:
        req = urllib.request.Request(
            url,
            data=encoded.encode("utf-8"),
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="replace")

        results = []
        # Extract result titles + snippets from DDG HTML
        title_re = re.compile(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
        snippet_re = re.compile(
            r'class="result__snippet"[^>]*>(.*?)</span>', re.S)
        tag_re = re.compile(r'<[^>]+>')

        titles = title_re.findall(html)
        snippets = [tag_re.sub('', s).strip()
                    for s in snippet_re.findall(html)]

        for i, (href, title_html) in enumerate(titles[:limit]):
            title = tag_re.sub('', title_html).strip()
            snippet = snippets[i] if i < len(snippets) else ""
            # DDG wraps URLs — try to extract real URL
            real_url = href
            uddg_m = re.search(r'uddg=([^&]+)', href)
            if uddg_m:
                try:
                    real_url = urllib.parse.unquote(uddg_m.group(1))
                except Exception:
                    pass
            if title:
                results.append({
                    "type": "statute" if "law.cornell.edu" in real_url else "secondary",
                    "name": title,
                    "citation": "",
                    "court": "",
                    "date": "",
                    "snippet": snippet[:400],
                    "url": real_url,
                    "source": site or "web",
                })
        log.info(f"DDG legal ({site}): {len(results)} results for {query!r}")
        return results
    except Exception as e:
        log.warning(f"DDG legal search failed ({site}): {e}")
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search(query: str, jurisdiction: str = "",
           practice_area: str = "") -> dict:
    """
    Run a full legal research search across all sources.

    Returns:
        {
          "cases": [...],        # CourtListener + CAP results
          "statutes": [...],     # Cornell LII results
          "secondary": [...],    # Justia + Google Scholar
          "query": str,
          "jurisdiction": str,
        }
    """
    enhanced_query = query
    if practice_area:
        enhanced_query = f"{practice_area}: {query}"

    cases = []
    statutes = []
    secondary = []

    # 1. CourtListener — best for federal + state precedential opinions
    cases.extend(_courtlistener_search(enhanced_query, jurisdiction, limit=6))

    # 2. Caselaw Access Project — historical + state court depth
    cap = _cap_search(query, jurisdiction, limit=4)
    # Deduplicate by citation
    existing_cites = {c["citation"] for c in cases if c["citation"]}
    for r in cap:
        if r["citation"] not in existing_cites:
            cases.append(r)
            existing_cites.add(r["citation"])

    # 3. Cornell LII for statutes and constitutional provisions
    statutes.extend(_ddg_legal(query, site="law.cornell.edu", limit=4))

    # 4. Justia for additional case law and code sections
    justia = _ddg_legal(query, site="justia.com", limit=4)
    for r in justia:
        r["type"] = "case" if "case" in r["url"].lower() or "law" in r["url"].lower() else "secondary"
        secondary.append(r)

    # 5. Google Scholar as a fallback for case law
    if len(cases) < 3:
        scholar = _ddg_legal(
            f"{query} case law {jurisdiction}".strip(),
            site="scholar.google.com", limit=3
        )
        for r in scholar:
            r["type"] = "case"
            cases.append(r)

    return {
        "query": query,
        "jurisdiction": jurisdiction,
        "practice_area": practice_area,
        "cases": cases[:10],
        "statutes": statutes[:5],
        "secondary": secondary[:5],
        "total": len(cases) + len(statutes) + len(secondary),
    }


def format_for_prompt(results: dict) -> str:
    """Format search results into a block to inject into the LLM prompt."""
    lines = [
        "═══ REAL LEGAL RESEARCH RESULTS ═══",
        "The following were retrieved from live legal databases.",
        "Build your memo around these actual sources. Do NOT invent",
        "citations not shown here. If a section has no results, say so.",
        "",
    ]

    if results.get("cases"):
        lines.append("── CASE LAW ──")
        for c in results["cases"]:
            name = c.get("name", "Unknown")
            cite = c.get("citation", "")
            court = c.get("court", "")
            date = c.get("date", "")
            snippet = c.get("snippet", "")
            source = c.get("source", "")
            url = c.get("url", "")
            line = f"• {name}"
            if cite:
                line += f", {cite}"
            if court or date:
                line += f" ({', '.join(x for x in [court, date] if x)})"
            if source:
                line += f" [via {source}]"
            lines.append(line)
            if snippet:
                lines.append(f"  {snippet[:300]}")
            if url:
                lines.append(f"  {url}")
        lines.append("")

    if results.get("statutes"):
        lines.append("── STATUTES / REGULATIONS ──")
        for s in results["statutes"]:
            lines.append(f"• {s.get('name','')}")
            if s.get("snippet"):
                lines.append(f"  {s['snippet'][:300]}")
            if s.get("url"):
                lines.append(f"  {s['url']}")
        lines.append("")

    if results.get("secondary"):
        lines.append("── SECONDARY SOURCES ──")
        for s in results["secondary"]:
            lines.append(f"• {s.get('name','')}")
            if s.get("snippet"):
                lines.append(f"  {s['snippet'][:200]}")
            if s.get("url"):
                lines.append(f"  {s['url']}")
        lines.append("")

    if not (results.get("cases") or results.get("statutes") or results.get("secondary")):
        lines.append("No results found in legal databases. LLM will draw from training data only.")
        lines.append("Attorney must independently verify all citations.")

    lines.append("═══ END RESEARCH RESULTS ═══")
    return "\n".join(lines)
