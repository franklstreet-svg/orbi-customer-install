"""
connectors.base — generic plumbing every external integration shares.

Each integration (Gmail, Slack, Notion, Stripe, etc.) is a class that
subclasses Connector. It only has to implement the bits that are unique
to that service (OAuth URLs, scopes, API request signing, what "list /
search / status" mean for that service).

Token storage:
    Per-user, under data/users/<username>/connector_tokens/<connector_id>.json
    File mode 0o600. JSON: {access_token, refresh_token?, expires_at?, meta...}.

OAuth flow:
    1. start_oauth() returns the auth URL the user should be redirected to.
    2. complete_oauth(code) exchanges the code for tokens and saves them.
    3. is_connected() checks if tokens exist (does NOT verify they're valid).
    4. disconnect() deletes the token file.

API-key connectors (Yelp, Stripe):
    Subclasses can override `auth_kind = "api_key"` and implement
    save_api_key() / get_api_key() — bypasses the OAuth flow entirely.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.connectors")

TOKENS_DIRNAME = "connector_tokens"
_LOCK = threading.Lock()


class Connector:
    """Abstract base — subclasses set class-level metadata and override methods."""

    # ── Metadata (override in subclasses) ──────────────────────────────
    id: str = ""               # unique key like "gmail", "slack", "stripe"
    label: str = ""            # display name like "Gmail"
    blurb: str = ""            # short user-facing description
    auth_kind: str = "oauth"   # "oauth" | "api_key" | "none"
    requires_owner_setup: list[str] = []   # extra setup steps owner must do
    scopes: list[str] = []     # OAuth scopes (oauth connectors only)

    def __init__(self, config: dict, user_dir: Path):
        self.config = config
        self.user_dir = Path(user_dir)
        self.tokens_dir = self.user_dir / TOKENS_DIRNAME
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.token_path = self.tokens_dir / f"{self.id}.json"

    # ── Token I/O (used by all auth kinds) ─────────────────────────────

    def _read_tokens(self) -> dict:
        if not self.token_path.exists():
            return {}
        try:
            return json.loads(self.token_path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"{self.id}: token read failed: {e}")
            return {}

    def _write_tokens(self, data: dict) -> None:
        with _LOCK:
            tmp = self.token_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.token_path)
            try:
                os.chmod(self.token_path, 0o600)
            except (OSError, NotImplementedError):
                pass

    def is_connected(self) -> bool:
        return self.token_path.exists() and bool(self._read_tokens())

    def disconnect(self) -> None:
        if self.token_path.exists():
            self.token_path.unlink()
            log.info(f"{self.id}: disconnected")

    # ── OAuth (override start_oauth + complete_oauth in subclasses) ────

    def start_oauth(self, redirect_uri: str) -> str:
        """Return the authorization URL the user should be sent to. Override."""
        raise NotImplementedError(f"{self.id}: start_oauth not implemented")

    def complete_oauth(self, code: str, redirect_uri: str) -> dict:
        """Exchange the OAuth code for tokens, save them, return a small status
        dict for the dashboard (typically {email, scopes_granted})."""
        raise NotImplementedError(f"{self.id}: complete_oauth not implemented")

    # ── API key (override save_api_key in subclasses if auth_kind="api_key") ──

    def save_api_key(self, key: str, meta: dict | None = None) -> dict:
        """For API-key connectors — store the key and optional metadata."""
        if not key or not key.strip():
            raise ValueError("api key required")
        record = {
            "api_key":    key.strip(),
            "saved_at":   _now_iso(),
            **(meta or {}),
        }
        self._write_tokens(record)
        return {"saved": True, **{k: v for k, v in record.items() if k != "api_key"}}

    def get_api_key(self) -> str:
        return self._read_tokens().get("api_key", "")

    # ── Status (override to add connector-specific info) ───────────────

    def status(self) -> dict:
        """Generic status reply. Subclasses extend this to add fields like
        last_sync, email, account_name, etc."""
        rec = self._read_tokens()
        return {
            "id":          self.id,
            "label":       self.label,
            "blurb":       self.blurb,
            "auth_kind":   self.auth_kind,
            "requires_owner_setup": list(self.requires_owner_setup),
            "connected":   bool(rec),
            "connected_at": rec.get("saved_at") or rec.get("issued_at") or "",
            "account":     rec.get("account") or rec.get("email") or "",
            "last_sync":   rec.get("last_sync") or "",
            "last_error":  rec.get("last_error") or "",
        }

    def update_status(self, **fields) -> None:
        """Merge fields into the stored token record (e.g. last_sync, last_error)."""
        rec = self._read_tokens()
        if not rec:
            return
        rec.update(fields)
        self._write_tokens(rec)


# ── Registry ────────────────────────────────────────────────────────────


_REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    """Decorator used by each connector module to plug itself in."""
    if not cls.id:
        raise ValueError(f"{cls.__name__}: must set class id")
    _REGISTRY[cls.id] = cls
    return cls


def list_connectors() -> list[type[Connector]]:
    return list(_REGISTRY.values())


def get_connector(connector_id: str) -> type[Connector] | None:
    return _REGISTRY.get(connector_id)


def get_instance(connector_id: str, config: dict, user_dir: Path) -> Connector | None:
    cls = get_connector(connector_id)
    if cls is None:
        return None
    return cls(config, user_dir)


# ── Helpers ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_all():
    """Import every connector module so its @register decorator fires.
    Tolerant of missing modules — a broken or not-yet-built connector
    won't take down the others (the app stays bootable even mid-development)."""
    import importlib
    for mod_name in ("gmail", "gcal_reviews", "stripe_conn",
                     "outlook", "yelp", "slack", "notion"):
        try:
            importlib.import_module(f"connectors.{mod_name}")
        except Exception as e:
            log.warning(f"connector {mod_name!r} unavailable: {e}")
