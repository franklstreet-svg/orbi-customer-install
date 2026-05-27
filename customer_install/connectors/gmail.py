"""
connectors.gmail — Gmail integration for the Orbi customer install.

Mirrors the OAuth pattern in customer_install/gcal.py (same Google Cloud
project, same Desktop-app client) and plugs into the generic Connector
base in connectors/base.py.

DESIGN NOTES
------------
- Reuses CONFIG["gcal_oauth"].client_id / client_secret. The Gmail and
  Calendar APIs live in the same Google project so one OAuth client serves
  both. Scopes are requested separately so the user explicitly consents
  to mail access.

- Scopes:
    https://www.googleapis.com/auth/gmail.readonly  (list / read inbox)
    https://www.googleapis.com/auth/gmail.compose   (create Drafts only —
                                                     never send)

- Drafts vs. Send: draft_reply() ALWAYS lands in the Drafts folder so the
  owner reviews before sending. This is a hard policy choice — the only
  compose scope we ask for is gmail.compose (which permits drafts and
  sending), but we never call users().messages().send().

- Tokens stored by base.Connector at
    <user_dir>/connector_tokens/gmail.json  (mode 0o600, atomic writes).

- Lazy imports of google_auth_oauthlib / googleapiclient so an install
  without the libs can still boot the rest of Orbi.
"""

from __future__ import annotations

import base64
import html
import logging
import re
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr

from .base import Connector, register

log = logging.getLogger("orbi.connectors.gmail")

AUTH_URI  = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

_LOCK = threading.Lock()

_BODY_CAP = 10_000  # max chars returned from get_message()


# ---------------------------------------------------------------------------
# Lazy imports — keep google libs out of boot path
# ---------------------------------------------------------------------------

def _google_flow():
    from google_auth_oauthlib.flow import Flow
    return Flow

def _google_creds_cls():
    from google.oauth2.credentials import Credentials
    return Credentials

def _google_request():
    from google.auth.transport.requests import Request
    return Request

