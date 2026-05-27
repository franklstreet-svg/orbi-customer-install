"""
connectors.yelp — Yelp Fusion review connector (read-only).

Pulls the 3 most recent Yelp reviews for the owner's business and confirms the
business exists / has a valid review count. Yelp does NOT permit programmatic
review replies, so this connector is intentionally read-only.

KEY YELP API CONSTRAINTS
------------------------
- Auth is a static Bearer token (Yelp Fusion API key). NOT OAuth — the owner
  generates the key once at https://fusion.yelp.com/v3/manage_app and pastes
  it in. We persist it via the base Connector's save_api_key().

- The Reviews endpoint (GET /v3/businesses/{id}/reviews) returns at most
  THREE reviews, ever. This is a documented Yelp Fusion limit. There is no
  pagination — `limit` in our list_reviews() is clamped to 3.

- The Business ID is the slug after `/biz/` on the public Yelp URL. The owner
  pastes it in alongside the API key.

ROUTE SURFACE — see bottom of file.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .base import Connector, register, _now_iso


log = logging.getLogger("orbi.connectors.yelp")

YELP_API_ROOT = "https://api.yelp.com/v3"
YELP_REVIEW_API_MAX = 3   # Yelp Fusion hard limit, documented.


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register
class YelpConnector(Connector):
    id     = "yelp"
    label  = "Yelp Reviews"
    blurb  = ("Pull recent Yelp reviews on your business. Read-only "
              "(Yelp doesn't allow programmatic responses).")
    auth_kind = "api_key"
    requires_owner_setup = [
        "Get a free Yelp Fusion API key at fusion.yelp.com/v3/manage_app and "
        "paste it here.",
        "Find your Yelp business ID — search yourself on yelp.com, the URL "
        "slug after /biz/ is the ID.",
    ]

    # ── HTTP helper ──────────────────────────────────────────────────────

    def _authed_get(self, path: str, params: dict | None = None) -> dict:
        """Stdlib-only GET against the Yelp Fusion API. Returns parsed JSON.
        Raises RuntimeError on failure with the Yelp error message attached."""
        key = self.get_api_key()
        if not key:
            raise RuntimeError("yelp: no API key saved")
        url = f"{YELP_API_ROOT}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {key}",
            "Accept":        "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8")
                parsed = json.loads(body) if body else {}
                msg = (parsed.get("error") or {}).get("description") or body or str(exc)
            except Exception:  # noqa: BLE001
                msg = str(exc)
            raise RuntimeError(f"yelp HTTP {exc.code}: {msg}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"yelp network error: {exc}") from exc

    # ── Override save_api_key to require business_id metadata ────────────

    def save_api_key(self, key: str, meta: dict | None = None) -> dict:
        meta = dict(meta or {})
        business_id = (meta.get("business_id") or "").strip()
        if not business_id:
            raise ValueError("yelp: business_id required (the slug after /biz/ on yelp.com)")
        meta["business_id"] = business_id
        return super().save_api_key(key, meta)

    def _business_id(self) -> str:
        return (self._read_tokens().get("business_id") or "").strip()

    # ── Public API ───────────────────────────────────────────────────────

    def get_business_info(self) -> dict:
        """
        Confirm the business exists and return basic public info.

        Returns {name, rating, review_count, error}. On failure, name/rating/
        review_count are empty and error holds the friendly message.
        """
        bid = self._business_id()
        if not bid:
            return {"name": "", "rating": 0, "review_count": 0,
                    "error": "no business_id saved"}
        try:
            payload = self._authed_get(f"/businesses/{urllib.parse.quote(bid)}")
        except Exception as exc:    # noqa: BLE001
            log.warning("get_business_info failed: %s", exc)
            self.update_status(last_error=str(exc))
            return {"name": "", "rating": 0, "review_count": 0, "error": str(exc)}

        info = {
            "name":         payload.get("name", "") or "",
            "rating":       float(payload.get("rating") or 0),
            "review_count": int(payload.get("review_count") or 0),
            "error":        "",
        }
        self.update_status(
            last_sync=_now_iso(),
            last_error="",
            business_name=info["name"],
            business_rating=info["rating"],
            business_review_count=info["review_count"],
        )
        return info

    def list_reviews(self, limit: int = 3) -> dict:
        """
        Recent reviews for the saved business_id. Yelp returns at most 3,
        so `limit` is clamped to YELP_REVIEW_API_MAX. Returns
        {"reviews": [{id, author, rating, text, time}, ...],
         "yelp_max": 3, "error": ""}.
        """
        bid = self._business_id()
        if not bid:
            return {"reviews": [], "yelp_max": YELP_REVIEW_API_MAX,
                    "error": "no business_id saved"}
        limit = max(1, min(int(limit or YELP_REVIEW_API_MAX), YELP_REVIEW_API_MAX))
        try:
            payload = self._authed_get(
                f"/businesses/{urllib.parse.quote(bid)}/reviews",
                params={"limit": limit, "sort_by": "newest"},
            )
        except Exception as exc:    # noqa: BLE001
            log.warning("list_reviews failed: %s", exc)
            self.update_status(last_error=str(exc))
            return {"reviews": [], "yelp_max": YELP_REVIEW_API_MAX, "error": str(exc)}

        out = []
        for r in payload.get("reviews", []) or []:
            user = r.get("user") or {}
            out.append({
                "id":     r.get("id", ""),
                "author": user.get("name", "Anonymous"),
                "rating": int(r.get("rating") or 0),
                "text":   r.get("text", "") or "",
                "time":   r.get("time_created", "") or "",
                "url":    r.get("url", "") or "",
            })
        self.update_status(
            last_sync=_now_iso(),
            last_reviews_count=len(out),
            last_error="",
        )
        return {"reviews": out, "yelp_max": YELP_REVIEW_API_MAX, "error": ""}

    # ── Status ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        s = super().status()
        rec = self._read_tokens()
        biz_id = (rec.get("business_id") or "") if rec else ""
        # Refresh business info on status read — single fast GET, gives the
        # owner immediate feedback if their key/business_id combo is wrong.
        biz = {"name": "", "rating": 0, "review_count": 0, "error": ""}
        if rec and biz_id:
            biz = self.get_business_info()
        s.update({
            "business_id":   biz_id,
            "business_name": biz["name"],
            "rating":        biz["rating"],
            "review_count":  biz["review_count"],
            "yelp_max":      YELP_REVIEW_API_MAX,
        })
        if biz["error"] and not s.get("last_error"):
            s["last_error"] = biz["error"]
        return s


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder.
#
#   POST /api/owner/connectors/yelp/save_key
#       body:   { "api_key": "...", "business_id": "purblum-deli-reno" }
#       calls:  YelpConnector(config, user_dir).save_api_key(
#                   api_key, {"business_id": business_id})
#       returns:{ "saved": true, "business_id": "...", "saved_at": "..." }
#
#   POST /api/owner/connectors/yelp/disconnect
#       calls:  disconnect()
#       returns:{ "ok": true }
#
#   GET  /api/owner/connectors/yelp/status
#       calls:  status()
#       returns:status dict (includes business_name, rating, review_count)
#
#   GET  /api/owner/connectors/yelp/reviews?limit=3
#       calls:  list_reviews(limit)
#       returns:{ "reviews": [...], "yelp_max": 3, "error": "" }
#
#   GET  /api/owner/connectors/yelp/business
#       calls:  get_business_info()
#       returns:{ "name": "...", "rating": 4.5, "review_count": 87,
#                 "error": "" }
#
# ---------------------------------------------------------------------------
