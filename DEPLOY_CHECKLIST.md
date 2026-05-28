# Orby Ship Checklist — $0 path to first paying customer

You're on bootstrap budget. Every step here uses what you already have. No new monthly fees.

---

## ⚡ Pre-flight (5 min)

- [ ] Confirm Orby is running on port 5050: `curl http://127.0.0.1:5050/health` returns `200`
- [ ] Pull latest from main: `cd /home/frank/orbi_web && git pull`
- [ ] Capability tests green: `python3 customer_install/test_capabilities.py --skip-fire` returns `0 fail`

---

## 1️⃣ Stripe — create the 8 products (~30 min, $0)

You have an existing Stripe account. Don't create a new one. Don't connect a new business — use the same legal entity (FST LLC) that's already verified.

### Steps

1. Sign in: https://dashboard.stripe.com
2. Go to **Products** → **+ Add product**
3. Create these 8 products. Copy each Price ID into the table at the bottom.

| Product name | Description (paste exactly) | Pricing model | Price | Billing | Other |
|---|---|---|---|---|---|
| **Orby Small** | AI receptionist + website chat + personal assistant for solo owners and small shops. Up to 500 website chats/mo, 1 user, all PA features. No phone receptionist on this tier. | Recurring | **$99.00 USD** | Monthly | — |
| **Orby Small Annual** | Same as Orby Small, paid yearly. Save 15% off the monthly rate. | Recurring | **$999.00 USD** | Yearly | — |
| **Orby Medium** | Everything in Small + phone receptionist (200 calls/mo), 2,000 chats/mo, 5 staff, Gmail/Outlook/Stripe/Calendar integrations. | Recurring | **$149.00 USD** | Monthly | — |
| **Orby Medium Annual** | Same as Orby Medium, paid yearly. Save 15%. | Recurring | **$1,499.00 USD** | Yearly | — |
| **Orby Large** | Everything in Medium + 10,000 chats/mo, 1,000 calls/mo, 15 staff, all 18 connectors, style-learning auto-reply, Pro 70B brain. | Recurring | **$249.00 USD** | Monthly | — |
| **Orby Large Annual** | Same as Orby Large, paid yearly. Save 15%. | Recurring | **$2,499.00 USD** | Yearly | — |
| **Orby Enterprise** | Multi-location chains. Unlimited chats + calls, unlimited staff, dedicated brain option, priority support, custom integrations. Contact for custom pricing. | Recurring | **$399.00 USD** | Monthly | — |
| **Orby Enterprise Annual** | Same as Enterprise, paid yearly. Save 15%. | Recurring | **$3,999.00 USD** | Yearly | — |

4. After creating, click each product → click its price → **copy the Price ID** (`price_1Ab...`). Paste into the table below.

### Copy these into `/etc/orbi-brain/stripe.env` when the brain proxy goes live

```
STRIPE_PRICE_SMALL_MO=price_______________________
STRIPE_PRICE_SMALL_YR=price_______________________
STRIPE_PRICE_MEDIUM_MO=price_______________________
STRIPE_PRICE_MEDIUM_YR=price_______________________
STRIPE_PRICE_LARGE_MO=price_______________________
STRIPE_PRICE_LARGE_YR=price_______________________
STRIPE_PRICE_ENT_MO=price_______________________
STRIPE_PRICE_ENT_YR=price_______________________
```

### Don't do these things in Stripe yet

- **Don't create a setup-fee product** — we don't charge one anymore
- **Don't enable "trial period"** on any of these — we don't offer trials
- **Don't enable tax collection** until you check with your accountant on your nexus
- **Don't add a coupon for the annual 15% off** — the annual price already has it baked in (just label it "save 15%" on the marketing page)

---

## 2️⃣ Cloudflare Tunnel — expose the brain proxy ($0)

You already have `cloudflared` installed at `/usr/local/bin/cloudflared`. Your existing tunnel on port 5051 (for the twickell.com chat widget) keeps running undisturbed.

