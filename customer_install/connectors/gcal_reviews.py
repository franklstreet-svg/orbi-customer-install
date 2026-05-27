"""
connectors.gcal_reviews — Google Business Profile (My Business) reviews connector.

Pulls recent customer reviews on the owner's Google Business Profile locations
and posts owner replies. Same Connector subclass pattern as gmail/gcal/etc.

DESIGN NOTES
------------
- OAuth flow + token storage mirrors gcal.py exactly, but rides on top of the
  base Connector helpers (_read_tokens/_write_tokens) so the per-user token
  file ends up at <user_dir>/connector_tokens/google_reviews.json (0o600),
  consistent with every other Connector in this folder.

- We REUSE the existing CONFIG.gcal_oauth.client_id / client_secret pair. The
  owner only registers ONE Google Cloud OAuth client for the install; the scope
  list is what differentiates calendar vs business reviews. The redirect_uri
  is /api/owner/connectors/google_reviews/callback (loopback exemption applies
  the same way it does for gcal).

- *** GMB API IS GATE-KEPT. ***
  Google's Business Profile APIs (mybusinessbusinessinformation /
  mybusinessaccountmanagement) are NOT publicly available — they require an
  approved access request via
      https://developers.google.com/my-business/content/prereqs
  Until Frank gets approval the OAuth handshake still works (the scope is
  legal, you'll see consent UI), but any call to the actual REST endpoints
  will return HTTP 403 with reason "accessNotConfigured" or similar.

  Per request, list_locations / list_reviews / respond_to_review all detect
  this case and return a friendly, actionable message instead of an
  exception. The Connector registration, OAuth surface, status surface, and
  the route surface are all wired up correctly so this connector "lights up"
  the moment API approval lands — no code changes required.

ROUTE SURFACE — see bottom of file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .base import Connector, register, _now_iso


log = logging.getLogger("orbi.connectors.google_reviews")

# ---------------------------------------------------------------------------
# Google endpoints
# ---------------------------------------------------------------------------

AUTH_URI  = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Business Profile API roots — gated behind Google's approval program.
GBP_ACCOUNTS_ROOT  = "https://mybusinessaccountmanagement.googleapis.com/v1"
GBP_INFO_ROOT      = "https://mybusinessbusinessinformation.googleapis.com/v1"
# Reviews + replies still live on the legacy v4 endpoint (mybusiness.googleapis.com).
GBP_LEGACY_ROOT    = "https://mybusiness.googleapis.com/v4"

# Sentinel error payload returned when the API rejects us with "not approved".
RESTRICTED_MSG = (
    "Google has restricted this API — apply for access at "
    "https://developers.google.com/my-business and re-connect once approved."
)


# ---------------------------------------------------------------------------
# Lazy imports (keep google libs off the import path unless needed)
# ---------------------------------------------------------------------------

def _google_flow():
    from google_auth_oauthlib.flow import Flow
    return Flow

def _google_creds():
    from google.oauth2.credentials import Credentials
    return Credentials

def _google_request():
    from google.auth.transport.requests import Request
    return Request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_config(client_id: str, client_secret: str, redirect_uri: str) -> dict:
    return {
        "installed": {
            "client_id":                   client_id,
            "client_secret":               client_secret,
            "auth_uri":                    AUTH_URI,
            "token_uri":                   TOKEN_URI,
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris":               [redirect_uri],
        }
    }


def _is_restricted_error(exc: Exception) -> bool:
    """True if an HTTP error looks like the GMB API gate-keep response."""
    s = str(exc).lower()
    needles = (
        "accessnotconfigured",
        "access not configured",
        "has not been used",
        "permission_denied",
        "permissiondenied",
        "403",
    )
    return any(n in s for n in needles)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register
class GoogleReviewsConnector(Connector):
    id     = "google_reviews"
    label  = "Google Business Reviews"
    blurb  = "See new Google reviews on your business and draft polite responses."
    auth_kind = "oauth"
    scopes = ["https://www.googleapis.com/auth/business.manage"]

    # ── OAuth ─────────────────────────────────────────────────────────────

    def _oauth_creds(self):
        """Pull (client_id, client_secret) from the shared CONFIG.gcal_oauth."""
        gcal_oauth = (self.config or {}).get("gcal_oauth") or {}
        return gcal_oauth.get("client_id", ""), gcal_oauth.get("client_secret", "")

    def start_oauth(self, redirect_uri: str) -> str:
        client_id, client_secret = self._oauth_creds()
        if not client_id or not client_secret:
            raise RuntimeError(
                "google_reviews: missing gcal_oauth.client_id / client_secret in config"
            )
        Flow = _google_flow()
        flow = Flow.from_client_config(
            _client_config(client_id, client_secret, redirect_uri),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        auth_url, _state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        log.info("started oauth (redirect_uri=%s)", redirect_uri)
        return auth_url

    def complete_oauth(self, code: str, redirect_uri: str) -> dict:
        client_id, client_secret = self._oauth_creds()
        Flow = _google_flow()
        flow = Flow.from_client_config(
            _client_config(client_id, client_secret, redirect_uri),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Best-effort grab the connecting email for display in the dashboard.
        email = ""
        try:
            from googleapiclient.discovery import build
            svc = build("oauth2", "v2", credentials=creds, cache_discovery=False)
            info = svc.userinfo().get().execute()
            email = info.get("email", "") or ""
        except Exception as exc:    # noqa: BLE001 — email is non-fatal metadata
            log.warning("could not fetch userinfo email: %s", exc)

        payload = {
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes or self.scopes),
            "expiry":        creds.expiry.isoformat() if creds.expiry else None,
            "email":         email,
            "account":       email,
            "saved_at":      _now_iso(),
            "issued_at":     _now_iso(),
        }
        self._write_tokens(payload)
        log.info("connected account %s", email or "<unknown>")
        return {"email": email, "scopes": payload["scopes"]}

    # ── Credentials loader ───────────────────────────────────────────────

    def _load_credentials(self):
        if not self.is_connected():
            raise RuntimeError("google_reviews: not connected")
        data = self._read_tokens()
        Credentials = _google_creds()
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", TOKEN_URI),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes", self.scopes),
        )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                Request = _google_request()
                creds.refresh(Request())
                data["token"]  = creds.token
                data["expiry"] = creds.expiry.isoformat() if creds.expiry else None
                self._write_tokens(data)
                log.info("refreshed access token")
            else:
                raise RuntimeError("google_reviews: credentials invalid and not refreshable")
        return creds

    def _authed_get(self, url: str) -> dict:
        """Authorized GET against a GBP REST endpoint, returns parsed JSON."""
        import urllib.request
        creds = self._load_credentials()
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {creds.token}",
            "Accept":        "application/json",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def _authed_put(self, url: str, body: dict) -> dict:
        """Authorized PUT against a GBP REST endpoint, returns parsed JSON."""
        import urllib.request
        creds = self._load_credentials()
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="PUT", headers={
            "Authorization": f"Bearer {creds.token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    # ── Public API ───────────────────────────────────────────────────────

    def list_locations(self) -> dict:
        """
        List all Business Profile locations the connected Google account owns.
        Returns {"locations": [...], "error": "..."} — error string is friendly
        if the GMB API is restricted.
        """
        try:
            accts = self._authed_get(f"{GBP_ACCOUNTS_ROOT}/accounts")
        except Exception as exc:    # noqa: BLE001
            if _is_restricted_error(exc):
                log.warning("list_locations: GMB API restricted")
                self.update_status(last_error=RESTRICTED_MSG)
                return {"locations": [], "error": RESTRICTED_MSG}
            log.warning("list_locations failed: %s", exc)
            return {"locations": [], "error": f"list_locations failed: {exc}"}

        locations = []
        for acct in accts.get("accounts", []) or []:
            acct_name = acct.get("name", "")
            if not acct_name:
                continue
            try:
                # readMask is required by the v1 API.
                url = (f"{GBP_INFO_ROOT}/{acct_name}/locations"
                       "?readMask=name,title,storefrontAddress,websiteUri")
                locs = self._authed_get(url)
            except Exception as exc:    # noqa: BLE001
                if _is_restricted_error(exc):
                    return {"locations": [], "error": RESTRICTED_MSG}
                log.warning("list locations under %s failed: %s", acct_name, exc)
                continue
            for loc in locs.get("locations", []) or []:
                locations.append({
                    "id":      loc.get("name", ""),
                    "title":   loc.get("title", ""),
                    "address": (loc.get("storefrontAddress") or {}).get("addressLines", []),
                    "website": loc.get("websiteUri", ""),
                    "account": acct_name,
                })
        return {"locations": locations, "error": ""}

    def list_reviews(self, location_id: str, limit: int = 10) -> dict:
        """
        Recent reviews for a single Business Profile location.

        `location_id` is the full resource name from list_locations() —
        e.g. "accounts/123/locations/456".

        Returns {"reviews": [{id, author, rating, text, time}, ...], "error": ...}.
        """
        if not location_id:
            return {"reviews": [], "error": "location_id required"}
        try:
            url = f"{GBP_LEGACY_ROOT}/{location_id}/reviews?pageSize={int(limit)}"
            payload = self._authed_get(url)
        except Exception as exc:    # noqa: BLE001
            if _is_restricted_error(exc):
                log.warning("list_reviews: GMB API restricted")
                self.update_status(last_error=RESTRICTED_MSG)
                return {"reviews": [], "error": RESTRICTED_MSG}
            log.warning("list_reviews failed: %s", exc)
            return {"reviews": [], "error": f"list_reviews failed: {exc}"}

        rating_map = {
            "ONE":   1, "TWO":   2, "THREE": 3, "FOUR":  4, "FIVE":  5,
            "STAR_RATING_UNSPECIFIED": 0,
        }
        out = []
        for r in payload.get("reviews", []) or []:
            reviewer = r.get("reviewer") or {}
            out.append({
                "id":     r.get("reviewId", "") or r.get("name", ""),
                "author": reviewer.get("displayName", "Anonymous"),
                "rating": rating_map.get(r.get("starRating", ""), 0),
                "text":   r.get("comment", "") or "",
                "time":   r.get("createTime", "") or "",
            })
        self.update_status(last_sync=_now_iso(), last_reviews_count=len(out), last_error="")
        return {"reviews": out, "error": ""}

    def respond_to_review(self, location_id: str, review_id: str, reply_text: str) -> dict:
        """
        Post (or update) the owner reply on a single review. Returns
        {"posted": bool, "error": "..."}.
        """
        if not location_id or not review_id:
            return {"posted": False, "error": "location_id and review_id required"}
        if not reply_text or not reply_text.strip():
            return {"posted": False, "error": "reply_text required"}
        try:
            url = f"{GBP_LEGACY_ROOT}/{location_id}/reviews/{review_id}/reply"
            self._authed_put(url, {"comment": reply_text.strip()})
        except Exception as exc:    # noqa: BLE001
            if _is_restricted_error(exc):
                log.warning("respond_to_review: GMB API restricted")
                self.update_status(last_error=RESTRICTED_MSG)
                return {"posted": False, "error": RESTRICTED_MSG}
            log.warning("respond_to_review failed: %s", exc)
            return {"posted": False, "error": f"respond failed: {exc}"}
        return {"posted": True, "error": ""}

    # ── Status ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        s = super().status()
        rec = self._read_tokens()
        location_count  = 0
        recent_reviews  = 0
        if rec:
            # Don't hit the API on every status read — surface cached counts
            # from update_status() instead. The owner can hit /reviews to
            # refresh.
            location_count  = int(rec.get("last_locations_count", 0) or 0)
            recent_reviews  = int(rec.get("last_reviews_count", 0) or 0)
        s.update({
            "location_count":      location_count,
            "recent_reviews":      recent_reviews,
            "api_restricted_note": RESTRICTED_MSG,
        })
        return s


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. config (with gcal_oauth.client_id/secret)
# is passed in by the orchestrator. redirect_uri is loopback, e.g.
# http://127.0.0.1:PORT/api/owner/connectors/google_reviews/callback.
#
#   POST /api/owner/connectors/google_reviews/connect
#       calls:  GoogleReviewsConnector(config, user_dir).start_oauth(redirect_uri)
#       returns:{ "auth_url": "https://accounts.google.com/o/oauth2/..." }
#
#   GET  /api/owner/connectors/google_reviews/callback?code=...
#       calls:  complete_oauth(code, redirect_uri)
#       then:   HTTP 302 -> /owner#connectors
#
#   POST /api/owner/connectors/google_reviews/disconnect
#       calls:  disconnect()
#       returns:{ "ok": true }
#
#   GET  /api/owner/connectors/google_reviews/status
#       calls:  status()
#       returns:status dict
#
#   GET  /api/owner/connectors/google_reviews/locations
#       calls:  list_locations()
#       returns:{ "locations": [...], "error": "" }
#
#   GET  /api/owner/connectors/google_reviews/reviews?location_id=...&limit=10
#       calls:  list_reviews(location_id, limit)
#       returns:{ "reviews": [...], "error": "" }
#
#   POST /api/owner/connectors/google_reviews/respond
#       body:   { "location_id": "...", "review_id": "...", "reply_text": "..." }
#       calls:  respond_to_review(location_id, review_id, reply_text)
#       returns:{ "posted": true|false, "error": "" }
#
# ---------------------------------------------------------------------------
