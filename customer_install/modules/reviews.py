"""
modules/reviews — customer satisfaction surveys at closeout.

After a project closes, the customer gets a unique URL to leave a 1-5
star rating + optional comment. Aggregated into "Your 4.7/5 across 23
jobs" — real social proof the contractor can use in future bids and
on their website.

Review shape:
  {
    "id":              "12-char hex",
    "project_id":      "12-char hex",
    "token":           "url-safe 24-char token",
    "rating":          5,                       # 1-5 stars, null until submitted
    "comment":         "Great work!",           # optional
    "would_recommend": true,                    # yes/no
    "permission_to_share": false,               # may we quote you publicly
    "issued_at":       1780000000,              # when token minted
    "submitted_at":    1780100000,              # when customer left review
    "submitter_ip":    "..."                    # captured for audit
  }
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()
FILE = "reviews.json"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


def _load(data_dir: Path) -> list[dict]:
    p = _path(data_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(data_dir: Path, reviews: list[dict]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reviews, indent=2), encoding="utf-8")
    tmp.replace(p)


def issue(data_dir: Path, project_id: str) -> dict:
    """Mint a fresh review token + record for the project. Returns the
    review record. Re-issuable if the prior token is unused — same
    project doesn't accumulate dead invitations."""
    pid = (project_id or "").strip()
    if not pid:
        raise ValueError("project_id required")
    with _LOCK:
        reviews = _load(data_dir)
        # Re-use any pending (unsubmitted) one for this project
        for r in reviews:
            if r.get("project_id") == pid and not r.get("submitted_at"):
                return r
        entry = {
            "id":                  uuid.uuid4().hex[:12],
            "project_id":          pid,
            "token":               secrets.token_urlsafe(24),
            "rating":              None,
            "comment":             "",
            "would_recommend":     None,
            "permission_to_share": False,
            "issued_at":           int(time.time()),
            "submitted_at":        None,
            "submitter_ip":        "",
        }
        reviews.append(entry)
        _save(data_dir, reviews)
    return entry


def get_by_token(data_dir: Path, token: str) -> dict | None:
    if not token:
        return None
    for r in _load(data_dir):
        if r.get("token") == token:
            return r
    return None


def submit(data_dir: Path, token: str, *,
            rating: int, comment: str = "",
            would_recommend: bool = True,
            permission_to_share: bool = False,
            submitter_ip: str = "") -> dict | None:
    rating = int(rating)
    if rating < 1 or rating > 5:
        raise ValueError("rating must be 1-5")
    with _LOCK:
        reviews = _load(data_dir)
        for r in reviews:
            if r.get("token") == token:
                if r.get("submitted_at"):
                    return None   # already submitted — single-use
                r["rating"] = rating
                r["comment"] = (comment or "").strip()[:2000]
                r["would_recommend"] = bool(would_recommend)
                r["permission_to_share"] = bool(permission_to_share)
                r["submitted_at"] = int(time.time())
                r["submitter_ip"] = submitter_ip[:64]
                _save(data_dir, reviews)
                return r
    return None


def list_submitted(data_dir: Path) -> list[dict]:
    return [r for r in _load(data_dir) if r.get("submitted_at")]


def summary(data_dir: Path) -> dict:
    """Aggregate stats — what shows on the contractor's website + the
    'what's my rating' chat command."""
    submitted = list_submitted(data_dir)
    n = len(submitted)
    if n == 0:
        return {"count": 0, "average": 0.0, "would_recommend_pct": 0.0,
                "issued_unanswered": sum(1 for r in _load(data_dir)
                                          if not r.get("submitted_at")),
                "distribution": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}}
    total_rating = sum(int(r.get("rating") or 0) for r in submitted)
    avg = total_rating / n
    recs = sum(1 for r in submitted if r.get("would_recommend"))
    rec_pct = (recs / n * 100) if n else 0
    dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in submitted:
        v = int(r.get("rating") or 0)
        if 1 <= v <= 5:
            dist[v] += 1
    return {
        "count":               n,
        "average":             round(avg, 2),
        "would_recommend_pct": round(rec_pct, 1),
        "issued_unanswered":   sum(1 for r in _load(data_dir)
                                    if not r.get("submitted_at")),
        "distribution":        dist,
    }


def list_shareable(data_dir: Path, min_rating: int = 4) -> list[dict]:
    """Reviews the customer authorized us to share publicly. These are
    the testimonials safe to put on a marketing site or use in a bid."""
    return [r for r in list_submitted(data_dir)
            if r.get("permission_to_share")
            and int(r.get("rating") or 0) >= min_rating
            and (r.get("comment") or "").strip()]
