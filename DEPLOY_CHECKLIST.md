# Orby Ship Checklist — $0 path to first paying customer

Bootstrap budget. Every step uses what you already have.

**As of v0.2.0 the chain is end-to-end ready** — once these 6 setup steps are done, a stranger can buy → install → use Orby with zero technical help from you.

---

## ⚡ Pre-flight (5 min)

- [ ] Confirm your dev Orby is running on port 5050: `curl http://127.0.0.1:5050/health` returns `200`
- [ ] Pull latest from main: `cd /home/frank/orbi_web && git pull`
- [ ] Tests green: `python3 customer_install/test_capabilities.py --skip-fire` → `0 fail`
- [ ] Latest GitHub release built: open https://github.com/franklstreet-svg/orbi-customer-install/releases — should show `v0.2.0` with .exe / .pkg / .sh artifacts (~110-165MB each)

---

## 1️⃣ Stripe — create 8 products (~30 min, $0)

Use your existing Stripe account.

1. Sign in: https://dashboard.stripe.com
2. **Test mode first** (toggle top-left) — we'll go live after the dry-run
3. Products → + Add product → create these 8:

| Product name | Description | Price | Billing |
|---|---|---|---|
| **Orby Small** | AI receptionist + website chat + personal assistant for solo owners. 500 chats/mo, 1 user, all PA features. No phone receptionist on this tier. | **$99 USD** | Monthly |
| **Orby Small Annual** | Same as Orby Small, paid yearly. Save 15%. | **$999 USD** | Yearly |
| **Orby Medium** | Small + phone receptionist (200 calls/mo), 2,000 chats/mo, 5 staff. | **$149 USD** | Monthly |
| **Orby Medium Annual** | Same as Medium, paid yearly. | **$1,499 USD** | Yearly |
| **Orby Large** | Medium + 10,000 chats/mo, 1,000 calls/mo, 15 staff, all 18 connectors, Pro 70B brain. | **$249 USD** | Monthly |
| **Orby Large Annual** | Same as Large, paid yearly. | **$2,499 USD** | Yearly |
| **Orby Enterprise** | Multi-location. Unlimited chats + calls, unlimited staff, custom integrations. | **$399 USD** | Monthly |
| **Orby Enterprise Annual** | Same as Enterprise, paid yearly. | **$3,999 USD** | Yearly |

4. After creating, copy each `price_xxx` ID → paste into `stripe.env` (step 4 below).

---

## 2️⃣ Cloudflare Tunnel — expose brain server ($0)

You already have `cloudflared` at `/usr/local/bin/cloudflared`. Your existing tunnel on port 5051 stays untouched.

