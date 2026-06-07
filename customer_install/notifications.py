"""
Owner notifications — three channels:

  1. Web Push (VAPID)  — owner's phone gets a push when their PWA is installed
  2. Email            — via Resend (free tier: 100 emails/day) or any SMTP
  3. SMS              — via Twilio (uses the customer's existing Twilio account)

All three are gated by the per-event toggles in config.notifications.
Failures in one channel never block the others.

Usage:
    import notifications as notify
    notify.send(CONFIG, DATA_DIR,
                event="new_lead",
                title="New lead from John Smith",
                body="John (555-1234): I need a quote for...",
                url="/owner")
"""

from __future__ import annotations

import base64
import json
import logging
import smtplib
import threading
import time
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger("orbi.notifications")

EVENT_TO_FLAG = {
    "new_lead":        "notify_on_new_lead",
    "new_message":     "notify_on_new_message",
    "new_order":       "notify_on_new_lead",
    "new_voicemail":   "notify_on_new_message",
    "new_question":    "notify_on_new_lead",   # learning-loop: treat unanswered Qs like leads
    "failed_billing":  "notify_on_failed_billing",
    "watchdog_rollback": "notify_on_failed_billing",  # operational alerts use same flag
    "watchdog_failed": "notify_on_failed_billing",
}


# Public helpers — same plumbing as the owner-notify send() but addressed
# to an ARBITRARY recipient. The customer-callback dispatcher uses these
# to deliver learned-answer responses back to the original visitor who
# asked the question.
def send_email_to(config: dict, to: str, subject: str, body: str) -> bool:
    """Send an email to anyone (visitor, vendor, etc.). Returns True on
    success. Same backend as owner-email (Resend or SMTP) — just a
    different recipient. Never raises; returns False on failure."""
    if not to or "@" not in to:
        return False
    try:
        return bool(_send_email(config, to, subject, body))
    except Exception as e:
        log.warning(f"send_email_to({to}) failed: {e}")
        return False


def send_sms_to(config: dict, to: str, body: str) -> bool:
    """Send an SMS to anyone (visitor, vendor, etc.). Returns True on
    success. Same Twilio backend as owner-sms. Never raises."""
    if not to or len(to) < 7:
        return False
    try:
        return bool(_send_sms(config, to, body))
    except Exception as e:
        log.warning(f"send_sms_to({to}) failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Subscription storage (web push)
# ---------------------------------------------------------------------------

def _subs_path(data_dir: Path) -> Path:
    return data_dir / "push_subscriptions.json"

def _vapid_path(data_dir: Path) -> Path:
    return data_dir / ".vapid_keys.json"

def load_subscriptions(data_dir: Path) -> list[dict]:
    p = _subs_path(data_dir)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("subscriptions", [])
    except (json.JSONDecodeError, OSError):
        return []

def save_subscriptions(data_dir: Path, subs: list[dict]) -> None:
    p = _subs_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"subscriptions": subs}, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# VAPID key management (auto-generate on first run)
# ---------------------------------------------------------------------------

