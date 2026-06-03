"""Save the crawl results to per-customer profile + raw-page index.

  data/customer_profiles/<domain>.json       — merged business profile
  data/customer_profiles/<domain>_pages/     — one .txt per page (raw
                                                 extracted text, for
                                                 full-text retrieval)
  data/customer_profiles/<domain>_index.json — page index: url → title,
                                                 length, signals, slug

The raw-page text store is the fallback when a visitor asks something
so weird it wasn't in the structured profile — handlers can grep the
page text and return the relevant page URL.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse


def domain_from_url(url: str) -> str:
    """purblum.com → purblum_com. Used as the per-customer slug."""
    netloc = urlparse(url).netloc.lower().removeprefix("www.")
    return re.sub(r"[^a-z0-9]+", "_", netloc).strip("_") or "unknown_site"


def customer_profile_path(data_dir: Path, url: str) -> Path:
    """Path to the merged business profile JSON for this customer."""
    return Path(data_dir) / "customer_profiles" / f"{domain_from_url(url)}.json"


def customer_pages_dir(data_dir: Path, url: str) -> Path:
    """Folder with raw per-page text files for this customer."""
    return Path(data_dir) / "customer_profiles" / f"{domain_from_url(url)}_pages"


def customer_index_path(data_dir: Path, url: str) -> Path:
    return Path(data_dir) / "customer_profiles" / f"{domain_from_url(url)}_index.json"


def _slug_for_url(page_url: str) -> str:
    """Make a filename-safe slug for one page URL."""
    parsed = urlparse(page_url)
    path = parsed.path.strip("/") or "_home"
    slug = re.sub(r"[^A-Za-z0-9_\-.]+", "_", path)
    return (slug[:80] or "_home")


def save_profile(data_dir: Path, url: str, profile: dict,
                  pages: list[dict]) -> dict:
    """Write the merged profile, the per-page text files, and the page
    index. Returns a dict describing where everything went."""
    prof_path = customer_profile_path(data_dir, url)
    pages_dir = customer_pages_dir(data_dir, url)
    idx_path = customer_index_path(data_dir, url)

    prof_path.parent.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)

    # Strip large per-page text from the profile (already saved separately)
    profile_compact = dict(profile)
    profile_compact["_pages"] = [{
        "url": p.get("url"),
        "title": p.get("title"),
        "signals": p.get("signals"),
    } for p in profile.get("_pages", [])]

    prof_path.write_text(json.dumps(profile_compact, indent=2, default=str),
                          encoding="utf-8")

    # Page index + per-page text files
    index = []
    for page in pages:
        url_p = page.get("url", "")
        slug = _slug_for_url(url_p)
        txt_path = pages_dir / f"{slug}.txt"
        if page.get("text"):
            # Prepend metadata so the text file is readable on its own
            header = (f"URL: {url_p}\n"
                       f"TITLE: {page.get('title','')}\n"
                       f"LENGTH: {page.get('length',0)}\n"
                       f"{'─' * 60}\n\n")
            txt_path.write_text(header + page["text"], encoding="utf-8")
        index.append({
            "url": url_p,
            "title": page.get("title", ""),
            "slug": slug,
            "length": page.get("length", 0),
            "signals": page.get("signals", {}),
        })
    idx_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    return {
        "profile_path": str(prof_path),
        "pages_dir":    str(pages_dir),
        "index_path":   str(idx_path),
        "pages_saved":  len(index),
    }
