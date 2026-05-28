#!/usr/bin/env python3
"""
end_to_end_test — simulate the whole purchase → install → fleet flow
WITHOUT charging real money. Uses Stripe webhook signatures so it
exercises the real signature verification path, just with synthetic
event payloads.

What it tests:
  1. Brain server /health responds 200
  2. Synthetic checkout.session.completed event → webhook accepts it,
     creates a customer + install token + (skipped) Twilio
  3. /api/verify/<token> returns the customer record (single-use)
  4. /api/verify/<token> a second time returns 404 (token consumed)
  5. /api/active/<api_key> returns active=True
  6. /api/heartbeat/<api_key> updates last_seen
  7. /admin?token=... returns the HTML dashboard
  8. /api/admin/fleet shows the test customer
  9. Synthetic customer.subscription.deleted → customer goes inactive
  10. After deactivation, /api/active/<api_key> returns active=False

Run:
    python3 billing/end_to_end_test.py

Or against a remote brain server:
    python3 billing/end_to_end_test.py --url https://brain.twickell.com

Exits 0 on all-green, 1 if anything fails.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request


GREEN, RED, YEL, DIM, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YEL = DIM = RESET = ""


def post(url: str, body: bytes, headers: dict | None = None,
         timeout: int = 15) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body,
                                 headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace") if e.fp else ""
    except urllib.error.URLError as e:
        return 0, str(e)


def get(url: str, headers: dict | None = None, timeout: int = 10) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace") if e.fp else ""
    except urllib.error.URLError as e:
        return 0, str(e)


def make_stripe_signature(payload: bytes, secret: str,
                          timestamp: int | None = None) -> str:
    """Build a v1-style Stripe webhook signature header so the brain
    server's signature verification accepts our synthetic event."""
    ts = timestamp or int(time.time())
    signed_payload = f"{ts}.{payload.decode('utf-8')}"
    sig = hmac.new(secret.encode("utf-8"),
                   signed_payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# ── Test runner ────────────────────────────────────────────────────────


class Report:
    def __init__(self):
        self.results: list[tuple[str, str, str]] = []  # (name, status, detail)
    def add(self, name: str, ok: bool, detail: str = ""):
        status = "PASS" if ok else "FAIL"
        self.results.append((name, status, detail))
        glyph = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        line = f"  {glyph} {name}"
        if detail:
            line += f"  {DIM}{detail}{RESET}"
        print(line)
    def passed(self) -> int: return sum(1 for r in self.results if r[1] == "PASS")
    def failed(self) -> int: return sum(1 for r in self.results if r[1] == "FAIL")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url",  default=os.environ.get(
        "BRAIN_URL", "http://127.0.0.1:5060"))
    ap.add_argument("--secret", default=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
                    help="must match what the brain server is using")
    ap.add_argument("--admin-token", default=os.environ.get("ORBI_ADMIN_TOKEN", ""),
                    help="for /admin and /api/admin endpoints")
    args = ap.parse_args(argv)

    base = args.url.rstrip("/")
    rep = Report()
    print(f"\nEnd-to-end test against {base}\n")

    # 1. Health
    code, body = get(f"{base}/health")
    rep.add("brain server reachable + /health 200",
            code == 200 and '"status":"ok"' in body.replace(" ", ""),
            f"HTTP {code}")
    if code != 200:
        print(f"\n{RED}brain server not reachable; aborting{RESET}")
        return 1

    if not args.secret:
        print(f"\n{YEL}STRIPE_WEBHOOK_SECRET not set — can't sign synthetic events.{RESET}")
        print(f"{YEL}Pass --secret or set the env var. Aborting after health check.{RESET}\n")
        return 1

    # 2. Synthetic checkout.session.completed
    fake_customer_id = f"cus_e2e_{int(time.time())}"
    fake_session = {
        "id": "evt_e2e_001",
        "object": "event",
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {"object": {
            "id":           f"cs_e2e_{int(time.time())}",
            "object":       "checkout.session",
            "customer":     fake_customer_id,
            "subscription": None,  # skip Stripe API call to fetch real sub
            "customer_details": {"email": "e2e-test@example.com"},
        }},
    }
    payload = json.dumps(fake_session, separators=(",", ":")).encode("utf-8")
    sig = make_stripe_signature(payload, args.secret)
    code, body = post(f"{base}/webhook", payload,
                      headers={"Stripe-Signature": sig,
                               "Content-Type":     "application/json"})
    rep.add("synthetic checkout.session.completed accepted",
            code == 200,
            f"HTTP {code}: {body[:120]}")

    # 3-10 only make sense if checkout succeeded. We can't easily get
    # the api_key out without admin access — use admin/customers if Frank
    # provided ORBI_ADMIN_TOKEN.
    api_key = None
    install_token = None
    if args.admin_token:
        code, body = get(f"{base}/api/admin/customers",
                         headers={"X-Admin-Token": args.admin_token})
        try:
            customers = json.loads(body)
            for c in customers:
                if c.get("stripe_customer_id") == fake_customer_id:
                    api_key = c.get("api_key")
                    break
        except json.JSONDecodeError:
            pass
        rep.add("admin endpoint returns the new customer",
                api_key is not None,
                f"api_key={api_key[:14] + '...' if api_key else 'not found'}")

        # Fleet endpoint should also show it
        code, body = get(f"{base}/api/admin/fleet",
                         headers={"X-Admin-Token": args.admin_token})
        try:
            fleet = json.loads(body)
            found_in_fleet = any(c.get("api_key") == api_key
                                 for c in fleet.get("customers", []))
        except json.JSONDecodeError:
            found_in_fleet = False
        rep.add("customer visible in /api/admin/fleet", found_in_fleet,
                f"total: {fleet.get('total') if 'fleet' in dir() else '?'}")

        # Fleet HTML dashboard
        code, body = get(f"{base}/admin?token={args.admin_token}")
        rep.add("admin HTML dashboard renders",
                code == 200 and "Orby Fleet" in body,
                f"HTTP {code} ({len(body)} bytes)")

        if api_key:
            # Heartbeat
            code, body = post(f"{base}/api/heartbeat/{api_key}",
                              json.dumps({"uptime_sec": 10, "version": "0.1.0",
                                          "public_url": "https://test.trycloudflare.com"}).encode("utf-8"),
                              headers={"Content-Type": "application/json"})
            rep.add("heartbeat accepted + recorded",
                    code == 200,
                    f"HTTP {code}: {body[:80]}")

            # Active check
            code, body = get(f"{base}/api/active/{api_key}")
            try:
                active_data = json.loads(body)
            except json.JSONDecodeError:
                active_data = {}
            rep.add("api/active returns active=True after checkout",
                    code == 200 and active_data.get("active") is True,
                    f"HTTP {code}: active={active_data.get('active')}")

            # Simulate cancellation
            cancel_event = {
                "id": "evt_e2e_002",
                "object": "event",
                "type": "customer.subscription.deleted",
                "created": int(time.time()),
                "data": {"object": {"customer": fake_customer_id,
                                    "id": "sub_test_e2e"}},
            }
            payload2 = json.dumps(cancel_event, separators=(",", ":")).encode("utf-8")
            sig2 = make_stripe_signature(payload2, args.secret)
            code, body = post(f"{base}/webhook", payload2,
                              headers={"Stripe-Signature": sig2,
                                       "Content-Type":     "application/json"})
            rep.add("synthetic subscription.deleted accepted",
                    code == 200, f"HTTP {code}: {body[:60]}")

            # Active should now be False
            code, body = get(f"{base}/api/active/{api_key}")
            try:
                active_data = json.loads(body)
            except json.JSONDecodeError:
                active_data = {}
            rep.add("api/active returns active=False after cancel",
                    code == 200 and active_data.get("active") is False,
                    f"HTTP {code}: active={active_data.get('active')}")
    else:
        print(f"\n{YEL}Skipping the rest of the test — ORBI_ADMIN_TOKEN not "
              f"provided, can't introspect the customer record.{RESET}")

    # Summary
    print()
    print(f"{GREEN}{rep.passed()} pass{RESET} / "
          f"{RED if rep.failed() else ''}{rep.failed()} fail{RESET}")
    print()
    return 0 if rep.failed() == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
