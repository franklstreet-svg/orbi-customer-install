# Orbi Web

Packaged, sellable B2B product. Built on the Orby engine. Sold to small
businesses (restaurants, plumbers, salons, dentists, contractors, etc.)
as a one-stop AI receptionist, website chatbot, and personal assistant.

**Status:** Phase 1 build complete. Ready to install on the brain machine
and the first customer box.

---

## Read these first, in this order

1. [`SOURCE_OF_TRUTH.md`](SOURCE_OF_TRUTH.md) — the master reference. What we're
   building, why, what's in scope, key decisions.
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) — how the pieces fit together
   (brain + customer install + tunnels + Twilio + Stripe).
3. [`PHASE_1_BUILD.md`](PHASE_1_BUILD.md) — the 9-deliverable v1 plan.

The phase docs after that (`PHASE_2_GROWTH.md`, `PHASE_3_SCALE.md`,
`PHASE_4_PROFESSIONAL.md`) are the longer roadmap.

---

## Repo layout

```
orbi_web/
├── README.md                   ← you are here
├── SOURCE_OF_TRUTH.md          ← master reference
├── ARCHITECTURE.md             ← technical diagram + components
├── PRICING.md                  ← tiers, costs, revenue projections
├── MODULE_INVENTORY.md         ← what ships, what's add-on, what's archived
├── PHASE_1_BUILD.md            ← v1 sellable MVP
├── PHASE_2_GROWTH.md           ← after 5 customers
├── PHASE_3_SCALE.md            ← after 25 customers
├── PHASE_4_PROFESSIONAL.md     ← after 50 customers
│
├── brain/                      ← Frank's centralized LLM server
│   ├── brain_server.py         ← auth + logging proxy in front of llama-server
│   ├── llama-server.service    ← systemd unit for llama.cpp
│   ├── orbi-brain.service      ← systemd unit for the proxy
│   ├── brain.env.template      ← config template (chmod 600 in production)
│   ├── install_brain.sh        ← one-shot installer for the brain box
│   └── README.md
│
├── billing/                    ← Stripe webhook handler (lives on brain box)
│   ├── stripe_webhook.py
│   ├── stripe-webhook.service
│   ├── stripe.env.template
│   └── README.md
│
├── customer_install/           ← what gets installed on each customer's box
│   ├── orbi.py                 ← main Flask service
│   ├── llm_client.py           ← three-tier failover (brain → HF → local)
│   ├── prompts.py              ← system prompts (public + owner)
│   ├── auth.py                 ← owner login + sessions
│   ├── voice.py                ← Twilio voice receptionist
│   ├── config.json.template    ← per-install config
│   ├── requirements.txt
│   ├── install.sh              ← one-shot installer for customer box
│   ├── modules/                ← the 4 default modules
│   │   ├── business_info.py
│   │   ├── memory.py
│   │   ├── notes.py
│   │   └── messages.py
│   ├── data/                   ← JSON templates for each module's data
│   ├── static/                 ← public chat shell (PWA)
│   │   ├── chat.html
│   │   ├── chat.css
│   │   └── chat.js
│   └── README.md
│
├── owner_dashboard/            ← owner-side UI (served by orbi.py at /owner)
│   ├── login.html
│   ├── dashboard.html          ← 4 tabs: Messages, Ask Orbi, Business Info, Settings
│   ├── dashboard.css
│   └── dashboard.js
│
├── pwa/                        ← shared PWA assets (manifest, SW, icons)
│   ├── manifest.json
│   ├── service-worker.js
│   ├── install-prompt.js
│   ├── offline.html
│   └── icons/
│       ├── icon-192.png        ← built by generate_icons.py
│       ├── icon-512.png
│       ├── icon-maskable-512.png
│       ├── icon-favicon.png
│       ├── generate_icons.py
│       └── README.md
│
└── watchdog/                   ← self-healing daemon for customer boxes
    ├── watchdog.py
    ├── orbi.service            ← systemd unit for orbi.py
    ├── orbi-watchdog.service   ← systemd unit for the watchdog itself
    └── README.md
```

---

## The two installers (the "happy path")

There are exactly two `install.sh`-style scripts. You'll never have to
manually copy files around.

### 1. Brain machine (run once — Frank's centralized 16GB box)

```bash
sudo bash /home/frank/orbi_web/brain/install_brain.sh
```

Walks through model download (Llama-3.1-8B Q6, ~6.5GB), llama.cpp build,
systemd setup, prompts for admin token + Stripe keys + HF token. About
30-60 minutes start to finish.

