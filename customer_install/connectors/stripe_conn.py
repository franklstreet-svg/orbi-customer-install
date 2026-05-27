"""
connectors.stripe_conn — Stripe read-only connector for Orbi customer installs.

Lets the owner give Orbi a Stripe Restricted Key (read-only) so she can answer
questions like:
    "Did Sandra Hadley pay her invoice?"
    "What's our take this week?"
    "List failed payments — anyone we need to follow up with?"

DESIGN NOTES
------------
- auth_kind = "api_key". The owner pastes a Stripe Restricted Key. We verify
  it works against stripe.Balance.retrieve() before persisting, so a typo
  fails immediately instead of silently saving a dead key.
- Restricted Key (rk_live_...) is strongly preferred over the Secret Key
  (sk_live_...). We tell the owner this in requires_owner_setup. The key is
  stored at <user_dir>/connector_tokens/stripe.json (mode 0o600) by the base.
- All Stripe SDK calls are lazy-imported so a missing `stripe` package
  doesn't crash Orbi at boot — the connector simply reports a clear error
  the moment the owner tries to use it.
- Read-only by design: no charges created, no refunds issued, no customers
  modified. Anything Orbi shows is for the owner's eyes (or — if the owner
  scopes it — to confirm payment to a customer on chat).

DEPS
----
    pip install stripe

ROUTE SURFACE (see bottom of file).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .base import Connector, register

log = logging.getLogger("orbi.connectors.stripe")


# ---------------------------------------------------------------------------
# Lazy import — keep `stripe` out of import path until first use.
# ---------------------------------------------------------------------------

def _stripe():
    try:
        import stripe  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "stripe package not installed — run: pip install stripe"
        ) from exc
    return stripe


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: int | None) -> str:
    """Convert a Stripe Unix timestamp (int) to ISO-8601 UTC."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc) \
        .replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cents_to_dollars(cents: int | None) -> float:
    if cents is None:
        return 0.0
    return round(cents / 100.0, 2)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

