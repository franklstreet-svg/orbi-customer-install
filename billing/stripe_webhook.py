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

# Map Stripe price IDs → human-readable tier names
# (set these to your real price IDs once you create the products in Stripe)
PRICE_TO_TIER = {
    os.environ.get("STRIPE_PRICE_CHAT",    "price_chat_placeholder"):    "chat_only",
    os.environ.get("STRIPE_PRICE_STD",     "price_std_placeholder"):     "standard",
    os.environ.get("STRIPE_PRICE_LOCAL",   "price_local_placeholder"):   "local_only_premium",
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
                active             INTEGER DEFAULT 0,
                subscription_id    TEXT,
                period_end         INTEGER,
                grace_until        INTEGER,
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

            CREATE INDEX IF NOT EXISTS idx_customers_stripe
                ON customers(stripe_customer_id);
        """)

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
                    active: bool | None = None, subscription_id: str | None = None,
                    period_end: int | None = None, grace_until: int | None = None) -> str:
    existing = get_customer_by_stripe_id(stripe_customer_id)
    with db() as conn:
        if existing:
            updates = []
            params = []
            for field, value in (
                ("email", email), ("business_name", business_name),
                ("tier", tier), ("subscription_id", subscription_id),
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
                "(api_key, stripe_customer_id, email, business_name, tier, "
                "active, subscription_id, period_end, grace_until, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    api_key, stripe_customer_id, email, business_name, tier,
                    1 if active else 0, subscription_id, period_end, grace_until,
                    now(), now(),
                ),
            )
            return api_key

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

    # Resolve which tier they bought
    tier = None
    if subscription_id:
        sub = stripe.Subscription.retrieve(subscription_id)
        for item in sub["items"]["data"]:
            price_id = item["price"]["id"]
            if price_id in PRICE_TO_TIER:
                tier = PRICE_TO_TIER[price_id]
                break
        period_end = sub["current_period_end"]
    else:
        period_end = None

    api_key = upsert_customer(
        stripe_customer_id=stripe_customer_id,
        email=email,
        tier=tier,
        active=True,
        subscription_id=subscription_id,
        period_end=period_end,
        grace_until=None,
    )

    # Mint an install token so the customer's downloaded installer can
    # claim its api_key without us emailing the raw key in plaintext.
    install_token = create_install_record(
        stripe_customer_id=stripe_customer_id,
        email=email or "",
        tier=tier,
        api_key=api_key,
    )
    print(f"[checkout] new customer {email} on tier {tier}, api_key={api_key[:14]}..., install_token={install_token[:14]}...")
    _send_install_email(email, install_token, tier)
    # TODO: in Phase 2, swap the print-based stub for Resend / SES.


def _send_install_email(email: str | None, install_token: str,
                        tier: str | None) -> None:
    """Send the install email via Resend's HTTPS API. Falls back to log-only
    if RESEND_API_KEY is missing — so the system is never broken by a missing
    secret, but Frank knows immediately what happened.

    Why Resend: simple HTTPS POST, generous free tier (3k emails/month),
    no SMTP config to wrangle, no DNS until you scale past 100/day."""
    if not email:
        print(f"[email] no address for token {install_token[:14]}... — skipping send")
        return

    download_url = f"{DOWNLOAD_BASE_URL.rstrip('/')}/download/{tier or 'standard'}"
    subject = "Your Orbi is ready — install token inside"
    text = (
        f"Welcome to Orbi!\n\n"
        f"Two things to do:\n\n"
        f"1) Download the installer for your operating system:\n"
        f"   {download_url}\n\n"
        f"2) When the installer asks for your install token, paste:\n\n"
        f"   {install_token}\n\n"
        f"The token is single-use. Don't share it — anyone with this token "
        f"could install Orbi as you.\n\n"
        f"Need help? Reply to this email.\n\n"
        f"— Frank @ My Orbi AI Solutions"
    )
    html = (
        f'<div style="font-family:system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.6;color:#1a2236">'
        f'<h2 style="color:#4f8cff;margin-bottom:6px">Welcome to Orbi 🎉</h2>'
        f'<p>Two quick steps to get you live:</p>'
        f'<p><strong>1.</strong> Download the installer for your operating system:<br>'
        f'<a href="{download_url}" style="display:inline-block;margin-top:6px;background:linear-gradient(135deg,#4f8cff,#8b5cf6);color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600">Download Orbi installer</a></p>'
        f'<p><strong>2.</strong> When the installer asks for your install token, paste this:</p>'
        f'<pre style="background:#0b0f1a;color:#eaf0ff;padding:14px;border-radius:8px;font-size:14px;'
        f'word-break:break-all">{install_token}</pre>'
        f'<p style="color:#666;font-size:13px">The token is single-use — anyone with it could install '
        f'Orbi as you, so keep it private.</p>'
        f'<p style="color:#666;font-size:13px;margin-top:20px">Need help? Just reply to this email.</p>'
        f'<p style="color:#888;font-size:12px;margin-top:24px">— Frank @ My Orbi AI Solutions</p>'
        f'</div>'
    )

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        print(f"[email] RESEND_API_KEY not set — would send to {email}:")
        print(text)
        return

    from_addr = os.environ.get("ORBI_FROM_EMAIL",
                                "Orbi <welcome@orbiaisolutions.com>")
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
                "User-Agent":    "Orbi-Billing/0.1",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"[email] Resend OK for {email}: {body[:200]}")
    except Exception as e:
        print(f"[email] Resend FAILED for {email}: {e}")
        print(f"[email] falling back to log so the token isn't lost:\n{text}")

def handle_subscription_updated(event: dict) -> None:
    """Subscription renewed, downgraded, upgraded, or canceled."""
    sub = event["data"]["object"]
    stripe_customer_id = sub["customer"]
    status             = sub["status"]
    period_end         = sub["current_period_end"]

    new_tier = None
    for item in sub["items"]["data"]:
        price_id = item["price"]["id"]
        if price_id in PRICE_TO_TIER:
            new_tier = PRICE_TO_TIER[price_id]
            break

    active = status in ("active", "trialing")
    upsert_customer(
        stripe_customer_id=stripe_customer_id,
        tier=new_tier,
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
    """Subscription canceled. Deactivate immediately."""
    sub = event["data"]["object"]
    upsert_customer(stripe_customer_id=sub["customer"], active=False)
    print(f"[canceled] {sub['customer']}")

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
