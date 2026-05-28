#!/usr/bin/env python3
"""
Orbi Stripe Webhook Handler
---------------------------
Receives Stripe events on Frank's brain machine. Toggles the customer's
service-active flag and tracks subscription state in a small SQLite DB.

This is the central source of truth for "is this customer paid up?"
The customer's install pings this on startup and once an hour to confirm
they should still be running.

Endpoints:
  POST /webhook            — Stripe sends events here
  GET  /api/active/<key>   — Customer install asks: am I still active?
  POST /api/admin/...      — Frank-only manual overrides (token-protected)

Run as a systemd service alongside the brain LLM.
Listens on localhost; Cloudflared tunnel exposes it.

Dependencies: flask, stripe
  pip install flask stripe
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import stripe
from flask import Flask, abort, jsonify, request

log = logging.getLogger("orbi.billing")

# ---------------------------------------------------------------------------
# Configuration (set via environment variables in the systemd unit)
# ---------------------------------------------------------------------------

STRIPE_API_KEY        = os.environ["STRIPE_API_KEY"]               # sk_live_... or sk_test_...
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]        # whsec_...
ADMIN_TOKEN           = os.environ["ORBI_ADMIN_TOKEN"]             # Frank's secret
DB_PATH               = Path(os.environ.get("ORBI_BILLING_DB", "/opt/orbi-brain/billing.db"))
GRACE_PERIOD_DAYS     = int(os.environ.get("ORBI_GRACE_DAYS", "3"))

# Where the per-customer install-token records live. Used by the
# Stripe-checkout → install bridge (separate from billing.db so it can be
# rsync'd / inspected independently and so we have a single-file fallback).
INSTALLS_PATH         = Path(os.environ.get(
    "ORBI_INSTALLS_PATH", "/opt/orbi-brain/installs.json"
))
DOWNLOAD_BASE_URL     = os.environ.get(
    "ORBI_DOWNLOAD_BASE_URL", "https://downloads.orbi.frank.com"
)

# Map Stripe price IDs → (tier_name, billing_cycle). Tiers are
# small / medium / large / enterprise (set 2026-05-27). billing_cycle is
# 'monthly' or 'annual'. The customer pays whatever Stripe charges; we
# look up the tier from this map when the webhook fires.
#
# Frank creates these Prices in the Stripe dashboard then sets the env
# vars in /etc/orbi-brain/stripe.env. If an env var is missing we still
# boot (placeholder string won't match anything) — the webhook will just
# log "unknown price_id" until the env is corrected.
PRICE_TO_TIER = {
    os.environ.get("STRIPE_PRICE_SMALL_MO",  "price_small_mo_placeholder"):  ("small",      "monthly"),
    os.environ.get("STRIPE_PRICE_SMALL_YR",  "price_small_yr_placeholder"):  ("small",      "annual"),
    os.environ.get("STRIPE_PRICE_MEDIUM_MO", "price_medium_mo_placeholder"): ("medium",     "monthly"),
    os.environ.get("STRIPE_PRICE_MEDIUM_YR", "price_medium_yr_placeholder"): ("medium",     "annual"),
    os.environ.get("STRIPE_PRICE_LARGE_MO",  "price_large_mo_placeholder"):  ("large",      "monthly"),
    os.environ.get("STRIPE_PRICE_LARGE_YR",  "price_large_yr_placeholder"):  ("large",      "annual"),
    os.environ.get("STRIPE_PRICE_ENT_MO",    "price_ent_mo_placeholder"):    ("enterprise", "monthly"),
    os.environ.get("STRIPE_PRICE_ENT_YR",    "price_ent_yr_placeholder"):    ("enterprise", "annual"),
}

# Tier → LLM model name. The brain proxy uses this to decide which
# model to call. Large + Enterprise get the bigger 70B brain.
TIER_TO_MODEL = {
    "small":      "meta-llama/Llama-3.1-8B-Instruct",
    "medium":     "meta-llama/Llama-3.1-8B-Instruct",
    "large":      "meta-llama/Llama-3.3-70B-Instruct",
    "enterprise": "meta-llama/Llama-3.3-70B-Instruct",
}

# Tier → monthly usage caps. The brain proxy enforces these soft caps —
# when a customer goes over, they get a polite "upgrade to continue"
# response instead of being silently cut off.
TIER_CAPS = {
    "small":      {"chats_per_mo":    500, "calls_per_mo":     0, "staff": 1},
    "medium":     {"chats_per_mo":  2_000, "calls_per_mo":   200, "staff": 5},
    "large":      {"chats_per_mo": 10_000, "calls_per_mo": 1_000, "staff": 15},
    "enterprise": {"chats_per_mo": 999_999,"calls_per_mo": 5_000, "staff": 999},
}

stripe.api_key = STRIPE_API_KEY
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS customers (
                api_key            TEXT PRIMARY KEY,
                stripe_customer_id TEXT UNIQUE,
                email              TEXT,
                business_name      TEXT,
                tier               TEXT,
                billing_cycle      TEXT,
                active             INTEGER DEFAULT 0,
                subscription_id    TEXT,
                period_end         INTEGER,
                grace_until        INTEGER,
                last_seen_at       INTEGER,            -- unix ts of last heartbeat
                last_heartbeat     TEXT,               -- last heartbeat payload (JSON)
                is_dark            INTEGER DEFAULT 0,  -- 1 = no heartbeat in 30+ min
                dark_since         INTEGER,            -- unix ts of when they went dark
                created_at         INTEGER,
                updated_at         INTEGER
            );

            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                stripe_event TEXT UNIQUE,
                event_type   TEXT,
                customer_id  TEXT,
                payload      TEXT,
                received_at  INTEGER
            );

            -- Per-customer per-month usage counters. The brain proxy
            -- increments these on every call so we can enforce tier caps
            -- and surface usage in the customer dashboard.
            CREATE TABLE IF NOT EXISTS usage (
                api_key      TEXT NOT NULL,
                period       TEXT NOT NULL,
                chats_count  INTEGER DEFAULT 0,
                calls_count  INTEGER DEFAULT 0,
                tokens_in    INTEGER DEFAULT 0,
                tokens_out   INTEGER DEFAULT 0,
                updated_at   INTEGER,
                PRIMARY KEY (api_key, period)
            );

            CREATE INDEX IF NOT EXISTS idx_customers_stripe
                ON customers(stripe_customer_id);
        """)
        # Backfill columns on existing DBs (no-op if already added).
        for col_def in (
            "ADD COLUMN billing_cycle TEXT",
            "ADD COLUMN last_seen_at INTEGER",
            "ADD COLUMN last_heartbeat TEXT",
            "ADD COLUMN is_dark INTEGER DEFAULT 0",
            "ADD COLUMN dark_since INTEGER",
            "ADD COLUMN public_url TEXT",        # customer's tunnel URL
            "ADD COLUMN twilio_number TEXT",     # e.g. +17755551234
            "ADD COLUMN twilio_number_sid TEXT", # for release/management
        ):
            try:
                conn.execute(f"ALTER TABLE customers {col_def}")
            except sqlite3.OperationalError:
                pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now() -> int:
    return int(time.time())

