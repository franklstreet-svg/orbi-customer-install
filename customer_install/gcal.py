"""
gcal.py — Google Calendar two-way sync for Orbi customer installs.

Each customer runs Orbi locally. This module bolts a Google Calendar bridge
onto the existing per-user calendar.json (see modules/calendar.py).

DESIGN NOTES
------------
- Tokens live at <user_dir>/gcal_tokens.json with file mode 0o600.
  We do NOT encrypt the token file. Rationale: the entire Orbi install
  runs on the customer's own machine under their own OS account. If an
  attacker can read 0o600 files in the user's home directory they already
  own the box, and any encryption key would have to live next to the
  ciphertext anyway. We trust the OS file-permission boundary, which is
  consistent with how google-auth's own credential cache behaves on disk.

- Sync state (last_sync watermark, last_error, etc.) lives at
  <user_dir>/gcal_sync_state.json with atomic .tmp + .replace() writes
  guarded by a module-level threading.Lock — same convention as
  modules/calendar.py.

- Conflict policy:
    * Events pulled from Google are tagged _gcal_id + _source="google".
    * Local-only events (no _gcal_id) are considered Orbi-native and
      are NEVER overwritten by a pull. They can be pushed UP to Google
      via push_to_google(), at which point they gain a _gcal_id and
      become two-way-synced.
    * If a pull finds an event whose _gcal_id already exists locally,
      only changed fields are updated.

- OAuth redirect-URI quirk:
  Google's "Desktop app" OAuth client type is the right pick for a
  customer install — it explicitly permits loopback redirects of the
  form  http://127.0.0.1:PORT/api/owner/gcal/callback . That means each
  customer can register one Desktop OAuth client in their own Google
  Cloud project (or use a shared Orbi-provided one) without having to
  whitelist a public HTTPS callback. localhost loopback redirects are
  exempt from Google's normal redirect-URI verification rules.

SCOPES
------
https://www.googleapis.com/auth/calendar  (read + write primary calendar)

ROUTE SURFACE (to be wired into orbi.py by the caller — see bottom of file).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import modules.calendar as cal

log = logging.getLogger("orbi.gcal")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKENS_FILE     = "gcal_tokens.json"
SYNC_STATE_FILE = "gcal_sync_state.json"

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Google's OAuth & calendar endpoints (constants live in the google libs;
# we name them here for clarity in logs).
AUTH_URI  = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

_STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Lazy imports — keep google libs out of import path until actually needed
# so that an install missing them can still boot Orbi for non-gcal features.
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

def _build_service():
    from googleapiclient.discovery import build
    return build


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _tokens_path(user_dir: Path) -> Path:
    return Path(user_dir) / TOKENS_FILE

def _state_path(user_dir: Path) -> Path:
    return Path(user_dir) / SYNC_STATE_FILE


def _client_config(client_id: str, client_secret: str, redirect_uri: str) -> dict:
    """Build the in-memory client config dict google-auth-oauthlib expects."""
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


# ---------------------------------------------------------------------------
# Atomic JSON read / write helpers (mirrors modules/calendar.py style)
# ---------------------------------------------------------------------------

def _read_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("gcal: failed to read %s: %s", p, exc)
        return {}


def _write_json_atomic(p: Path, data: dict, *, mode: int | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    if mode is not None:
        try:
            os.chmod(p, mode)
        except OSError as exc:
            log.warning("gcal: chmod %o on %s failed: %s", mode, p, exc)


def _save_state(user_dir: Path, **updates) -> dict:
    """Merge updates into sync_state.json atomically; returns the new state."""
    with _STATE_LOCK:
        path = _state_path(user_dir)
        state = _read_json(path)
        state.update(updates)
        _write_json_atomic(path, state)
        return state


def _load_state(user_dir: Path) -> dict:
    with _STATE_LOCK:
        return _read_json(_state_path(user_dir))


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def start_auth_flow(client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Build a Google authorization URL. Caller redirects the owner to it."""
    Flow = _google_flow()
    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _state = flow.authorization_url(
        access_type="offline",      # we want a refresh_token
        include_granted_scopes="true",
        prompt="consent",           # force refresh_token issuance on re-auth
    )
    log.info("gcal: built auth URL (redirect_uri=%s)", redirect_uri)
    return auth_url