Sets up three services on that box:
- `llama-server` — the actual LLM (port 11434, internal only)
- `orbi-brain` — auth + logging proxy (port 5070, exposed via cloudflared)
- `stripe-webhook` — billing event handler (port 5060, exposed via cloudflared)

### 2. Customer box (run for each new customer)

```bash
sudo bash /home/frank/orbi_web/customer_install/install.sh
```

Walks through owner credentials, brain URL + API key, HuggingFace token,
Twilio number, tunnel URL. About 10-15 minutes per customer.

Sets up two services on the customer box:
- `orbi` — the Flask service (port 5050, exposed via the customer's tunnel)
- `orbi-watchdog` — self-healing supervisor (no port, runs every 30s)

---

## How a new customer comes online (the end-to-end flow)

1. Customer pays via Stripe Checkout link.
2. Stripe sends a `checkout.session.completed` webhook to `billing.orbi.frank.com/webhook`.
3. `stripe_webhook.py` creates a customer record in `billing.db`, generates an `orbi_...` API key.
4. Frank reads the API key from the admin endpoint:
   ```bash
   curl -H "X-Admin-Token: $TOKEN" https://billing.orbi.frank.com/api/admin/customers | jq
   ```
5. Frank goes to the customer's location (or remotes into their box) and runs `install.sh`.
6. He pastes the API key, picks a tunnel subdomain, fills in business basics.
7. Customer's box pings `billing.orbi.frank.com/api/active/<key>` and confirms it's active.
8. Customer's `orbi.service` starts, exposed via their cloudflared tunnel.
9. Twilio webhook configured to point at `<tunnel>/voice/incoming`.
10. Done. Customer can chat at their tunnel URL, owner can log in at `<tunnel>/owner/login`,
    customers can call the phone number and talk to Orbi.

---

## Architecture in one diagram

```
                         FRANK'S BRAIN BOX
                         (16GB Linux box)
                    ┌─────────────────────────┐
                    │ llama-server  (11434)   │
                    │   ↑                     │
                    │ brain_server  (5070) ───┼──→ orbi-brain.frank.com
                    │                         │
                    │ stripe_webhook (5060) ──┼──→ billing.orbi.frank.com
                    └─────────────────────────┘
                                 ↑
                                 │ HTTPS
                                 │ (per-customer API key)
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
   ┌────────────┐           ┌────────────┐           ┌────────────┐
   │ Customer A │           │ Customer B │           │ Customer C │
   │ orbi.py    │           │ orbi.py    │           │ orbi.py    │
   │ port 5050  │           │ port 5050  │           │ port 5050  │
   │            │           │            │           │            │
   │ watchdog   │           │ watchdog   │           │ watchdog   │
   │            │           │            │           │            │
   │ /opt/orbi/ │           │ /opt/orbi/ │           │ /opt/orbi/ │
   │ data/      │           │ data/      │           │ data/      │
   └─────┬──────┘           └─────┬──────┘           └─────┬──────┘
         │                        │                        │
   cloudflared              cloudflared              cloudflared
         │                        │                        │
   ┌──────────────┐         ┌──────────────┐         ┌──────────────┐
   │  PWA app +   │         │  PWA app +   │         │  PWA app +   │
   │  Twilio call │         │  Twilio call │         │  Twilio call │
   └──────────────┘         └──────────────┘         └──────────────┘
```

---

## Tomorrow's plan

1. Back up everything important on the second 16GB box.
2. Install Ubuntu 24.04 LTS Server fresh.
3. Copy `orbi_web/` onto the new box (USB stick, rsync, scp, whatever).
4. Run `sudo bash /home/frank/orbi_web/brain/install_brain.sh`.
5. Set up two cloudflared tunnels (one for brain, one for billing).
6. Run a curl smoke test from another machine.
7. Done. The brain is live.

---

## Important constraints (don't violate these)

These come from `SOURCE_OF_TRUTH.md` and exist for good reasons.

- **Personal Orby (`/home/frank/orby_5050/`) stays untouched** — it's the engine
  we copy patterns FROM, not a deployment target.
- **The Relay (`bridge_rebuild_20260407`, port 8088) is untouchable.**
- **Customer data never leaves the customer's box.** The brain processes
  queries in memory and discards.
- **Brain stays lean.** No bloating the system prompt with every professional
  vertical. Use targeted RAG for specialized knowledge in Phase 3.
- **Default install ships 4 modules.** Adding a 5th default needs a strong
  reason. New modules ship as paid add-ons.
- **Watchdog must run on every customer install.** Self-healing isn't optional.

---

## When in doubt

Read `SOURCE_OF_TRUTH.md`. If something contradicts it, either the
contradiction is wrong, or `SOURCE_OF_TRUTH.md` needs updating. Don't let
both stay out of sync.
