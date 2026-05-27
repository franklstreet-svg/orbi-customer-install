"""
connectors.slack — Slack OAuth connector for the Orbi customer install.

PURPOSE
-------
Lets the owner connect their Slack workspace so Orbi can:
  - list channels the bot is a member of
  - search messages across the workspace
  - post messages into a channel

DESIGN NOTES
------------
- OAuth v2 ("granular" Slack apps). The auth URL goes to
  https://slack.com/oauth/v2/authorize and the token exchange POSTs
  form-encoded data to https://slack.com/api/oauth.v2.access — NOT to a
  generic OAuth2 token endpoint. Slack's response is JSON with an
  ``ok`` boolean we have to check on every call.

- Slack-isms baked in here:
    * scopes are comma-separated on the auth URL (every other OAuth
      provider uses spaces — this is genuinely Slack-specific).
    * the bot token is ``access_token`` on the top-level response;
      a separate ``authed_user.access_token`` exists if user scopes
      are requested. We only ask for bot scopes here.
    * almost every Web API method accepts either GET or POST. We POST
      with form encoding for writes (chat.postMessage) and GET with
      query strings for reads (conversations.list, search.messages).

- Owner OAuth app registration:
    Frank (or the customer) registers an app at https://api.slack.com/apps,
    adds the bot scopes listed in ``scopes`` below, sets the redirect
    URL to ``http://127.0.0.1:<port>/api/owner/connectors/slack/callback``,
    and pastes Client ID + Client Secret into Orbi settings under
    ``slack_oauth.client_id`` / ``slack_oauth.client_secret``.
    No Slack review process for distribution to a single workspace.

- All HTTP uses stdlib ``urllib.request`` + ``json`` — no extra deps.

ROUTE SURFACE
-------------
See the comment block at the bottom of this file for the routes the
orchestrator should wire up in orbi.py.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from .base import Connector, register, _now_iso

log = logging.getLogger("orbi.connectors.slack")

# ---------------------------------------------------------------------------
# Slack endpoints
# ---------------------------------------------------------------------------

AUTH_URL  = "https://slack.com/oauth/v2/authorize"
TOKEN_URL = "https://slack.com/api/oauth.v2.access"
API_BASE  = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Lazy HTTP helpers (stdlib only — kept inside functions to mirror gcal.py)
# ---------------------------------------------------------------------------

def _http_post_form(url: str, data: dict, headers: dict | None = None,
                    timeout: float = 15.0) -> dict:
    """POST application/x-www-form-urlencoded; return parsed JSON dict."""
    import urllib.request
    import urllib.error

    body = urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept":       "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        log.warning("slack POST %s -> HTTP %s: %s", url, exc.code, raw[:200])
        raise
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        log.warning("slack POST %s: non-JSON response: %s", url, raw[:200])
        raise RuntimeError(f"slack non-JSON response from {url}") from exc


def _http_post_json(url: str, payload: dict, bearer: str,
                    timeout: float = 15.0) -> dict:
    """POST application/json with Bearer auth; return parsed JSON dict."""
    import urllib.request
    import urllib.error

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type":  "application/json; charset=utf-8",
            "Accept":        "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        log.warning("slack POST(json) %s -> HTTP %s: %s", url, exc.code, raw[:200])
        raise
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        log.warning("slack POST(json) %s: non-JSON response: %s", url, raw[:200])
        raise RuntimeError(f"slack non-JSON response from {url}") from exc


def _http_get(url: str, params: dict, bearer: str,
              timeout: float = 15.0) -> dict:
    """GET with Bearer auth + query params; return parsed JSON dict."""
    import urllib.request
    import urllib.error

    qs = urlencode({k: v for k, v in params.items() if v is not None})
    full = f"{url}?{qs}" if qs else url
    req = urllib.request.Request(
        full,
        method="GET",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept":        "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        log.warning("slack GET %s -> HTTP %s: %s", full, exc.code, raw[:200])
        raise
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        log.warning("slack GET %s: non-JSON response: %s", full, raw[:200])
        raise RuntimeError(f"slack non-JSON response from {url}") from exc


def _check_ok(payload: dict, where: str) -> dict:
    """Raise if Slack returned ``ok: false``. Otherwise pass-through."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"slack {where}: response not a dict")
    if not payload.get("ok"):
        err = payload.get("error") or "unknown_error"
        raise RuntimeError(f"slack {where}: {err}")
    return payload


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register
class SlackConnector(Connector):
    id = "slack"
    label = "Slack"
    blurb = ("Search messages, post updates to a channel. "
             "For owners who use Slack for their team.")
    auth_kind = "oauth"
    # Bot scopes only — keeps the consent screen short and avoids needing
    # user-scope tokens.
    scopes = [
        "channels:history",
        "channels:read",
        "chat:write",
        "search:read",
        "users:read",
    ]

    # ── Config helpers ─────────────────────────────────────────────────

    def _client(self) -> tuple[str, str]:
        cfg = (self.config or {}).get("slack_oauth") or {}
        cid = (cfg.get("client_id")     or "").strip()
        sec = (cfg.get("client_secret") or "").strip()
        if not cid or not sec:
            raise RuntimeError(
                "slack: missing slack_oauth.client_id / slack_oauth.client_secret "
                "in config — register the app at api.slack.com/apps first."
            )
        return cid, sec

    def _bearer(self) -> str:
        rec = self._read_tokens()
        tok = rec.get("access_token") or ""
        if not tok:
            raise RuntimeError("slack: not connected (no access_token)")
        return tok

    # ── OAuth ──────────────────────────────────────────────────────────

    def start_oauth(self, redirect_uri: str) -> str:
        """
        Build the Slack authorization URL. Returns the URL the owner should
        be redirected to. ``state`` is generated and stashed on the token
        record so callback can verify it.
        """
        client_id, _ = self._client()
        state = secrets.token_urlsafe(24)

        # Stash state into the token file (we don't have any tokens yet,
        # but we can still write a pending record).
        pending = self._read_tokens()
        pending["_pending_state"]        = state
        pending["_pending_redirect_uri"] = redirect_uri
        pending["_pending_started_at"]   = _now_iso()
        self._write_tokens(pending)

        # Slack-specific: comma-separated scopes (not space-separated).
        params = {
            "client_id":    client_id,
            "scope":        ",".join(self.scopes),
            "redirect_uri": redirect_uri,
            "state":        state,
        }
        url = f"{AUTH_URL}?{urlencode(params)}"
        log.info("slack: built auth URL (redirect_uri=%s)", redirect_uri)
        return url

    def complete_oauth(self, code: str, redirect_uri: str) -> dict:
        """
        Exchange the auth code for a bot access token + team info. Saves
        the token record and returns a small status dict for the dashboard.
        """
        client_id, client_secret = self._client()

        resp = _http_post_form(
            TOKEN_URL,
            {
                "code":          code,
                "client_id":     client_id,
                "client_secret": client_secret,
                "redirect_uri":  redirect_uri,
            },
        )
        _check_ok(resp, "oauth.v2.access")

        team       = resp.get("team")        or {}
        authed_usr = resp.get("authed_user") or {}
        record = {
            "access_token":  resp.get("access_token", ""),
            "token_type":    resp.get("token_type", "bot"),
            "scope":         resp.get("scope", ""),
            "bot_user_id":   resp.get("bot_user_id", ""),
            "app_id":        resp.get("app_id", ""),
            "team_id":       team.get("id", ""),
            "team_name":     team.get("name", ""),
            "authed_user_id": authed_usr.get("id", ""),
            "saved_at":      _now_iso(),
            "issued_at":     _now_iso(),
        }
        self._write_tokens(record)
        log.info("slack: connected workspace %r (team_id=%s)",
                 record["team_name"], record["team_id"])

        return {
            "team_name":   record["team_name"],
            "team_id":     record["team_id"],
            "bot_user_id": record["bot_user_id"],
            "scopes":      record["scope"],
        }

    # ── API methods ────────────────────────────────────────────────────

    def list_channels(self, limit: int = 50) -> dict:
        """List public + private channels the bot can see."""
        tok = self._bearer()
        payload = _http_get(
            f"{API_BASE}/conversations.list",
            {
                "types": "public_channel,private_channel",
                "limit": max(1, min(int(limit), 1000)),
                "exclude_archived": "true",
            },
            tok,
        )
        _check_ok(payload, "conversations.list")
        channels = [
            {
                "id":          c.get("id", ""),
                "name":        c.get("name", ""),
                "is_private":  bool(c.get("is_private")),
                "is_member":   bool(c.get("is_member")),
                "num_members": c.get("num_members", 0),
                "topic":       (c.get("topic") or {}).get("value", ""),
            }
            for c in payload.get("channels", [])
        ]
        return {"channels": channels, "count": len(channels)}

    def search_messages(self, query: str, limit: int = 20) -> dict:
        """Search messages. Requires search:read (user scope on some plans)."""
        if not query or not query.strip():
            return {"messages": [], "count": 0, "query": query or ""}
        tok = self._bearer()
        payload = _http_get(
            f"{API_BASE}/search.messages",
            {
                "query": query.strip(),
                "count": max(1, min(int(limit), 100)),
                "sort":  "timestamp",
                "sort_dir": "desc",
            },
            tok,
        )
        _check_ok(payload, "search.messages")
        matches = ((payload.get("messages") or {}).get("matches")) or []
        results = [
            {
                "text":      m.get("text", ""),
                "user":      m.get("user", ""),
                "username":  m.get("username", ""),
                "ts":        m.get("ts", ""),
                "permalink": m.get("permalink", ""),
                "channel":   (m.get("channel") or {}).get("name", ""),
                "channel_id": (m.get("channel") or {}).get("id", ""),
            }
            for m in matches
        ]
        return {"messages": results, "count": len(results), "query": query.strip()}

    def post_message(self, channel_id: str, text: str) -> dict:
        """Post a message to a channel as the bot user."""
        if not channel_id or not channel_id.strip():
            raise ValueError("slack post_message: channel_id required")
        if not text or not text.strip():
            raise ValueError("slack post_message: text required")
        tok = self._bearer()
        payload = _http_post_json(
            f"{API_BASE}/chat.postMessage",
            {"channel": channel_id.strip(), "text": text},
            tok,
        )
        _check_ok(payload, "chat.postMessage")
        self.update_status(last_sync=_now_iso(), last_error="")
        return {
            "ok":      True,
            "ts":      payload.get("ts", ""),
            "channel": payload.get("channel", ""),
        }

    # ── Status (extends base.status) ───────────────────────────────────

    def status(self) -> dict:
        base = super().status()
        rec  = self._read_tokens()
        base["team_name"]   = rec.get("team_name", "") or ""
        base["team_id"]     = rec.get("team_id", "") or ""
        base["bot_user_id"] = rec.get("bot_user_id", "") or ""
        base["account"]     = rec.get("team_name", "") or base.get("account", "")

        # channel_count is a best-effort live probe; if Slack is unreachable
        # or scopes are missing we just leave it 0 and surface the error.
        channel_count: int = 0
        if base["connected"]:
            try:
                ch = self.list_channels(limit=200)
                channel_count = ch.get("count", 0)
            except Exception as exc:    # noqa: BLE001
                base["last_error"] = f"channel probe: {exc}"
        base["channel_count"] = channel_count
        return base


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. config (slack_oauth.client_id /
# slack_oauth.client_secret) comes from owner settings.
#
#   POST /api/owner/connectors/slack/connect
#       body:    {} (settings already saved)
#       calls:   SlackConnector(config, user_dir).start_oauth(redirect_uri)
#       returns: { "auth_url": "https://slack.com/oauth/v2/authorize?..." }
#
#   GET  /api/owner/connectors/slack/callback?code=...&state=...
#       calls:   SlackConnector(config, user_dir).complete_oauth(code, redirect_uri)
#       then:    HTTP 302 → /owner#connectors
#
#   POST /api/owner/connectors/slack/disconnect
#       calls:   SlackConnector(config, user_dir).disconnect()
#       returns: { "ok": true }
#
#   GET  /api/owner/connectors/slack/status
#       calls:   SlackConnector(config, user_dir).status()
#       returns: status dict (connected, team_name, channel_count, ...)
#
#   GET  /api/owner/connectors/slack/channels?limit=50
#       calls:   SlackConnector(...).list_channels(limit)
#       returns: { "channels": [...], "count": N }
#
#   GET  /api/owner/connectors/slack/search?q=...&limit=20
#       calls:   SlackConnector(...).search_messages(q, limit)
#       returns: { "messages": [...], "count": N, "query": "..." }
#
#   POST /api/owner/connectors/slack/post
#       body:    { "channel_id": "C123...", "text": "hello" }
#       calls:   SlackConnector(...).post_message(channel_id, text)
#       returns: { "ok": true, "ts": "...", "channel": "C123..." }
#
# ---------------------------------------------------------------------------
