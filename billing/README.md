# Orbi Billing

Stripe webhook handler that lives on Frank's brain machine (deploy target: `billing.orbi.frank.com` — Render / Fly.io / a VPS). It does two jobs:

1. **Subscription monitor** — tracks "is this customer paid up?" via Stripe events, SQLite-backed.
2. **Install-token bridge** — when a customer completes Stripe Checkout, mints a one-shot install token, stores it in `installs.json`, and the downloaded installer trades that token for the customer's real `api_key` + `owner_email` over HTTPS. This avoids ever emailing the raw API key in plaintext.

## How it fits in

```
Customer pays via Stripe Checkout
      ↓
Stripe sends checkout.session.completed
      ↓
This server: upsert customer + mint install_token → installs.json
      ↓
Email customer the install_token + download link
      ↓
Customer downloads + runs orbi-installer (built by customer_install/installer/build_orbi_installer.py)
      ↓
Installer calls GET /api/verify/<install_token>
      ↓
Returns {customer_id, api_key, tier, owner_email} (single-use)
      ↓
Installer writes config.json, bootstraps owner user, starts service
      ↓
From then on, the install pings /api/active/<api_key> hourly
```

## Install-token endpoints

- **GET `/api/verify/<token>`** — public, one-shot. The installer calls this. Returns 200 + the install payload on first call, 404 on second / unknown / malformed.
- The token is minted inside `handle_checkout_completed()` and stored in `installs.json` (single JSON file, atomic writes, threading.Lock).
- Tokens are `inst_` + 32 hex chars (37 chars total).

## Sending the install email (Resend)

When `RESEND_API_KEY` is set in the environment, the install email goes out via Resend's HTTPS API:

```bash
export RESEND_API_KEY="re_..."          # from resend.com/api-keys
export ORBI_FROM_EMAIL="Orbi <welcome@orbiaisolutions.com>"
```

If `RESEND_API_KEY` is unset, the email is logged to stdout/journald instead — so a missing secret never silently drops the install token. Frank can read the journal, copy the token, and forward by hand.

### Why Resend (the engineering call):

- **Free tier covers 3,000 emails/month** — enough for the first hundred customers
- **Single HTTPS POST**, no SMTP server / DNS dance for the first 100/day
- **Built-in retries + bounce handling** so we don't have to
- **HTML + plain-text** delivered in one call
- Alternatives considered: AWS SES (cheaper at scale but requires DNS + sandbox-exit), Postmark (great deliverability but $15/mo minimum), SendGrid (older API). Resend wins on developer experience and time-to-shipping.

### To set up Resend (one-time, ~5 min):

1. Sign up at resend.com (free)
2. Add your sending domain (`orbiaisolutions.com`) — copy the 3 DNS records they show you into Cloudflare under that zone
3. Wait ~5 minutes for DNS to propagate, click "Verify" on Resend
4. Generate an API key, set `RESEND_API_KEY` in `/etc/orbi-brain/stripe.env`
5. Restart the systemd unit — `sudo systemctl restart stripe-webhook`

## Install on the brain machine

```bash
# 1. Create the directory + user
sudo mkdir -p /opt/orbi-brain /etc/orbi-brain
sudo useradd -r -s /usr/sbin/nologin -d /opt/orbi-brain orbi-brain 2>/dev/null || true

# 2. Copy files
sudo cp stripe_webhook.py /opt/orbi-brain/
sudo cp stripe-webhook.service /etc/systemd/system/
sudo cp stripe.env.template /etc/orbi-brain/stripe.env

# 3. Edit the env file with REAL values
sudo nano /etc/orbi-brain/stripe.env
sudo chmod 600 /etc/orbi-brain/stripe.env
sudo chown orbi-brain:orbi-brain /etc/orbi-brain/stripe.env
sudo chown -R orbi-brain:orbi-brain /opt/orbi-brain

# 4. Install Python deps
sudo pip3 install flask stripe

# 5. Enable + start
sudo systemctl daemon-reload
sudo systemctl enable --now stripe-webhook.service

# 6. Verify
systemctl status stripe-webhook
curl http://127.0.0.1:5060/health
```

