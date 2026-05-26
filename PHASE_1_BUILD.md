# Phase 1 — Sellable v1

**Goal:** A product Frank can sell to the first paying customer.
**Estimated time:** 3 weeks of focused work (Stripe + Twilio accounts already exist, saves ~1 week of setup).
**Done when:** Frank has installed Orbi at one paying customer and it runs reliably for 30 days.

---

## Phase 1 deliverables (the 9 things)

### 1. Brain Machine — set up Frank's second 16GB box

**What:** Wipe the second 16GB box, install Linux, run llama-server with Llama-3.1-8B-Q6.

**Steps:**
- Wipe and install Ubuntu 24.04 LTS Server
- Install llama.cpp + download Llama-3.1-8B-Instruct-Q6_K
- Configure llama-server: port 11434, ctx 4096, host 0.0.0.0
- Add basic API key auth (simple header check, not full OAuth yet)
- Set up systemd service so it starts on boot and auto-restarts on crash
- Verify with curl from another machine on Frank's network

**Time:** 2 days

**Done when:** Frank can curl the brain from his laptop and get a valid Llama response.

---

### 2. Cloudflared tunnel for the brain

**What:** Make the brain reachable from the internet at a stable URL.

**Steps:**
- Register a domain (or use a subdomain Frank already owns)
- Install cloudflared on the brain machine
- Create a tunnel pointing to localhost:11434
- Configure DNS in Cloudflare dashboard
- Verify: curl from outside Frank's network hits the brain successfully
- Document the stable URL (e.g. `orbi-brain.frank.com`)

**Time:** 4 hours

**Done when:** Public HTTPS URL returns Llama responses with valid API key.

---

### 3. Lean Customer Install — `/opt/orbi/`

**What:** Strip down the existing Orby codebase to just the 4 default modules and the chat shell.

**Steps:**
- Copy `/home/frank/orby_5050/website_server.py` → `/home/frank/orbi_web/customer_install/orbi.py`
- Remove all non-default modules (chef, fitness, mood, etc.)
- Keep: business_info (rename from `profile.py`), memory, notes, messages
- Strip out the LLM tier loop's local-only paths
- Reconfigure LLM tier to: brain (primary) → HuggingFace (failover) → local Llama-3.2-3B (offline)
- Add config.json reader for brain URL, API key, owner credentials
- Add health endpoint (`/health`) for the watchdog
- Test on Frank's laptop, then on a fresh VM to verify clean install

**Time:** 1 week

**Done when:** Fresh Ubuntu VM with `/opt/orbi/` runs, answers chat queries through brain via API.

---

### 4. PWA — Public chat view

**What:** Add the manifest + service worker so the chat UI is installable as an app.

**Steps:**
- Take existing `home.html` chat UI, strip to just the chat panel (no module tiles, no owner controls)
- Add `manifest.json` (~20 lines: app name, icon, theme color, display: standalone)
- Add `service-worker.js` (~30 lines: cache shell, offline fallback page)
- Add `<link rel="manifest">` and service worker registration to the page
- Design 3 icon sizes (192x192, 512x512, maskable)
- Test "Add to Home Screen" on iPhone and Android
- Make it the default route at `/` for unauthenticated visitors

**Time:** 2 days

**Done when:** Frank installs Orbi on his phone, opens it, chats with it.

---

### 5. Owner Dashboard — `/owner`

**What:** Web UI behind a login where the owner sees messages, leads, and edits business info.

**Steps:**
- Add login endpoint (email + password, bcrypt hash stored in `data/config.json`)
- Add session cookie / JWT
- Build dashboard page with three tabs:
  - **Messages** — list of captured leads, voicemails, orders (newest first)
  - **Business Info** — editable form for hours, services, FAQ, prices
  - **Settings** — Orbi's personality, scope, what topics to discuss/avoid
- Wire each tab to the relevant data file
- Add "Logout" button
- Add basic responsive CSS so it works on phone too

**Time:** 1 week

**Done when:** Frank can log in on his phone, see messages, edit hours.

---

### 6. Twilio phone receptionist

**What:** When the business's phone number rings, Twilio routes it to Orbi who answers in voice.

**Frank already has:** Twilio account set up. Saves ~half a day of account/billing setup.

