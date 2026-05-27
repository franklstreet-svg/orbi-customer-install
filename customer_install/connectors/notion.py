"""
connectors.notion — Notion OAuth connector for the Orbi customer install.

PURPOSE
-------
Lets the owner connect a Notion workspace so Orbi can:
  - search the user's pages
  - capture notes from chat as new Notion pages under a chosen parent
  - list databases (so the owner can pick a parent later)

DESIGN NOTES
------------
- Notion's OAuth differs from most providers in two important ways:
    1. There are NO per-scope scopes. Permissions are workspace-level,
       granted by the user when they pick which pages/dbs the integration
       can see. The connector therefore reports ``scopes = []``.
    2. The token-exchange endpoint requires HTTP Basic auth using the
       integration's ``client_id:client_secret`` (NOT the body — the
       body carries ``grant_type``, ``code``, ``redirect_uri``).

- Every API call must include the ``Notion-Version`` header. We pin to
  ``2022-06-28`` which matches Notion's current stable contract.

- Owner integration registration:
    Frank registers a PUBLIC integration at https://notion.so/my-integrations,
    sets the redirect URI to
    ``http://127.0.0.1:<port>/api/owner/connectors/notion/callback``,
    and pastes Client ID + Client Secret into Orbi settings under
    ``notion_oauth.client_id`` / ``notion_oauth.client_secret``.
    No Notion review process for a public integration.

- All HTTP uses stdlib ``urllib.request`` + ``json`` — no extra deps.

- ``create_page`` accepts plain text and chunks it into a series of
  paragraph blocks (one block per non-empty line). Notion enforces a
  2000-character limit per rich_text run; we don't try to be clever about
  splitting long lines beyond that — callers writing huge dumps should
  pre-chunk.

ROUTE SURFACE
-------------
See the comment block at the bottom of this file for the routes the
orchestrator should wire up in orbi.py.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

from .base import Connector, register, _now_iso

log = logging.getLogger("orbi.connectors.notion")

# ---------------------------------------------------------------------------
# Notion endpoints
# ---------------------------------------------------------------------------

AUTH_URL       = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL      = "https://api.notion.com/v1/oauth/token"
API_BASE       = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion's per-rich-text-run limit. We split paragraph text to stay under it.
_RICH_TEXT_LIMIT = 2000


# ---------------------------------------------------------------------------
# Lazy HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _default_headers(bearer: str | None = None) -> dict:
    h = {
        "Notion-Version": NOTION_VERSION,
        "Accept":         "application/json",
    }
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    return h


def _http_post_json(url: str, payload: dict, *,
                    bearer: str | None = None,
                    basic_auth: tuple[str, str] | None = None,
                    timeout: float = 15.0) -> dict:
    """POST application/json; supports either Bearer or Basic auth."""
    import urllib.request
    import urllib.error

    body = json.dumps(payload).encode("utf-8")
    headers = _default_headers(bearer)
    headers["Content-Type"] = "application/json; charset=utf-8"
    if basic_auth is not None:
        creds = f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(creds).decode("ascii")

    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        log.warning("notion POST %s -> HTTP %s: %s", url, exc.code, raw[:200])
        raise RuntimeError(f"notion POST {url} HTTP {exc.code}: {raw[:200]}") from exc
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        log.warning("notion POST %s: non-JSON response: %s", url, raw[:200])
        raise RuntimeError(f"notion non-JSON response from {url}") from exc


def _http_get(url: str, *, bearer: str, timeout: float = 15.0) -> dict:
    """GET with Bearer + Notion-Version headers."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, method="GET", headers=_default_headers(bearer))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        log.warning("notion GET %s -> HTTP %s: %s", url, exc.code, raw[:200])
        raise RuntimeError(f"notion GET {url} HTTP {exc.code}: {raw[:200]}") from exc
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        log.warning("notion GET %s: non-JSON response: %s", url, raw[:200])
        raise RuntimeError(f"notion non-JSON response from {url}") from exc


# ---------------------------------------------------------------------------
# Plain-text -> Notion paragraph blocks
# ---------------------------------------------------------------------------

