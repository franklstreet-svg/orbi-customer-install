"""
connectors.outlook — Outlook / Microsoft 365 mail connector for Orbi.

Lets Orbi read the owner's recent mail, search it, fetch a single message,
and draft a reply that lands in the Drafts folder (the owner always reviews
before hitting send — we never send mail directly).

DESIGN NOTES
------------
- OAuth via Microsoft Identity Platform v2.0:
    Authorization:  https://login.microsoftonline.com/common/oauth2/v2.0/authorize
    Token:          https://login.microsoftonline.com/common/oauth2/v2.0/token
- API calls go through Microsoft Graph at https://graph.microsoft.com/v1.0
- HTTP is done with stdlib urllib so we don't pull `requests` into the
  customer install just for Outlook. The base connector handles the
  per-user token file (mode 0o600).
- Scopes: Mail.Read, Mail.ReadWrite (for draft), User.Read (for /me).
  We add `offline_access` automatically at request time so we get a
  refresh_token — Microsoft requires that scope explicitly.
- Owner registers an App in portal.azure.com (single-tenant or personal
  + work accounts, no review process). They paste client_id and a
  client_secret value into config.outlook_oauth — same shape as gcal_oauth.

ROUTE SURFACE (see bottom of file).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .base import Connector, register

log = logging.getLogger("orbi.connectors.outlook")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

AUTH_URL  = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strip_html(html: str) -> str:
    """Cheap HTML -> text. Good enough for previews; we keep links visible."""
    if not html:
        return ""
    # Drop <style>/<script> blocks entirely.
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html,
                 flags=re.IGNORECASE | re.DOTALL)
    # Replace <br>, <p>, <li> with newlines.
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"</p\s*>", "\n\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"</li\s*>", "\n", txt, flags=re.IGNORECASE)
    # Strip remaining tags.
    txt = re.sub(r"<[^>]+>", "", txt)
    # Decode common entities.
    txt = (txt.replace("&nbsp;", " ")
              .replace("&amp;", "&")
              .replace("&lt;", "<")
              .replace("&gt;", ">")
              .replace("&quot;", '"')
              .replace("&#39;", "'"))
    # Collapse whitespace.
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _http_json(method: str, url: str, *,
               headers: dict | None = None,
               body_form: dict | None = None,
               body_json: dict | None = None,
               timeout: int = 20) -> tuple[int, dict | str]:
    """Tiny stdlib HTTP helper. Returns (status, parsed_body|text)."""
    hdrs = dict(headers or {})
    data: bytes | None = None

    if body_form is not None:
        data = urllib.parse.urlencode(body_form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body_json is not None:
        data = json.dumps(body_json).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "") or ""
            if not raw:
                return status, {}
            if "application/json" in ctype:
                try:
                    return status, json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return status, raw.decode("utf-8", errors="replace")
            return status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:   # noqa: BLE001
            pass
        try:
            return e.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return e.code, body
    except urllib.error.URLError as e:
        log.warning("outlook: network error %s %s: %s", method, url, e)
        return 0, {"error": "network", "detail": str(e)}


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register
class OutlookConnector(Connector):
    id = "outlook"
    label = "Outlook / Microsoft 365"
    blurb = "Read recent emails, search, draft replies (saved to Drafts, owner reviews before sending)."
    auth_kind = "oauth"
    scopes = ["Mail.Read", "Mail.ReadWrite", "User.Read"]

    # ── Config helpers ────────────────────────────────────────────────

    def _oauth_creds(self) -> tuple[str, str]:
        """Return (client_id, client_secret) or raise ValueError with hint."""
        block = (self.config or {}).get("outlook_oauth") or {}
        cid    = (block.get("client_id") or "").strip()
        secret = (block.get("client_secret") or "").strip()
        if not cid or not secret or "REPLACE" in cid or "REPLACE" in secret:
            raise ValueError(
                "outlook: missing client_id / client_secret in config.outlook_oauth. "
                "Register an app at portal.azure.com → Azure AD → App registrations "
                "(it's a 5-min step, no review). Add a Client Secret under "
                "Certificates & secrets, then paste the Application (client) ID "
                "and the secret VALUE into config.json."
            )
        return cid, secret

    def _scope_string(self) -> str:
        """Build the space-delimited scope string. offline_access is required
        to get a refresh_token from Microsoft."""
        scopes = list(self.scopes)
        if "offline_access" not in scopes:
            scopes = ["offline_access"] + scopes
        return " ".join(scopes)

    # ── OAuth: start ──────────────────────────────────────────────────

    def start_oauth(self, redirect_uri: str) -> str:
        client_id, _ = self._oauth_creds()
        state = secrets.token_urlsafe(24)
        # Persist state so callback can verify (best-effort).
        self._write_tokens({
            **(self._read_tokens() or {}),
            "_pending_state":        state,
            "_pending_redirect_uri": redirect_uri,
            "_pending_started_at":   _now_iso(),
        })

        params = {
            "client_id":     client_id,
            "response_type": "code",
            "redirect_uri":  redirect_uri,
            "response_mode": "query",
            "scope":         self._scope_string(),
            "state":         state,
            "prompt":        "select_account",
        }
        url = AUTH_URL + "?" + urllib.parse.urlencode(params)
        log.info("outlook: built auth URL (redirect_uri=%s)", redirect_uri)
        return url

    # ── OAuth: complete ───────────────────────────────────────────────

    def complete_oauth(self, code: str, redirect_uri: str) -> dict:
        client_id, client_secret = self._oauth_creds()

        status, body = _http_json(
            "POST", TOKEN_URL,
            body_form={
                "client_id":     client_id,
                "client_secret": client_secret,
                "code":          code,
                "redirect_uri":  redirect_uri,
                "grant_type":    "authorization_code",
                "scope":         self._scope_string(),
            },
        )
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            err = body if isinstance(body, dict) else {"error_detail": body}
            log.warning("outlook: token exchange failed (status=%s): %s", status, err)
            raise RuntimeError(f"outlook: token exchange failed: {err}")

        access_token  = body.get("access_token", "")
        refresh_token = body.get("refresh_token", "")
        expires_in    = int(body.get("expires_in", 3600))
        expires_at    = (_now_utc() + timedelta(seconds=expires_in - 60)) \
            .replace(microsecond=0).isoformat().replace("+00:00", "Z")

        # Fetch /me for email.
        email = ""
        me_status, me_body = _http_json(
            "GET", f"{GRAPH_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_status == 200 and isinstance(me_body, dict):
            email = me_body.get("mail") or me_body.get("userPrincipalName") or ""
        else:
            log.info("outlook: /me fetch returned %s: %s", me_status, me_body)

        record = {
            "access_token":  access_token,
            "refresh_token": refresh_token,
            "expires_at":    expires_at,
            "scopes":        body.get("scope", "").split() or self.scopes,
            "email":         email,
            "saved_at":      _now_iso(),
            "issued_at":     _now_iso(),
        }
        self._write_tokens(record)
        log.info("outlook: connected account %s", email or "<unknown>")
        return {"email": email, "scopes_granted": record["scopes"]}

    # ── Token refresh ─────────────────────────────────────────────────

    def _refresh_if_needed(self) -> str:
        """Return a valid access_token, refreshing first if expired/expiring."""
        rec = self._read_tokens()
        if not rec or not rec.get("access_token"):
            raise RuntimeError("outlook: not connected")

        expires_at = rec.get("expires_at", "")
        needs_refresh = False
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp_dt <= _now_utc():
                    needs_refresh = True
            except ValueError:
                needs_refresh = True
        else:
            needs_refresh = True

        if not needs_refresh:
            return rec["access_token"]

        refresh_token = rec.get("refresh_token", "")
        if not refresh_token:
            raise RuntimeError("outlook: access token expired and no refresh_token saved — reconnect required")

        client_id, client_secret = self._oauth_creds()
        status, body = _http_json(
            "POST", TOKEN_URL,
            body_form={
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
                "scope":         self._scope_string(),
            },
        )
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            err = body if isinstance(body, dict) else {"error_detail": body}
            log.warning("outlook: refresh failed (status=%s): %s", status, err)
            raise RuntimeError(f"outlook: token refresh failed: {err}")

        new_access  = body.get("access_token", "")
        new_refresh = body.get("refresh_token", refresh_token)
        expires_in  = int(body.get("expires_in", 3600))
        new_expires = (_now_utc() + timedelta(seconds=expires_in - 60)) \
            .replace(microsecond=0).isoformat().replace("+00:00", "Z")

        rec["access_token"]  = new_access
        rec["refresh_token"] = new_refresh
        rec["expires_at"]    = new_expires
        self._write_tokens(rec)
        log.info("outlook: refreshed access token (new expiry=%s)", new_expires)
        return new_access

    # ── Graph HTTP wrapper ────────────────────────────────────────────

    def _graph(self, method: str, path: str, *,
               params: dict | None = None,
               json_body: dict | None = None,
               extra_headers: dict | None = None) -> tuple[int, dict | str]:
        """Authenticated call to Microsoft Graph. `path` is the part after /v1.0."""
        access = self._refresh_if_needed()
        url = GRAPH_BASE + (path if path.startswith("/") else "/" + path)
        if params:
            url = url + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

        hdrs = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
        if extra_headers:
            hdrs.update(extra_headers)

        return _http_json(
            method, url,
            headers=hdrs,
            body_json=json_body,
        )

    # ── list_recent ───────────────────────────────────────────────────

    def list_recent(self, limit: int = 20) -> list[dict]:
        """GET /me/messages — recent mail. Shape mirrors gmail.list_recent."""
        params = {
            "$top":     max(1, min(limit, 100)),
            "$select":  "id,subject,from,bodyPreview,receivedDateTime,isRead",
            "$orderby": "receivedDateTime desc",
        }
        status, body = self._graph("GET", "/me/messages", params=params)
        if status != 200 or not isinstance(body, dict):
            log.warning("outlook: list_recent failed (status=%s): %s", status, body)
            self.update_status(last_error=f"list_recent: status={status}")
            return []
        return [self._msg_dict(m) for m in body.get("value", []) or []]

    # ── search ────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> list[dict]:
        if not query or not query.strip():
            return []
        params = {
            "$top":    max(1, min(limit, 100)),
            "$select": "id,subject,from,bodyPreview,receivedDateTime,isRead",
            "$search": f'"{query.strip()}"',
        }
        # Graph $search requires ConsistencyLevel: eventual.
        status, body = self._graph(
            "GET", "/me/messages",
            params=params,
            extra_headers={"ConsistencyLevel": "eventual"},
        )
        if status != 200 or not isinstance(body, dict):
            log.warning("outlook: search failed (status=%s): %s", status, body)
            self.update_status(last_error=f"search: status={status}")
            return []
        return [self._msg_dict(m) for m in body.get("value", []) or []]

    # ── get_message ───────────────────────────────────────────────────

    def get_message(self, message_id: str) -> dict:
        if not message_id:
            return {}
        status, body = self._graph("GET", f"/me/messages/{message_id}")
        if status != 200 or not isinstance(body, dict):
            log.warning("outlook: get_message failed (status=%s): %s", status, body)
            self.update_status(last_error=f"get_message: status={status}")
            return {}

        body_obj = body.get("body") or {}
        ctype    = (body_obj.get("contentType") or "").lower()
        content  = body_obj.get("content", "") or ""
        if ctype == "html":
            content = _strip_html(content)

        msg = self._msg_dict(body)
        msg["body"] = content
        msg["to"]   = [
            (r.get("emailAddress") or {}).get("address", "")
            for r in (body.get("toRecipients") or [])
        ]
        msg["cc"]   = [
            (r.get("emailAddress") or {}).get("address", "")
            for r in (body.get("ccRecipients") or [])
        ]
        return msg

    # ── draft_reply ───────────────────────────────────────────────────

    def draft_reply(self, message_id: str, reply_text: str) -> dict:
        """Create a reply draft in Drafts. Owner reviews & sends from Outlook."""
        if not message_id:
            raise ValueError("outlook: message_id required")
        if not (reply_text or "").strip():
            raise ValueError("outlook: reply_text required")

        # POST /me/messages/{id}/createReply returns a draft message.
        status, body = self._graph("POST", f"/me/messages/{message_id}/createReply")
        if status not in (200, 201) or not isinstance(body, dict):
            log.warning("outlook: createReply failed (status=%s): %s", status, body)
            self.update_status(last_error=f"createReply: status={status}")
            return {"created": False, "error": f"createReply status={status}"}

        draft_id = body.get("id", "")
        if not draft_id:
            return {"created": False, "error": "createReply returned no id"}

        # PATCH the draft to set the body text.
        patch_status, patch_body = self._graph(
            "PATCH", f"/me/messages/{draft_id}",
            json_body={"body": {"contentType": "Text", "content": reply_text}},
        )
        if patch_status not in (200, 204):
            log.warning("outlook: draft PATCH failed (status=%s): %s", patch_status, patch_body)
            self.update_status(last_error=f"draft_reply PATCH: status={patch_status}")
            return {"created": True, "draft_id": draft_id,
                    "body_set": False,
                    "error": f"PATCH status={patch_status}"}

        return {"created": True, "draft_id": draft_id, "body_set": True}

    # ── status (extended) ─────────────────────────────────────────────

    def status(self) -> dict:
        base_status = super().status()
        rec = self._read_tokens()
        base_status["email"] = rec.get("email", "") or ""
        base_status["total_unread"] = 0

        if base_status.get("connected"):
            try:
                s, body = self._graph("GET", "/me/mailFolders/inbox",
                                      params={"$select": "unreadItemCount"})
                if s == 200 and isinstance(body, dict):
                    base_status["total_unread"] = int(body.get("unreadItemCount", 0) or 0)
            except Exception as exc:    # noqa: BLE001
                log.info("outlook: status unread count failed: %s", exc)
                base_status["last_error"] = str(exc)
        return base_status

    # ── Internal: shape one message dict like gmail.list_recent ───────

    @staticmethod
    def _msg_dict(m: dict) -> dict:
        frm = (m.get("from") or {}).get("emailAddress") or {}
        return {
            "id":           m.get("id", ""),
            "subject":      m.get("subject", "") or "",
            "from":         frm.get("address", "") or "",
            "from_name":    frm.get("name", "") or "",
            "snippet":      m.get("bodyPreview", "") or "",
            "received_iso": m.get("receivedDateTime", "") or "",
            "is_read":      bool(m.get("isRead", False)),
        }


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. config is the loaded config.json (must
# include outlook_oauth.client_id and outlook_oauth.client_secret).
#
#   POST /api/owner/connectors/outlook/connect
#       body:   {} (settings already saved)
#       calls:  OutlookConnector(config, user_dir).start_oauth(redirect_uri)
#       returns:{"auth_url": "https://login.microsoftonline.com/..."}
#       errors: 400 if ValueError (missing config.outlook_oauth)
#
#   GET  /api/owner/connectors/outlook/callback?code=...&state=...
#       calls:  OutlookConnector(config, user_dir).complete_oauth(code, redirect_uri)
#       then:   HTTP 302 → /owner#outlook
#
#   POST /api/owner/connectors/outlook/disconnect
#       calls:  OutlookConnector(config, user_dir).disconnect()
#       returns:{"ok": true}
#
#   GET  /api/owner/connectors/outlook/status
#       calls:  status()
#       returns:{connected, email, total_unread, ...}
#
#   GET  /api/owner/connectors/outlook/messages?limit=20&q=
#       calls:  search(q, limit) if q else list_recent(limit)
#       returns:[{id, subject, from, from_name, snippet, received_iso, is_read}, ...]
#
#   GET  /api/owner/connectors/outlook/message/<id>
#       calls:  get_message(id)
#       returns:{id, subject, from, to[], cc[], body, received_iso, is_read, ...}
#
#   POST /api/owner/connectors/outlook/draft_reply
#       body:   {"message_id": "...", "reply_text": "..."}
#       calls:  draft_reply(message_id, reply_text)
#       returns:{"created": true, "draft_id": "...", "body_set": true}
#
# ---------------------------------------------------------------------------
