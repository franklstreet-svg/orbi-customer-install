"""
Owner authentication — simple email + bcrypt password, session cookies.
For Phase 1 there's only one owner per install. Multi-user comes in Phase 2.
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

def issue_session(orbi_dir: Path, owner_email: str) -> str:
    payload = {
        "email": owner_email,
        "issued": int(time.time()),
        "expires": int(time.time()) + SESSION_DURATION_SECONDS,
    }
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

def current_owner(orbi_dir: Path) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return validate_session(orbi_dir, token)

def require_owner(orbi_dir: Path) -> dict:
    owner = current_owner(orbi_dir)
    if not owner:
        abort(401)
    return owner

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
