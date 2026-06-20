"""
session — per-site cookie + auth persistence for the web agent.

When Orbi logs into the customer's dispatch system the first time, we save
that browser session (cookies, localStorage, IndexedDB) to disk so the
next task on the same site picks up already-signed-in. The customer
doesn't get an MFA challenge every single time Orbi opens the page.

Storage path:
    DATA_DIR / "web_agent_sessions" / <site_key>.json

Local-only fit:
    These cookies are CUSTOMER DATA — they grant access to the customer's
    accounts. They MUST live on the customer's machine and never leave.
    We don't centralize them on the brain. The agent loads/saves them
    locally only.

Encryption:
    Cookies are auth secrets. Even though they live on the customer's
    own disk, we encrypt at rest with the install's session secret
    (the same one auth.py uses for session cookies) so a malicious
    process that can read files but not the secret can't replay them.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("orbi.web_agent.session")

_SESSION_DIRNAME = "web_agent_sessions"
_SESSION_FILE_VERSION = 1


def site_key_for(url: str) -> str:
    """Compute the per-site key from a URL. dispatch.acme.com and
    Dispatch.Acme.com:443 normalize to the same key so we don't end up
    with N redundant session files for the same site."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().strip()
    if not host:
        raise ValueError(f"could not extract host from URL: {url!r}")
    # Strip www. since dispatch.example.com and www.dispatch.example.com
    # share the same cookies on modern browsers via the eTLD+1.
    host = re.sub(r"^www\.", "", host)
    # Replace dots with underscores so the filename is filesystem-safe.
    return host.replace(".", "_")


def _session_path(data_dir: Path, site_key: str) -> Path:
    return data_dir / _SESSION_DIRNAME / f"{site_key}.json"


def load(data_dir: Path, site_key: str) -> dict | None:
    """Return the saved Playwright storage_state for this site, or None
    if none exists / file is corrupted / past its grace window."""
    p = _session_path(data_dir, site_key)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("session %s unreadable, will treat as missing: %s",
                    site_key, e)
        return None
    if payload.get("_version") != _SESSION_FILE_VERSION:
        log.info("session %s version mismatch — will re-auth", site_key)
        return None
    return payload.get("storage_state")


def save(data_dir: Path, site_key: str, storage_state: dict) -> Path:
    """Persist the Playwright storage_state for this site. Atomic write
    so a crash mid-write can't leave a corrupted half-file behind."""
    p = _session_path(data_dir, site_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "_version":      _SESSION_FILE_VERSION,
        "_saved_at":     int(time.time()),
        "_site_key":     site_key,
        "storage_state": storage_state,
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(p)
    log.info("session %s saved (%d cookies, %d origins)",
             site_key,
             len(storage_state.get("cookies") or []),
             len(storage_state.get("origins") or []))
    return p


def forget(data_dir: Path, site_key: str) -> bool:
    """Remove the saved session for this site. Called when the customer
    revokes Orbi's access or the site forces a re-login. Returns True
    if a file was removed."""
    p = _session_path(data_dir, site_key)
    if not p.exists():
        return False
    p.unlink()
    log.info("session %s forgotten by request", site_key)
    return True


def list_known_sites(data_dir: Path) -> list[str]:
    """All sites Orbi has a saved session for."""
    d = data_dir / _SESSION_DIRNAME
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json") if p.is_file())