```bash
# One-time login
cloudflared tunnel login

# Create the brain tunnel
cloudflared tunnel create orby-brain
# (prints a UUID — copy it)

# DNS: Cloudflare dashboard → twickell.com → DNS → Add record:
#   Type: CNAME
#   Name: brain
#   Target: <UUID>.cfargotunnel.com
#   Proxy: ON (orange cloud)

# Config file
mkdir -p ~/.cloudflared
cat > ~/.cloudflared/orby-brain.yml <<EOF
tunnel: <UUID>
credentials-file: /home/frank/.cloudflared/<UUID>.json
ingress:
  - hostname: brain.twickell.com
    service: http://localhost:5060
  - service: http_status:404
EOF

# Test
cloudflared tunnel --config ~/.cloudflared/orby-brain.yml run
# In another shell: curl https://brain.twickell.com/health → should return JSON

# Make it survive reboots
sudo cloudflared service install --config ~/.cloudflared/orby-brain.yml
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

---

## 3️⃣ Twilio — fund + grab credentials (~15 min, ~$20 initial deposit)

This is the only step that costs money on day 1.

1. Sign up: https://twilio.com (if you don't have an account)
2. Fund with $20 (covers ~10 customers × $1 number for a month + minutes)
3. Console → Account Info → copy **Account SID** and **Auth Token**
4. Paste into `stripe.env` (step 4)

**Don't buy any numbers manually.** The brain server auto-buys one per customer on Stripe checkout. Numbers are released back to Twilio when the customer cancels (no $1/mo charge for dead accounts).

---

## 4️⃣ Brain server env file — fill all credentials (~20 min)

```bash
sudo mkdir -p /opt/orbi-brain /etc/orbi-brain
sudo useradd -r -s /usr/sbin/nologin -d /opt/orbi-brain orbi-brain 2>/dev/null || true
sudo cp /home/frank/orbi_web/billing/stripe_webhook.py /opt/orbi-brain/
sudo cp /home/frank/orbi_web/billing/twilio_central.py /opt/orbi-brain/
sudo cp /home/frank/orbi_web/billing/stripe-webhook.service /etc/systemd/system/
sudo cp /home/frank/orbi_web/billing/stripe.env.template /etc/orbi-brain/stripe.env
```

Now edit `/etc/orbi-brain/stripe.env` with REAL values:

```bash
sudo nano /etc/orbi-brain/stripe.env
```

Fill in:
- `STRIPE_API_KEY` — Stripe Dashboard → Developers → API keys (test key first)
- `STRIPE_WEBHOOK_SECRET` — created in step 5 below; leave empty for now
- `ORBI_ADMIN_TOKEN` — generate: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- 8 `STRIPE_PRICE_*` IDs from step 1
- `HF_TOKEN` — huggingface.co/settings/tokens (free tier OK for first 5-10 customers)
- `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` from step 3
- `TWILIO_VOICE_WEBHOOK_BASE=https://brain.twickell.com`
- `ORBI_SMTP_HOST=smtp.mail.yahoo.com`
- `ORBI_SMTP_USER=franklstreet@yahoo.com`
- `ORBI_SMTP_PASSWORD` — your Yahoo App Password from earlier (the same 16-char one Orby uses for inbox triage)
- `ORBI_FROM_EMAIL=Orby <franklstreet@yahoo.com>`
- `ORBI_BRAIN_INBOX=/opt/orbi-brain/fleet_inbox.json`

Lock it down + start the service:
```bash
sudo chmod 600 /etc/orbi-brain/stripe.env
sudo chown orbi-brain:orbi-brain /etc/orbi-brain/stripe.env
sudo chown -R orbi-brain:orbi-brain /opt/orbi-brain
sudo pip3 install flask stripe
sudo systemctl daemon-reload
sudo systemctl enable --now stripe-webhook
```

Verify:
```bash
curl https://brain.twickell.com/health   # → {"status":"ok","service":"stripe-webhook"}
```

---

## 5️⃣ Stripe webhook — wire it up

Stripe Dashboard → Developers → Webhooks → **Add endpoint**:

- URL: `https://brain.twickell.com/webhook`
- Events:
  - `checkout.session.completed`
  - `customer.subscription.created`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_failed`
- Save → copy **Signing secret** (`whsec_...`)
- Paste into `STRIPE_WEBHOOK_SECRET` in `/etc/orbi-brain/stripe.env`
- Restart: `sudo systemctl restart stripe-webhook`

Test from Stripe dashboard → Webhooks → click the endpoint → **Send test webhook** (`checkout.session.completed`) → should show "Succeeded".

---

## 6️⃣ End-to-end test (~15 min)

In Stripe **TEST mode**:

```bash
cd /home/frank/orbi_web
python3 billing/end_to_end_test.py \
    --url https://brain.twickell.com \
    --secret YOUR_WEBHOOK_SIGNING_SECRET \
    --admin-token YOUR_ORBI_ADMIN_TOKEN
