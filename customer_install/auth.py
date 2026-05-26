"""
Authentication — PBKDF2 password hashing + signed-payload session cookies.
Now multi-user: sessions carry username + role; users registry lives in
users.py. Visitors are unauthenticated (no cookie). Owner/staff log in
with username + password; their session cookie names them so per-user
data folders can be addressed correctly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from flask import request, make_response, abort

SESSION_DURATION_SECONDS = 60 * 60 * 24 * 30  # 30 days
COOKIE_NAME = "orbi_session"

# Session secret is generated on first run and stored in /opt/orbi/data/.session_secret
def _session_secret(orbi_dir: Path) -> bytes:
    secret_path = orbi_dir / "data" / ".session_secret"
    if not secret_path.exists():
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_bytes(secrets.token_bytes(32))
        try:
            os.chmod(secret_path, 0o600)  # POSIX only — no-op on Windows
        except (OSError, NotImplementedError):
            pass
    return secret_path.read_bytes()


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256, 200k iterations — bcrypt-equivalent strength)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000$" + salt.hex() + "$" + digest.hex()

def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(expected, actual)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session tokens (signed payload, no server-side storage)
# ---------------------------------------------------------------------------

def issue_session(orbi_dir: Path, username: str,
                  role: str = "staff", owner_email: str | None = None) -> str:
    """Issue a session cookie payload for a user. Embeds username + role so
    middleware can scope per-user data and gate role-restricted routes
    without hitting users.json on every request."""
    payload = {
        "username": (username or "").strip().lower(),
        "role":     role,
        "issued":   int(time.time()),
        "expires":  int(time.time()) + SESSION_DURATION_SECONDS,
    }
    # Backward compat: older single-owner installs may still write `email`
    if owner_email:
        payload["email"] = owner_email
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_session_secret(orbi_dir), raw, hashlib.sha256).hexdigest()
    return sig + "." + raw.hex()

def validate_session(orbi_dir: Path, token: str) -> dict | None:
    try:
        sig, raw_hex = token.split(".", 1)
        raw = bytes.fromhex(raw_hex)
    except (ValueError, AttributeError):
        return None
    expected_sig = hmac.new(_session_secret(orbi_dir), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None
    if payload.get("expires", 0) < int(time.time()):
        return None
    return payload


# ---------------------------------------------------------------------------
# Flask helpers
# ---------------------------------------------------------------------------

def current_session(orbi_dir: Path) -> dict | None:
    """Return the session payload (username/role/issued/expires) or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return validate_session(orbi_dir, token)


def current_user(orbi_dir: Path, data_dir: Path) -> dict | None:
    """Resolve the session cookie to a live user record from users.json.
    Returns None if not logged in, session expired, or user archived.
    Returned dict has username/role/status/display_name (no pw_hash)."""
    sess = current_session(orbi_dir)
    if not sess:
        return None
    username = sess.get("username") or ""
    if not username:
        # Legacy single-owner session that pre-dates multi-user — accept as owner.
        legacy_email = sess.get("email")
        if legacy_email:
            return {"username": "owner", "role": "owner", "status": "active",
                    "display_name": legacy_email}
        return None
    import users as users_mod
    rec = users_mod.get_user(data_dir, username)
    if not rec:
        return None
    return {k: v for k, v in rec.items() if k != "pw_hash"}


def require_user(orbi_dir: Path, data_dir: Path) -> dict:
    """Abort 401 if no live user; else return the user record."""
    user = current_user(orbi_dir, data_dir)
    if not user:
        abort(401)
    return user


def require_role(orbi_dir: Path, data_dir: Path, role: str) -> dict:
    """Abort 401 if not logged in, 403 if logged in but wrong role.
    'owner' role implicitly satisfies any role check (owner can do anything)."""
    user = require_user(orbi_dir, data_dir)
    if user.get("role") == "owner":
        return user
    if user.get("role") != role:
        abort(403)
    return user


# ── Backward-compat shims (kept so existing /owner routes don't break) ──

def current_owner(orbi_dir: Path) -> dict | None:
    """Legacy: returns the session payload if any. Use current_user() for
    new code — it does the users.json lookup and respects archived status."""
    return current_session(orbi_dir)


def require_owner(orbi_dir: Path) -> dict:
    """Legacy: 401 if not logged in. New code should use require_role(orbi_dir, data_dir, 'owner')
    so that staff can't reach owner-only routes."""
    sess = current_session(orbi_dir)
    if not sess:
        abort(401)
    return sess

def set_session_cookie(response, token: str):
    # Secure cookie if request came over HTTPS (always true through the
    # cloudflared tunnel). For local-only http testing the cookie still works.
    from flask import request as _req
    is_https = _req.is_secure or _req.headers.get("X-Forwarded-Proto", "") == "https"
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_DURATION_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=is_https,
        path="/",
    )

def clear_session_cookie(response):
    response.set_cookie(COOKIE_NAME, "", max_age=0, path="/")
