"""
url_fetch — fetch a specific URL the owner pastes into chat and return
its readable text content for the LLM.

This is the missing companion to web_search:
  - web_search   = "find me pages about X" (DuckDuckGo/Wikipedia)
  - url_fetch    = "go to THIS page and tell me what it says"

Pure stdlib (urllib.request + html.parser). No new pip deps.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

log = logging.getLogger("orbi.url_fetch")

USER_AGENT = "Orbi/0.1 (+https://twickell.com)"
MAX_BYTES = 1_500_000      # 1.5 MB cap on download
MAX_TEXT_CHARS = 12_000    # 12k chars of extracted text max

# Loose URL detector — matches http(s):// and bare domains the user pasted
_URL_RE = re.compile(
    r"https?://[^\s<>\"']+|(?<![\w/])(?:www\.)?[a-z0-9-]+\.(?:com|net|org|io|co|ai|app|gov|edu|us|uk|ca|au|de)(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """Pull every plausible URL out of the message. Returns a deduped,
    order-preserving list."""
    if not text:
        return []
    raw_hits = _URL_RE.findall(text)
    out = []
    seen = set()
    for hit in raw_hits:
        # Normalize: ensure http(s):// prefix
        if not hit.lower().startswith(("http://", "https://")):
            hit = "https://" + hit.lstrip("/")
        if hit in seen:
            continue
        seen.add(hit)
        out.append(hit)
    return out


def fetch(url: str, timeout: int = 10, follow_about: bool = True) -> dict:
    """Fetch a URL and return its readable text. Always returns a dict —
    on error, populates the 'error' field instead of raising.

    If follow_about=True (default), and the landing page has a visible
    'About' link, also pulls that page and merges its identity signals
    into the result — that's usually where the real company name lives.

    Returns:
      {
        "url":         "https://...",
        "ok":          True/False,
        "status":      200,
        "title":       "Page <title> tag",
        "official_name": "company name from structured data / footer / og:site_name",
        "tagline":     "from og:description / meta description",
        "copyright":   "© 2026 Sierra Contractors Source LLC.",
        "text":        "extracted readable text",
        "byte_count":  N,
        "error":       "" or "reason",
      }"""
    result = {"url": url, "ok": False, "status": 0, "title": "",
              "official_name": "", "tagline": "", "copyright": "",
              "text": "", "byte_count": 0, "error": ""}
    if not url:
        result["error"] = "no_url"
        return result
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result["status"] = resp.status
            ctype = resp.headers.get("Content-Type", "").lower()
            if "text/html" not in ctype and "text/plain" not in ctype and "application/xhtml" not in ctype:
                result["error"] = f"not_html (content-type: {ctype})"
                return result
            raw = resp.read(MAX_BYTES)
            result["byte_count"] = len(raw)
        # Decode best-effort
        charset = _detect_charset(ctype, raw)
        try:
            html = raw.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            html = raw.decode("utf-8", errors="replace")
        # Pull rich identity signals BEFORE stripping the markup
        identity = _extract_identity(html)
        title, text = _extract_readable(html)
        result["title"] = title[:200]
        result["text"] = text[:MAX_TEXT_CHARS]
        result["official_name"] = identity.get("official_name", "")[:200]
        result["tagline"]       = identity.get("tagline", "")[:300]
        result["copyright"]     = identity.get("copyright", "")[:200]
        result["ok"] = True

        # If we still don't have a confident official_name, peek at the About page
        if follow_about and not result["official_name"]:
            about_url = _find_about_url(html, url)
            if about_url and about_url != url:
                try:
                    sub = fetch(about_url, timeout=timeout, follow_about=False)
                    if sub.get("ok"):
                        if sub.get("official_name"):
                            result["official_name"] = sub["official_name"]
                        if sub.get("copyright"):
                            result["copyright"] = result["copyright"] or sub["copyright"]
                        # Append the About body so the LLM has more context
                        about_snippet = sub.get("text", "")[:3000]
                        if about_snippet:
                            result["text"] = (result["text"] + "\n\n--- ABOUT PAGE ---\n"
                                              + about_snippet)
                except Exception as e:
                    log.debug(f"about-page fetch failed: {e}")
    except urllib.error.HTTPError as e:
        result["status"] = e.code
        result["error"] = f"http_{e.code}"
    except urllib.error.URLError as e:
        result["error"] = f"network_error: {e.reason}"
    except Exception as e:
        result["error"] = f"fetch_failed: {e}"
    return result


def context_block(urls_text: list[dict]) -> str:
    """Format one or more fetch() results into an LLM-ready system addendum.
    Surfaces structured identity (official_name, tagline, copyright) at the
    top so the LLM doesn't mis-quote the page headline as the company name."""
    if not urls_text:
        return ""
    parts = ["WEB PAGES THE OWNER ASKED ME TO READ.\n"
             "When you answer, write in flowing prose — DO NOT print labels "
             "like 'Official Name:' or 'Copyright:' or 'Tagline:' literally. "
             "Use the values below as authoritative facts to weave into your "
             "answer. Specifically: if OFFICIAL_NAME is present, that is the "
             "real company name (the PAGE_TITLE is often a marketing brand "
             "or product name, not the company). If OFFICIAL_NAME is missing "
             "or blank, look inside PAGE_TEXT for a real company name (often "
             "in the 'About' section or footer). Quote real facts; never invent."]
    for r in urls_text:
        if not r.get("ok"):
            parts.append(f"\n--- {r.get('url')} ---\n(could not load: {r.get('error', 'unknown')})")
            continue
        url = r.get("url", "")
        parts.append(f"\n--- {url} ---")
        if r.get("official_name"):
            parts.append(f"OFFICIAL_NAME: {r['official_name']}")
        if r.get("copyright"):
            parts.append(f"COPYRIGHT: {r['copyright']}")
        if r.get("title"):
            parts.append(f"PAGE_TITLE: {r['title']}")
        if r.get("tagline"):
            parts.append(f"TAGLINE: {r['tagline']}")
        parts.append("PAGE_TEXT:")
        parts.append(r.get("text", "")[:8000])
    return "\n".join(parts)