def generate_api_key() -> str:
    import secrets
    return "orbi_" + secrets.token_urlsafe(24)

def event_already_seen(stripe_event_id: str) -> bool:
    with db() as conn:
        cur = conn.execute(
            "SELECT 1 FROM events WHERE stripe_event = ?", (stripe_event_id,)
        )
        return cur.fetchone() is not None

def record_event(stripe_event: dict) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO events "
            "(stripe_event, event_type, customer_id, payload, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                stripe_event["id"],
                stripe_event["type"],
                (stripe_event.get("data", {}).get("object") or {}).get("customer"),
                json.dumps(stripe_event)[:50000],
                now(),
            ),
        )

def get_customer_by_stripe_id(stripe_customer_id: str) -> dict | None:
    with db() as conn:
        cur = conn.execute(
            "SELECT * FROM customers WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

def upsert_customer(*, stripe_customer_id: str, email: str | None = None,
                    business_name: str | None = None, tier: str | None = None,
                    billing_cycle: str | None = None,
                    active: bool | None = None, subscription_id: str | None = None,
                    period_end: int | None = None, grace_until: int | None = None) -> str:
    existing = get_customer_by_stripe_id(stripe_customer_id)
    with db() as conn:
        if existing:
            updates = []
            params = []
            for field, value in (
                ("email", email), ("business_name", business_name),
                ("tier", tier), ("billing_cycle", billing_cycle),
                ("subscription_id", subscription_id),
                ("period_end", period_end), ("grace_until", grace_until),
            ):
                if value is not None:
                    updates.append(f"{field} = ?")
                    params.append(value)
            if active is not None:
                updates.append("active = ?")
                params.append(1 if active else 0)
            updates.append("updated_at = ?")
            params.append(now())
            params.append(existing["api_key"])
            conn.execute(
                f"UPDATE customers SET {', '.join(updates)} WHERE api_key = ?",
                params,
            )
            return existing["api_key"]
        else:
            api_key = generate_api_key()
            conn.execute(
                "INSERT INTO customers "
                "(api_key, stripe_customer_id, email, business_name, tier, billing_cycle, "
                "active, subscription_id, period_end, grace_until, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    api_key, stripe_customer_id, email, business_name, tier, billing_cycle,
                    1 if active else 0, subscription_id, period_end, grace_until,
                    now(), now(),
                ),
            )
            return api_key


