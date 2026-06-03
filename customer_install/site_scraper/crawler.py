"""BFS crawler over a single domain.

  start_url → fetch → parse text → extract links → enqueue same-domain
  → fetch next → ... until max_pages OR max_depth OR wall-time OR queue
  empty.

No prioritization, no "this URL looks unimportant" filtering. Every page
gets visited and read. Safety caps prevent infinite loops on calendar
widgets and other URL-explosion patterns.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from . import http_client, link_extractor, page_parser, llm_extract, merge, storage

log = logging.getLogger("orbi.site_scraper.crawler")


def _normalize_start(url: str) -> str:
    s = (url or "").strip()
    if not s:
        raise ValueError("empty URL")
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    parsed = urlparse(s)
    if not parsed.netloc:
        raise ValueError(f"invalid URL: {url!r}")
    # Strip trailing fragment + normalize trailing slash
    s = s.split("#", 1)[0]
    return s


def crawl_site(start_url: str,
                data_dir: Path | None = None,
                brain_call=None,
                max_pages: int = 500,
                max_depth: int = 5,
                wall_time_seconds: int = 600,
                fetch_delay_seconds: float = 0.5,
                save: bool = True) -> dict:
    """Crawl every same-domain page reachable from `start_url`, run LLM
    extraction on each, merge into a unified business profile.

    Returns:
        {
          "ok": True,
          "start_url": "...",
          "domain": "...",
          "pages_visited": N,
          "pages_failed": M,
          "elapsed_seconds": ...,
          "stopped_reason": "...",
          "profile": {...merged...},
          "confidence": {...field → high/low/missing...},
          "storage": {...paths...} (if save=True),
        }
    """
    start_url = _normalize_start(start_url)
    domain = urlparse(start_url).netloc.lower().removeprefix("www.")
    started_at = time.time()

    queue: deque = deque([(start_url, 0)])  # (url, depth)
    visited: set[str] = set()
    failed: list[dict] = []
    pages: list[dict] = []
    extracts: list[dict] = []
    stopped_reason = "queue_empty"

    while queue:
        if len(visited) >= max_pages:
            stopped_reason = f"max_pages_{max_pages}"
            break
        if (time.time() - started_at) > wall_time_seconds:
            stopped_reason = f"wall_time_{wall_time_seconds}s"
            break

        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        # Fetch
        fetched = http_client.fetch(url)
        if not fetched.get("ok"):
            failed.append({"url": url, "error": fetched.get("error", "")})
            log.debug(f"crawl: skip {url} ({fetched.get('error', '')})")
            continue

        # Skip non-HTML responses (some servers send JSON or PDF without
        # the .pdf extension)
        ct = (fetched.get("content_type") or "").lower()
        if ct and "html" not in ct and "xml" not in ct:
            log.debug(f"crawl: skip {url} non-HTML ({ct})")
            continue

        # Parse text + signals
        parsed = page_parser.parse_page(url, fetched["html"])
        pages.append(parsed)

        # LLM extraction (or regex fallback if brain not provided)
        llm_data = llm_extract.extract_from_page(
            url, parsed["text"], brain_call=brain_call,
        )
        extracts.append({
            "url": url,
            "title": parsed["title"],
            "signals": parsed["signals"],
            "llm_data": llm_data,
        })

        # Discover more links to crawl (only if we haven't hit depth limit)
        if depth < max_depth:
            new_links = link_extractor.extract_links(url, fetched["html"])
            for link in new_links:
                if link not in visited:
                    queue.append((link, depth + 1))

        # Politeness — small delay between fetches so we don't hammer
        # the customer's server. They authorized this crawl but we still
        # behave like a friendly user.
        if queue and fetch_delay_seconds > 0:
            time.sleep(fetch_delay_seconds)

    # Merge all extractions into a single profile
    profile = merge.merge_pages(extracts)
    confidence = merge.confidence(profile)

    result = {
        "ok": True,
        "start_url": start_url,
        "domain": domain,
        "pages_visited": len(visited),
        "pages_failed": len(failed),
        "failed": failed[:20],  # cap so the response doesn't blow up
        "elapsed_seconds": round(time.time() - started_at, 1),
        "stopped_reason": stopped_reason,
        "profile": profile,
        "confidence": confidence,
    }

    # Save to per-customer profile + raw page index
    if save and data_dir is not None:
        result["storage"] = storage.save_profile(data_dir, start_url, profile, pages)

    return result
