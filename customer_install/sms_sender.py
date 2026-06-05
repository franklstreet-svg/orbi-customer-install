"""
sms_sender.py — Twilio outbound SMS for order receipts + owner alerts.

Uses the same Twilio account already configured for inbound voice
(config.phone.twilio_account_sid + twilio_auth_token).

US SMS cost: ~$0.008 per outbound message. Negligible compared to the
inbound call cost itself.

On TRIAL Twilio accounts: outbound SMS only works to numbers added under
Phone Numbers → Manage → Verified Caller IDs. On paid accounts: works to
any US number.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

log = logging.getLogger("orbi.sms")


def _norm_e164(num: str) -> str:
    """Strip everything but digits + leading +. US default: assume +1
    if no country code."""
    if not num:
        return ""
    s = re.sub(r"[^\d+]", "", num)
    if s.startswith("+"):
        return s
    if len(s) == 10:
        return "+1" + s
    if len(s) == 11 and s.startswith("1"):
        return "+" + s
    return s


def send(config: dict, to_number: str, body: str) -> dict:
    """Send a single SMS. Returns {ok, sid, error}."""
    phone_cfg = config.get("phone") or {}
    sid = phone_cfg.get("twilio_account_sid", "").strip()
    token = phone_cfg.get("twilio_auth_token", "").strip()
    from_num = phone_cfg.get("twilio_number", "").strip()

    if not (sid and token and from_num):
        return {"ok": False, "error": "twilio_not_configured"}

    to = _norm_e164(to_number)
    if not to:
        return {"ok": False, "error": "bad_to_number"}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urlencode({"From": from_num, "To": to, "Body": body}).encode("utf-8")
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        log.info(f"sms sent → {to} sid={payload.get('sid','?')}")
        return {"ok": True, "sid": payload.get("sid", "")}
    except HTTPError as e:
        body_resp = ""
        try:
            body_resp = e.read().decode("utf-8")[:400]
        except Exception:
            pass
        log.warning(f"sms send failed {e.code}: {body_resp}")
        # Trial-account unverified-recipient is a common case — give a
        # clearer error so callers can show a sensible message.
        if "21608" in body_resp or "unverified" in body_resp.lower():
            return {"ok": False, "error": "unverified_recipient",
                    "detail": "Trial Twilio accounts can only SMS verified numbers"}
        return {"ok": False, "error": f"http_{e.code}", "detail": body_resp}
    except Exception as e:
        log.warning(f"sms send exception: {e}")
        return {"ok": False, "error": f"exception: {e}"}