def get_customer_by_api_key(api_key: str) -> dict | None:
    if not api_key:
        return None
    with db() as conn:
        cur = conn.execute(
            "SELECT * FROM customers WHERE api_key = ?", (api_key,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Usage counters (used by the brain proxy)
# ---------------------------------------------------------------------------

def _current_period() -> str:
    """YYYY-MM in UTC. Used as the partition key for monthly usage counters."""
    return datetime.utcnow().strftime("%Y-%m")


def get_usage(api_key: str, period: str | None = None) -> dict:
    """Return the usage row for (api_key, period). Period defaults to current month."""
    period = period or _current_period()
    with db() as conn:
        cur = conn.execute(
            "SELECT chats_count, calls_count, tokens_in, tokens_out "
            "FROM usage WHERE api_key = ? AND period = ?",
            (api_key, period),
        )
        row = cur.fetchone()
        if not row:
            return {"period": period, "chats_count": 0, "calls_count": 0,
                    "tokens_in": 0, "tokens_out": 0}
        d = dict(row)
        d["period"] = period
        return d


def increment_usage(api_key: str, *, chats: int = 0, calls: int = 0,
                    tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Add to this customer's current-period usage counters."""
    period = _current_period()
    with db() as conn:
        conn.execute(
            "INSERT INTO usage (api_key, period, chats_count, calls_count, "
            "tokens_in, tokens_out, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(api_key, period) DO UPDATE SET "
            "chats_count = chats_count + excluded.chats_count, "
            "calls_count = calls_count + excluded.calls_count, "
            "tokens_in   = tokens_in   + excluded.tokens_in, "
            "tokens_out  = tokens_out  + excluded.tokens_out, "
            "updated_at  = excluded.updated_at",
            (api_key, period, chats, calls, tokens_in, tokens_out, now()),
        )

# ---------------------------------------------------------------------------
# Install-token store  (Stripe-checkout → installer bridge)
# ---------------------------------------------------------------------------
#
# The flow:
#   1) checkout.session.completed fires → we mint an install_token + api_key
#   2) Customer receives the token by email (Phase 2) or Frank pastes it
#      into the manual demo install (Phase 1).
#   3) The downloaded installer calls GET /api/verify/<install_token>.
#      We return {customer_id, api_key, tier, owner_email} ONCE, then mark
#      the token as used so it can't be replayed.

import threading as _threading
import secrets as _secrets

_INSTALLS_LOCK = _threading.Lock()


def _read_installs() -> dict:
    """Return the installs.json contents. Safe if file is missing/corrupt."""
    if not INSTALLS_PATH.exists():
        return {}
    try:
        return json.loads(INSTALLS_PATH.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[installs] read failed: {e}")
        return {}


def _write_installs(records: dict) -> None:
    """Atomic write of installs.json. Holds _INSTALLS_LOCK around the swap."""
    INSTALLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INSTALLS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, INSTALLS_PATH)


def generate_install_token() -> str:
    """32 random hex chars, prefixed for human-recognition.
    Total length: 5 + 32 = 37."""
    return "inst_" + _secrets.token_hex(16)


def create_install_record(*, stripe_customer_id: str, email: str,
                          tier: str | None, api_key: str) -> str:
    """Mint an install token, store the record, return the token. Idempotent
    on (stripe_customer_id, tier): re-issues the SAME unused token if the
    customer is already pending. Returns the token string."""
    with _INSTALLS_LOCK:
        records = _read_installs()
        # Reuse an unused token for this customer if one exists
        for tok, rec in records.items():
            if (rec.get("stripe_customer_id") == stripe_customer_id
                    and not rec.get("used_at")):
                # Refresh the api_key + tier in case the subscription changed
                rec["api_key"] = api_key
                rec["tier"] = tier
                rec["email"] = email
                records[tok] = rec
                _write_installs(records)
                return tok
        token = generate_install_token()
        records[token] = {
            "stripe_customer_id": stripe_customer_id,
            "email":              email,
            "tier":               tier,
            "api_key":            api_key,
            "created_at":         now(),
            "used_at":            None,
            "used_ip":            None,
        }
        _write_installs(records)
        return token


def verify_and_consume_install_token(token: str,
                                     remote_ip: str = "") -> dict | None:
    """Return the install record if the token is valid + unused, marking it
    consumed in the same atomic write. Returns None if missing/used/invalid.

    Sanitization is the caller's job — this validates shape too as a
    defense in depth measure.
    """
    if not token or not isinstance(token, str):
        return None
    if len(token) > 80 or not token.replace("_", "").isalnum():
        return None
    with _INSTALLS_LOCK:
        records = _read_installs()
        rec = records.get(token)
        if not rec:
            return None
        if rec.get("used_at"):
            return None
        rec["used_at"] = now()
        rec["used_ip"] = remote_ip[:64]
        records[token] = rec
        _write_installs(records)
        return rec


# ---------------------------------------------------------------------------
# Stripe event handlers
# ---------------------------------------------------------------------------

def handle_checkout_completed(event: dict) -> None:
    """A new customer just paid. Create their record + send the install email."""
    obj = event["data"]["object"]
    stripe_customer_id = obj["customer"]
    subscription_id    = obj.get("subscription")
    email              = obj.get("customer_details", {}).get("email")

    # Resolve which tier + billing cycle they bought
    tier = None
    billing_cycle = None
    if subscription_id:
        sub = stripe.Subscription.retrieve(subscription_id)
        for item in sub["items"]["data"]:
            price_id = item["price"]["id"]
            if price_id in PRICE_TO_TIER:
                tier, billing_cycle = PRICE_TO_TIER[price_id]
                break
        period_end = sub["current_period_end"]
    else:
        period_end = None

    api_key = upsert_customer(
        stripe_customer_id=stripe_customer_id,
        email=email,
        tier=tier,
        billing_cycle=billing_cycle,
        active=True,
        subscription_id=subscription_id,
        period_end=period_end,
        grace_until=None,
    )

    # Auto-provision a Twilio number for tiers that include the phone
    # receptionist. NO-OPs if Twilio creds not in env (e.g., during
    # bootstrap before Frank funds the account).
    twilio_number = None
    try:
        import twilio_central
        if tier in twilio_central.TIERS_WITH_PHONE and twilio_central.is_configured():
            result = twilio_central.provision_for_customer(api_key)
            if result.get("ok"):
                twilio_number = result["phone_number"]
                with db() as conn:
                    conn.execute(
                        "UPDATE customers SET twilio_number = ?, "
                        "twilio_number_sid = ? WHERE api_key = ?",
                        (twilio_number, result["sid"], api_key),
                    )
                print(f"[twilio] {api_key[:14]} got {twilio_number} ({result['sid']})")
            else:
                log.warning(f"twilio provision failed for {api_key[:14]}: {result}")
        elif tier in twilio_central.TIERS_WITH_PHONE:
            print(f"[twilio] skipped — credentials not set in env (tier={tier})")
    except Exception as e:
        log.warning(f"twilio provision crashed for {api_key[:14]}: {e}")

    # Mint an install token so the customer's downloaded installer can
    # claim its api_key without us emailing the raw key in plaintext.
    install_token = create_install_record(
        stripe_customer_id=stripe_customer_id,
        email=email or "",
        tier=tier,
        api_key=api_key,
    )
    print(f"[checkout] new customer {email} on tier {tier}, api_key={api_key[:14]}..., install_token={install_token[:14]}..., phone={twilio_number or '(none)'}")
    _send_install_email(email, install_token, tier, twilio_number=twilio_number)
    # TODO: in Phase 2, swap the print-based stub for Resend / SES.


def _send_install_email(email: str | None, install_token: str,
                        tier: str | None,
                        twilio_number: str | None = None) -> None:
    """Send the install email. Tries SMTP first (Yahoo / iCloud / Gmail —
    whatever Frank has an app password for), then Resend's HTTPS API as a
    fallback. Falls back to log-only if NEITHER is configured.

    Why SMTP first: zero recurring cost (Frank already has Yahoo). Resend
    is the upgrade path once volume passes ~100/day.

    Env vars (set in /etc/orbi-brain/stripe.env):
      ORBI_SMTP_HOST            e.g. smtp.mail.yahoo.com
      ORBI_SMTP_PORT            e.g. 587
      ORBI_SMTP_USER            e.g. franklstreet@yahoo.com
      ORBI_SMTP_PASSWORD        Yahoo App Password (16 chars, no spaces)
      ORBI_FROM_EMAIL           e.g. 'Orby <franklstreet@yahoo.com>'
      RESEND_API_KEY            (optional fallback)
    """
    if not email:
        print(f"[email] no address for token {install_token[:14]}... — skipping send")
        return

    download_url = f"{DOWNLOAD_BASE_URL.rstrip('/')}/download/{tier or 'standard'}"
    subject = "Your Orby is ready — install token inside"

    phone_line_text = ""
    phone_line_html = ""
    if twilio_number:
        phone_line_text = (
            f"\nYour business phone number: {twilio_number}\n"
            f"This number is yours — give it to customers, put it on your "
            f"site, your business cards, whatever. Orby will answer it 24/7 "
            f"once the install finishes.\n"
        )
        phone_line_html = (
            f'<p style="background:#0b0f1a;color:#4ade80;padding:14px;border-radius:8px;'
            f'font-size:15px;text-align:center;margin:18px 0;">'
            f'<strong>Your business phone number:</strong><br>'
            f'<span style="font-size:22px;font-weight:700;color:#fff;">{twilio_number}</span><br>'
            f'<span style="font-size:13px;color:#8aa3c8;">Orby answers 24/7 once your install finishes.</span>'
            f'</p>'
        )

    text = (
        f"Welcome to Orby!\n\n"
        f"Two things to do:\n\n"
        f"1) Download the installer for your operating system:\n"
        f"   {download_url}\n\n"
        f"2) When the installer asks for your install token, paste:\n\n"
        f"   {install_token}\n\n"
        f"{phone_line_text}"
        f"The token is single-use. Don't share it — anyone with this token "
        f"could install Orby as you.\n\n"
        f"Need help? Reply to this email.\n\n"
        f"— Frank @ My Orby AI Solutions"
    )
    html = (
        f'<div style="font-family:system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.6;color:#1a2236">'
        f'<h2 style="color:#4f8cff;margin-bottom:6px">Welcome to Orby 🎉</h2>'
        f'<p>Two quick steps to get you live:</p>'
        f'<p><strong>1.</strong> Download the installer for your operating system:<br>'
        f'<a href="{download_url}" style="display:inline-block;margin-top:6px;background:linear-gradient(135deg,#4f8cff,#8b5cf6);color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600">Download Orby installer</a></p>'
        f'<p><strong>2.</strong> When the installer asks for your install token, paste this:</p>'
        f'<pre style="background:#0b0f1a;color:#eaf0ff;padding:14px;border-radius:8px;font-size:14px;'
        f'word-break:break-all">{install_token}</pre>'
        f'{phone_line_html}'
        f'<p style="color:#666;font-size:13px">The token is single-use — anyone with it could install '
        f'Orby as you, so keep it private.</p>'
        f'<p style="color:#666;font-size:13px;margin-top:20px">Need help? Just reply to this email.</p>'
        f'<p style="color:#888;font-size:12px;margin-top:24px">— Frank @ My Orby AI Solutions</p>'
        f'</div>'
    )

    from_addr = os.environ.get("ORBI_FROM_EMAIL",
                                "Orby <orbiaisolutions@gmail.com>")

    # 1) Try SMTP first (Yahoo / iCloud / Gmail — Frank's free path)
    smtp_host = os.environ.get("ORBI_SMTP_HOST", "").strip()
    smtp_user = os.environ.get("ORBI_SMTP_USER", "").strip()
    smtp_pass = os.environ.get("ORBI_SMTP_PASSWORD", "").strip()
    if smtp_host and smtp_user and smtp_pass:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = from_addr
            msg["To"]      = email
            msg.attach(MIMEText(text, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
            smtp_port = int(os.environ.get("ORBI_SMTP_PORT", "587"))
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                s.login(smtp_user, smtp_pass)
                s.send_message(msg)
            print(f"[email] SMTP OK for {email} via {smtp_host}")
            return
        except Exception as e:
            print(f"[email] SMTP failed via {smtp_host} for {email}: {e}")
            print("[email] trying Resend fallback…")

    # 2) Try Resend
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if api_key:
        payload = {
            "from":    from_addr,
            "to":      [email],
            "subject": subject,
            "text":    text,
            "html":    html,
            "tags":    [{"name": "kind", "value": "install_token"},
                        {"name": "tier", "value": tier or "standard"}],
        }
        try:
            import json as _json
            import urllib.request
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=_json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                    "User-Agent":    "Orby-Billing/0.1",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                print(f"[email] Resend OK for {email}: {body[:200]}")
            return
        except Exception as e:
            print(f"[email] Resend FAILED for {email}: {e}")

    # 3) Log-only fallback — token is never silently lost
    print(f"[email] NO email channel configured (no SMTP, no Resend).")
    print(f"[email] Would have sent to {email}:")
    print(text)

def handle_subscription_updated(event: dict) -> None:
    """Subscription renewed, downgraded, upgraded, or canceled."""
    sub = event["data"]["object"]
    stripe_customer_id = sub["customer"]
    status             = sub["status"]
    period_end         = sub["current_period_end"]

    new_tier = None
    new_cycle = None
    for item in sub["items"]["data"]:
        price_id = item["price"]["id"]
        if price_id in PRICE_TO_TIER:
            new_tier, new_cycle = PRICE_TO_TIER[price_id]
            break

    active = status in ("active", "trialing")
    upsert_customer(
        stripe_customer_id=stripe_customer_id,
        tier=new_tier,
        billing_cycle=new_cycle,
        active=active,
        subscription_id=sub["id"],
        period_end=period_end,
        grace_until=None if active else now() + GRACE_PERIOD_DAYS * 86400,
    )
    print(f"[subscription] {stripe_customer_id} status={status} active={active}")

def handle_invoice_payment_failed(event: dict) -> None:
    """Payment failed. Start the grace period."""
    invoice = event["data"]["object"]
    stripe_customer_id = invoice["customer"]
    grace_until = now() + GRACE_PERIOD_DAYS * 86400
    upsert_customer(
        stripe_customer_id=stripe_customer_id,
        grace_until=grace_until,
    )
    print(f"[payment_failed] {stripe_customer_id} grace until "
          f"{datetime.fromtimestamp(grace_until).isoformat()}")

def handle_subscription_deleted(event: dict) -> None:
    """Subscription canceled. Deactivate + release Twilio number."""
    sub = event["data"]["object"]
    stripe_customer_id = sub["customer"]
    # Look up the customer to grab the Twilio SID BEFORE we deactivate
    cust = get_customer_by_stripe_id(stripe_customer_id)
    twilio_sid = cust.get("twilio_number_sid") if cust else None
    upsert_customer(stripe_customer_id=stripe_customer_id, active=False)
    print(f"[canceled] {stripe_customer_id}")

    # Release the Twilio number so Frank stops paying $1/mo for it.
    if twilio_sid:
        try:
            import twilio_central
            result = twilio_central.release_number(twilio_sid)
            if result.get("ok"):
                with db() as conn:
                    conn.execute(
                        "UPDATE customers SET twilio_number = NULL, "
                        "twilio_number_sid = NULL WHERE stripe_customer_id = ?",
                        (stripe_customer_id,),
                    )
                print(f"[twilio] released {cust.get('twilio_number')} ({twilio_sid})")
            else:
                log.warning(f"twilio release failed for {twilio_sid}: {result}")
        except Exception as e:
            log.warning(f"twilio release crashed for {twilio_sid}: {e}")

EVENT_HANDLERS = {
    "checkout.session.completed":   handle_checkout_completed,
    "customer.subscription.created": handle_subscription_updated,
    "customer.subscription.updated": handle_subscription_updated,
    "customer.subscription.deleted": handle_subscription_deleted,
    "invoice.payment_failed":        handle_invoice_payment_failed,
}

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    payload   = request.data
    sig       = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        abort(400, "Invalid signature")

    if event_already_seen(event["id"]):
        return jsonify({"status": "duplicate"}), 200

    record_event(event)
    handler = EVENT_HANDLERS.get(event["type"])
    if handler:
        try:
            handler(event)
        except Exception as e:
            print(f"[error] handling {event['type']}: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        print(f"[ignored] {event['type']}")
    return jsonify({"status": "ok"}), 200

@app.route("/api/active/<api_key>", methods=["GET"])
def is_active(api_key: str):
    """Customer install pings this on startup + hourly to check status."""
    with db() as conn:
        cur = conn.execute(
            "SELECT active, tier, period_end, grace_until, business_name "
            "FROM customers WHERE api_key = ?",
            (api_key,),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"active": False, "reason": "unknown_key"}), 404

    active = bool(row["active"])
    grace_until = row["grace_until"]
    if not active and grace_until and now() < grace_until:
        # In grace period — allow but warn
        return jsonify({
            "active": True,
            "warning": "billing_issue",
            "grace_until": grace_until,
            "tier": row["tier"],
        })
    return jsonify({
        "active": active,
        "tier": row["tier"],
        "period_end": row["period_end"],
        "business_name": row["business_name"],
    })

@app.route("/api/admin/customers", methods=["GET"])
def admin_list():
    """Frank-only: list all customers."""
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        abort(403)
    with db() as conn:
        cur = conn.execute(
            "SELECT api_key, email, business_name, tier, active, "
            "period_end, grace_until, created_at FROM customers "
            "ORDER BY created_at DESC"
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"customers": rows, "count": len(rows)})

@app.route("/api/admin/activate/<api_key>", methods=["POST"])
def admin_activate(api_key: str):
    """Frank-only: force-activate (e.g. for the manual install during demo)."""
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        abort(403)
    with db() as conn:
        conn.execute(
            "UPDATE customers SET active = 1, grace_until = NULL, updated_at = ? "
            "WHERE api_key = ?", (now(), api_key),
        )
    return jsonify({"status": "activated", "api_key": api_key})

@app.route("/api/admin/deactivate/<api_key>", methods=["POST"])
def admin_deactivate(api_key: str):
    """Frank-only: force-deactivate."""
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        abort(403)
    with db() as conn:
        conn.execute(
            "UPDATE customers SET active = 0, updated_at = ? WHERE api_key = ?",
            (now(), api_key),
        )
    return jsonify({"status": "deactivated", "api_key": api_key})

@app.route("/api/verify/<token>", methods=["GET"])
def api_verify(token: str):
    """Public endpoint — the customer's installer calls this with the token
    they got in their post-checkout email.

    On success: returns {customer_id, api_key, tier, owner_email} and marks
    the token used (one-shot).  On failure: 404 with reason.

    Defense in depth — the token store has its own shape check on top of
    this route's validation."""
    # Strip whitespace, reject anything obviously off-shape
    token = (token or "").strip()
    if not token or len(token) > 80:
        return jsonify({"error": "invalid_token"}), 404
    if not token.replace("_", "").isalnum():
        return jsonify({"error": "invalid_token"}), 404

    # Rate-limit-friendly client IP (works behind cloudflared/X-Forwarded-For)
    remote_ip = (request.headers.get("X-Forwarded-For", "")
                 .split(",")[0].strip()
                 or request.remote_addr or "")

    rec = verify_and_consume_install_token(token, remote_ip=remote_ip)
    if not rec:
        return jsonify({"error": "token_not_found_or_used"}), 404

    return jsonify({
        "customer_id": rec["stripe_customer_id"],
        "api_key":     rec["api_key"],
        "tier":        rec.get("tier") or "standard",
        "owner_email": rec.get("email") or "",
    })


# ---------------------------------------------------------------------------
# Brain Proxy
# ---------------------------------------------------------------------------
#
# The customer's local Orbi never talks to HuggingFace directly. It POSTs to
# us with its api_key as a Bearer token, we look the key up, check the
# subscription is active, check the monthly cap, then forward the request
# to HF using OUR HF token.
#
# Result: a refunded/cancelled customer keeps the software but loses the
# brain — no way to keep using Orbi on Frank's HF budget after they stop
# paying.
#
# The proxy speaks the OpenAI chat-completions shape on purpose:
#   - customer_install/llm_client.py already calls POST /v1/chat/completions
#     with Bearer auth, so it works without any client changes (just point
#     config.brain.url at billing.orbi.frank.com)
#   - any future OpenAI-compatible tool will work too
#
# Endpoints:
#   POST /v1/chat/completions           — OpenAI-shape chat (the workhorse)
#   POST /api/brain/tts                 — text-to-speech proxy (placeholder)
#   GET  /api/brain/usage/<api_key>     — current-month usage + caps

HF_TOKEN          = os.environ.get("HF_TOKEN", "").strip()
HF_API_BASE       = os.environ.get("HF_API_BASE",
                                   "https://api-inference.huggingface.co/models")
BRAIN_TIMEOUT_S   = int(os.environ.get("BRAIN_TIMEOUT_S", "60"))


def _extract_bearer(req) -> str:
    """Pull the api_key out of Authorization: Bearer / X-Orbi-Key / body."""
    auth = (req.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    hdr = (req.headers.get("X-Orbi-Key") or "").strip()
    if hdr:
        return hdr
    try:
        body = req.get_json(silent=True) or {}
        return (body.get("api_key") or "").strip()
    except Exception:
        return ""


def _hf_chat(model: str, messages: list, max_new_tokens: int = 512,
             temperature: float = 0.7) -> dict:
    """Call HF Inference and return the raw OpenAI-shaped JSON body."""
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN not configured on brain server")

    import urllib.request
    url = f"{HF_API_BASE.rstrip('/')}/{model}/v1/chat/completions"
    payload = {
        "model":       model,
        "messages":    messages,
        "max_tokens":  max_new_tokens,
        "temperature": temperature,
        "stream":      False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {HF_TOKEN}",
            "Content-Type":  "application/json",
            "User-Agent":    "Orbi-Brain-Proxy/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=BRAIN_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


@app.route("/v1/chat/completions", methods=["POST"])
def brain_chat():
    """OpenAI-compatible chat completions, gated by customer api_key.

    Request:   { "messages": [...], "model"?: ..., "max_tokens"?: ..., "temperature"?: ... }
    Auth:      Authorization: Bearer <api_key>  (or X-Orbi-Key header)
    Routing:   TIER_TO_MODEL picks the model from the customer's tier; the
               client's `model` field is treated as a hint only — we ignore
               it if the customer's tier doesn't entitle them to it.
    Cap:       TIER_CAPS[tier]["chats_per_mo"]. Returns 429 when exceeded.
    Errors:    401 invalid key, 402 subscription inactive, 429 cap, 502 upstream.
    Response:  Full OpenAI chat-completions JSON pass-through (model overridden
               to what we actually called) so llm_client.py + any other
               OpenAI-compatible tool just works.
    """
    api_key = _extract_bearer(request)
    cust = get_customer_by_api_key(api_key)
    if not cust:
        return jsonify({"error": {"message": "invalid_api_key", "type": "auth_error"}}), 401
    if not cust.get("active"):
        return jsonify({"error": {
            "message": "Your Orbi subscription isn't active. Visit your billing portal to resume service.",
            "type":    "subscription_inactive",
            "tier":    cust.get("tier"),
        }}), 402

    tier = cust.get("tier") or "small"
    used = get_usage(api_key)
    cap  = TIER_CAPS.get(tier, TIER_CAPS["small"])
    if used["chats_count"] >= cap["chats_per_mo"]:
        return jsonify({"error": {
            "message": (f"You've used all {cap['chats_per_mo']} chats included in your "
                        f"{tier.title()} tier this month. Upgrade to continue."),
            "type":    "monthly_cap_exceeded",
            "tier":    tier,
            "cap":     cap["chats_per_mo"],
            "used":    used["chats_count"],
        }}), 429

    body = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": {"message": "messages required", "type": "bad_request"}}), 400

    # Customer's `model` is a HINT — actual model is decided by tier.
    model = TIER_TO_MODEL.get(tier, TIER_TO_MODEL["small"])

    try:
        upstream = _hf_chat(
            model,
            messages,
            max_new_tokens=int(body.get("max_tokens", 512)),
            temperature=float(body.get("temperature", 0.7)),
        )
    except Exception as e:
        log.warning("brain upstream failed for %s: %s", api_key[:14], e)
        return jsonify({"error": {
            "message": "Brain is temporarily unavailable — your Orbi will fall back to its other tiers.",
            "type":    "upstream_failure",
            "detail":  str(e)[:200],
        }}), 502

    usage = upstream.get("usage") or {}
    tin   = int(usage.get("prompt_tokens", 0))
    tout  = int(usage.get("completion_tokens", 0))
    increment_usage(api_key, chats=1, tokens_in=tin, tokens_out=tout)

    upstream["model"] = model
    upstream.setdefault("x_orbi", {})
    upstream["x_orbi"].update({"tier": tier, "remaining_chats": max(0, cap["chats_per_mo"] - used["chats_count"] - 1)})
    return jsonify(upstream)


@app.route("/api/brain/tts", methods=["POST"])
def brain_tts():
    """Proxy text-to-speech. Placeholder for now — edge-tts runs locally on
    the customer's box already, so this endpoint mostly exists to let us
    flip TTS to a hosted provider later (ElevenLabs / OpenAI / Cartesia)
    without touching customer installs.

    Returns 501 today; the customer Orbi falls back to local edge-tts.
    """
    body = request.get_json(force=True, silent=True) or {}
    api_key = (body.get("api_key")
               or request.headers.get("X-Orbi-Key", "")).strip()
    cust = get_customer_by_api_key(api_key)
    if not cust:
        return jsonify({"error": "invalid_api_key"}), 401
    if not cust.get("active"):
        return jsonify({"error": "subscription_inactive"}), 402

    return jsonify({
        "error":   "not_implemented",
        "message": "Hosted TTS isn't wired yet — your local Orbi will use edge-tts directly.",
    }), 501


@app.route("/api/brain/usage/<api_key>", methods=["GET"])
def brain_usage(api_key: str):
    """Return current-month usage and the customer's caps."""
    cust = get_customer_by_api_key(api_key)
    if not cust:
        return jsonify({"error": "invalid_api_key"}), 401
    tier = cust.get("tier") or "small"
    cap  = TIER_CAPS.get(tier, TIER_CAPS["small"])
    used = get_usage(api_key)
    return jsonify({
        "tier":          tier,
        "billing_cycle": cust.get("billing_cycle"),
        "active":        bool(cust.get("active")),
        "period":        used["period"],
        "used": {
            "chats":      used["chats_count"],
            "calls":      used["calls_count"],
            "tokens_in":  used["tokens_in"],
            "tokens_out": used["tokens_out"],
        },
        "cap":  cap,
    })


# ---------------------------------------------------------------------------
# Fleet health — every customer Orby phones home every ~5 min so Frank
# knows who's up and who's gone dark. Layered on top of each customer's
# local watchdog (which restarts + rolls back THEIR Orby). Fleet health
# catches the failures the local watchdog can't (their PC is off, their
# internet is down, the watchdog itself crashed).
# ---------------------------------------------------------------------------

DARK_THRESHOLD_SEC = int(os.environ.get("ORBI_DARK_THRESHOLD_SEC", "1800"))  # 30 min
FLEET_CHECK_INTERVAL_SEC = int(os.environ.get("ORBI_FLEET_CHECK_SEC", "300"))  # 5 min


@app.route("/api/heartbeat/<api_key>", methods=["POST"])
def customer_heartbeat(api_key: str):
    """Customer Orby pings here every ~5 min so the central server knows
    they're alive. Payload is whatever the customer wants to report —
    uptime, version, OS, recent activity. Stored verbatim for the fleet
    dashboard. Returns commands the central server wants the customer
    to act on (currently always empty; reserved for remote-wake / update
    nudges)."""
    cust = get_customer_by_api_key(api_key)
    if not cust:
        return jsonify({"error": "invalid_api_key"}), 401
    body = request.get_json(silent=True) or {}
    payload_blob = json.dumps(body)[:4000]
    ts = now()
    was_dark = bool(cust.get("is_dark"))
    # Customer may have reported their current public tunnel URL — store
    # it so the Twilio voice-webhook proxy knows where to forward calls.
    new_public_url = (body.get("public_url") or "").strip() or None
    with db() as conn:
        if new_public_url and new_public_url != cust.get("public_url"):
            conn.execute(
                "UPDATE customers SET last_seen_at = ?, last_heartbeat = ?, "
                "is_dark = 0, dark_since = NULL, public_url = ?, "
                "updated_at = ? WHERE api_key = ?",
                (ts, payload_blob, new_public_url, ts, api_key),
            )
        else:
            conn.execute(
                "UPDATE customers SET last_seen_at = ?, last_heartbeat = ?, "
                "is_dark = 0, dark_since = NULL, updated_at = ? "
                "WHERE api_key = ?",
                (ts, payload_blob, ts, api_key),
            )
    # If they were dark and just came back, log + tell Frank via inbox
    if was_dark:
        dark_for = ts - (cust.get("dark_since") or ts)
        title = f"✅ {cust.get('business_name') or cust.get('email') or api_key[:14]} is back"
        body_text = (f"Customer was offline for {_fmt_duration(dark_for)}. "
                     f"They're checking in again now.")
        try:
            notifications_inbox_add(event="fleet_recovered", title=title, body=body_text)
        except Exception as e:
            log.warning(f"could not write fleet_recovered notification: {e}")
    return jsonify({"ok": True, "now": ts, "commands": []})


def _fmt_duration(seconds: int) -> str:
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60} min"
    if seconds < 86400: return f"{seconds // 3600} hr"
    return f"{seconds // 86400} days"


def notifications_inbox_add(*, event: str, title: str, body: str) -> None:
    """Append a fleet-health alert to the brain server's local inbox file.
    Frank's dashboard can poll this. Path is configurable so the brain
    server's data dir stays separate from any customer Orby's data dir."""
    inbox_path = Path(os.environ.get(
        "ORBI_BRAIN_INBOX", "/opt/orbi-brain/fleet_inbox.json"))
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    import secrets as _secrets
    rec = {
        "id":    _secrets.token_urlsafe(8),
        "event": event,
        "title": title,
        "body":  body,
        "ts":    now(),
        "seen":  False,
    }
    try:
        existing = json.loads(inbox_path.read_text(encoding="utf-8")) if inbox_path.exists() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    existing.append(rec)
    if len(existing) > 500:
        existing = existing[-500:]
    tmp = inbox_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(inbox_path)


def _fleet_health_loop():
    """Background worker: every FLEET_CHECK_INTERVAL_SEC, find every
    customer whose last_seen is older than DARK_THRESHOLD_SEC and flag
    them as dark. Writes an alert to the brain-server inbox the FIRST
    time we notice — does NOT re-alert until they come back (heartbeat
    handler clears is_dark + sends a 'they came back' note)."""
    time.sleep(60)  # let the server come up first
    while True:
        try:
            _scan_for_dark_customers()
        except Exception as e:
            log.warning(f"fleet health loop error: {e}")
        time.sleep(FLEET_CHECK_INTERVAL_SEC)


def _scan_for_dark_customers() -> None:
    cutoff = now() - DARK_THRESHOLD_SEC
    with db() as conn:
        # Find customers who are active, have phoned home before, haven't
        # done so recently, and aren't already flagged as dark.
        cur = conn.execute(
            "SELECT api_key, business_name, email, tier, last_seen_at "
            "FROM customers "
            "WHERE active = 1 "
            "AND last_seen_at IS NOT NULL "
            "AND last_seen_at < ? "
            "AND (is_dark = 0 OR is_dark IS NULL)",
            (cutoff,),
        )
        going_dark = [dict(r) for r in cur.fetchall()]
    for cust in going_dark:
        ts = now()
        dark_for = ts - cust["last_seen_at"]
        title = f"⚠ {cust.get('business_name') or cust.get('email') or cust['api_key'][:14]} went dark"
        body = (f"Customer hasn't checked in for {_fmt_duration(dark_for)}. "
                f"Tier: {cust.get('tier') or '?'}. Their machine may be off, "
                f"their internet may be down, or their watchdog couldn't recover.")
        try:
            notifications_inbox_add(event="fleet_dark", title=title, body=body)
        except Exception as e:
            log.warning(f"could not write fleet_dark notification: {e}")
        with db() as conn:
            conn.execute(
                "UPDATE customers SET is_dark = 1, dark_since = ? WHERE api_key = ?",
                (ts, cust["api_key"]),
            )
        log.warning(f"fleet: {cust['api_key'][:14]} went dark "
                    f"({_fmt_duration(dark_for)} since last heartbeat)")


_threading.Thread(target=_fleet_health_loop, daemon=True).start()
log.info("fleet health worker started (dark threshold: %ds, check interval: %ds)",
         DARK_THRESHOLD_SEC, FLEET_CHECK_INTERVAL_SEC)


# ── Admin: fleet status + inbox  ────────────────────────────────────────


@app.route("/api/admin/fleet", methods=["GET"])
def admin_fleet():
    """JSON dump of every customer's health status. Use to power a fleet
    dashboard or just to curl-and-grep when something's wrong.

    Auth: X-Admin-Token header must match ORBI_ADMIN_TOKEN env var."""
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    customers = []
    with db() as conn:
        cur = conn.execute(
            "SELECT api_key, email, business_name, tier, billing_cycle, "
            "active, period_end, last_seen_at, is_dark, dark_since, "
            "created_at, last_heartbeat FROM customers ORDER BY created_at DESC"
        )
        for row in cur.fetchall():
            c = dict(row)
            last_seen = c.get("last_seen_at") or 0
            age = now() - last_seen if last_seen else None
            if not c.get("active"):
                status = "inactive"
            elif not last_seen:
                status = "never_seen"
            elif c.get("is_dark"):
                status = "dark"
            elif age and age > 600:  # 10 min
                status = "stale"
            else:
                status = "healthy"
            c["status"] = status
            c["last_seen_ago_sec"] = age
            # Hide raw payload from the brief summary; include separately if needed
            try:
                c["last_heartbeat_parsed"] = json.loads(c.get("last_heartbeat") or "{}")
            except (json.JSONDecodeError, TypeError):
                c["last_heartbeat_parsed"] = {}
            del c["last_heartbeat"]
            customers.append(c)
    counts = {"healthy": 0, "stale": 0, "dark": 0, "never_seen": 0, "inactive": 0}
    for c in customers:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return jsonify({
        "now":       now(),
        "total":     len(customers),
        "counts":    counts,
        "customers": customers,
    })


@app.route("/api/admin/fleet/inbox", methods=["GET"])
def admin_fleet_inbox():
    """Read the fleet-alert inbox (written by _scan_for_dark_customers
    and the heartbeat-recovered handler)."""
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    inbox_path = Path(os.environ.get(
        "ORBI_BRAIN_INBOX", "/opt/orbi-brain/fleet_inbox.json"))
    if not inbox_path.exists():
        return jsonify({"items": []})
    try:
        items = json.loads(inbox_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        items = []
    unseen_only = request.args.get("unseen", "").lower() in ("1", "true", "yes")
    if unseen_only:
        items = [i for i in items if not i.get("seen")]
    items.sort(key=lambda i: i.get("ts", 0), reverse=True)
    return jsonify({"items": items})


# ---------------------------------------------------------------------------
# Twilio webhook proxy — Twilio calls THE BRAIN, the brain proxies to the
# customer's local Orby. Brain learns the customer's URL from heartbeats.
# ---------------------------------------------------------------------------

_TWIML_NO_CUSTOMER_URL = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Our system is temporarily unavailable. Please try again in a few minutes, or send us an email instead. Goodbye.</Say>
    <Hangup/>
</Response>"""


def _twiml_offline_message(business_name: str = "") -> str:
    msg = (f"{business_name} is currently offline. " if business_name else
           "We're currently offline. ")
    msg += ("Please leave a brief message after the tone and we'll call you "
            "back as soon as possible. Thank you.")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{msg}</Say>
    <Record maxLength="120" playBeep="true"/>
</Response>"""


@app.route("/twilio/voice/<api_key>", methods=["POST", "GET"])
def twilio_voice_proxy(api_key: str):
    """Twilio sends incoming-call webhook here. We look up the customer
    and forward the webhook to their local Orby (URL from heartbeat).
    If the customer's machine isn't reachable, fall back to a friendly
    voicemail TwiML so the caller isn't dropped on the floor."""
    cust = get_customer_by_api_key(api_key)
    if not cust:
        log.warning(f"twilio voice: invalid api_key {api_key[:14]}")
        return _TWIML_NO_CUSTOMER_URL, 200, {"Content-Type": "application/xml"}
    if not cust.get("active"):
        log.warning(f"twilio voice: inactive customer {api_key[:14]}")
        return _twiml_offline_message(cust.get("business_name") or ""), 200, \
               {"Content-Type": "application/xml"}

    public_url = (cust.get("public_url") or "").rstrip("/")
    if not public_url:
        log.warning(f"twilio voice: no public_url for {api_key[:14]}")
        return _twiml_offline_message(cust.get("business_name") or ""), 200, \
               {"Content-Type": "application/xml"}

    # Forward the Twilio webhook (form-encoded body) to the customer's
    # local /voice/incoming endpoint. Pass through all the original form
    # fields so Orby's voice.py receives exactly what Twilio sent.
    forward_url = f"{public_url}/voice/incoming"
    import urllib.request
    import urllib.error
    try:
        form_data = request.form.to_dict()
        encoded = urllib.parse.urlencode(form_data).encode("ascii")
        req = urllib.request.Request(
            forward_url, data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent":   "Orbi-Brain-VoiceProxy/0.1"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.read(), resp.status, {"Content-Type": "application/xml"}
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        log.warning(f"twilio voice proxy to {forward_url} failed: {e}")
        return _twiml_offline_message(cust.get("business_name") or ""), 200, \
               {"Content-Type": "application/xml"}


@app.route("/twilio/sms/<api_key>", methods=["POST", "GET"])
def twilio_sms_proxy(api_key: str):
    """Twilio inbound-SMS webhook. Same pattern as voice: lookup customer,
    forward to their local /sms/incoming. Silent failure if offline."""
    cust = get_customer_by_api_key(api_key)
    if not cust or not cust.get("active") or not cust.get("public_url"):
        return ("", 204)
    forward_url = f"{cust['public_url'].rstrip('/')}/sms/incoming"
    import urllib.request, urllib.error
    try:
        encoded = urllib.parse.urlencode(request.form.to_dict()).encode("ascii")
        req = urllib.request.Request(
            forward_url, data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read(), resp.status, {"Content-Type": "application/xml"}
    except Exception as e:
        log.warning(f"twilio sms proxy failed for {api_key[:14]}: {e}")
        return ("", 204)


@app.route("/admin", methods=["GET"])
@app.route("/admin/", methods=["GET"])
def admin_dashboard():
    """Single-page HTML fleet dashboard. Auto-refreshes every 30s.
    Auth via ?token=<ORBI_ADMIN_TOKEN> in the URL (token-protected)
    OR via X-Admin-Token header. Bookmark with the token query string."""
    token_qs = request.args.get("token", "")
    if token_qs != ADMIN_TOKEN and request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return ("<h1>Forbidden</h1><p>Add <code>?token=YOUR_ORBI_ADMIN_TOKEN</code> "
                "to the URL.</p>"), 403, {"Content-Type": "text/html"}
    return _FLEET_DASHBOARD_HTML.replace("__ADMIN_TOKEN__", ADMIN_TOKEN), 200, \
           {"Content-Type": "text/html; charset=utf-8"}


_FLEET_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Orby Fleet — Frank's Dashboard</title>
<style>
  :root {
    --bg: #0a0f1e; --surface: #111827; --surface2: #1a2235;
    --text: #e8eaf0; --text-muted: #aab0bc; --border: rgba(45,212,191,0.18);
    --good: #4ade80; --warn: #f59e0b; --err: #ef4444; --gold: #2dd4bf;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       padding:18px;min-height:100vh}
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;flex-wrap:wrap;gap:12px}
  h1{font-size:20px;color:var(--gold)}
  .meta{font-size:12px;color:var(--text-muted)}
  .meta strong{color:var(--text)}
  .summary{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
  .pill{padding:8px 14px;border-radius:20px;font-size:13px;font-weight:600;
        background:var(--surface);border:1px solid var(--border)}
  .pill.healthy{color:var(--good);border-color:rgba(74,222,128,0.35)}
  .pill.stale  {color:var(--warn);border-color:rgba(245,158,11,0.35)}
  .pill.dark   {color:var(--err); border-color:rgba(239,68,68,0.45);background:rgba(239,68,68,0.06)}
  .pill.never_seen{color:var(--text-muted)}
  .pill.inactive{color:var(--text-muted);opacity:0.6}
  table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:10px;overflow:hidden;font-size:13px}
  thead{background:var(--surface2)}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--border)}
  th{color:var(--text-muted);font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:0.5px}
  tr:last-child td{border-bottom:none}
  tr:hover{background:rgba(45,212,191,0.04)}
  td.email{color:var(--text);font-weight:500}
  td.bizname{color:var(--gold)}
  .status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
  .status-dot.healthy{background:var(--good)}
  .status-dot.stale{background:var(--warn)}
  .status-dot.dark{background:var(--err)}
  .status-dot.never_seen, .status-dot.inactive{background:var(--text-muted)}
  .ago{color:var(--text-muted);font-size:12px}
  .empty{padding:40px;text-align:center;color:var(--text-muted)}
  .err-banner{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.35);
              color:#fca5a5;padding:12px 16px;border-radius:8px;margin-bottom:18px}
  details summary{cursor:pointer;color:var(--gold);font-size:13px}
  details pre{background:#0b0f1a;color:#cfe0ff;padding:12px;border-radius:6px;
              font-size:11px;overflow-x:auto;margin-top:8px}
  code{font-family:ui-monospace,"SF Mono",Consolas,monospace;font-size:12px;color:var(--gold)}
</style>
</head>
<body>

<header>
  <div>
    <h1>Orby Fleet</h1>
    <div class="meta">My Orby AI Solutions — every customer Orby reports here every 5 min</div>
  </div>
  <div class="meta" id="last-update">Loading…</div>
</header>

<div id="err-zone"></div>
<div class="summary" id="summary"></div>
<table>
  <thead>
    <tr>
      <th>Status</th>
      <th>Business</th>
      <th>Email</th>
      <th>Tier</th>
      <th>Phone</th>
      <th>Last seen</th>
      <th>Version</th>
      <th>Public URL</th>
    </tr>
  </thead>
  <tbody id="rows">
    <tr><td colspan="8" class="empty">Loading customers…</td></tr>
  </tbody>
</table>

<details style="margin-top:20px">
  <summary>Recent alerts (fleet inbox)</summary>
  <pre id="inbox-pre">Loading…</pre>
</details>

<script>
const TOKEN = "__ADMIN_TOKEN__";
const H = {"X-Admin-Token": TOKEN};

function fmtAgo(sec){
  if (sec == null) return "never";
  if (sec < 60) return sec + "s ago";
  if (sec < 3600) return Math.floor(sec/60) + " min ago";
  if (sec < 86400) return Math.floor(sec/3600) + " hr ago";
  return Math.floor(sec/86400) + " days ago";
}

async function loadFleet(){
  try {
    const r = await fetch("/api/admin/fleet", {headers: H});
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();

    document.getElementById("last-update").innerHTML =
      "Updated <strong>" + new Date().toLocaleTimeString() + "</strong> · " +
      data.total + " customers";

    const counts = data.counts || {};
    document.getElementById("summary").innerHTML = [
      ["healthy", "✓ Healthy"],
      ["stale", "⏳ Stale"],
      ["dark", "⚠ Dark"],
      ["never_seen", "○ Never seen"],
      ["inactive", "⊘ Inactive"],
    ].map(function([key, label]){
      const n = counts[key] || 0;
      return '<div class="pill ' + key + '">' + label + ': ' + n + '</div>';
    }).join("");

    const tbody = document.getElementById("rows");
    if (!data.customers || data.customers.length === 0){
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No customers yet. They\\'ll show up here after their first heartbeat.</td></tr>';
      return;
    }
    tbody.innerHTML = data.customers.map(function(c){
      return "<tr>" +
        '<td><span class="status-dot ' + c.status + '"></span>' + c.status + "</td>" +
        '<td class="bizname">' + (c.business_name || '—') + "</td>" +
        '<td class="email">' + (c.email || '—') + "</td>" +
        "<td>" + (c.tier || '—') + (c.billing_cycle ? ' / ' + c.billing_cycle : '') + "</td>" +
        "<td>" + (c.twilio_number || '—') + "</td>" +
        '<td><span class="ago">' + fmtAgo(c.last_seen_ago_sec) + "</span></td>" +
        "<td>" + ((c.last_heartbeat_parsed && c.last_heartbeat_parsed.version) || '—') + "</td>" +
        '<td><code>' + ((c.public_url || '').replace('https://', '').slice(0, 40)) + "</code></td>" +
      "</tr>";
    }).join("");

    document.getElementById("err-zone").innerHTML = "";
  } catch (e) {
    document.getElementById("err-zone").innerHTML =
      '<div class="err-banner">Couldn\\'t reach the brain server: ' + e + '</div>';
  }
}

async function loadInbox(){
  try {
    const r = await fetch("/api/admin/fleet/inbox?unseen=1", {headers: H});
    if (!r.ok) return;
    const data = await r.json();
    const pre = document.getElementById("inbox-pre");
    if (!data.items || data.items.length === 0){
      pre.textContent = "(no unseen alerts)";
      return;
    }
    pre.textContent = data.items.slice(0, 20).map(function(i){
      return "[" + new Date(i.ts * 1000).toLocaleString() + "] " +
        i.event + " — " + i.title + "\\n    " + i.body;
    }).join("\\n\\n");
  } catch (e) { /* silent */ }
}

loadFleet(); loadInbox();
setInterval(loadFleet, 30000);
setInterval(loadInbox, 60000);
</script>

</body>
</html>"""


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "stripe-webhook"})

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5060"))
    app.run(host="127.0.0.1", port=port)
