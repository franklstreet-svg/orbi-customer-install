# Orbi Web — Architecture

## The big picture

```
                     ┌─────────────────────────────────────┐
                     │  FRANK'S BRAIN MACHINE              │
                     │  (16GB Linux box, dedicated)        │
                     │                                     │
                     │  - 8B-13B LLM via llama-server      │
                     │  - HTTPS API endpoint                │
                     │  - Per-customer API keys             │
                     │  - Usage logging                     │
                     │  - Health endpoint                   │
                     └────────────────┬────────────────────┘
                                      │
                              Cloudflared tunnel
                              (HTTPS, encrypted)
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            │                         │                         │
            ▼                         ▼                         ▼
    ┌──────────────┐         ┌──────────────┐         ┌──────────────┐
    │ Customer A's │         │ Customer B's │         │ Customer C's │
    │   box        │         │   box        │         │   box        │
    │              │         │              │         │              │
    │ - Lean Orbi  │         │ - Lean Orbi  │         │ - Lean Orbi  │
    │ - Local 3B   │         │ - Local 3B   │         │ - Local 3B   │
    │ - Watchdog   │         │ - Watchdog   │         │ - Watchdog   │
    │ - Tunnel     │         │ - Tunnel     │         │ - Tunnel     │
    │ - Modules    │         │ - Modules    │         │ - Modules    │
    │ - Data folder│         │ - Data folder│         │ - Data folder│
    └──────┬───────┘         └──────┬───────┘         └──────┬───────┘
           │                        │                        │
       Cloudflared              Cloudflared              Cloudflared
       tunnel out               tunnel out               tunnel out
           │                        │                        │
           ▼                        ▼                        ▼
    ┌──────────────┐         ┌──────────────┐         ┌──────────────┐
    │ Public URL   │         │ Public URL   │         │ Public URL   │
    │ for          │         │ for          │         │ for          │
    │ visitors and │         │ visitors and │         │ visitors and │
    │ phone webhook│         │ phone webhook│         │ phone webhook│
    └──────┬───────┘         └──────┬───────┘         └──────┬───────┘
           │                        │                        │
    ┌──────┴───────┐         ┌──────┴───────┐         ┌──────┴───────┐
    │  Twilio      │         │  Twilio      │         │  Twilio      │
    │  phone #     │         │  phone #     │         │  phone #     │
    │  + PWA users │         │  + PWA users │         │  + PWA users │
    └──────────────┘         └──────────────┘         └──────────────┘
```

---

## Request flow — when a customer's customer asks something

```
1. Visitor opens PWA on their phone OR calls the business phone number
2. Request hits the customer's tunnel → customer's local Orbi
3. Orbi inspects the query:
   - Folder lookup needed?    → checks local data folders → returns exact answer
   - General chat / free-form? → goes to LLM tier loop
4. LLM tier loop (in order):
   a. Frank's brain (15s timeout)         → 95% of the time, answers here
   b. HuggingFace cloud (15s timeout)     → only if brain is down
   c. Local Llama-3.2-3B                  → only if internet is also down
5. Response flows back through tunnel to visitor / caller
6. If lead/order captured, written to customer's local "messages" folder
7. Push notification sent to owner's PWA
```

---

## Component breakdown

### 1. Brain Machine (Frank's centralized LLM)

- **Hardware:** 16GB Linux box, CPU-only (GPU added later for scale)
- **Software:** llama-server (llama.cpp) running Llama-3.1-8B Q6 (~6.5GB)
- **Exposure:** Cloudflared tunnel → stable HTTPS URL (e.g. `orbi-brain.frank.com`)
- **Auth:** per-customer API key in request header
- **Capacity:** 2-3 concurrent queries comfortably, queues beyond that
- **Cost:** ~$300-500 hardware (one-time), $0.50/mo electricity per active customer

### 2. Customer Install (their box)

Lives in `/opt/orbi/` on the customer's machine. Contents:

