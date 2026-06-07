"""
marketing module — multi-platform ad / campaign copy generation.

The customer describes a campaign in natural language ("a Mother's Day
brunch ad for my deli, budget $100, audience local Reno families") and
Orbi generates ready-to-paste copy across the platforms the small
business actually uses: Facebook, Instagram, TikTok, LinkedIn, Google
Search ads, email newsletters, and print flyers. The customer copies
the output into their own social/ad accounts manually — no API
publish in v1, no platform OAuth, no ad-spend risk for Frank.

Saved campaigns live under `data/users/<username>/campaigns.json` so
each owner / staff user sees their own library.

Shape of a saved campaign:
  {
    "id":         "12-char hex",
    "brief":      "Mother's Day brunch promo, $100 budget, local Reno families",
    "title":      "Mother's Day Brunch — Reno families",
    "created_at": "2026-06-06T22:15:00Z",
    "updated_at": "2026-06-06T22:15:00Z",
    "assets": {
        "facebook_post": "...",
        "instagram_post": "...",
        "tiktok_caption": "...",
        "linkedin_post": "...",
        "google_search_ad": {"headline_1": "...", "headline_2": "...",
                              "headline_3": "...", "description_1": "...",
                              "description_2": "..."},
        "email_newsletter": {"subject": "...", "preheader": "...",
                              "body": "..."},
        "print_flyer": "..."
    },
    "images": [           # populated when the image sub-module is enabled
        {"id": "...", "prompt": "...", "url": "/static/marketing/<id>.png",
         "created_at": "..."},
        ...
    ]
  }
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()

# Platforms the generator targets. The order matters — it's the order the
# UI renders the outputs.
PLATFORMS = (
    "facebook_post",
    "instagram_post",
    "tiktok_caption",
    "linkedin_post",
    "google_search_ad",
    "email_newsletter",
    "print_flyer",
)


def _path(user_dir: Path) -> Path:
    return user_dir / "campaigns.json"


def _load(user_dir: Path) -> list[dict]:
    p = _path(user_dir)
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_dir: Path, campaigns: list[dict]) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(campaigns, f, indent=2, ensure_ascii=False)
    tmp.replace(p)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def list_campaigns(user_dir: Path) -> list[dict]:
    """Most-recent first."""
    with _LOCK:
        items = _load(user_dir)
    items.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "",
               reverse=True)
    return items


def get_campaign(user_dir: Path, campaign_id: str) -> dict | None:
    with _LOCK:
        for c in _load(user_dir):
            if c.get("id") == campaign_id:
                return c
    return None


def save_campaign(user_dir: Path, brief: str, title: str,
                   assets: dict, campaign_id: str | None = None) -> dict:
    """Insert OR update. If `campaign_id` is given and matches, the
    existing campaign is updated; otherwise a new one is created."""
    now = _now_iso()
    with _LOCK:
        items = _load(user_dir)
        if campaign_id:
            for i, c in enumerate(items):
                if c.get("id") == campaign_id:
                    items[i].update({
                        "brief":      brief or items[i].get("brief", ""),
                        "title":      title or items[i].get("title", ""),
                        "assets":     assets or items[i].get("assets", {}),
                        "updated_at": now,
                    })
                    _save(user_dir, items)
                    return items[i]
        # New campaign
        rec = {
            "id":         new_id(),
            "brief":      brief or "",
            "title":      title or "(untitled campaign)",
            "created_at": now,
            "updated_at": now,
            "assets":     assets or {},
            "images":     [],
        }
        items.append(rec)
        _save(user_dir, items)
        return rec


def delete_campaign(user_dir: Path, campaign_id: str) -> bool:
    with _LOCK:
        items = _load(user_dir)
        before = len(items)
        items = [c for c in items if c.get("id") != campaign_id]
        if len(items) == before:
            return False
        _save(user_dir, items)
        return True


def attach_image(user_dir: Path, campaign_id: str, image: dict) -> dict | None:
    """Append a generated image record to a campaign's images[] list.
    `image` should already include {id, prompt, url, created_at}.
    Returns the updated campaign, or None if the campaign isn't found."""
    with _LOCK:
        items = _load(user_dir)
        for i, c in enumerate(items):
            if c.get("id") == campaign_id:
                imgs = c.setdefault("images", [])
                imgs.append(image)
                items[i]["updated_at"] = _now_iso()
                _save(user_dir, items)
                return items[i]
    return None