def get_vapid_keys(data_dir: Path) -> dict | None:
    """Returns {'public_key': ..., 'private_key': ..., 'subject': ...} or None
    if pywebpush isn't installed."""
    try:
        from py_vapid import Vapid
    except ImportError:
        return None
    p = _vapid_path(data_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # Generate new keys
    vapid = Vapid()
    vapid.generate_keys()
    keys = {
        "private_key": vapid.private_pem().decode("utf-8"),
        "public_key": _bytes_to_b64url(vapid.public_key.public_bytes(
            encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.X962,
            format=__import__("cryptography").hazmat.primitives.serialization.PublicFormat.UncompressedPoint,
        )),
        "subject": "mailto:noreply@orbi.local",
        "created": int(time.time()),
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    import os
    try:
        os.chmod(p, 0o600)
    except (OSError, NotImplementedError):
        pass
    return keys

def _bytes_to_b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Web Push delivery
# ---------------------------------------------------------------------------

def _send_web_push(data_dir: Path, payload: dict) -> int:
    """Send to all stored subscriptions. Returns number successfully delivered."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.debug("pywebpush not installed — skipping web push")
        return 0
    keys = get_vapid_keys(data_dir)
    if not keys:
        log.debug("no VAPID keys — skipping web push")
        return 0
    subs = load_subscriptions(data_dir)
    if not subs:
        return 0
    # Parse the PEM private key ONCE into a Vapid02 instance. Newer
    # pywebpush refuses raw PEM strings with a cryptic "Could not
    # deserialize key data" / "ASN.1 invalid length" — it expects either
    # a Vapid02 instance or a file path. Was failing 46 times in the log
    # before this fix.
    try:
        from py_vapid import Vapid
        vapid_instance = Vapid.from_pem(keys["private_key"].encode("utf-8"))
    except Exception as e:
        log.warning(f"VAPID key parse failed — push delivery disabled: {e}")
        return 0
    delivered = 0
    surviving = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload),
                vapid_private_key=vapid_instance,
                vapid_claims={"sub": keys["subject"]},
                timeout=10,
            )
            delivered += 1
            surviving.append(sub)
        except WebPushException as e:
            # 410 = subscription expired (drop it). Anything else (e.g. 404 endpoint gone) drop it.
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                log.info(f"dropping expired push subscription (status={status})")
                continue
            log.warning(f"push failed (status={status}): {e}")
            surviving.append(sub)
        except Exception as e:
            log.warning(f"push send error: {e}")
            surviving.append(sub)
    if len(surviving) != len(subs):
        save_subscriptions(data_dir, surviving)
    return delivered


# ---------------------------------------------------------------------------
# Email delivery (Resend if configured, else SMTP, else skip)
# ---------------------------------------------------------------------------

def _send_email(config: dict, to: str, subject: str, body: str) -> bool:
    notify_cfg = config.get("notifications", {})
    resend_key = notify_cfg.get("resend_api_key")
    if resend_key:
        return _send_resend(resend_key, notify_cfg.get("from_email", "orbi@yourdomain.com"),
                            to, subject, body)
    smtp_cfg = notify_cfg.get("smtp")
    if smtp_cfg:
        return _send_smtp(smtp_cfg, to, subject, body)
    return False

def _send_resend(api_key: str, from_email: str, to: str,
                 subject: str, body: str) -> bool:
    url = "https://api.resend.com/emails"
    data = json.dumps({
        "from": from_email,
        "to": [to],
        "subject": subject,
        "text": body,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        log.warning(f"resend rejected ({e.code}): {e.read()[:200]}")
        return False
    except Exception as e:
        log.warning(f"resend error: {e}")
        return False

def _send_smtp(smtp_cfg: dict, to: str, subject: str, body: str) -> bool:
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = smtp_cfg.get("from", smtp_cfg.get("user", "noreply@orbi.local"))
        msg["To"]      = to
        if smtp_cfg.get("use_ssl"):
            server = smtplib.SMTP_SSL(smtp_cfg["host"], smtp_cfg.get("port", 465), timeout=10)
        else:
            server = smtplib.SMTP(smtp_cfg["host"], smtp_cfg.get("port", 587), timeout=10)
            if smtp_cfg.get("use_starttls", True):
                server.starttls()
        if smtp_cfg.get("user"):
            server.login(smtp_cfg["user"], smtp_cfg["password"])
        server.sendmail(msg["From"], [to], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        log.warning(f"smtp send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# SMS delivery (Twilio)
# ---------------------------------------------------------------------------

def _send_sms(config: dict, to: str, body: str) -> bool:
    phone_cfg = config.get("phone", {})
    sid   = phone_cfg.get("twilio_account_sid")
    token = phone_cfg.get("twilio_auth_token")
    from_ = phone_cfg.get("twilio_number")
    if not (sid and token and from_):
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib_parse_encode({"To": to, "From": from_, "Body": body[:1500]})
    auth_b64 = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data.encode("utf-8"), headers={
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/x-www-form-urlencoded",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 400
    except Exception as e:
        log.warning(f"twilio sms failed: {e}")
        return False

def urllib_parse_encode(d: dict) -> str:
    import urllib.parse
    return urllib.parse.urlencode(d)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

INBOX_FILE = "notifications_inbox.json"
INBOX_MAX  = 200  # keep last 200 — older auto-purged
_INBOX_LOCK = threading.Lock()


def _save_in_app(data_dir: Path, *, event: str, title: str, body: str,
                 url: str = "/owner") -> str:
    """Append a notification to the in-app inbox so the dashboard can
    show a toast next time it polls. This is the SAFETY NET channel —
    always runs so reminders never silently disappear when push/email/
    sms aren't configured. Returns the notification id."""
    import secrets
    nid = secrets.token_urlsafe(8)
    rec = {
        "id":      nid,
        "event":   event,
        "title":   title,
        "body":    body,
        "url":     url,
        "ts":      time.time(),
        "seen":    False,
    }
    path = data_dir / INBOX_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _INBOX_LOCK:
        try:
            inbox = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (json.JSONDecodeError, OSError):
            inbox = []
        inbox.append(rec)
        if len(inbox) > INBOX_MAX:
            inbox = inbox[-INBOX_MAX:]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(inbox, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    return nid


def list_inbox(data_dir: Path, unseen_only: bool = False) -> list[dict]:
    path = data_dir / INBOX_FILE
    if not path.exists():
        return []
    try:
        inbox = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if unseen_only:
        inbox = [n for n in inbox if not n.get("seen")]
    return sorted(inbox, key=lambda n: n.get("ts", 0), reverse=True)


def mark_inbox_seen(data_dir: Path, notification_id: str) -> bool:
    path = data_dir / INBOX_FILE
    if not path.exists():
        return False
    with _INBOX_LOCK:
        try:
            inbox = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        hit = False
        for n in inbox:
            if n.get("id") == notification_id and not n.get("seen"):
                n["seen"] = True
                n["seen_at"] = time.time()
                hit = True
        if hit:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(inbox, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
        return hit


def mark_inbox_acknowledged(data_dir: Path, notification_id: str) -> bool:
    """Mark a notification as acknowledged — distinct from 'seen'. Used
    by reminder events that need an explicit 'got it' from the user, so
    the dashboard can stop re-firing the chime + TTS prompt."""
    path = data_dir / INBOX_FILE
    if not path.exists():
        return False
    with _INBOX_LOCK:
        try:
            inbox = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        hit = False
        for n in inbox:
            if n.get("id") == notification_id and not n.get("acknowledged"):
                n["acknowledged"] = True
                n["seen"] = True
                n["acknowledged_at"] = time.time()
                hit = True
        if hit:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(inbox, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
        return hit


def mark_inbox_all_seen(data_dir: Path) -> int:
    path = data_dir / INBOX_FILE
    if not path.exists():
        return 0
    with _INBOX_LOCK:
        try:
            inbox = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        n_marked = 0
        for n in inbox:
            if not n.get("seen"):
                n["seen"] = True
                n["seen_at"] = time.time()
                n_marked += 1
        if n_marked:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(inbox, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
        return n_marked


def send(config: dict, data_dir: Path, *, event: str,
         title: str, body: str, url: str = "/owner") -> dict:
    """Fire a notification across all enabled channels for this event.
    Non-blocking for external channels (push/email/sms); in-app inbox
    is written synchronously so the dashboard sees it on the next poll."""
    result = {"queued": True, "channels": []}

    notify_cfg = config.get("notifications", {})
    event_flag = EVENT_TO_FLAG.get(event)
    if event_flag and not notify_cfg.get(event_flag, True):
        return {"queued": False, "reason": f"event '{event}' disabled"}

    # 0. In-app inbox — ALWAYS write, sync. This is the safety net so a
    # missing push subscription / unconfigured email / no Twilio never
    # results in a silently-dropped reminder.
    try:
        nid = _save_in_app(data_dir, event=event, title=title, body=body, url=url)
        result["channels"].append(f"in_app({nid})")
    except Exception as e:
        log.warning(f"in_app inbox write failed: {e}")

    def _run():
        channels = []
        # 1. Web push
        if notify_cfg.get("owner_pwa_push", True):
            try:
                n = _send_web_push(data_dir, {
                    "title": title, "body": body, "url": url, "tag": event,
                })
                if n: channels.append(f"push({n})")
            except Exception as e:
                log.warning(f"push channel failed: {e}")
        # 2. Email
        owner_email = (config.get("owner") or {}).get("email")
        if notify_cfg.get("owner_email") and owner_email:
            try:
                if _send_email(config, owner_email, title, body):
                    channels.append("email")
            except Exception as e:
                log.warning(f"email channel failed: {e}")
        # 3. SMS
        owner_phone = (config.get("owner") or {}).get("phone")
        if notify_cfg.get("owner_sms") and owner_phone:
            try:
                if _send_sms(config, owner_phone, f"{title}: {body[:140]}"):
                    channels.append("sms")
            except Exception as e:
                log.warning(f"sms channel failed: {e}")
        if channels:
            log.info(f"notify[{event}] external delivered: {','.join(channels)}")

    threading.Thread(target=_run, daemon=True).start()
    return result
