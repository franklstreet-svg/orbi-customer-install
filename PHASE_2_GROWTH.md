# Phase 2 — Growth (after first 5 paying customers)

**Goal:** Polish the product based on real customer feedback. Make onboarding self-serve. Reduce support burden.
**Estimated time:** 4-6 weeks.
**Trigger to start:** 5 paying customers, all running stable for 30+ days.

---

## Why wait for 5 customers?

The features below are educated guesses about what'll matter. Real customers will tell us which ones actually do. Don't build Phase 2 features speculatively — wait for the data.

---

## Phase 2 deliverables

### 1. Email and SMS notifications (in addition to PWA push)

**Why:** PWA push only works when the owner has the app installed and notifications enabled. Email and SMS work always.

**Build:**
- Add Resend.com integration for email (free tier covers <3000 emails/mo)
- Add Twilio SMS for text alerts (~$0.01 per SMS)
- Owner dashboard: "Notify me by: [ ] PWA push  [x] Email  [x] SMS  for: [x] New leads [ ] Daily summary [x] Urgent issues"
- Default: PWA push + email for new leads. SMS opt-in.

**Time:** 3 days

---

### 2. Staff logins (multi-user)

**Why:** Owners want their employees to take messages and see today's schedule without giving up admin access.

**Build:**
- Add "users" section to dashboard (owner only)
- Each user: name, email, password, role (owner / manager / staff)
- Permissions table per role
- Brain knows who's logged in (system prompt: "You're talking to Maria, a staff member")
- Maria's view of dashboard: today's messages, take new message, see today's calendar (if integration enabled). No business info edit, no billing, no user management.

**Time:** 1 week

---

### 3. Self-service onboarding wizard

**Why:** Frank installs each of the first 5 customers manually. Onboarding wizard reduces that from 4 hours of his time to 30 minutes.

**Build:**
- After Stripe payment, customer gets an email with install link + setup token
- They run a one-line install script: `curl orbi.frank.com/install | bash`
- Script downloads `/opt/orbi/`, generates config.json with their token, starts services
- First-launch wizard in browser asks 8-10 questions:
  - Business name, address, phone, website
  - Hours (per day)
  - Top 5 services / products with prices
  - Top 10 FAQs (or import from existing website)
  - Owner email, password
  - Pick Orbi's voice (3-4 voice options)
  - Optional: paste their existing menu/services document
- Saves all answers, Orbi is ready to chat

**Time:** 2 weeks

---

### 4. Self-service module store

**Why:** Owners want to add Chef Mode or Recipes themselves, not wait for Frank.

**Build:**
- "Modules" tab in dashboard
- Lists available modules (Chef, Recipes, Inventory, etc.) with description + price
- "Add" button → Stripe charge for the upgrade → backend pushes module to customer's install
- "Remove" button → backend disables module + prorated refund
- Frank doesn't touch anything; Stripe + the auto-install handle it

**Time:** 1 week

---

### 5. Customer-facing polish

**Why:** First impression matters. The PWA needs to look professional, not like a coding project.

**Build:**
- Custom branding per customer (their logo, their colors, their business name as the app name)
- Better chat bubble design
- "Orbi is typing..." indicator
- Image upload (customer can send Orbi a photo, Orbi can store it as context)
- Improved voice UI on mobile (visible mic button, recording indicator)
- Splash screen for PWA install
- Better error messages ("I'm having trouble right now — call (number) and they'll help you")

**Time:** 1 week

---

### 6. Admin dashboard FOR FRANK

**Why:** Frank needs to see all his customers at a glance, spot problems early.

**Build:**
- New site at `admin.frank.com` (only Frank can log in)
- Shows list of all customers with:
  - Green/yellow/red health status
  - Last heard from (last health ping)
  - Last error (if any in past 24h)
  - Calls handled this month
  - Messages captured this month
  - Brain usage (queries/day)
  - Subscription status (active / past due / canceled)
- Click a customer → detail view: recent errors, recent activity, manual restart button, emergency snapshot rollback button
- Alert thresholds: ping Frank if any customer goes red

**Time:** 1 week

---

### 7. Staged rollout system

**Why:** When Frank pushes an update, he doesn't want it hitting 30 customers at once and breaking everyone.

**Build:**
- Update channel system: "canary" customers (2-3 volunteers) get updates first
- Pre-update snapshot taken automatically on each customer's box
- Canary updates run for 24 hours
- If no errors, auto-rollout to all customers
- If errors detected, halt rollout, alert Frank
- Frank gets one-click "force rollback" button in admin dashboard

**Time:** 1 week

---

## Phase 2 sequencing

By Phase 2 start, the product is live and earning. Priorities should be ordered by what's actually causing support pain.

**Likely order based on probable pain points:**

1. Admin dashboard (so Frank can see what's breaking) — week 1
2. Email/SMS notifications (customers will ask) — week 1
3. Staged rollout system (before pushing any more updates to live customers) — week 2
4. Customer-facing polish (first impression, helps sales) — week 2
5. Self-service onboarding wizard (every new customer saves Frank 4 hours) — week 3-4
6. Staff logins (customers will ask once they have multiple employees) — week 5
7. Self-service module store (only when there are premium modules ready to sell) — week 6

Adjust based on what real customers actually complain about.

---

## What Phase 2 is NOT

- Not integrations (Google Calendar, Quickbooks). Phase 3.
- Not native iOS/Android apps. Phase 4.
- Not encrypted offsite backup. Phase 3.
- Not advanced compliance (GDPR delete, full audit trail). Phase 3.
- Not custom RAG per customer. Phase 3.
- Not vertical-specific bundles. Phase 4.

---

## Phase 2 success metrics

- New customer onboarding takes < 1 hour of Frank's time (from 4-6)
- Customer-reported issues drop by 50% (because of polish + admin dashboard catching things first)
- Frank can take a weekend off without checking on customers
- Total customers grows to 15-25 by end of Phase 2