def _text_to_paragraph_blocks(text: str) -> list[dict]:
    """
    Turn a plain-text string into a list of Notion paragraph block objects.
    One block per non-empty line. Lines longer than Notion's per-run limit
    are split across multiple rich_text runs in the same block.
    """
    blocks: list[dict] = []
    for line in (text or "").splitlines() or [text or ""]:
        stripped = line.rstrip()
        if not stripped:
            # Render blank lines as an empty paragraph for visual spacing.
            blocks.append({
                "object": "block",
                "type":   "paragraph",
                "paragraph": {"rich_text": []},
            })
            continue

        runs: list[dict] = []
        remaining = stripped
        while remaining:
            chunk = remaining[:_RICH_TEXT_LIMIT]
            remaining = remaining[_RICH_TEXT_LIMIT:]
            runs.append({
                "type": "text",
                "text": {"content": chunk},
            })
        blocks.append({
            "object": "block",
            "type":   "paragraph",
            "paragraph": {"rich_text": runs},
        })
    return blocks


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register
class NotionConnector(Connector):
    id = "notion"
    label = "Notion"
    blurb = ("Search your Notion pages, capture notes from Orbi chat as "
             "new Notion pages.")
    auth_kind = "oauth"
    # Notion uses workspace-level grants, not per-scope.
    scopes: list[str] = []

    # ── Config helpers ─────────────────────────────────────────────────

    def _client(self) -> tuple[str, str]:
        cfg = (self.config or {}).get("notion_oauth") or {}
        cid = (cfg.get("client_id")     or "").strip()
        sec = (cfg.get("client_secret") or "").strip()
        if not cid or not sec:
            raise RuntimeError(
                "notion: missing notion_oauth.client_id / notion_oauth.client_secret "
                "in config — register a public integration at notion.so/my-integrations."
            )
        return cid, sec

    def _bearer(self) -> str:
        rec = self._read_tokens()
        tok = rec.get("access_token") or ""
        if not tok:
            raise RuntimeError("notion: not connected (no access_token)")
        return tok

    # ── OAuth ──────────────────────────────────────────────────────────

    def start_oauth(self, redirect_uri: str) -> str:
        """
        Build the Notion authorization URL. Stashes a CSRF ``state`` value
        into the (pending) token file so the callback can verify it.
        """
        client_id, _ = self._client()
        state = secrets.token_urlsafe(24)

        pending = self._read_tokens()
        pending["_pending_state"]        = state
        pending["_pending_redirect_uri"] = redirect_uri
        pending["_pending_started_at"]   = _now_iso()
        self._write_tokens(pending)

        params = {
            "client_id":     client_id,
            "response_type": "code",
            "owner":         "user",
            "redirect_uri":  redirect_uri,
            "state":         state,
        }
        url = f"{AUTH_URL}?{urlencode(params)}"
        log.info("notion: built auth URL (redirect_uri=%s)", redirect_uri)
        return url

    def complete_oauth(self, code: str, redirect_uri: str) -> dict:
        """
        Exchange auth code for access token using HTTP Basic auth (the
        Notion-specific twist). Saves token + workspace + bot + owner info.
        """
        client_id, client_secret = self._client()

        resp = _http_post_json(
            TOKEN_URL,
            {
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": redirect_uri,
            },
            basic_auth=(client_id, client_secret),
        )

        owner    = resp.get("owner")    or {}
        owner_u  = owner.get("user")    or {}
        record = {
            "access_token":   resp.get("access_token", ""),
            "token_type":     resp.get("token_type", "bearer"),
            "bot_id":         resp.get("bot_id", ""),
            "workspace_id":   resp.get("workspace_id", ""),
            "workspace_name": resp.get("workspace_name", "") or "",
            "workspace_icon": resp.get("workspace_icon", "") or "",
            "owner_type":     owner.get("type", ""),
            "owner_user_id":  owner_u.get("id", ""),
            "owner_user_name": (owner_u.get("name") or
                                ((owner_u.get("person") or {}).get("email") or "")),
            "duplicated_template_id": resp.get("duplicated_template_id", ""),
            "saved_at":       _now_iso(),
            "issued_at":      _now_iso(),
        }
        if not record["access_token"]:
            raise RuntimeError(f"notion oauth/token: no access_token in response: {resp}")

        self._write_tokens(record)
        log.info("notion: connected workspace %r (workspace_id=%s)",
                 record["workspace_name"], record["workspace_id"])

        return {
            "workspace_name": record["workspace_name"],
            "workspace_id":   record["workspace_id"],
            "bot_id":         record["bot_id"],
            "owner_user_id":  record["owner_user_id"],
        }

    # ── API methods ────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> dict:
        """Search the workspace for pages matching ``query``."""
        tok = self._bearer()
        payload = _http_post_json(
            f"{API_BASE}/search",
            {
                "query":     (query or "").strip(),
                "page_size": max(1, min(int(limit), 100)),
                "filter":    {"value": "page", "property": "object"},
            },
            bearer=tok,
        )
        results = []
        for item in payload.get("results", []):
            results.append({
                "id":              item.get("id", ""),
                "object":          item.get("object", ""),
                "url":             item.get("url", ""),
                "title":           _extract_page_title(item),
                "created_time":    item.get("created_time", ""),
                "last_edited_time": item.get("last_edited_time", ""),
                "archived":        bool(item.get("archived")),
            })
        return {"results": results, "count": len(results),
                "has_more": bool(payload.get("has_more"))}

    def list_databases(self, limit: int = 20) -> dict:
        """List databases the integration has access to."""
        tok = self._bearer()
        payload = _http_post_json(
            f"{API_BASE}/search",
            {
                "page_size": max(1, min(int(limit), 100)),
                "filter":    {"value": "database", "property": "object"},
            },
            bearer=tok,
        )
        results = []
        for item in payload.get("results", []):
            results.append({
                "id":              item.get("id", ""),
                "object":          item.get("object", ""),
                "url":             item.get("url", ""),
                "title":           _extract_database_title(item),
                "created_time":    item.get("created_time", ""),
                "last_edited_time": item.get("last_edited_time", ""),
            })
        return {"databases": results, "count": len(results),
                "has_more": bool(payload.get("has_more"))}

    def create_page(self, parent_page_id: str, title: str,
                    content_text: str = "") -> dict:
        """
        Create a new page as a child of ``parent_page_id``. ``content_text``
        is converted to a series of paragraph blocks (one per line).
        """
        if not parent_page_id or not parent_page_id.strip():
            raise ValueError("notion create_page: parent_page_id required")
        if not title or not title.strip():
            raise ValueError("notion create_page: title required")

        tok = self._bearer()
        children = _text_to_paragraph_blocks(content_text or "")

        body = {
            "parent":     {"type": "page_id", "page_id": parent_page_id.strip()},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": title.strip()}}]
                }
            },
            "children":   children,
        }
        resp = _http_post_json(f"{API_BASE}/pages", body, bearer=tok)
        self.update_status(last_sync=_now_iso(), last_error="")
        return {
            "ok":    True,
            "id":    resp.get("id", ""),
            "url":   resp.get("url", ""),
            "title": title.strip(),
            "blocks_added": len(children),
        }

    # ── Status (extends base.status) ───────────────────────────────────

    def status(self) -> dict:
        base = super().status()
        rec  = self._read_tokens()
        base["workspace_name"] = rec.get("workspace_name", "") or ""
        base["workspace_id"]   = rec.get("workspace_id", "") or ""
        base["bot_id"]         = rec.get("bot_id", "") or ""
        base["owner_user_id"]  = rec.get("owner_user_id", "") or ""
        base["account"]        = (rec.get("workspace_name", "")
                                  or rec.get("owner_user_name", "")
                                  or base.get("account", ""))

        # Best-effort live probe: count databases the integration can see.
        # Useful because Notion's per-page sharing model means "connected"
        # alone doesn't tell the owner whether they shared anything yet.
        db_count: int = 0
        if base["connected"]:
            try:
                db = self.list_databases(limit=100)
                db_count = db.get("count", 0)
            except Exception as exc:    # noqa: BLE001
                base["last_error"] = f"db probe: {exc}"
        base["databases_visible"] = db_count
        return base