def complete_auth_flow(client_id: str, client_secret: str, redirect_uri: str,
                        code: str, user_dir: Path) -> dict:
    """
    Exchange the auth code for tokens, persist them at <user_dir>/gcal_tokens.json
    with file mode 0o600. Returns {email, scopes}.
    """
    Flow = _google_flow()
    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials

    # Pull the user's email from the userinfo / id_token if we can.
    email = ""
    try:
        build = _build_service()
        # Use the OAuth2 v2 userinfo endpoint via the discovery service.
        svc = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = svc.userinfo().get().execute()
        email = info.get("email", "") or ""
    except Exception as exc:    # noqa: BLE001 — email is non-fatal metadata
        log.warning("gcal: could not fetch userinfo email: %s", exc)

    payload = {
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        list(creds.scopes or SCOPES),
        "expiry":        creds.expiry.isoformat() if creds.expiry else None,
        "email":         email,
    }
    _write_json_atomic(_tokens_path(user_dir), payload, mode=0o600)
    _save_state(user_dir, email=email, connected_at=_now_iso(), last_error=None)
    log.info("gcal: connected account %s for user_dir=%s", email or "<unknown>", user_dir)
    return {"email": email, "scopes": payload["scopes"]}


def is_connected(user_dir: Path) -> bool:
    return _tokens_path(user_dir).exists()


def disconnect(user_dir: Path) -> None:
    p = _tokens_path(user_dir)
    try:
        if p.exists():
            p.unlink()
            log.info("gcal: disconnected (tokens removed) for user_dir=%s", user_dir)
    except OSError as exc:
        log.warning("gcal: failed to delete tokens file %s: %s", p, exc)
    _save_state(user_dir, disconnected_at=_now_iso(), email=None)


# ---------------------------------------------------------------------------
# Credentials loader (refreshes silently if expired)
# ---------------------------------------------------------------------------

def _load_credentials(user_dir: Path):
    """Load saved tokens, refresh if needed, persist refreshed token back."""
    if not is_connected(user_dir):
        raise RuntimeError("gcal: not connected")

    data = _read_json(_tokens_path(user_dir))
    Credentials = _google_creds()
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", TOKEN_URI),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", SCOPES),
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            Request = _google_request()
            creds.refresh(Request())
            # Persist refreshed access token.
            data["token"]  = creds.token
            data["expiry"] = creds.expiry.isoformat() if creds.expiry else None
            _write_json_atomic(_tokens_path(user_dir), data, mode=0o600)
            log.info("gcal: refreshed access token for user_dir=%s", user_dir)
        else:
            raise RuntimeError("gcal: credentials invalid and not refreshable")

    return creds


def _service(user_dir: Path):
    creds = _load_credentials(user_dir)
    build = _build_service()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Google event <-> Orbi event translation
# ---------------------------------------------------------------------------

def _gevent_times(gevent: dict) -> tuple[str, str, bool]:
    """Return (start, end, all_day) extracted from a Google event payload."""
    start = gevent.get("start", {}) or {}
    end   = gevent.get("end",   {}) or {}
    if "date" in start:
        return start["date"], end.get("date", start["date"]), True
    return start.get("dateTime", ""), end.get("dateTime", start.get("dateTime", "")), False


def _gevent_to_local_fields(gevent: dict) -> dict:
    """Translate a Google event into the kwargs/fields Orbi's calendar uses."""
    start, end, all_day = _gevent_times(gevent)
    attendees = [
        a.get("displayName") or a.get("email", "")
        for a in (gevent.get("attendees") or [])
        if a.get("email") or a.get("displayName")
    ]
    return {
        "title":    gevent.get("summary", "(untitled)"),
        "start":    start,
        "end":      end,
        "all_day":  all_day,
        "notes":    gevent.get("description", "") or "",
        "with":     attendees,
        "location": gevent.get("location", "") or "",
    }


