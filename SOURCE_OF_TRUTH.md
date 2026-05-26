# Orbi Web — Source of Truth

**Last updated:** 2026-05-24
**Owner:** Frank Street
**Status:** Pre-build (blueprint complete, ready for Phase 1)

---

## What we're building

**Orbi Web** is a packaged, sellable B2B product built on the Orby AI engine.

Small businesses (restaurants, plumbers, salons, dentists, contractors, etc.) install Orbi on their own computer. Orbi acts as:

1. **A 24/7 AI receptionist** — answers their phone, takes messages, captures leads
2. **A website chatbot** — answers customer questions instantly on their site
3. **A general AI assistant** — like ChatGPT, but knows their specific business
4. **A mobile app for owner and customers** — installable as a PWA (no App Store needed)

One product. One monthly fee. Replaces 3-5 tools the business is probably already paying for separately.

---

## Why this product

- Frank already built the entire Orby engine (modules, dispatcher, LLM tier loop, anti-hallucination guards, memory tiers, UI shell). Orbi Web is a packaged spin of that engine, not a rebuild.
- Small businesses are paying $500-1500/month across separate tools (website, phone answering, ordering, AI subscriptions). Orbi bundles those for $79-249/month.
- Local-first architecture = customer data never leaves their box. Strong privacy story.
- Scales horizontally with cheap hardware (stack of $400 mini-PCs as the customer base grows).

---

## Architecture (one-line summary)

> **Lean LLM on Frank's brain machine, accessed by each customer's box through a Cloudflared tunnel, with HuggingFace and local Llama-3.2-3B as failover. Customer data stays on the customer's box.**

See `ARCHITECTURE.md` for the full diagram and component breakdown.

---

## The three-tier LLM failover (KEY DECISION)

| Tier | When used | Quality | Cost |
|------|-----------|---------|------|
| 1. Frank's brain (8B-13B local LLM) | Default — internet up, brain up | Good | ~$0 marginal |
| 2. HuggingFace Inference API | Brain unreachable, internet works | Better (Llama-3.3-70B) | Pennies per call (Frank has $20 credit) |
| 3. Customer's local Llama-3.2-3B | Internet down entirely | Backup-grade | $0 |

Customer machine tries in order, falls through on timeout/error. Customer never sees an error — only a quality shift they may not even notice.

---

## What's IN scope for v1

