"""
twilio_central — Frank's-Twilio-account number provisioning.

When a customer pays for a tier that includes a phone receptionist
(Medium / Large / Enterprise), the Stripe webhook calls into this
module to:
  1. Search Twilio for an available US local number
  2. Purchase it (~$1/mo on Frank's account)
  3. Configure its Voice webhook to point at brain.twickell.com so
     incoming calls get proxied to the customer's local Orby
  4. Return the number + Twilio SID for storage in customers table

On cancellation, release_number() frees the number back to Twilio so
Frank stops paying for a dead account.

Designed to NO-OP gracefully when Twilio credentials are not set in
the env — so the rest of the stack works during the bootstrap phase
before Frank funds the Twilio account.

Env vars (set in /etc/orbi-brain/stripe.env):
    TWILIO_ACCOUNT_SID            (starts with AC)
    TWILIO_AUTH_TOKEN
    TWILIO_VOICE_WEBHOOK_BASE     e.g. https://brain.twickell.com
    TWILIO_PREFERRED_COUNTRY      default 'US'
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional

log = logging.getLogger("orbi.twilio_central")

_TW_BASE = "https://api.twilio.com/2010-04-01"


def is_configured() -> bool:
    """True if Frank's Twilio credentials are present in env."""
    return bool(os.environ.get("TWILIO_ACCOUNT_SID") and
                os.environ.get("TWILIO_AUTH_TOKEN"))


def _basic_auth_header() -> dict:
    sid   = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    raw = f"{sid}:{token}".encode("ascii")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _tw_request(method: str, path: str, data: dict | None = None,
                timeout: int = 15) -> dict:
    """Wrap a Twilio REST call. Raises on non-2xx so the caller decides
    how to recover."""
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    url = f"{_TW_BASE}/Accounts/{sid}{path}"
    headers = {**_basic_auth_header(),
               "User-Agent": "Orbi-Brain/0.1"}
    body_bytes = None
    if data is not None:
        body_bytes = urllib.parse.urlencode(data).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body_bytes, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"Twilio {method} {path} HTTP {e.code}: {body[:200]}")


def list_available_numbers(country: str = "US",
                           area_code: str | None = None,
                           limit: int = 10) -> list[dict]:
    """Search Twilio's catalog for purchasable local numbers."""
    if not is_configured():
        return []
    params = {"SmsEnabled": "true", "VoiceEnabled": "true",
              "PageSize": str(min(limit, 30))}
    if area_code:
        params["AreaCode"] = area_code
    path = f"/AvailablePhoneNumbers/{country}/Local.json?{urllib.parse.urlencode(params)}"
    body = _tw_request("GET", path)
    return body.get("available_phone_numbers", []) or []


def provision_for_customer(api_key: str, *,
                           area_code: str | None = None,
                           preferred_country: str | None = None) -> dict:
    """Buy a number + configure its voice webhook. Returns
    {ok, phone_number, sid, error?}. Idempotency: caller should check
    customers.twilio_number first to avoid double-buying.

    Voice webhook is set to:
        <TWILIO_VOICE_WEBHOOK_BASE>/twilio/voice/<api_key>

    The brain server's /twilio/voice/<api_key> route proxies to the
    customer's local Orby (whose URL it learned via heartbeat)."""
    if not is_configured():
        return {"ok": False, "error": "twilio_not_configured",
                "message": "TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN not set in env"}

    country = preferred_country or os.environ.get("TWILIO_PREFERRED_COUNTRY", "US")
    webhook_base = os.environ.get("TWILIO_VOICE_WEBHOOK_BASE",
                                   "https://brain.twickell.com").rstrip("/")
    voice_url = f"{webhook_base}/twilio/voice/{api_key}"

    # 1. Find an available number
    try:
        candidates = list_available_numbers(country=country,
                                            area_code=area_code, limit=10)
    except Exception as e:
        log.warning(f"twilio search failed for {api_key[:14]}: {e}")
        return {"ok": False, "error": "search_failed", "message": str(e)}
    if not candidates:
        # Retry without area_code if we narrowed
        if area_code:
            log.info(f"twilio: no numbers in area code {area_code} — retrying nationwide")
            try:
                candidates = list_available_numbers(country=country, limit=10)
            except Exception as e:
                return {"ok": False, "error": "search_failed", "message": str(e)}
        if not candidates:
            return {"ok": False, "error": "no_numbers_available"}

    picked = candidates[0]
    number = picked.get("phone_number")
    if not number:
        return {"ok": False, "error": "no_phone_number_in_response"}

    # 2. Purchase it + set the voice webhook in one POST
    try:
        body = _tw_request("POST", "/IncomingPhoneNumbers.json", data={
            "PhoneNumber":    number,
            "VoiceUrl":       voice_url,
            "VoiceMethod":    "POST",
            "FriendlyName":   f"Orby customer {api_key[:14]}",
            "SmsUrl":         f"{webhook_base}/twilio/sms/{api_key}",
            "SmsMethod":      "POST",
        })
    except Exception as e:
        log.warning(f"twilio purchase failed for {api_key[:14]} ({number}): {e}")
        return {"ok": False, "error": "purchase_failed", "message": str(e)}

    sid = body.get("sid")
    final_number = body.get("phone_number", number)
    log.info(f"twilio: bought {final_number} ({sid}) for customer {api_key[:14]}")
    return {"ok": True, "phone_number": final_number, "sid": sid}


def release_number(twilio_number_sid: str) -> dict:
    """Free a number back to the Twilio pool. Stops Frank's $1/mo charge."""
    if not is_configured():
        return {"ok": False, "error": "twilio_not_configured"}
    if not twilio_number_sid:
        return {"ok": False, "error": "no_sid_provided"}
    try:
        _tw_request("DELETE", f"/IncomingPhoneNumbers/{twilio_number_sid}.json")
        log.info(f"twilio: released number sid {twilio_number_sid}")
        return {"ok": True}
    except Exception as e:
        log.warning(f"twilio release failed for {twilio_number_sid}: {e}")
        return {"ok": False, "error": "release_failed", "message": str(e)}


def update_voice_webhook(twilio_number_sid: str, new_voice_url: str) -> dict:
    """Reconfigure an existing number's voice webhook. Useful if the
    brain server moves to a new domain."""
    if not is_configured():
        return {"ok": False, "error": "twilio_not_configured"}
    try:
        _tw_request("POST", f"/IncomingPhoneNumbers/{twilio_number_sid}.json",
                    data={"VoiceUrl": new_voice_url, "VoiceMethod": "POST"})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": "update_failed", "message": str(e)}


# Tier → whether the customer gets a phone number provisioned
TIERS_WITH_PHONE = {"medium", "large", "enterprise"}
