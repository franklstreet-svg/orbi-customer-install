"""
twilio_provision — programmatic Twilio number provisioning for the
Orbi onboarding wizard.

The owner pastes their own Twilio Account SID + Auth Token (they sign up
at twilio.com, fund the account with $20, and copy the two values from
the console). Orbi then drives the rest:

    1. list_available_numbers(area_code="775")  → 10 nearby numbers
    2. provision_number(picked_number, voice_webhook_url)
         → buys the number AND points its Voice webhook at
           https://<tunnel-hostname>/voice/incoming
    3. save_credentials(...)
         → persists the SID/token/number/number_sid into CONFIG["phone"]
           and atomically rewrites config.json so voice.py picks them up
           on the next request.

Design notes
------------
* The owner provides their OWN Twilio credentials (BYO subaccount) — we
  do NOT route through a Frank-owned shared subaccount. Reasons:
  - usage stays on the owner's billing (no markup/reseller plumbing),
  - the local-data promise (memory: feedback_local_data_promise.md)
    extends naturally: their carrier account, their numbers, their
    minutes,
  - if Frank ever takes a vacation / dies, the owner's phone keeps
    working — there's no shared account to revoke.
  The downside is one extra screen in the wizard ("paste your Twilio
  SID + token"). The dashboard explains the why and links to a
  90-second screen-recording of where in the Twilio console to copy
  them from.

* All raw tokens are REDACTED before logging. The redact_token()
  helper takes "AC9b34…XYZ" → "AC9b…[redacted]…XYZ" so log files /
  crash reports never leak the secret.

* twilio.rest is lazy-imported so the install survives without the
  `twilio` package present until the owner reaches this step in the
  wizard. We attempt a one-shot `pip install` if the module is missing
  and the caller passes auto_install=True (default True).

* Atomic config.json writes use the same .tmp + os.replace() pattern
  as orbi.save_config(), behind a threading.Lock so two concurrent
  wizard tabs can't corrupt the file.

Routes the orchestrator (orbi.py) should wire — do NOT add them here:
    POST /api/owner/twilio/list_available    {area_code?, limit?}
        → list_available_numbers(...)
    POST /api/owner/twilio/provision         {phone_number}
        → provision_number(...) + save_credentials(...)
    POST /api/owner/twilio/update_webhook    {voice_webhook_url}
        → update_webhook(...)
    POST /api/owner/twilio/save_credentials  {account_sid, auth_token,
                                              twilio_number, number_sid}
        → save_credentials(...) only (when owner brings an existing #)
    GET  /api/owner/twilio/status
        → current_phone_config(CONFIG)
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("orbi.twilio_provision")

# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_CONFIG_LOCK = threading.Lock()


def _config_path() -> Path:
    """config.json lives next to orbi.py in the install dir."""
    env = os.environ.get("ORBI_CONFIG")
    if env:
        return Path(env)
    # Default for the standard install layout.
    return Path(__file__).resolve().parent / "config.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)  # config has secrets — owner-readable only
    except OSError as exc:
        log.warning("twilio_provision: chmod 0o600 on %s failed: %s",
                    path, exc)


def redact_token(value: str | None) -> str:
    """Redact a Twilio SID / auth token so it's safe to log.

    "ACabcdef0123456789…XYZ"  →  "ACab…[redacted]…7XYZ"
    Strings shorter than 8 chars are fully redacted.
    """
    if not value:
        return ""
    s = str(value)
    if len(s) < 8:
        return "[redacted]"
    return f"{s[:4]}…[redacted]…{s[-3:]}"


def _ensure_twilio_module(auto_install: bool = True):
    """Lazy-import the twilio package, installing it once if missing."""
    try:
        return importlib.import_module("twilio.rest")
    except ImportError:
        if not auto_install:
            raise
        log.info("twilio_provision: twilio package missing, attempting "
                 "one-shot pip install")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet", "twilio"],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                timeout=180,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError) as exc:
            raise RuntimeError(
                "Could not install the 'twilio' Python package. Run "
                "`pip install twilio` in the Orbi environment and "
                "try again."
            ) from exc
        return importlib.import_module("twilio.rest")


def _twilio_client(account_sid: str, auth_token: str):
    if not account_sid or not auth_token:
        raise ValueError(
            "Twilio account_sid and auth_token are required. Paste them "
            "from https://console.twilio.com under Account Info."
        )
    twilio_rest = _ensure_twilio_module()
    return twilio_rest.Client(account_sid, auth_token)


def _err(msg: str, exc: Exception | None = None) -> dict:
    """Return a friendly error dict (never raw stack traces to the UI)."""
    if exc is not None:
        log.exception("twilio_provision: %s", msg)
    else:
        log.error("twilio_provision: %s", msg)
    return {"ok": False, "error": msg}


# ---------------------------------------------------------------------------
# Public API — number discovery + provisioning
# ---------------------------------------------------------------------------

def list_available_numbers(account_sid: str,
                           auth_token: str,
                           area_code: str = "",
                           country: str = "US",
                           limit: int = 10) -> list[dict]:
    """Return a list of available local numbers from Twilio that the owner
    can buy. Each entry is:

        {
            "phone_number":  "+17755551234",
            "friendly_name": "(775) 555-1234",
            "locality":      "Reno",
            "region":        "NV",
            "capabilities":  {"voice": True, "sms": True, "mms": True}
        }

    If `area_code` is empty Twilio returns nationwide local numbers.
    """
    log.info("twilio_provision: searching %s numbers (area_code=%r, sid=%s)",
             country, area_code, redact_token(account_sid))
    try:
        client = _twilio_client(account_sid, auth_token)
        kwargs: dict[str, Any] = {"limit": max(1, min(int(limit or 10), 30))}
        if area_code:
            # Twilio rejects non-digits silently — strip them ourselves.
            digits = "".join(c for c in str(area_code) if c.isdigit())
            if digits:
                kwargs["area_code"] = int(digits)
        avail = (client.available_phone_numbers(country)
                       .local.list(**kwargs))
    except Exception as exc:
        return [{"_error": _err(
            "Could not list numbers from Twilio. Double-check your "
            "Account SID and Auth Token, then try again.",
            exc,
        )["error"]}]

    result = []
    for n in avail:
        caps = getattr(n, "capabilities", {}) or {}
        result.append({
            "phone_number":  getattr(n, "phone_number", ""),
            "friendly_name": getattr(n, "friendly_name", "")
                             or getattr(n, "phone_number", ""),
            "locality":      getattr(n, "locality", "") or "",
            "region":        getattr(n, "region", "") or "",
            "capabilities":  {
                "voice": bool(caps.get("voice", True)),
                "sms":   bool(caps.get("SMS", caps.get("sms", True))),
                "mms":   bool(caps.get("MMS", caps.get("mms", False))),
            },
        })
    log.info("twilio_provision: found %d candidate numbers", len(result))
    return result


def provision_number(account_sid: str,
                     auth_token: str,
                     phone_number: str,
                     voice_webhook_url: str) -> dict:
    """Purchase `phone_number` on the owner's Twilio account and point
    its Voice webhook at `voice_webhook_url` (typically
    https://<tunnel-hostname>/voice/incoming).

    Returns {ok, sid, phone_number, status, voice_url} on success
    or {ok: False, error: ...} on failure.
    """
    if not phone_number:
        return _err("Pick a phone number first.")
    if not voice_webhook_url or not voice_webhook_url.startswith(("http://", "https://")):
        return _err("voice_webhook_url must be a full https:// URL pointing "
                    "at your Orbi tunnel (e.g. https://orbi.yourdomain.com/voice/incoming).")
    log.info("twilio_provision: buying %s with webhook=%s (sid=%s)",
             phone_number, voice_webhook_url, redact_token(account_sid))
    try:
        client = _twilio_client(account_sid, auth_token)
        bought = client.incoming_phone_numbers.create(
            phone_number=phone_number,
            voice_url=voice_webhook_url,
            voice_method="POST",
            status_callback=voice_webhook_url.rsplit("/", 1)[0] + "/status",
            status_callback_method="POST",
        )
    except Exception as exc:
        return _err(
            f"Could not buy {phone_number}. The number may have been "
            "taken in the last few seconds, or your Twilio balance "
            "may be too low. Try a different number or fund your "
            "Twilio account first.",
            exc,
        )

    out = {
        "ok":           True,
        "sid":          getattr(bought, "sid", ""),
        "phone_number": getattr(bought, "phone_number", phone_number),
        "status":       getattr(bought, "status", "in-use") or "in-use",
        "voice_url":    getattr(bought, "voice_url", voice_webhook_url),
    }
    log.info("twilio_provision: bought %s (sid=%s)",
             out["phone_number"], out["sid"])
    return out


def update_webhook(account_sid: str,
                   auth_token: str,
                   number_sid: str,
                   voice_webhook_url: str) -> dict:
    """Repoint an already-owned Twilio number at a new webhook URL.
    Used when the owner moves their tunnel to a new hostname."""
    if not number_sid:
        return _err("number_sid is required (the PNxxxx… id from "
                    "Twilio for the number to update).")
    if not voice_webhook_url or not voice_webhook_url.startswith(("http://", "https://")):
        return _err("voice_webhook_url must be a full https:// URL.")
    log.info("twilio_provision: updating webhook on %s → %s",
             number_sid, voice_webhook_url)
    try:
        client = _twilio_client(account_sid, auth_token)
        updated = client.incoming_phone_numbers(number_sid).update(
            voice_url=voice_webhook_url,
            voice_method="POST",
            status_callback=voice_webhook_url.rsplit("/", 1)[0] + "/status",
            status_callback_method="POST",
        )
    except Exception as exc:
        return _err(
            "Could not update the webhook on that Twilio number. "
            "Verify the number SID and try again.",
            exc,
        )
    return {
        "ok":           True,
        "sid":          getattr(updated, "sid", number_sid),
        "phone_number": getattr(updated, "phone_number", ""),
        "voice_url":    getattr(updated, "voice_url", voice_webhook_url),
    }


def release_number(account_sid: str,
                   auth_token: str,
                   number_sid: str) -> dict:
    """Release a Twilio number. Twilio refunds the partial-month charge
    when released within the first 30 days, which makes this safe for
    wizard "try-again" loops during testing."""
    if not number_sid:
        return _err("number_sid is required.")
    log.info("twilio_provision: releasing number sid=%s", number_sid)
    try:
        client = _twilio_client(account_sid, auth_token)
        client.incoming_phone_numbers(number_sid).delete()
    except Exception as exc:
        return _err(
            "Could not release that Twilio number. It may already "
            "have been released, or the SID is wrong.",
            exc,
        )
    return {"ok": True, "sid": number_sid, "status": "released"}


# ---------------------------------------------------------------------------
# Public API — config persistence
# ---------------------------------------------------------------------------

def save_credentials(config: dict,
                     account_sid: str,
                     auth_token: str,
                     twilio_number: str,
                     number_sid: str) -> dict:
    """Write the four Twilio fields into CONFIG["phone"] and persist the
    full config to config.json atomically. The orbi.py process picks up
    the new values on its next config-reload (or restart).

    Returns the updated phone block (with the auth_token REDACTED in the
    returned dict — the file on disk still has the real token, of course,
    but we don't echo it back to the UI).
    """
    if not isinstance(config, dict):
        return _err("Internal: config must be a dict.")
    if not account_sid or not auth_token:
        return _err("Both Account SID and Auth Token are required.")
    if not twilio_number:
        return _err("twilio_number is required.")

    with _CONFIG_LOCK:
        phone = config.setdefault("phone", {})
        phone["twilio_account_sid"] = account_sid.strip()
        phone["twilio_auth_token"]  = auth_token.strip()
        phone["twilio_number"]      = twilio_number.strip()
        phone["twilio_number_sid"]  = (number_sid or "").strip()
        # Preserve voice_minutes_included if already set, default 200.
        phone.setdefault("voice_minutes_included", 200)

        # Atomic write.
        path = _config_path()
        try:
            _atomic_write_json(path, config)
        except OSError as exc:
            return _err(
                f"Could not write config.json at {path}. Check file "
                "permissions and try again.",
                exc,
            )

    log.info("twilio_provision: saved credentials for %s (sid=%s)",
             twilio_number, redact_token(account_sid))
    # Return a redacted copy — never bounce the raw token back to the UI.
    return {
        "ok": True,
        "phone": {
            "twilio_number":      phone["twilio_number"],
            "twilio_account_sid": redact_token(phone["twilio_account_sid"]),
            "twilio_auth_token":  redact_token(phone["twilio_auth_token"]),
            "twilio_number_sid":  phone["twilio_number_sid"],
            "voice_minutes_included": phone.get("voice_minutes_included", 200),
        },
    }


def current_phone_config(config: dict) -> dict:
    """Return the current Twilio settings for the dashboard. Tokens are
    REDACTED so the dashboard never displays a usable secret."""
    phone = (config or {}).get("phone") or {}
    return {
        "twilio_number":      phone.get("twilio_number", "") or "",
        "twilio_account_sid": redact_token(phone.get("twilio_account_sid", "")),
        "twilio_auth_token":  redact_token(phone.get("twilio_auth_token", "")),
        "twilio_number_sid":  phone.get("twilio_number_sid", "") or "",
        "voice_minutes_included": phone.get("voice_minutes_included", 200),
        "configured": bool(phone.get("twilio_number")
                           and phone.get("twilio_account_sid")
                           and phone.get("twilio_auth_token")),
    }
