"""HTML → readable text. Strips scripts/styles, keeps headings/links,
captures image alt-text (some restaurants put their menu in <img alt>!),
preserves paragraph structure so the LLM sees a real page.

Returns text PLUS a short "snapshot" describing what kinds of content
were on the page (has-prices? has-hours-pattern? has-menu-like-list?)
so the merger can prioritize structured extraction on pages that look
menu-ish without filtering OUT pages that don't.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser


_SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg"}
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3",
                "h4", "h5", "h6", "section", "article", "header",
                "footer", "tr", "td", "th"}


class _ReadableParser(HTMLParser):
    """Pulls clean text + image alt-text. Inserts newlines on block tags
    so the LLM gets paragraph structure, not one giant blob."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0
        self._title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if t == "title":
            self._in_title = True
            return
        if t == "img":
            alt = dict(attrs).get("alt", "").strip()
            if alt:
                self.parts.append(f"[image: {alt}]")
        if t in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if t == "title":
            self._in_title = False
            return
        if t in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        s = data.strip()
        if not s:
            return
        if self._in_title:
            self._title = (self._title + " " + s).strip()
            return
        self.parts.append(s)


# Cheap pattern checks for the "what's on this page?" snapshot.
_PRICE_RE = re.compile(r"\$\s*\d{1,4}(?:[.,]\d{1,2})?")
_HOURS_RE = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b.*?\d{1,2}(?::\d{2})?\s*(?:am|pm)",
    re.IGNORECASE | re.DOTALL,
)
_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def parse_page(url: str, html: str) -> dict:
    """Returns:
        {url, title, text, length, signals: {has_prices, has_hours,
         has_phone, has_email, has_zip, price_count}}"""
    parser = _ReadableParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    raw_text = "".join(parser.parts)
    # Collapse runs of blank lines but keep paragraph breaks
    text = re.sub(r"\n{3,}", "\n\n", raw_text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines()).strip()

    prices = _PRICE_RE.findall(text)
    return {
        "url": url,
        "title": parser._title.strip(),
        "text": text,
        "length": len(text),
        "signals": {
            "has_prices":  bool(prices),
            "price_count": len(prices),
            "has_hours":   bool(_HOURS_RE.search(text)),
            "has_phone":   bool(_PHONE_RE.search(text)),
            "has_email":   bool(_EMAIL_RE.search(text)),
            "has_zip":     bool(_ZIP_RE.search(text)),
        },
    }