def _build_service():
    from googleapiclient.discovery import build
    return build


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register
class GmailConnector(Connector):
    id = "gmail"
    label = "Gmail"
    blurb = ("Read recent emails, search by query, draft replies "
             "(goes to Drafts — owner reviews before sending).")
    auth_kind = "oauth"
    scopes = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        # gmail.compose does NOT include send permission — that's a Google
        # quirk. We add gmail.send so safe_send.send_email() can route
        # whitelisted categories straight to the wire. Owners who connected
        # before this scope was added must re-connect to grant it.
        "https://www.googleapis.com/auth/gmail.send",
    ]

    # ── OAuth helpers ─────────────────────────────────────────────────────

    def _oauth_creds(self) -> tuple[str, str]:
        """Pull Google Cloud client_id / client_secret from CONFIG.gcal_oauth.
        Same project as Calendar; just a different scope set."""
        g = (self.config or {}).get("gcal_oauth") or {}
        return g.get("client_id", "") or "", g.get("client_secret", "") or ""

    def _client_config(self, redirect_uri: str) -> dict:
        client_id, client_secret = self._oauth_creds()
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

    def start_oauth(self, redirect_uri: str) -> str:
        """Return the Google authorization URL the owner should be sent to."""
        client_id, client_secret = self._oauth_creds()
        if not client_id or not client_secret:
            raise RuntimeError(
                "gmail: gcal_oauth.client_id / client_secret not configured in CONFIG"
            )
        Flow = _google_flow()
        flow = Flow.from_client_config(
            self._client_config(redirect_uri),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        auth_url, _state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        log.info("gmail: built auth URL (redirect_uri=%s)", redirect_uri)
        return auth_url

    def complete_oauth(self, code: str, redirect_uri: str) -> dict:
        """Exchange the code for tokens, persist them, return {email, scopes_granted}."""
        Flow = _google_flow()
        flow = Flow.from_client_config(
            self._client_config(redirect_uri),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Fetch the connected mailbox's email address from the Gmail profile.
        email = ""
        try:
            build = _build_service()
            svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
            profile = svc.users().getProfile(userId="me").execute()
            email = profile.get("emailAddress", "") or ""
        except Exception as exc:    # noqa: BLE001 — non-fatal metadata
            log.warning("gmail: could not fetch profile email: %s", exc)

        scopes_granted = list(creds.scopes or self.scopes)
        record = {
            "access_token":   creds.token,
            "refresh_token":  creds.refresh_token,
            "token_uri":      creds.token_uri,
            "client_id":      creds.client_id,
            "client_secret":  creds.client_secret,
            "scopes_granted": scopes_granted,
            "expires_at":     creds.expiry.isoformat() if creds.expiry else None,
            "email":          email,
            "saved_at":       _now_iso(),
            "issued_at":      _now_iso(),
        }
        self._write_tokens(record)
        log.info("gmail: connected account %s", email or "<unknown>")
        return {"email": email, "scopes_granted": scopes_granted}

    # ── Credentials loader (refreshes silently) ───────────────────────────

    def _load_credentials(self):
        rec = self._read_tokens()
        if not rec:
            raise RuntimeError("gmail: not connected")
        Credentials = _google_creds_cls()
        creds = Credentials(
            token=rec.get("access_token"),
            refresh_token=rec.get("refresh_token"),
            token_uri=rec.get("token_uri", TOKEN_URI),
            client_id=rec.get("client_id"),
            client_secret=rec.get("client_secret"),
            scopes=rec.get("scopes_granted", self.scopes),
        )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                Request = _google_request()
                creds.refresh(Request())
                # Persist refreshed access token + expiry.
                with _LOCK:
                    rec = self._read_tokens()
                    rec["access_token"] = creds.token
                    rec["expires_at"]   = (creds.expiry.isoformat()
                                           if creds.expiry else None)
                    self._write_tokens(rec)
                log.info("gmail: refreshed access token")
            else:
                raise RuntimeError("gmail: credentials invalid and not refreshable")
        return creds

    def _get_service(self):
        """Return an authenticated gmail v1 service. Auto-refreshes the token."""
        creds = self._load_credentials()
        build = _build_service()
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ── Inbox listing & search ────────────────────────────────────────────

    def list_recent(self, limit: int = 20) -> list[dict]:
        """Return the most recent INBOX messages with summary metadata."""
        return self._search_internal("in:inbox", limit)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Free-form Gmail search (uses Gmail's native search syntax)."""
        q = (query or "").strip()
        if not q:
            return []
        return self._search_internal(q, limit)

    def _search_internal(self, q: str, limit: int) -> list[dict]:
        limit = max(1, min(int(limit or 20), 100))
        svc = self._get_service()
        try:
            resp = svc.users().messages().list(
                userId="me", q=q, maxResults=limit,
            ).execute()
        except Exception as exc:    # noqa: BLE001
            log.warning("gmail search %r failed: %s", q, exc)
            self.update_status(last_error=f"search failed: {exc}")
            return []

        ids = [m["id"] for m in (resp.get("messages") or []) if m.get("id")]
        out: list[dict] = []
        for mid in ids:
            try:
                msg = svc.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
            except Exception as exc:    # noqa: BLE001
                log.warning("gmail get(meta) %s failed: %s", mid, exc)
                continue
            out.append(self._format_summary(msg))
        self.update_status(last_sync=_now_iso(), last_error="")
        return out

    @staticmethod
    def _format_summary(msg: dict) -> dict:
        headers = {h.get("name", "").lower(): h.get("value", "")
                   for h in (msg.get("payload", {}) or {}).get("headers", []) or []}
        label_ids = set(msg.get("labelIds") or [])
        return {
            "id":      msg.get("id", ""),
            "subject": headers.get("subject", "") or "",
            "from":    headers.get("from", "") or "",
            "date":    headers.get("date", "") or "",
            "snippet": msg.get("snippet", "") or "",
            "unread":  "UNREAD" in label_ids,
        }

    # ── Full message retrieval ────────────────────────────────────────────

    def get_message(self, message_id: str) -> dict:
        """Return the full message: headers + plain-text body (capped at 10k chars)."""
        if not message_id:
            return {}
        svc = self._get_service()
        try:
            msg = svc.users().messages().get(
                userId="me", id=message_id, format="full",
            ).execute()
        except Exception as exc:    # noqa: BLE001
            log.warning("gmail get(full) %s failed: %s", message_id, exc)
            self.update_status(last_error=f"get_message failed: {exc}")
            return {}

        headers_list = (msg.get("payload", {}) or {}).get("headers", []) or []
        headers = {h.get("name", ""): h.get("value", "") for h in headers_list}
        # Lowercase index for friendly access too.
        headers_lc = {k.lower(): v for k, v in headers.items()}

        body = _extract_body(msg.get("payload") or {})
        if len(body) > _BODY_CAP:
            body = body[:_BODY_CAP] + "\n…[truncated]"

        label_ids = set(msg.get("labelIds") or [])
        return {
            "id":         msg.get("id", ""),
            "thread_id":  msg.get("threadId", ""),
            "subject":    headers_lc.get("subject", "") or "",
            "from":       headers_lc.get("from", "") or "",
            "to":         headers_lc.get("to", "") or "",
            "cc":         headers_lc.get("cc", "") or "",
            "date":       headers_lc.get("date", "") or "",
            "message_id": headers_lc.get("message-id", "") or "",
            "references": headers_lc.get("references", "") or "",
            "in_reply_to": headers_lc.get("in-reply-to", "") or "",
            "snippet":    msg.get("snippet", "") or "",
            "body":       body,
            "unread":     "UNREAD" in label_ids,
            "headers":    headers,
        }

    # ── Draft a reply (never send) ────────────────────────────────────────

    def draft_reply(self, message_id: str, reply_text: str) -> dict:
        """Create a Drafts entry that replies to `message_id`. Owner sends manually."""
        if not message_id:
            raise ValueError("message_id required")
        if reply_text is None:
            reply_text = ""

        svc = self._get_service()
        # Fetch the original to get threading headers + From/To/Subject.
        try:
            orig = svc.users().messages().get(
                userId="me", id=message_id, format="metadata",
                metadataHeaders=["From", "To", "Cc", "Subject",
                                 "Message-ID", "References", "In-Reply-To"],
            ).execute()
        except Exception as exc:    # noqa: BLE001
            log.warning("gmail draft_reply: cannot read original %s: %s",
                        message_id, exc)
            raise

        thread_id = orig.get("threadId", "")
        headers_lc = {h.get("name", "").lower(): h.get("value", "")
                      for h in (orig.get("payload", {}) or {}).get("headers", []) or []}

        orig_from   = headers_lc.get("from", "")
        orig_msgid  = headers_lc.get("message-id", "")
        orig_refs   = headers_lc.get("references", "")
        orig_subj   = headers_lc.get("subject", "") or ""

        # Reply recipient = original sender. Use bare email if parseable.
        _name, reply_to_addr = parseaddr(orig_from)
        if not reply_to_addr:
            reply_to_addr = orig_from

        subject = orig_subj if orig_subj.lower().startswith("re:") else f"Re: {orig_subj}".strip()

        # Build the References chain: prior References + original Message-ID.
        new_refs = " ".join(x for x in (orig_refs.strip(), orig_msgid.strip()) if x).strip()

        em = EmailMessage()
        em["To"]      = reply_to_addr
        em["Subject"] = subject
        if orig_msgid:
            em["In-Reply-To"] = orig_msgid
        if new_refs:
            em["References"] = new_refs
        em.set_content(reply_text or "")

        raw = base64.urlsafe_b64encode(em.as_bytes()).decode("ascii")
        body = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id

        try:
            draft = svc.users().drafts().create(userId="me", body=body).execute()
        except Exception as exc:    # noqa: BLE001
            log.warning("gmail draft_reply: drafts.create failed: %s", exc)
            self.update_status(last_error=f"draft_reply failed: {exc}")
            raise

        draft_id = draft.get("id", "")
        log.info("gmail: drafted reply %s to message %s", draft_id, message_id)
        return {"draft_id": draft_id}

    # ── Actually send a new message (NOT a reply) ─────────────────────────
    #
    # Used by safe_send.send_email() for whitelisted categories. The
    # caller is responsible for gating this behind the owner's safe-send
    # policy — this method does NOT check the whitelist itself.

    def send_message(self, to_email: str, subject: str, body: str) -> dict:
        """Send a brand-new email via users.messages.send. Returns
        {message_id, thread_id}. Raises RuntimeError on API failure."""
        if not to_email or not to_email.strip():
            raise ValueError("to_email required")
        em = EmailMessage()
        em["To"]      = to_email.strip()
        em["Subject"] = (subject or "").strip() or "(no subject)"
        em.set_content(body or "")

        raw = base64.urlsafe_b64encode(em.as_bytes()).decode("ascii")
        svc = self._get_service()
        try:
            resp = svc.users().messages().send(
                userId="me", body={"raw": raw},
            ).execute()
        except Exception as exc:    # noqa: BLE001
            log.warning("gmail send_message to %s failed: %s", to_email, exc)
            self.update_status(last_error=f"send_message failed: {exc}")
            raise RuntimeError(f"gmail send failed: {exc}") from exc
        mid = resp.get("id", "")
        tid = resp.get("threadId", "")
        log.info("gmail: SENT message %s to %s", mid, to_email)
        return {"message_id": mid, "thread_id": tid}

    # ── Status (extends base) ─────────────────────────────────────────────

    def status(self) -> dict:
        base = super().status()
        rec = self._read_tokens()
        email = rec.get("email", "") or ""
        total_unread = 0
        if rec:
            try:
                svc = self._get_service()
                lbl = svc.users().labels().get(userId="me", id="INBOX").execute()
                total_unread = int(lbl.get("messagesUnread", 0) or 0)
            except Exception as exc:    # noqa: BLE001 — status must not raise
                log.warning("gmail status: unread count failed: %s", exc)
                base["last_error"] = base.get("last_error") or f"status: {exc}"
        base.update({
            "email":        email,
            "total_unread": total_unread,
            "last_sync":    rec.get("last_sync") or base.get("last_sync") or "",
        })
        return base


# ---------------------------------------------------------------------------
# MIME body extraction
# ---------------------------------------------------------------------------

_TAG_RE   = re.compile(r"<[^>]+>")
_WSPACE_RE = re.compile(r"[ \t]+\n")
_BLANKS_RE = re.compile(r"\n{3,}")


def _b64url_decode(data: str) -> bytes:
    if not data:
        return b""
    # Gmail uses URL-safe base64 without padding.
    pad = (-len(data)) % 4
    return base64.urlsafe_b64decode(data + ("=" * pad))


def _strip_html(s: str) -> str:
    # Remove <script>/<style> blocks entirely before tag stripping.
    s = re.sub(r"<(script|style)\b.*?</\1>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WSPACE_RE.sub("\n", s)
    s = _BLANKS_RE.sub("\n\n", s)
    return s.strip()


def _extract_body(payload: dict) -> str:
    """Walk a Gmail payload tree, prefer text/plain, fall back to stripped text/html."""
    plain_parts: list[str] = []
    html_parts:  list[str] = []

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        data = body.get("data") or ""
        if mime == "text/plain" and data:
            try:
                plain_parts.append(_b64url_decode(data).decode("utf-8", "replace"))
            except Exception:    # noqa: BLE001
                pass
        elif mime == "text/html" and data:
            try:
                html_parts.append(_b64url_decode(data).decode("utf-8", "replace"))
            except Exception:    # noqa: BLE001
                pass
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload or {})

    if plain_parts:
        return "\n".join(p.strip() for p in plain_parts if p).strip()
    if html_parts:
        return _strip_html("\n".join(html_parts))
    return ""


# ---------------------------------------------------------------------------
# Internal utils
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. The connector instance is built with
#   GmailConnector(CONFIG, user_dir).
#
#   POST /api/owner/connectors/gmail/connect
#       body:    {}
#       calls:   conn.start_oauth(redirect_uri)
#       returns: { "auth_url": "https://accounts.google.com/o/oauth2/..." }
#
#   GET  /api/owner/connectors/gmail/callback?code=...
#       calls:   conn.complete_oauth(code, redirect_uri)
#       then:    HTTP 302 → /owner#integrations
#
#   POST /api/owner/connectors/gmail/disconnect
#       calls:   conn.disconnect()
#       returns: { "ok": true }
#
#   GET  /api/owner/connectors/gmail/status
#       calls:   conn.status()
#       returns: dict from conn.status()
#
#   GET  /api/owner/connectors/gmail/messages?q=...&limit=...
#       calls:   conn.search(q, limit) if q else conn.list_recent(limit)
#       returns: { "messages": [ {id, subject, from, snippet, date, unread}, ... ] }
#
#   GET  /api/owner/connectors/gmail/message/<id>
#       calls:   conn.get_message(id)
#       returns: full message dict (see get_message())
#
#   POST /api/owner/connectors/gmail/draft_reply
#       body:    { "message_id": "...", "reply_text": "..." }
#       calls:   conn.draft_reply(message_id, reply_text)
#       returns: { "draft_id": "..." }
#
#   POST /api/owner/connectors/gmail/send_message
#       body:    { "to_email": "...", "subject": "...", "body": "..." }
#       calls:   conn.send_message(to_email, subject, body)
#       returns: { "message_id": "...", "thread_id": "..." }
#       NOTE — prefer routing through safe_send.send_email() so the
#       owner's category whitelist is honoured. This raw endpoint is for
#       the orchestrator's "Send Now" override only.
#
# SCOPE CHANGE (this feature):
#   Added https://www.googleapis.com/auth/gmail.send to `scopes`. Owners
#   who connected before this change must DISCONNECT and re-CONNECT to
#   grant the new permission. Until then send_message() will raise (or
#   safe_send will fall back to drafts).
#
# ---------------------------------------------------------------------------