### Steps

1. Check that you have a Cloudflare account with twickell.com (or whatever domain you use): https://dash.cloudflare.com
2. **Login cloudflared** (one-time, opens browser):
   ```
   cloudflared tunnel login
   ```
3. **Create a named tunnel for the brain proxy**:
   ```
   cloudflared tunnel create orby-brain
   ```
   Cloudflare prints a tunnel UUID — copy it.
4. **Point a DNS record** at it. In Cloudflare dashboard → DNS → Add record:
   - Type: `CNAME`
   - Name: `brain` (so the full hostname is `brain.twickell.com` — or use any subdomain you want)
   - Target: `<UUID>.cfargotunnel.com`
   - Proxy status: **Proxied** (orange cloud ON)
5. **Create a config file** at `~/.cloudflared/orby-brain.yml`:
   ```yaml
   tunnel: <UUID>
   credentials-file: /home/frank/.cloudflared/<UUID>.json
   ingress:
     - hostname: brain.twickell.com
       service: http://localhost:5060
     - service: http_status:404
   ```
6. **Run the tunnel** (test first):
   ```
   cloudflared tunnel --config ~/.cloudflared/orby-brain.yml run
   ```
7. **Verify**: in another shell, `curl https://brain.twickell.com/health` should return the same `200 OK` as `http://127.0.0.1:5060/health`.
8. **Make it survive reboots** — create a systemd unit:
   ```
   sudo cloudflared service install --config ~/.cloudflared/orby-brain.yml
   sudo systemctl enable cloudflared
   sudo systemctl start cloudflared
   ```

### Common gotcha

- If `cloudflared tunnel login` opens a browser to the wrong Cloudflare account, log out at https://dash.cloudflare.com first.
- Don't use `cloudflared tunnel --url http://localhost:5060` (the quick mode) — it generates a random ephemeral URL that changes on every restart. Use a NAMED tunnel as above.

---

## 3️⃣ Brain proxy + billing webhook — go live ($0)

The code is already in `/home/frank/orbi_web/billing/stripe_webhook.py`. It needs to run as a service on your machine, exposed via the tunnel from step 2.

### Steps

1. **Create the runtime dir**:
   ```
   sudo mkdir -p /opt/orbi-brain /etc/orbi-brain
   sudo useradd -r -s /usr/sbin/nologin -d /opt/orbi-brain orbi-brain 2>/dev/null || true
   sudo cp /home/frank/orbi_web/billing/stripe_webhook.py /opt/orbi-brain/
   sudo cp /home/frank/orbi_web/billing/stripe-webhook.service /etc/systemd/system/
   sudo cp /home/frank/orbi_web/billing/stripe.env.template /etc/orbi-brain/stripe.env
   ```
2. **Fill in `/etc/orbi-brain/stripe.env`** with:
   - `STRIPE_API_KEY` — from Stripe dashboard → Developers → API keys (test key first, switch to live when ready)
   - `STRIPE_WEBHOOK_SECRET` — created in step 4 below
   - `ORBI_ADMIN_TOKEN` — generate: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
   - 8 `STRIPE_PRICE_*` IDs from step 1
   - `HF_TOKEN` — from huggingface.co/settings/tokens (free tier OK for first 5-10 customers)
   - `RESEND_API_KEY` — skip for now. Without it, install tokens get logged to journald and you can copy-paste manually for the first customer. Sign up at resend.com when you want auto-email (free 3,000/mo).
3. **Lock down the env file**:
   ```
   sudo chmod 600 /etc/orbi-brain/stripe.env
   sudo chown orbi-brain:orbi-brain /etc/orbi-brain/stripe.env
   sudo chown -R orbi-brain:orbi-brain /opt/orbi-brain
   sudo pip3 install flask stripe
   sudo systemctl daemon-reload
   sudo systemctl enable --now stripe-webhook
   ```