# ---------------------------------------------------------------------------
# Internal: title extractors
# ---------------------------------------------------------------------------

def _extract_page_title(page: dict) -> str:
    """Pull a printable title out of a Notion page object, best-effort."""
    props = page.get("properties") or {}
    for _name, prop in props.items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            runs = prop.get("title") or []
            return "".join(r.get("plain_text", "") for r in runs).strip() or "(untitled)"
    # Top-level "title" fallback (some payload shapes)
    runs = page.get("title") or []
    if isinstance(runs, list) and runs:
        return "".join(r.get("plain_text", "") for r in runs).strip() or "(untitled)"
    return "(untitled)"


def _extract_database_title(db: dict) -> str:
    """Pull a printable title out of a Notion database object."""
    runs = db.get("title") or []
    if isinstance(runs, list) and runs:
        return "".join(r.get("plain_text", "") for r in runs).strip() or "(untitled)"
    return "(untitled)"


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. config (notion_oauth.client_id /
# notion_oauth.client_secret) comes from owner settings.
#
#   POST /api/owner/connectors/notion/connect
#       body:    {} (settings already saved)
#       calls:   NotionConnector(config, user_dir).start_oauth(redirect_uri)
#       returns: { "auth_url": "https://api.notion.com/v1/oauth/authorize?..." }
#
#   GET  /api/owner/connectors/notion/callback?code=...&state=...
#       calls:   NotionConnector(config, user_dir).complete_oauth(code, redirect_uri)
#       then:    HTTP 302 → /owner#connectors
#
#   POST /api/owner/connectors/notion/disconnect
#       calls:   NotionConnector(config, user_dir).disconnect()
#       returns: { "ok": true }
#
#   GET  /api/owner/connectors/notion/status
#       calls:   NotionConnector(config, user_dir).status()
#       returns: status dict (connected, workspace_name, databases_visible, ...)
#
#   GET  /api/owner/connectors/notion/search?q=...&limit=20
#       calls:   NotionConnector(...).search(q, limit)
#       returns: { "results": [...], "count": N, "has_more": bool }
#
#   POST /api/owner/connectors/notion/create_page
#       body:    { "parent_page_id": "abc...", "title": "...", "content_text": "..." }
#       calls:   NotionConnector(...).create_page(parent_page_id, title, content_text)
#       returns: { "ok": true, "id": "...", "url": "...", "blocks_added": N }
#
#   GET  /api/owner/connectors/notion/databases?limit=20
#       calls:   NotionConnector(...).list_databases(limit)
#       returns: { "databases": [...], "count": N, "has_more": bool }
#
# ---------------------------------------------------------------------------