**Steps:**
- Buy a Twilio phone number through existing account ($1.15/mo)
- Configure voice webhook → customer's tunnel URL `/voice/incoming`
- Add `/voice/incoming` endpoint in `orbi.py` that returns TwiML
- Use Twilio Voice Streams to send audio to Orbi
- Orbi transcribes (Whisper or Twilio's built-in STT), feeds to brain, generates response
- Use Edge TTS (already in Orby) for voice output
- Test by calling the number, having a 1-minute conversation, hanging up
- Verify the call was logged to `messages.json`

**Time:** 4-5 days (was 1 week)

**Done when:** Frank calls the test number, has a real conversation with Orbi, message appears in dashboard.

---

### 7. Watchdog (auto-restart + snapshot rollback)

**What:** Separate process that monitors Orbi health and self-heals.

**Steps:**
- Write `watchdog.py` (~200 lines)
- Ping `http://localhost:5050/health` every 30 seconds
- 3 fails → restart Orbi via systemctl
- 3 restart fails → restore last snapshot
- Snapshot daily at 3am, keep last 7 (tar + rotate)
- Snapshot before any update
- Log all actions to `watchdog.log`
- Send PWA push notification on rollback or unrecoverable failure
- Set up as systemd service so it starts on boot

**Time:** 2 days

**Done when:** Frank kills the Orbi process manually, watchdog restarts it within 60 seconds.

---

### 8. Stripe billing

**What:** Recurring monthly billing for the customer.

**Frank already has:** Stripe account set up. Saves account creation and verification time.

**Steps:**
- Create 3 products in existing Stripe account: Chat Only ($79), Standard ($149), Local-Only Premium ($179)
- Create Stripe Checkout link per tier
- Set up webhook endpoint on Frank's brain machine
- After successful payment, webhook activates customer's install (sets `config.json` "active": true)
- On failed payment: webhook → after 3 days grace → set "active": false → PWA shows "billing issue" banner
- Manual cancel: customer emails Frank, Frank cancels in Stripe dashboard

**Time:** Half a day (was 1 day)

**Done when:** Frank can send a Stripe link, customer pays, his install auto-activates.

---

### 9. LLM failover (already exists, needs configuration)

**What:** The tier loop already exists in `website_server.py`. Reconfigure it for the new architecture.

**Steps:**
- Edit the tier list in `orbi.py`:
  - Tier 1: Frank's brain (HTTPS, 15s timeout)
  - Tier 2: HuggingFace Inference API (15s timeout)
  - Tier 3: Local Llama-3.2-3B
- Remove all other tiers (Groq, Anthropic, OpenAI, etc.)
- Add a small banner state: when tier 2 or 3 is used, set a flag the dashboard can show
- Test by killing the brain → verify HuggingFace answers
- Test by killing internet → verify local 3B answers

**Time:** 4 hours (mostly testing)

**Done when:** All three tiers verified working independently.

---

## Phase 1 sequencing

Tasks 1, 2, 3, 4 can be done in parallel as available. Tasks 5, 6, 7, 8, 9 depend on 3 being done first.

**Suggested order:**

1. Brain machine + tunnel (parallel) — week 1
2. Lean customer install — week 1-2
3. PWA + Owner dashboard (parallel) — week 2-3
4. Watchdog + Stripe billing (parallel) — week 3
5. Twilio receptionist — week 3-4
6. LLM failover config + end-to-end testing — week 4

---

## Phase 1 acceptance test

Before declaring Phase 1 done, run this end-to-end test:

1. Frank installs Orbi on a fresh VM as if it's a new customer
2. Frank fills in business info (fake restaurant: "Joe's Pizza")
3. Frank opens the PWA on his phone, chats with Orbi — works ✓
4. Frank's brother (or anyone) calls the Twilio number, has a conversation, hangs up — works ✓
5. Message appears in dashboard — works ✓
6. Frank kills the Orbi process on the VM — watchdog restarts it within 60s — works ✓
7. Frank disconnects the VM from internet — local 3B answers chat queries — works ✓
8. Frank reconnects internet — back to brain — works ✓
9. Stripe test payment activates the install — works ✓

If all 9 pass, Phase 1 is done. Find a real customer.

---

## What Phase 1 is NOT

- Not pretty. Phase 1 is functional, not polished. UI polish is Phase 2.
- Not multi-user. Owner only. Staff logins are Phase 2.
- Not auto-onboarding. Frank does each install personally for the first 5 customers.
- Not email notifications. PWA push only.
- Not encrypted offsite backup. Watchdog rollback is enough.
- Not integrations with anything. No Google Calendar, no Quickbooks. Phase 3.

If a Phase 1 task starts pulling in Phase 2 features, STOP and add it to Phase 2.