def _local_to_gevent_body(local: dict) -> dict:
    """Translate an Orbi event dict into a Google insert/update body."""
    all_day = bool(local.get("all_day"))
    start_v = local.get("start", "")
    end_v   = local.get("end", "") or start_v
    if all_day:
        start_obj = {"date": start_v[:10]}
        end_obj   = {"date": (end_v[:10] or start_v[:10])}
    else:
        start_obj = {"dateTime": start_v}
        end_obj   = {"dateTime": end_v}
    body = {
        "summary":     local.get("title", ""),
        "description": local.get("notes", "") or "",
        "location":    local.get("location", "") or "",
        "start":       start_obj,
        "end":         end_obj,
    }
    attendees = local.get("with") or []
    if attendees and not all_day:
        body["attendees"] = [
            {"email": a} if "@" in a else {"displayName": a}
            for a in attendees
        ]
    return body


# ---------------------------------------------------------------------------
# Pull (Google -> local)
# ---------------------------------------------------------------------------

def pull_from_google(user_dir: Path, since_iso: str | None = None) -> dict:
    """
    Fetch events from the primary calendar and merge into the local store.

    - Dedupes by `_gcal_id`.
    - Updates only changed fields on existing matches.
    - Never overwrites local events that lack a `_gcal_id`.
    - `since_iso` is an RFC3339 watermark; if omitted, we use last_sync from state.
    """
    pulled = 0
    skipped = 0
    updated = 0
    errors: list[str] = []

    try:
        svc = _service(user_dir)
    except Exception as exc:    # noqa: BLE001
        msg = f"connect/refresh failed: {exc}"
        log.warning("gcal pull: %s", msg)
        _save_state(user_dir, last_error=msg)
        return {"pulled": 0, "skipped_existing": 0, "updated": 0, "errors": [msg]}

    state = _load_state(user_dir)
    watermark = since_iso or state.get("last_sync")

    # Index existing local events by _gcal_id for fast dedupe.
    local_events = cal.list_all(Path(user_dir))
    by_gcal_id   = {e["_gcal_id"]: e for e in local_events if e.get("_gcal_id")}

    page_token = None
    while True:
        try:
            kwargs = {
                "calendarId":   "primary",
                "singleEvents": True,
                "orderBy":      "startTime",
                "maxResults":   250,
                "pageToken":    page_token,
            }
            if watermark:
                kwargs["updatedMin"] = watermark
            resp = svc.events().list(**kwargs).execute()
        except Exception as exc:    # noqa: BLE001
            msg = f"list events failed: {exc}"
            log.warning("gcal pull: %s", msg)
            errors.append(msg)
            break

        for gevent in resp.get("items", []):
            if gevent.get("status") == "cancelled":
                continue
            gid = gevent.get("id")
            if not gid:
                continue
            fields = _gevent_to_local_fields(gevent)

            if gid in by_gcal_id:
                # Update only changed fields on the existing local event.
                existing = by_gcal_id[gid]
                changes  = {k: v for k, v in fields.items() if existing.get(k) != v}
                if changes:
                    cal.update(Path(user_dir), existing["id"], **changes)
                    updated += 1
                else:
                    skipped += 1
                continue

            # New event from Google — add locally and tag it.
            try:
                created = cal.add(
                    Path(user_dir),
                    title=fields["title"],
                    start=fields["start"],
                    end=fields["end"],
                    all_day=fields["all_day"],
                    notes=fields["notes"],
                    with_=fields["with"],
                    location=fields["location"],
                )
                cal.update(
                    Path(user_dir), created["id"],
                    _gcal_id=gid, _source="google",
                    _gcal_etag=gevent.get("etag", ""),
                )
                pulled += 1
            except Exception as exc:    # noqa: BLE001
                errors.append(f"add {gid}: {exc}")

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("gcal pull: +%d new, %d updated, %d skipped, %d errors",
             pulled, updated, skipped, len(errors))
    return {
        "pulled":            pulled,
        "skipped_existing":  skipped,
        "updated":           updated,
        "errors":            errors,
    }


# ---------------------------------------------------------------------------
# Push (local -> Google)
# ---------------------------------------------------------------------------

