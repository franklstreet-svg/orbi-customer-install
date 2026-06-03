"""Extract every same-domain link from a page.

The original web_scraper/link_extractor.py had a prioritize() that ranked
by keyword (services, about, contact, faq...) and took a subset. Frank's
correction was: drop that. Every same-domain link gets visited. The
crawler enforces safety caps (max pages, depth) — link discovery itself
returns everything it can find.
"""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse


# File extensions that aren't HTML — skip these so the crawler doesn't
# try to "read" a PDF or image as text. The customer's docs DO get
# scraped if/when they upload them through the Files tab — that's a
# different pipeline.
_SKIP_EXT = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".mp4", ".mov", ".webm", ".avi", ".mkv", ".mp3", ".wav", ".m4a",
    ".zip", ".tar", ".gz", ".rar", ".7z", ".dmg", ".exe", ".apk",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".otf",
    ".xml", ".rss", ".atom",
}


class _HrefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        attr_map = dict(attrs)
        href = (attr_map.get("href") or "").strip()
        if href:
            self.links.append(href)


# Query params that just preselect UI state (cart preloads, filters, UTM
# tags) but don't change the page CONTENT. Strip these so we don't crawl
# order.html?add=X 14 times for 14 different add params.
_NOISE_QUERY_PREFIXES = ("add", "utm_", "fbclid", "gclid", "msclkid",
                          "ref", "source", "campaign")


def _canonicalize(url: str) -> str:
    """Normalize a URL so query-variants of the same page collapse:
       order.html?add=x and order.html?utm_source=fb → order.html.
    Real content-bearing query params (?category=lunch, ?slug=story)
    are KEPT — only known UI/tracking params are stripped."""
    from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
    p = urlparse(url)
    if not p.query:
        # Normalize trailing slash so /menu and /menu/ aren't both queued
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme, p.netloc, path, p.params, "", ""))
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
             if not any(k.lower().startswith(pfx) for pfx in _NOISE_QUERY_PREFIXES)]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc, path, p.params,
                        urlencode(kept, doseq=True), ""))


def extract_links(base_url: str, html: str) -> list[str]:
    """Pull every <a href> from `html`, resolve relative to `base_url`,
    keep only same-domain http(s) links, drop fragments and asset
    extensions, canonicalize away noise query params. Returns deduped
    in source order."""
    parser = _HrefParser()
    try:
        parser.feed(html)
    except Exception:
        return []

    base_netloc = urlparse(base_url).netloc.lower().removeprefix("www.")
    if not base_netloc:
        return []

    seen = set()
    out: list[str] = []
    for href in parser.links:
        if href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        host = parsed.netloc.lower().removeprefix("www.")
        if host != base_netloc:
            continue
        if parsed.fragment:
            absolute = absolute.split("#", 1)[0]
        path_lower = parsed.path.lower()
        skip = False
        for ext in _SKIP_EXT:
            if path_lower.endswith(ext):
                skip = True
                break
        if skip:
            continue
        canonical = _canonicalize(absolute)
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out
