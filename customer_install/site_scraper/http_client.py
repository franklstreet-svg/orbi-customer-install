"""HTTP fetch helper — Playwright-first (renders JS), urllib fallback.

Why Playwright first: Wix / Squarespace / Shopify / many restaurant sites
render menus and product lists in JavaScript. Plain HTTP fetch sees only
the skeleton HTML and misses everything. Playwright drives a real
headless Chromium so we capture the post-render DOM the visitor sees.

The browser is launched lazily once per process and reused across all
crawled URLs in a session (one boot, many pages — keeps perf reasonable
on multi-page crawls). atexit closes it cleanly.

Returns a dict for every URL — never raises — so the crawler can keep
going past per-page failures.
"""
from __future__ import annotations

import atexit
import logging
import urllib.error
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger("orbi.site_scraper.http")

# 1.5 MB cap per page — anything bigger is probably an asset (PDF, image
# served as HTML, etc.). The body gets truncated cleanly.
DEFAULT_MAX_BYTES = 1_500_000

# Realistic Chrome UA — some sites (especially behind Cloudflare) refuse
# bot-shaped strings outright.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Lazy-init browser singletons. None until first call. Set to a sentinel
# False if Playwright is unavailable, so we don't retry every page.
_PLAYWRIGHT = None
_BROWSER = None
_PLAYWRIGHT_AVAILABLE: bool | None = None


def _get_browser():
    """Lazy-init Playwright + Chromium. Returns the browser handle or None.
    On any setup failure, marks Playwright unavailable so subsequent calls
    skip straight to urllib without retrying."""
    global _PLAYWRIGHT, _BROWSER, _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is False:
        return None
    if _BROWSER is not None:
        return _BROWSER
    try:
        from playwright.sync_api import sync_playwright
        _PLAYWRIGHT = sync_playwright().start()
        _BROWSER = _PLAYWRIGHT.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        _PLAYWRIGHT_AVAILABLE = True
        log.info("playwright/chromium launched for scraping")
        return _BROWSER
    except Exception as e:
        log.warning(f"playwright unavailable, urllib only: {type(e).__name__}: {e}")
        _PLAYWRIGHT_AVAILABLE = False
        return None


def _cleanup_browser():
    """atexit handler — close Chromium and Playwright cleanly."""
    global _PLAYWRIGHT, _BROWSER
    if _BROWSER:
        try:
            _BROWSER.close()
        except Exception:
            pass
        _BROWSER = None
    if _PLAYWRIGHT:
        try:
            _PLAYWRIGHT.stop()
        except Exception:
            pass
        _PLAYWRIGHT = None


atexit.register(_cleanup_browser)


def _fetch_rendered(url: str, timeout: int = 18,
                     max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Playwright fetch — opens the URL in headless Chrome, blocks images
    + fonts + media (we only need text), waits for DOM + short settle,
    returns the rendered HTML. Same dict shape as the urllib path. On any
    failure returns ok=False so callers can fall back."""
    browser = _get_browser()
    if browser is None:
        return {"ok": False, "error": "playwright_unavailable",
                "url": url, "final_url": url, "status_code": 0,
                "content_type": "", "html": "", "bytes_read": 0,
                "truncated": False}
    try:
        page = browser.new_page(user_agent=USER_AGENT)
        try:
            # Block heavyweight assets we don't need (images, fonts, media,
            # stylesheets). We only care about the rendered text — saves
            # 5-30 seconds per page on image-heavy restaurant sites.
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font", "stylesheet"}
                else route.continue_(),
            )
            page.set_default_timeout(timeout * 1000)
            page.goto(url, wait_until="domcontentloaded")
            # JS settle window — shorter now that we're not waiting for
            # images. 800ms is enough for most lazy-hydrated menus.
            page.wait_for_timeout(800)
            html = page.content()
            final_url = page.url
            truncated = len(html) > max_bytes
            if truncated:
                html = html[:max_bytes]
            return {
                "ok": True,
                "url": url,
                "final_url": final_url,
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "html": html,
                "bytes_read": len(html),
                "truncated": truncated,
                "error": "",
                "renderer": "playwright",
            }
        finally:
            try:
                page.close()
            except Exception:
                pass
    except Exception as e:
        log.warning(f"playwright fetch failed for {url}: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"playwright: {e}",
                "url": url, "final_url": url, "status_code": 0,
                "content_type": "", "html": "", "bytes_read": 0,
                "truncated": False}


def _fetch_urllib(url: str, timeout: int = 12,
                   max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Plain urllib fetch — no JS, just raw server HTML. Fast fallback
    used when Playwright fails or isn't installed."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            if truncated:
                raw = raw[:max_bytes]
            html = raw.decode("utf-8", errors="ignore")
            return {
                "ok": True,
                "url": url,
                "final_url": resp.geturl(),
                "status_code": getattr(resp, "status", 200),
                "content_type": content_type,
                "html": html,
                "bytes_read": len(raw),
                "truncated": truncated,
                "error": "",
                "renderer": "urllib",
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False, "url": url, "final_url": url,
            "status_code": e.code, "content_type": "",
            "html": "", "bytes_read": 0, "truncated": False,
            "error": f"HTTP {e.code}: {e.reason}",
            "renderer": "urllib",
        }
    except urllib.error.URLError as e:
        return {
            "ok": False, "url": url, "final_url": url,
            "status_code": 0, "content_type": "",
            "html": "", "bytes_read": 0, "truncated": False,
            "error": f"URL error: {e.reason}",
            "renderer": "urllib",
        }
    except Exception as e:
        log.exception("unexpected urllib fetch error for %s", url)
        return {
            "ok": False, "url": url, "final_url": url,
            "status_code": 0, "content_type": "",
            "html": "", "bytes_read": 0, "truncated": False,
            "error": f"{type(e).__name__}: {e}",
            "renderer": "urllib",
        }


def fetch(url: str, timeout: int = 12,
           max_bytes: int = DEFAULT_MAX_BYTES,
           render: bool = True) -> dict:
    """Fetch one URL. With render=True (default), tries Playwright first
    so JS-rendered content (Wix/Squarespace/Shopify menus, product grids,
    lazy-loaded sections) is captured. Falls back to urllib if Playwright
    fails. Set render=False to skip straight to urllib (faster, useful
    for static sites or robots.txt / sitemap fetches)."""
    if render:
        rendered_timeout = max(timeout, 15)
        result = _fetch_rendered(url, timeout=rendered_timeout, max_bytes=max_bytes)
        if result.get("ok"):
            return result
        log.info(f"playwright miss, falling back to urllib: {url}")
    return _fetch_urllib(url, timeout=timeout, max_bytes=max_bytes)


def same_domain(url1: str, url2: str) -> bool:
    """True if two URLs share a registrable host (ignoring www prefix)."""
    h1 = urlparse(url1).netloc.lower().removeprefix("www.")
    h2 = urlparse(url2).netloc.lower().removeprefix("www.")
    return h1 == h2 and h1 != ""
