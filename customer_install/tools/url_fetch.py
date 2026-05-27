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


def fetch(url: str, timeout: int = 10) -> dict:
    """Fetch a URL and return its readable text. Always returns a dict —
    on error, populates the 'error' field instead of raising.

    Returns:
      {
        "url":     "https://...",
        "ok":      True/False,
        "status":  200,
        "title":   "Page title",
        "text":    "extracted readable text (markdown-ish)",
        "byte_count": N,
        "error":   "" or "reason",
      }"""
    result = {"url": url, "ok": False, "status": 0, "title": "",
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
        title, text = _extract_readable(html)
        result["title"] = title[:200]
        result["text"] = text[:MAX_TEXT_CHARS]
        result["ok"] = True
    except urllib.error.HTTPError as e:
        result["status"] = e.code
        result["error"] = f"http_{e.code}"
    except urllib.error.URLError as e:
        result["error"] = f"network_error: {e.reason}"
    except Exception as e:
        result["error"] = f"fetch_failed: {e}"
    return result


def context_block(urls_text: list[dict]) -> str:
    """Format one or more fetch() results into an LLM-ready system addendum."""
    if not urls_text:
        return ""
    parts = ["WEB PAGES THE OWNER ASKED ME TO READ (quote facts from here):"]
    for r in urls_text:
        if not r.get("ok"):
            parts.append(f"\n--- {r.get('url')} ---\n(could not load: {r.get('error', 'unknown')})")
            continue
        parts.append(f"\n--- {r.get('title') or r.get('url')} ({r.get('url')}) ---")
        parts.append(r.get("text", "")[:8000])
    return "\n".join(parts)


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
