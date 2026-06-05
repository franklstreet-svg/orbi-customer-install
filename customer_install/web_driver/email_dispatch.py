"""
email_dispatch — generic order delivery via email.

For restaurants without a per-customer Playwright web_driver (which is
most of them at launch), Orby formats the order as a clean email and
sends it to the restaurant's kitchen email address. Kitchen staff opens
the email, enters the order into whatever POS they already use, and
fulfills like any other order.

Same input shape as `web_driver.submit_purblum_order` so the dispatch
layer in `orbi.py` can pick between them transparently based on the
customer profile's `order_submission.method` field.

Inputs:
    cart      — list of items with name, qty, price, modifiers
    customer  — {name, phone, pickup_time}
    profile   — the scraped/canonical customer profile JSON

Output:
    {ok: True, order_id: "ord_xxx", method: "email",
     to: "kitchen@...", total: X.YZ}
    or
    {ok: False, error: "..."}
"""

from __future__ import annotations

import logging
import os
import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("orbi.web_driver.email")


def _format_order_email(order_id: str, cart: list[dict],
                         customer: dict, totals: dict,
                         profile: dict) -> tuple[str, str]:
    """Return (subject, body) for the kitchen order email. Plain text +
    clearly structured so staff can parse it fast."""
    biz_name = profile.get("name", "the restaurant")
    cust_name = (customer or {}).get("name", "Walk-in")
    cust_phone = (customer or {}).get("phone", "(no phone)")
    pickup = (customer or {}).get("pickup_time") or "ASAP"

    subject = f"NEW ORDER — {cust_name} @ {pickup} — ${totals.get('total', 0):.2f}"

    lines = []
    lines.append(f"NEW ORDER from {biz_name}'s Orby line")
    lines.append("=" * 60)
    lines.append(f"Order ID:   {order_id}")
    lines.append(f"Customer:   {cust_name}")
    lines.append(f"Phone:      {cust_phone}")
    lines.append(f"Pickup:     {pickup}")
    lines.append("")
    lines.append("ITEMS")
    lines.append("-" * 60)
    for item in cart or []:
        qty = item.get("qty", 1)
        name = item.get("name", "(item)")
        price = item.get("price") or item.get("base_price") or 0
        line_total = float(price) * int(qty)
        lines.append(f"  {qty} × {name}    ${line_total:.2f}")
        for mod in item.get("modifiers") or []:
            mod_name = mod if isinstance(mod, str) else \
                       (mod.get("label") or mod.get("name") or "")
            mod_price = 0 if isinstance(mod, str) else (mod.get("price_delta") or 0)
            if mod_price:
                lines.append(f"      + {mod_name} (+${mod_price:.2f})")
            elif mod_name:
                lines.append(f"      + {mod_name}")
    lines.append("-" * 60)
    lines.append(f"Subtotal:   ${totals.get('subtotal', 0):.2f}")
    if totals.get("tax", 0):
        lines.append(f"Tax:        ${totals.get('tax', 0):.2f}")
    lines.append(f"TOTAL:      ${totals.get('total', 0):.2f}")
    lines.append("")
    lines.append("=" * 60)
    lines.append("This order arrived through Orby — your AI receptionist.")
    lines.append("Reply to this email to leave a note for the customer.")
    return subject, "\n".join(lines)


def submit_order(cart: list[dict], customer: dict, totals: dict,
                  profile: dict) -> dict:
    """Send the order as an email to the kitchen address configured in
    the customer's profile. Returns a result dict matching the shape
    `web_driver.submit_purblum_order` uses, so the dispatch layer
    upstream can swap between methods transparently.

    Config (in the customer profile JSON):
        order_submission:
            method: "email"
            kitchen_email: "kitchen@restaurant.com"

    SMTP credentials are pulled from environment vars set in the
    customer install's config.json or .env:
        ORBI_SMTP_HOST, ORBI_SMTP_PORT, ORBI_SMTP_USER,
        ORBI_SMTP_PASSWORD, ORBI_FROM_EMAIL
    Same env vars the billing webhook uses for install emails — so a
    single SMTP setup serves both flows.
    """
    cfg = (profile or {}).get("order_submission") or {}
    kitchen_email = (cfg.get("kitchen_email") or "").strip()
    if not kitchen_email:
        return {"ok": False, "error": "no_kitchen_email_configured",
                "method": "email"}

    smtp_host = os.environ.get("ORBI_SMTP_HOST", "smtp.mail.yahoo.com")
    smtp_port = int(os.environ.get("ORBI_SMTP_PORT", "587"))
    smtp_user = os.environ.get("ORBI_SMTP_USER", "").strip()
    smtp_pw   = os.environ.get("ORBI_SMTP_PASSWORD", "").strip()
    from_addr = os.environ.get("ORBI_FROM_EMAIL", smtp_user or "noreply@example.com")

    # Fallback: if no env-var SMTP, pull from the owner's connected
    # imap_smtp account (Yahoo / Gmail / iCloud — whatever they wired
    # in Settings → Email Accounts). This is how a fresh customer will
    # have working dispatch without needing to set env vars by hand.
    if not smtp_user or not smtp_pw:
        try:
            import imap_smtp  # type: ignore
            from pathlib import Path
            # Pull owner user_dir from profile or env
            user_dir_str = ((profile or {}).get("_owner_user_dir")
                             or os.environ.get("ORBI_OWNER_USER_DIR", ""))
            if user_dir_str:
                accts = imap_smtp._read_accounts(Path(user_dir_str))
                if accts:
                    a = accts[0]
                    smtp_host = a.get("smtp_host") or smtp_host
                    smtp_port = int(a.get("smtp_port") or smtp_port)
                    smtp_user = a.get("email") or ""
                    smtp_pw   = a.get("password") or ""
                    if smtp_user and not os.environ.get("ORBI_FROM_EMAIL"):
                        from_addr = f"Orby <{smtp_user}>"
        except Exception as e:
            log.debug(f"imap_smtp fallback failed: {e}")

    if not smtp_user or not smtp_pw or "PASTE" in smtp_pw or "REPLACE" in smtp_pw:
        return {"ok": False, "error": "smtp_not_configured",
                "method": "email", "to": kitchen_email,
                "hint": ("Connect a mailbox in Settings → Email Accounts "
                          "(any Gmail / Yahoo / iCloud with an app password), "
                          "OR set ORBI_SMTP_USER + ORBI_SMTP_PASSWORD in env.")}

    order_id = "ord_" + uuid.uuid4().hex[:12]
    subject, body = _format_order_email(order_id, cart, customer, totals, profile)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = kitchen_email
    msg["Reply-To"] = (customer or {}).get("email") or from_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pw)
            s.send_message(msg)
        log.info(f"order email sent to {kitchen_email} (order_id={order_id}, "
                 f"total=${totals.get('total', 0):.2f})")
        return {
            "ok": True,
            "order_id": order_id,
            "method": "email",
            "to": kitchen_email,
            "total": float(totals.get("total", 0)),
        }
    except Exception as e:
        log.warning(f"order email send failed to {kitchen_email}: {e}")
        return {"ok": False, "error": f"smtp_send_failed: {e}",
                "method": "email", "to": kitchen_email}