def push_to_google(user_dir: Path, event_id: str) -> dict:
    """
    Push a local event up to Google. If the event already has a `_gcal_id`,
    PATCH it instead of inserting a duplicate.
    """
    local_events = cal.list_all(Path(user_dir))
    local = next((e for e in local_events if e.get("id") == event_id), None)
    if not local:
        return {"pushed": False, "gcal_id": "", "error": f"event {event_id} not found"}

    try:
        svc = _service(user_dir)
    except Exception as exc:    # noqa: BLE001
        msg = f"connect/refresh failed: {exc}"
        log.warning("gcal push: %s", msg)
        return {"pushed": False, "gcal_id": local.get("_gcal_id", ""), "error": msg}

    body = _local_to_gevent_body(local)

    try:
        if local.get("_gcal_id"):
            resp = svc.events().patch(
                calendarId="primary",
                eventId=local["_gcal_id"],
                body=body,
            ).execute()
        else:
            resp = svc.events().insert(
                calendarId="primary",
                body=body,
            ).execute()
            cal.update(
                Path(user_dir), event_id,
                _gcal_id=resp.get("id", ""),
                _source="orbi",     # originated locally, now also on Google
                _gcal_etag=resp.get("etag", ""),
            )
    except Exception as exc:    # noqa: BLE001
        msg = f"push failed: {exc}"
        log.warning("gcal push %s: %s", event_id, msg)
        return {"pushed": False, "gcal_id": local.get("_gcal_id", ""), "error": msg}

    log.info("gcal push: event %s -> %s", event_id, resp.get("id", "?"))
    return {"pushed": True, "gcal_id": resp.get("id", ""), "error": ""}


# ---------------------------------------------------------------------------
# Combined sync + state
# ---------------------------------------------------------------------------

def sync_all(user_dir: Path) -> dict:
    """
    Pull from Google, then push any local events that don't yet have a
    `_gcal_id`. Writes last_sync / last_error to gcal_sync_state.json.
    """
    started = _now_iso()
    pull_stats = pull_from_google(Path(user_dir))

    push_results = []
    push_errors  = []
    for e in cal.list_all(Path(user_dir)):
        if e.get("_gcal_id"):
            continue
        # Don't auto-push events whose source is explicitly something else.
        if e.get("_source") and e["_source"] != "orbi":
            continue
        res = push_to_google(Path(user_dir), e["id"])
        push_results.append(res)
        if not res["pushed"] and res.get("error"):
            push_errors.append(res["error"])

    pushed_count = sum(1 for r in push_results if r["pushed"])
    combined_errors = list(pull_stats.get("errors", [])) + push_errors
    last_error = combined_errors[-1] if combined_errors else None

    state = _save_state(
        Path(user_dir),
        last_sync=started,
        last_error=last_error,
        last_pull=pull_stats,
        last_push_count=pushed_count,
    )

    return {
        "last_sync_iso":     started,
        "pulled":            pull_stats.get("pulled", 0),
        "updated":           pull_stats.get("updated", 0),
        "skipped_existing":  pull_stats.get("skipped_existing", 0),
        "pushed":            pushed_count,
        "errors":            combined_errors,
        "email":             state.get("email", ""),
    }


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------

def get_status(user_dir: Path) -> dict:
    """Compact dict suitable for /api/owner/gcal/status."""
    user_dir = Path(user_dir)
    state = _load_state(user_dir)
    events = cal.list_all(user_dir)
    return {
        "connected":    is_connected(user_dir),
        "email":        state.get("email", "") or "",
        "last_sync":    state.get("last_sync", "") or "",
        "last_error":   state.get("last_error", "") or "",
        "events_count": len(events),
        "gcal_events":  sum(1 for e in events if e.get("_gcal_id")),
    }


# ---------------------------------------------------------------------------
# Internal utils
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. client_id / client_secret / redirect_uri
# come from owner settings (e.g. config.json or settings UI).
#
#   POST /api/owner/gcal/connect
#       body:   {} (settings already saved)
#       calls:  start_auth_flow(client_id, client_secret, redirect_uri)
#       returns:{ "auth_url": "https://accounts.google.com/o/oauth2/..." }
#
#   GET  /api/owner/gcal/callback?code=...&state=...
#       calls:  complete_auth_flow(client_id, client_secret, redirect_uri,
#                                  code, user_dir)
#       then:   HTTP 302 → /owner#gcal
#
#   POST /api/owner/gcal/disconnect
#       calls:  disconnect(user_dir)
#       returns:{ "ok": true }
#
#   POST /api/owner/gcal/sync_now
#       calls:  sync_all(user_dir)
#       returns:result dict from sync_all()
#
#   GET  /api/owner/gcal/status
#       calls:  get_status(user_dir)
#       returns:result dict from get_status()
#
# ---------------------------------------------------------------------------