4. **Stripe dashboard → Developers → Webhooks → Add endpoint**:
   - Endpoint URL: `https://brain.twickell.com/webhook`
   - Events to send:
     - `checkout.session.completed`
     - `customer.subscription.created`
     - `customer.subscription.updated`
     - `customer.subscription.deleted`
     - `invoice.payment_failed`
   - After saving, copy the **Signing secret** (`whsec_...`) and paste into `STRIPE_WEBHOOK_SECRET` in `/etc/orbi-brain/stripe.env`
   - Restart: `sudo systemctl restart stripe-webhook`
5. **Verify**:
   - `curl https://brain.twickell.com/health` → `{"status":"ok","service":"stripe-webhook"}`
   - In Stripe dashboard → Webhooks → click your endpoint → **Send test webhook** (`checkout.session.completed`) — it should show "Succeeded"

---

## 4️⃣ First-customer self-test (~1 hour, $0 — Stripe test mode)

Before charging a real customer, run yourself through the buy flow using Stripe TEST mode.

1. Switch Stripe dashboard to **Test mode** (toggle top-left)
2. Create the 8 test-mode products (same as step 1 but in test mode). Update `stripe.env` with test mode Price IDs.
3. Use Stripe's **Payment Link** feature on one of the products (Small Monthly) → copy the link
4. Open in incognito browser → checkout with test card `4242 4242 4242 4242` (any future expiry, any CVC)
5. After checkout, check `journalctl -u stripe-webhook -f` — you should see the install-token email logged
6. Copy the token, simulate the installer: `curl https://brain.twickell.com/api/verify/<token>` → returns the install record

If all that works, you have a complete self-serve buy → install flow.

---

## 5️⃣ Switch to live (10 min, $0)

Once test mode works end-to-end:

1. Switch Stripe dashboard to **Live mode**
2. Confirm the 8 LIVE products exist (you have to re-create them in live mode — they don't auto-sync from test)
3. Update `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, and all 8 `STRIPE_PRICE_*` IDs in `stripe.env`
4. `sudo systemctl restart stripe-webhook`
5. Generate a real Payment Link for the first customer

---

## Money facts to know

| Cost | When it kicks in |
|---|---|
| **Stripe fees**: ~2.9% + $0.30 per transaction | On every customer charge |
| **HuggingFace free tier**: usable until ~5-10 customers | Then $9/mo for Pro covers maybe 50-100 customers |
| **Cloudflare Tunnel**: $0 free forever | Never charges |
| **Your machine's electricity**: ~$5-15/mo if running 24/7 | Already in your bill |
| **Resend**: $0 for first 3,000 emails/month | Past 3k/mo, ~$20/mo |
| **Domain renewal (twickell.com)**: ~$15/year | Already in your bill |

**You can ship today with zero new outgoing spend.** First customer's $99 covers HuggingFace Pro the moment you outgrow free tier.

---

## When something goes wrong

- **Tunnel won't connect**: check `journalctl -u cloudflared -f` for errors
- **Webhook 401**: signing secret mismatch — recopy from Stripe dashboard
- **Brain proxy 502**: HF token bad or HF down — check `journalctl -u stripe-webhook -f`
- **Install token expired**: tokens are single-use; mint a new one via Stripe webhook resend

---

## Marketing site sweep (separate task — needs you to confirm where the source lives)

twickell.com is hosted on Vercel. The local source isn't in obvious places — only backups exist:
- `/home/frank/orbi_web_repo_backup_20260525_172748/index.html` (last touched 5/25)
- `/home/frank/BACKUP_BEFORE_CLEANUP_20260522/twickell_deploy/website/business.html`

Once you tell me where you edit the live site (which git repo / which folder), I can:
- Rename every "Orbi" → "Orby" in customer-facing copy
- Update pricing page to current $99 / $149 / $249 / $399 tiers (no setup fee, annual 15% off)
- Sweep legal docs (privacy, terms, refund) for stale tier names
- Verify the "Buy" button leads to a real Stripe Checkout once the test-mode flow works