```
/opt/orbi/
├── orbi.py                 ← main service (web server)
├── watchdog.py             ← health monitor + restart daemon
├── modules/
│   ├── business_info.py    ← hours, services, menu, FAQ
│   ├── memory.py           ← short/mid/long-term conversation memory
│   ├── notes.py            ← arbitrary notes
│   └── messages.py         ← captured leads, voicemails, orders
├── data/
│   ├── business_info.json
│   ├── short_term.json
│   ├── mid_term.json
│   ├── long_term.json
│   ├── notes.json
│   └── messages.json
├── snapshots/              ← rotating backups (7 days)
├── llm_local/              ← Llama-3.2-3B for offline fallback
├── tunnel/                 ← Cloudflared config + binary
└── config.json             ← brain URL, API key, owner login hash, etc.
```

### 3. Public PWA

- Lives at the customer's tunnel URL (e.g. `joespizza.frank.com/chat`)
- No login required for public view
- Customers ask questions, place orders, request callbacks
- Captured info → customer's `messages` folder + push to owner
- Installable as an app via "Add to Home Screen"

### 4. Owner Dashboard

- Same URL with `/owner` (login required)
- Sees all messages, leads, call logs
- Edits business_info (hours, services, FAQ)
- Configures Orbi's personality / scope
- Sees usage stats

### 5. Watchdog

- Separate Python process, runs as systemd service
- Pings Orbi health endpoint every 30 seconds
- 3 failed pings → restart Orbi
- 3 failed restarts → rollback to last good snapshot
- Failed rollback → alert Frank via PWA push + email
- Daily snapshots at 3am, keep last 7
- Pre-update snapshot before any Frank-pushed update

### 6. Twilio Integration

- One phone number per customer ($1.15/mo)
- Voice webhook → tunnel URL → Orbi handles the call
- Orbi uses local TTS (Edge TTS) for voice
- Average call: 2 minutes, ~$0.04 in Twilio fees

### 7. Stripe Billing

- Customer pays via Stripe Checkout at signup
- Monthly subscription auto-renews
- Failed payment → 3-day grace → suspend service (display "billing issue, contact owner" on PWA)
- All managed via Stripe dashboard, no manual chasing

---

## Data ownership rules

| Data | Lives where | Backed up where |
|------|-------------|-----------------|
| Customer business info, leads, messages, notes | Customer's box only | Watchdog snapshots on same box |
| LLM model weights | Brain machine (Frank) AND customer's box (3B backup) | N/A — re-downloadable |
| LLM query content (transient) | Nowhere persistent | Brain logs only the timing, not content |
| Billing info | Stripe | Stripe |
| Owner login credentials | Customer's box (hashed) | Snapshots |
| API keys (customer → brain) | Customer's `config.json` (encrypted) AND Frank's brain DB | Snapshot + brain DB |

**Bedrock rule:** customer data never moves from customer's box. Brain processes queries in memory and discards. Phase 3 may add encrypted offsite backup as an OPT-IN feature.

---

## Failure modes and what happens

| What fails | What happens |
|------------|--------------|
| Orbi crashes on customer's box | Watchdog restarts within 30s, customer unaware |
| Orbi can't restart | Watchdog rolls back to last snapshot, customer unaware |
| Frank's brain machine down | Customer auto-routes to HuggingFace, slight quality shift, customer unaware |
| Internet down at customer's box | Local 3B answers, slower + lower quality, owner sees "offline mode" banner |
| Customer's box hardware fails | Phase 1: Frank manually restores from snapshot copy. Phase 3: encrypted offsite backup auto-restores. |
| Customer's tunnel disconnects | Watchdog restarts tunnel, customer unaware |
| HuggingFace down too | Falls to local 3B (third tier covers it) |
| Twilio down | Calls fail at carrier — this is outside our system, very rare |
| Stripe payment fails | 3-day grace, then suspend with friendly message to contact owner |

The whole architecture is built so that no single failure takes a customer offline. Every failure has an automatic fallback that the customer either doesn't notice or sees as a clearly labeled "degraded mode."