# ── Identity extraction (the bit that fixes the "Builders Exchange" bug) ──

_OG_RE = re.compile(
    r'<meta\s+[^>]*?property=["\']og:([\w_:]+)["\'][^>]*?content=["\']([^"\']+)',
    re.IGNORECASE,
)
_META_NAME_RE = re.compile(
    r'<meta\s+[^>]*?name=["\']([\w_:-]+)["\'][^>]*?content=["\']([^"\']+)',
    re.IGNORECASE,
)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)\s*\d{4}(?:\s*[-–—]\s*\d{4})?\s+([^\n.<>|]{3,80})",
    re.IGNORECASE,
)
# Trailing license-cruft that's never part of a real company name
_LICENSE_CRUFT_RE = re.compile(
    r"\s*[-–—,.]?\s*(?:all\s+rights\s+reserved|rights\s+reserved|"
    r"all\s+rights|registered\s+trademark|tm|®|™)\.?$",
    re.IGNORECASE,
)
_FOOTER_BLOCK_RE = re.compile(
    r"<footer[^>]*>(.*?)</footer>", re.IGNORECASE | re.DOTALL,
)
_ABOUT_LINK_RE = re.compile(
    r'<a\s+[^>]*?href=["\']([^"\']+)["\'][^>]*?>([^<]*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _extract_identity(html: str) -> dict:
    """Pull official_name / tagline / copyright from structured page metadata.
    Priority order: schema.org JSON-LD > og:site_name > meta application-name
    > footer copyright > visible title."""
    out = {"official_name": "", "tagline": "", "copyright": ""}

    # 1. JSON-LD schema.org/Organization (most authoritative)
    for m in _JSONLD_RE.finditer(html):
        raw = m.group(1).strip()
        try:
            import json as _json
            data = _json.loads(raw)
        except (ValueError, _json.JSONDecodeError if False else ValueError):
            continue
        # Could be a single object, a list, or @graph
        candidates = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            candidates = data["@graph"]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            obj_type = obj.get("@type", "")
            if isinstance(obj_type, list):
                obj_type = obj_type[0] if obj_type else ""
            if obj_type and ("Organization" in obj_type or "LocalBusiness" in obj_type
                              or obj_type in ("Corporation", "Company")):
                name = obj.get("legalName") or obj.get("name") or ""
                if name and not out["official_name"]:
                    out["official_name"] = str(name).strip()
                desc = obj.get("description", "")
                if desc and not out["tagline"]:
                    out["tagline"] = str(desc).strip()

    # 2. og: + meta name= signals
    og = {k.lower(): v for k, v in _OG_RE.findall(html)}
    meta_names = {k.lower(): v for k, v in _META_NAME_RE.findall(html)}
    if not out["official_name"]:
        out["official_name"] = (og.get("site_name") or meta_names.get("application-name")
                                or meta_names.get("publisher") or "").strip()
    if not out["tagline"]:
        out["tagline"] = (og.get("description") or meta_names.get("description") or "").strip()

    # 3. Footer copyright line
    footers = _FOOTER_BLOCK_RE.findall(html)
    haystack = " ".join(footers) if footers else html[-4000:]   # fallback to bottom of doc
    # Strip tags from the haystack for easier regex
    haystack_text = re.sub(r"<[^>]+>", " ", haystack)
    cmatch = _COPYRIGHT_RE.search(haystack_text)
    if cmatch:
        copyright_who = cmatch.group(1).strip(" ,.|-—–")
        # Strip "All Rights Reserved" etc. tails that aren't part of the name
        copyright_who = _LICENSE_CRUFT_RE.sub("", copyright_who).strip(" ,.|-—–")
        if copyright_who and not copyright_who.lower().startswith(("all ", "the ")) \
           and copyright_who.lower() not in ("rights reserved", "all rights reserved"):
            out["copyright"] = "© " + copyright_who
            # Use the copyright's company name as official_name fallback —
            # but only if we got a real name (not just license text)
            if not out["official_name"] and 3 <= len(copyright_who) <= 100:
                out["official_name"] = copyright_who

    # Final sanity scrub on official_name — strip license cruft if any slipped through
    if out["official_name"]:
        cleaned = _LICENSE_CRUFT_RE.sub("", out["official_name"]).strip(" ,.|-—–")
        if cleaned and cleaned.lower() not in ("rights reserved", "all rights reserved", ""):
            out["official_name"] = cleaned
        else:
            out["official_name"] = ""

    # If official_name has newlines (page jammed multiple names together),
    # split and pick the one that looks most "legal-entity-like" — prefers
    # the candidate with corporate suffixes (LLC / Inc / Co / Corp / Source)
    # or simply the longer multi-word string over an all-caps tagline.
    if out["official_name"] and ("\n" in out["official_name"] or "  " in out["official_name"]):
        parts = [p.strip() for p in re.split(r"[\n\r]+", out["official_name"]) if p.strip()]
        if len(parts) > 1:
            corp_hints = re.compile(r"\b(LLC|Inc|Corp|Co\.?|Company|Source|Group|Holdings|Partners)\b", re.IGNORECASE)
            scored = sorted(parts, key=lambda s: (
                bool(corp_hints.search(s)),     # corporate suffix wins
                sum(c.islower() for c in s) > 0,  # mixed-case beats ALL CAPS
                len(s),                          # longer is usually more specific
            ), reverse=True)
            out["official_name"] = scored[0]

    # 4. Body-text intro pattern — "Company Name, 'tagline' is more than..."
    # If a full name appears in the page's narrative intro AND it's longer
    # / more specific than what we already have, prefer it. This catches
    # the "SCS" vs "Sierra Contractors Source" case where the visible
    # heading is an acronym but the prose spells out the full name.
    # Collapse the HTML to single-line plain text per line so the patterns
    # below can't accidentally span heading blocks.
    body_text_lines = []
    for line in re.sub(r"<[^>]+>", " ", html).splitlines():
        s = re.sub(r"[ \t]+", " ", line).strip()
        if s:
            body_text_lines.append(s)
    body_text = "\n".join(body_text_lines)

    # All patterns use [ ] (single space) instead of \s+ so they don't
    # accidentally glue together text from separate lines.
    intro_patterns = [
        # "Sierra Contractors Source, 'The Builders Exchange' is more than..."
        re.compile(r"([A-Z][\w]+(?:[ ][A-Z][\w&]+){1,5})[ ]*,[ ]*[\"'][^\"']+[\"'][ ]+(?:is|are|was)\b"),
        # "Welcome to Sierra Contractors Source."
        re.compile(r"(?:welcome to|founded by|established as|known as|operating as)[ ]+([A-Z][\w]+(?:[ ][A-Z][\w&]+){1,5})\b", re.IGNORECASE),
        # "Sierra Contractors Source has been serving / providing / etc."
        re.compile(r"\b([A-Z][\w]+(?:[ ][A-Z][\w&]+){2,5})[ ]+has[ ]+been[ ]+(?:serving|providing|helping|offering)\b"),
    ]
    for pat in intro_patterns:
        m = pat.search(body_text)
        if m:
            candidate = m.group(1).strip()
            # Prefer the candidate if it's NOTABLY longer than what we have
            # (catches "SCS" → "Sierra Contractors Source")
            if len(candidate) > len(out["official_name"]) + 4:
                out["official_name"] = candidate
            break

    return out


def _find_about_url(html: str, base_url: str) -> str:
    """Look for a visible 'About' / 'About Us' / 'Company' link and return
    the absolute URL. Returns '' if nothing found."""
    candidates = []
    for href, label in _ABOUT_LINK_RE.findall(html):
        label_clean = re.sub(r"<[^>]+>", "", label).strip().lower()
        # Only the obvious About-style labels — don't blindly follow every link
        if label_clean in ("about", "about us", "about-us", "our company",
                           "who we are", "our story", "company"):
            candidates.append(href)
    if not candidates:
        return ""
    href = candidates[0]
    # Resolve relative URLs
    return urllib.parse.urljoin(base_url, href)


# ── HTML → readable text ────────────────────────────────────────────────


class _Extractor(HTMLParser):
    """Strips boilerplate (scripts, styles, nav, footer) and keeps body text
    with structural cues — # for h1/h2/h3, blank lines for paragraphs."""

    SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "form"}
    # Tags we'd usually skip but they often contain page content — keep them
    KEEP_TAGS = {"main", "article", "section", "div", "p", "li", "td", "blockquote"}
    HEADING_TAGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### "}
    BLOCK_TAGS = {"p", "br", "li", "tr", "div", "section", "article",
                  "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}

    def __init__(self):
        super().__init__()
        self.skip_depth = 0
        self.title_parts = []
        self.in_title = False
        self.out = []  # list of text chunks

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in self.HEADING_TAGS:
            self.out.append("\n\n" + self.HEADING_TAGS[tag])
        elif tag in self.BLOCK_TAGS:
            self.out.append("\n")
        # Drop nav-y / footer-y blocks by class hint
        attrs_d = dict(attrs)
        classy = (attrs_d.get("class", "") + " " + attrs_d.get("id", "")).lower()
        if any(b in classy for b in ("navbar", "navigation", "site-footer",
                                      "cookie", "newsletter", "subscribe",
                                      "social-links", "share-bar")):
            self.skip_depth += 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in self.BLOCK_TAGS:
            self.out.append("\n")
        # Hard to track which closing matches the class-skip we opened;
        # accept a small over/undercount — usually evens out on real pages.

    def handle_data(self, data):
        if self.skip_depth > 0:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        s = data.strip()
        if s:
            self.out.append(s + " ")


def _extract_readable(html: str) -> tuple[str, str]:
    parser = _Extractor()
    try:
        parser.feed(html)
    except Exception as e:
        log.debug(f"HTML parse partial failure: {e}")
    title = " ".join(parser.title_parts).strip()
    text = "".join(parser.out)
    # Collapse runs of whitespace; preserve single blank lines as paragraph breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" *\n *", "\n", text)
    return title, text.strip()


def _detect_charset(content_type: str, raw: bytes) -> str:
    m = re.search(r"charset=([a-zA-Z0-9_\-]+)", content_type)
    if m:
        return m.group(1)
    m = re.search(rb'<meta[^>]+charset=["\']?([a-zA-Z0-9_\-]+)', raw[:2048], re.IGNORECASE)
    if m:
        return m.group(1).decode("ascii", errors="replace")
    return "utf-8"