```

Should report `8+ pass / 0 fail`. If anything fails, the output tells you what.

Then do a real-flow test:

1. Stripe Dashboard → Products → **Orby Small** → **Create payment link**
2. Open the payment link in incognito browser
3. Pay with test card `4242 4242 4242 4242` (any future expiry, any CVC)
4. Check `journalctl -u stripe-webhook -f` — should see:
   - `[checkout] new customer test@example.com on tier small...`
   - `[twilio] ...` (or `skipped — credentials not set` if you haven't added Twilio yet)
   - `[email] SMTP OK for test@example.com via smtp.mail.yahoo.com`
5. Check the email you used — should land in your inbox within 30s with the magic link
6. Click magic link → twickell.com/install?t=... opens → token auto-copies
7. Click **Download Windows installer** → orbi-installer.exe lands
8. Open https://brain.twickell.com/admin?token=YOUR_ORBI_ADMIN_TOKEN → customer shows in fleet (never_seen status until they install)

If all that works, you have a complete buy-flow without spending a real dollar.

---

## 7️⃣ Switch to LIVE (~10 min)

Once test mode works end-to-end:

1. Stripe → toggle to **Live mode**
2. **Re-create all 8 products in live mode** (they don't sync from test)
3. Update `stripe.env`:
   - `STRIPE_API_KEY` → live key (`sk_live_...`)
   - `STRIPE_WEBHOOK_SECRET` → live webhook secret
   - 8 `STRIPE_PRICE_*` → live price IDs
4. `sudo systemctl restart stripe-webhook`
5. Create one **Payment Link** per tier → put those URLs on twickell.com "Get Started" buttons (or use Stripe Checkout's embedded mode for inline checkout)

---

## 8️⃣ Update marketing site Buy buttons (~10 min)

Today the Buy buttons on twickell.com are `mailto:` links. Once you have Stripe Payment Links, swap them:

1. Edit `/home/frank/twickell_live/website/index.html` and `orbi.html`
2. Find the 4 `<a href="mailto:orbiaisolutions@gmail.com..."` buttons
3. Replace each with the corresponding Stripe Payment Link URL
4. `git add . && git commit -m "Wire Stripe Payment Links" && git push`
5. Vercel auto-deploys in ~30s

---

## Money facts

| Cost | When |
|---|---|
| **Stripe fees**: ~2.9% + $0.30 per transaction | Every charge |
| **HuggingFace free tier**: free to ~5-10 customers | Then $9/mo Pro covers ~50-100 customers |
| **Cloudflare Tunnel**: $0 forever | — |
| **Twilio numbers**: ~$1/mo per active customer | Per Medium/Large/Enterprise customer (Small skips phone) |
| **Twilio voice**: ~$0.013/min inbound + ~$0.0085/min TTS | Per call |
| **Resend (optional)**: $0 free tier (3k/mo emails) | Only if you outgrow Yahoo SMTP |
| **Your machine electricity**: ~$5-15/mo (already paid) | — |
| **twickell.com renewal**: ~$15/year (already paid) | — |

**Net cost to ship today: $20 (initial Twilio deposit).** First customer's $99 pays it back in week 1.

---

## When something breaks

- **Brain server 500s** — `journalctl -u stripe-webhook -f`
- **Webhook 401** — signing secret mismatch — recopy from Stripe dashboard
- **Cloudflared down** — `sudo systemctl status cloudflared` → restart if needed
- **Customer install fails** — they email you the install token; you can curl `/api/verify/<token>` from the brain server to see what's there
- **Fleet shows everyone dark** — your machine's internet died or cloudflared crashed; restart cloudflared
- **Twilio call rings forever** — check `/admin` dashboard; customer is probably dark, brain falls back to voicemail TwiML automatically

---

## After the first customer is live

Things you'll probably want next (not blockers, but real once you have 2-3 customers):

- **Named Cloudflare tunnels** per customer (stable subdomains instead of ephemeral trycloudflare URLs)
- **Migrate brain off your home machine** → $10/mo VPS (paid for by customer #1's subscription)
- **HuggingFace Pro** — $9/mo, lifts the rate limits (handles ~50-100 customers)
- **Customer self-service portal** — change tier, update billing, see usage
- **Stripe Tax** automatic tax collection (you'll need to register in NV first)