- Lean Orbi install (4 modules: business_info, memory, notes, messages)
- Cloudflared tunnel (customer's box reachable from anywhere)
- PWA (installable on owner's AND customers' phones)
- Public-facing chat view (no login required — for customers of the business)
- Owner login + simple dashboard (see leads/messages, edit business info)
- Twilio phone receptionist (24/7 AI answering)
- Watchdog (auto-restart + snapshot rollback)
- Stripe recurring billing
- LLM failover (already built in Orby engine)

## What's OUT of scope for v1 (Phase 2+)

- Staff logins (owner only at first)
- Email/SMS notifications (PWA push is enough)
- Self-service module store
- Encrypted offsite backups (watchdog rollback is enough for v1)
- Customer self-service onboarding wizard (Frank installs manually for first 10)
- Native iOS/Android apps
- Google Calendar / Quickbooks / Shopify integrations
- Multi-user support beyond owner
- Audit logs, compliance pages, GDPR delete button

---

## Pricing (v1 — see PRICING.md for details)

| Tier | Monthly | Setup |
|------|---------|-------|
| Chat Only (web chatbot, no phone) | $79 | $199 |
| Standard (chat + 24/7 phone receptionist) | $149 | $349 |
| Local-Only Premium (no cloud, their own brain box) | $179 | $799 + hardware cost |

Add-on modules ($5-15/mo) sold à la carte after v1.

---

## Hardware Frank already has

- 16GB Linux box for personal Orby development (current dev environment)
- Second 16GB box available, will be wiped and turned into the brain machine

## Tools / accounts Frank already has

- **Stripe account** — billing accounts and webhooks ready, just needs product/price IDs configured
- **Twilio account** — ready for phone number provisioning and voice webhooks
- **Personal Orby codebase** (`/home/frank/orby_5050/`) — the engine to copy from, do not modify in place

## Hardware Frank needs to buy for Phase 1

- Nothing immediately. The second 16GB box becomes the brain machine.
- Phase 2 (after 5 customers): a $300-500 mini-PC as second brain box for redundancy/scaling.

---

## Key decisions made (don't re-litigate)

1. **Brain stays lean.** Do not bloat the system prompt or load every professional vertical. Specialized knowledge gets added through targeted RAG lookups only when needed.
2. **Pro modules are killed as standalone things.** Pro knowledge moves to the brain (via prompt + RAG). Workflow modules (job trackers, client matter logs) stay as modules because they hold customer-specific data.
3. **Default install ships only 4 modules.** Calendar, accounting, inventory, etc. are NOT shipped by default — the customer already uses their own tools for those. Integrations (Google Calendar, Quickbooks, etc.) come as paid add-ons in Phase 3.
4. **PWA in v1, native app in Phase 4.** No App Store fees, no review process, free to update.
5. **Watchdog with rollback is non-negotiable for v1.** Self-healing is what makes "software on customer's box" tolerable.
6. **No nickel-and-diming.** Simple tiers, generous defaults, add-ons only for genuine integrations or premium modules.
7. **Customer data stays on customer's box.** Always. Tunnel makes it reachable, but the data never moves.
8. **Personal Orby and Orbi Web are different products.** Personal Orby keeps all 56 modules (Frank uses it). Orbi Web ships lean.

---

## Anti-bloat rules (so the brain stays reliable)

1. Default system prompt = under 1000 tokens
2. Each customer's profile slice (passed per request) = under 2000 tokens
3. RAG lookups: pull max 3 paragraphs, never the whole reference doc
4. Never load more than one professional context at once
5. Never auto-load all modules into context — only the one the router selected

---

## Personal Orby (the dev environment) — DO NOT MODIFY

`/home/frank/orby_5050/` is Frank's PERSONAL Orby and remains untouched during Orbi Web development. We may PORT improvements OUT of it (into Orbi Web), but we do not modify it as part of this project. If Phase work would change Personal Orby, ask first.

`/home/frank/my_orby/` (port 5001) is a divergent fork, also untouched.

The Relay (`bridge_rebuild_20260407`, port 8088) is untouchable — copy parts out only, original stays put.

---

## Repo structure (will be built in Phase 1)

```
/home/frank/orbi_web/
├── SOURCE_OF_TRUTH.md          ← this file
├── ARCHITECTURE.md             ← technical diagram + component breakdown
├── PRICING.md                  ← pricing tiers and strategy
├── MODULE_INVENTORY.md         ← what ships, what's add-on, what's archived
├── PHASE_1_BUILD.md            ← v1 (sellable MVP) — ~3-4 weeks
├── PHASE_2_GROWTH.md           ← after first 5 customers
├── PHASE_3_SCALE.md            ← after 25 customers
├── PHASE_4_PROFESSIONAL.md     ← after 50 customers
│
├── brain/                      ← Frank's centralized LLM server (Phase 1)
├── customer_install/           ← what gets installed on customer's box (Phase 1)
├── owner_dashboard/            ← web UI for the owner (Phase 1)
├── watchdog/                   ← restart + rollback daemon (Phase 1)
├── billing/                    ← Stripe integration (Phase 1)
├── pwa/                        ← manifest, service worker, install prompt (Phase 1)
└── docs/                       ← customer-facing documentation (Phase 2)
```

Subdirectories are created as their phase begins, not all at once.

---

## How to use this document

- This is the master reference. Everything else points back here.
- If something contradicts this document, this document wins (or update this document).
- New decisions get added to "Key decisions made" with a date.
- Out-of-scope features get added to "What's OUT of scope" so they don't sneak in.