@register
class StripeConnector(Connector):
    id = "stripe"
    label = "Stripe"
    blurb = "See payments, find customers by email or last 4, summarize daily revenue. Read-only."
    auth_kind = "api_key"
    requires_owner_setup = [
        "Get your Stripe Secret Key from dashboard.stripe.com/apikeys. "
        "Use the RESTRICTED KEY with read-only permissions for safety."
    ]

    # ── Internal: configure the SDK with the saved key ─────────────────

    def _client(self):
        """Return the stripe module with api_key already set, or raise."""
        key = self.get_api_key()
        if not key:
            raise RuntimeError("stripe: not connected (no api key saved)")
        stripe = _stripe()
        stripe.api_key = key
        return stripe

    # ── save_api_key (override to verify the key before persisting) ────

    def save_api_key(self, key: str, meta: dict | None = None) -> dict:
        if not key or not key.strip():
            raise ValueError("api key required")
        key = key.strip()

        # Verify with a tiny read-only call before we save.
        stripe = _stripe()
        stripe.api_key = key
        try:
            stripe.Balance.retrieve()
        except Exception as exc:    # noqa: BLE001
            # stripe.error.AuthenticationError lives at stripe.error.* but
            # we'd rather not bind tightly — any exception here means the
            # key didn't work.
            msg = str(exc)
            log.warning("stripe: key verification failed: %s", msg)
            if "401" in msg or "Invalid API Key" in msg or "authentication" in msg.lower():
                raise ValueError(f"stripe: invalid API key ({msg})") from exc
            raise ValueError(f"stripe: could not verify key — {msg}") from exc

        # Also try to grab the account's display name for status display.
        account_name = ""
        try:
            acct = stripe.Account.retrieve()
            bp = getattr(acct, "business_profile", None) or {}
            if isinstance(bp, dict):
                account_name = bp.get("name") or ""
            else:
                account_name = getattr(bp, "name", "") or ""
        except Exception as exc:    # noqa: BLE001
            log.info("stripe: account name fetch failed (non-fatal): %s", exc)

        merged_meta = dict(meta or {})
        if account_name:
            merged_meta["account_name"] = account_name
        return super().save_api_key(key, meta=merged_meta)

    # ── list_recent_payments ──────────────────────────────────────────

    def list_recent_payments(self, limit: int = 20) -> list[dict]:
        """Recent successful charges. Returns simple dicts for UI/LLM use."""
        stripe = self._client()
        try:
            resp = stripe.Charges.list(limit=max(1, min(limit, 100)))
        except Exception as exc:    # noqa: BLE001
            log.warning("stripe: list_recent_payments failed: %s", exc)
            self.update_status(last_error=f"list_recent_payments: {exc}")
            return []

        out: list[dict] = []
        for ch in resp.auto_paging_iter() if hasattr(resp, "auto_paging_iter") else resp.data:
            # Stop once we've satisfied limit (auto_paging_iter would keep going).
            if len(out) >= limit:
                break
            if not getattr(ch, "paid", False) or getattr(ch, "status", "") != "succeeded":
                continue
            cust_email = (
                getattr(ch, "receipt_email", None)
                or (getattr(ch, "billing_details", None) or {}).get("email") if isinstance(getattr(ch, "billing_details", None), dict) else getattr(getattr(ch, "billing_details", None), "email", None)
            )
            # Defensive: re-derive cust_email if the ternary above produced anything weird.
            if not cust_email:
                bd = getattr(ch, "billing_details", None)
                if isinstance(bd, dict):
                    cust_email = bd.get("email") or ""
                else:
                    cust_email = getattr(bd, "email", "") if bd else ""
            out.append({
                "id":              getattr(ch, "id", ""),
                "amount_dollars":  _cents_to_dollars(getattr(ch, "amount", 0)),
                "customer_email":  cust_email or "",
                "description":     getattr(ch, "description", "") or "",
                "created_iso":     _iso(getattr(ch, "created", 0)),
                "status":          getattr(ch, "status", ""),
            })
        return out

    # ── find_customer ────────────────────────────────────────────────

    def find_customer(self, query: str) -> list[dict]:
        """Search by email substring OR last4 of a card.
        Returns list of {id, name, email, created_iso, last4_cards?}.
        """
        if not query or not query.strip():
            return []
        q = query.strip()
        stripe = self._client()

        results: list[dict] = []

        # 1) Treat as email substring — Stripe customer search supports email:"..."
        try:
            # Use search API if available, fall back to filter by email exact.
            if "@" in q or len(q) > 4 or not q.isdigit():
                resp = stripe.Customer.search(query=f'email:"{q}"', limit=20)
                for c in getattr(resp, "data", []) or []:
                    results.append(self._customer_dict(c))
        except Exception as exc:    # noqa: BLE001
            log.info("stripe: customer email search fell back: %s", exc)
            # Fallback: list + filter manually (small accounts)
            try:
                resp = stripe.Customer.list(limit=100)
                for c in getattr(resp, "data", []) or []:
                    ce = getattr(c, "email", "") or ""
                    if q.lower() in ce.lower():
                        results.append(self._customer_dict(c))
            except Exception as exc2:    # noqa: BLE001
                log.warning("stripe: customer list fallback failed: %s", exc2)

        # 2) Treat as last4 — query charges by card last4 (only if q is 4 digits).
        if q.isdigit() and len(q) == 4:
            seen_ids = {r["id"] for r in results}
            try:
                charges = stripe.Charges.list(limit=100)
                for ch in getattr(charges, "data", []) or []:
                    pm = getattr(ch, "payment_method_details", None)
                    card = None
                    if isinstance(pm, dict):
                        card = pm.get("card") or {}
                    else:
                        card = getattr(pm, "card", None)
                    last4 = ""
                    if isinstance(card, dict):
                        last4 = card.get("last4", "") or ""
                    elif card is not None:
                        last4 = getattr(card, "last4", "") or ""
                    if last4 != q:
                        continue
                    cust_id = getattr(ch, "customer", None)
                    if not cust_id or cust_id in seen_ids:
                        continue
                    try:
                        c = stripe.Customer.retrieve(cust_id)
                        d = self._customer_dict(c)
                        d["matched_last4"] = q
                        results.append(d)
                        seen_ids.add(cust_id)
                    except Exception:   # noqa: BLE001
                        pass
            except Exception as exc:    # noqa: BLE001
                log.warning("stripe: last4 charge scan failed: %s", exc)

        return results

    @staticmethod
    def _customer_dict(c) -> dict:
        return {
            "id":          getattr(c, "id", ""),
            "name":        getattr(c, "name", "") or "",
            "email":       getattr(c, "email", "") or "",
            "phone":       getattr(c, "phone", "") or "",
            "created_iso": _iso(getattr(c, "created", 0)),
        }

    # ── daily_summary ────────────────────────────────────────────────

    def daily_summary(self, days: int = 7) -> list[dict]:
        """
        For each of the last `days` days (UTC):
            {date, gross_dollars, net_dollars, count, refunds}
        Counts only succeeded charges. Net = gross - refunded amount.
        """
        stripe = self._client()
        days = max(1, min(days, 90))
        now = _now_utc()
        # Start of "today" UTC, then go back `days - 1` days.
        end_dt   = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start_dt = end_dt - timedelta(days=days)

        # Initialize buckets per date.
        buckets: dict[str, dict] = {}
        for i in range(days):
            d = (start_dt + timedelta(days=i)).date().isoformat()
            buckets[d] = {"date": d, "gross_dollars": 0.0, "net_dollars": 0.0,
                          "count": 0, "refunds": 0.0}

        # Pull charges in the window.
        try:
            params = {
                "created": {"gte": int(start_dt.timestamp()),
                            "lt":  int(end_dt.timestamp())},
                "limit":   100,
            }
            it = stripe.Charges.list(**params)
            charges = list(it.auto_paging_iter()) if hasattr(it, "auto_paging_iter") else list(getattr(it, "data", []) or [])
        except Exception as exc:    # noqa: BLE001
            log.warning("stripe: daily_summary list failed: %s", exc)
            self.update_status(last_error=f"daily_summary: {exc}")
            return list(buckets.values())

        for ch in charges:
            if getattr(ch, "status", "") != "succeeded":
                continue
            created = getattr(ch, "created", 0)
            if not created:
                continue
            d_iso = datetime.fromtimestamp(created, tz=timezone.utc).date().isoformat()
            if d_iso not in buckets:
                continue
            gross = _cents_to_dollars(getattr(ch, "amount", 0))
            refunded = _cents_to_dollars(getattr(ch, "amount_refunded", 0) or 0)
            b = buckets[d_iso]
            b["gross_dollars"] = round(b["gross_dollars"] + gross, 2)
            b["refunds"]       = round(b["refunds"] + refunded, 2)
            b["net_dollars"]   = round(b["net_dollars"] + (gross - refunded), 2)
            b["count"]        += 1

        return list(buckets.values())

    # ── failed_payments ──────────────────────────────────────────────

    def failed_payments(self, limit: int = 10) -> list[dict]:
        """Recent failed charges — useful for owner follow-up."""
        stripe = self._client()
        try:
            resp = stripe.Charges.list(limit=max(1, min(limit * 5, 100)))
        except Exception as exc:    # noqa: BLE001
            log.warning("stripe: failed_payments list failed: %s", exc)
            self.update_status(last_error=f"failed_payments: {exc}")
            return []

        out: list[dict] = []
        for ch in getattr(resp, "data", []) or []:
            if len(out) >= limit:
                break
            if getattr(ch, "status", "") != "failed" and getattr(ch, "paid", True):
                # status=='failed' OR paid==False both indicate trouble
                continue
            bd = getattr(ch, "billing_details", None)
            email = ""
            if isinstance(bd, dict):
                email = bd.get("email", "") or ""
            elif bd is not None:
                email = getattr(bd, "email", "") or ""
            out.append({
                "id":               getattr(ch, "id", ""),
                "amount_dollars":   _cents_to_dollars(getattr(ch, "amount", 0)),
                "customer_email":   email,
                "description":      getattr(ch, "description", "") or "",
                "failure_code":     getattr(ch, "failure_code", "") or "",
                "failure_message":  getattr(ch, "failure_message", "") or "",
                "created_iso":      _iso(getattr(ch, "created", 0)),
                "status":           getattr(ch, "status", ""),
            })
        return out

    # ── status (extended) ────────────────────────────────────────────

    def status(self) -> dict:
        base_status = super().status()
        rec = self._read_tokens()
        base_status["account_name"] = rec.get("account_name", "") or ""

        # Count recent payments (cheap, ~last 10) only if connected.
        recent_count = 0
        if base_status.get("connected"):
            try:
                stripe = self._client()
                resp = stripe.Charges.list(limit=10)
                for ch in getattr(resp, "data", []) or []:
                    if getattr(ch, "status", "") == "succeeded":
                        recent_count += 1
            except Exception as exc:    # noqa: BLE001
                log.info("stripe: status recent count failed: %s", exc)
                base_status["last_error"] = str(exc)

        base_status["recent_payments_count"] = recent_count
        return base_status


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. config is the loaded config.json.
#
#   POST /api/owner/connectors/stripe/save_key
#       body:   {"api_key": "rk_live_..."}
#       calls:  StripeConnector(config, user_dir).save_api_key(api_key)
#       returns:{"saved": true, "account_name": "...", "saved_at": "..."}
#       errors: 400 if ValueError (invalid key)
#
#   POST /api/owner/connectors/stripe/disconnect
#       calls:  StripeConnector(config, user_dir).disconnect()
#       returns:{"ok": true}
#
#   GET  /api/owner/connectors/stripe/status
#       calls:  StripeConnector(config, user_dir).status()
#       returns:{connected, account_name, recent_payments_count, ...}
#
#   GET  /api/owner/connectors/stripe/payments?limit=20
#       calls:  list_recent_payments(limit)
#       returns:[{id, amount_dollars, customer_email, description, created_iso, status}, ...]
#
#   GET  /api/owner/connectors/stripe/customers/<query>
#       calls:  find_customer(query)
#       returns:[{id, name, email, phone, created_iso, matched_last4?}, ...]
#
#   GET  /api/owner/connectors/stripe/summary?days=7
#       calls:  daily_summary(days)
#       returns:[{date, gross_dollars, net_dollars, count, refunds}, ...]
#
#   GET  /api/owner/connectors/stripe/failed?limit=10
#       calls:  failed_payments(limit)
#       returns:[{id, amount_dollars, customer_email, failure_code,
#                 failure_message, created_iso, status}, ...]
#
# ---------------------------------------------------------------------------