## Stripe Dashboard setup

Once the service is running and exposed via cloudflared (e.g. `billing.orbi.frank.com`):

1. Go to Stripe Dashboard > Developers > Webhooks > Add endpoint
2. Endpoint URL: `https://billing.orbi.frank.com/webhook`
3. Select events:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Copy the signing secret into `/etc/orbi-brain/stripe.env` as `STRIPE_WEBHOOK_SECRET`
5. Restart the service: `sudo systemctl restart stripe-webhook`

## Create the eight products in Stripe

In Stripe Dashboard > Products, create eight recurring products — four tiers × two billing cycles. No setup fee (dropped 2026-05-27 — the onboarding wizard handles it).

| Tier | Monthly | Annual (15% off) | Notes |
|------|---------|------------------|-------|
| Small Business    | $99/mo  | $999/yr   | 500 chats/mo, 1 user, no phone |
| Medium Business   | $149/mo | $1,499/yr | 2,000 chats + 200 calls, 5 users |
| Large Business    | $249/mo | $2,499/yr | 10,000 chats + 1,000 calls, 15 users, 70B brain |
| Enterprise        | $399/mo | $3,999/yr | unlimited, custom integrations |

Copy each Price ID (starts with `price_`) into the matching `STRIPE_PRICE_*` slot in `/etc/orbi-brain/stripe.env`.

## Brain proxy

Customers' local Orbis call **`POST /v1/chat/completions`** on this server using their api_key as a Bearer token. The proxy:

1. Looks up the api_key → tier + active flag
2. Returns 402 if the subscription is inactive (cancelled / refunded / unpaid)
3. Returns 429 if they've hit their monthly chat cap
4. Otherwise forwards to HuggingFace Inference using **Frank's** HF token, picking the model from `TIER_TO_MODEL` (8B for Small/Medium, 70B for Large/Enterprise)
5. Records the usage and returns the OpenAI-shaped response

This closes the refund loophole: software is free, the brain (LLM access) is what customers actually pay for.

### Brain config in stripe.env:

```
HF_TOKEN=hf_...                    # huggingface.co/settings/tokens (Read + Inference)
HF_API_BASE=https://api-inference.huggingface.co/models   # only override to swap providers
BRAIN_TIMEOUT_S=60
```

### Usage endpoint

The customer dashboard can show usage by hitting `GET /api/brain/usage/<api_key>` — returns `{tier, period, used: {chats, calls, tokens_in, tokens_out}, cap: {...}}`.

## Frank-only admin endpoints

All require the `X-Admin-Token` header matching `ORBI_ADMIN_TOKEN`:

```bash
# List all customers
curl -H "X-Admin-Token: $TOKEN" https://billing.orbi.frank.com/api/admin/customers

# Force-activate (e.g. for manual demo install)
curl -X POST -H "X-Admin-Token: $TOKEN" \
  https://billing.orbi.frank.com/api/admin/activate/orbi_KEY...

# Force-deactivate
curl -X POST -H "X-Admin-Token: $TOKEN" \
  https://billing.orbi.frank.com/api/admin/deactivate/orbi_KEY...
```

## Customer install side

The customer install reads `api_key` from its `config.json` and pings:

```
GET https://billing.orbi.frank.com/api/active/<api_key>
```

Response on healthy account:
```json
{
  "active": true,
  "tier": "standard",
  "period_end": 1748793600,
  "business_name": "Joe's Pizza"
}
```

Response during grace period:
```json
{
  "active": true,
  "warning": "billing_issue",
  "grace_until": 1748793600,
  "tier": "standard"
}
```

Response when inactive:
```json
{
  "active": false,
  "tier": "standard"
}
```

The customer install shows the "billing issue" banner when `warning` is present.
When `active` is false, it shows a friendly "subscription paused — please contact owner" message.
