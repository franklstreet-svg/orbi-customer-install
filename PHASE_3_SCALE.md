# Phase 3 — Scale (after 25 paying customers)

**Goal:** Scale to 50+ customers without proportionally scaling Frank's time. Add the integrations customers are asking for. Harden security/compliance.
**Estimated time:** 6-8 weeks.
**Trigger to start:** 25 paying customers, monthly recurring revenue ~$3,500+.

---

## Phase 3 deliverables

### 1. Second brain machine (redundancy + scaling)

**Why:** One brain at 25-50 customers means a single point of failure. Two boxes = no single failure can take everyone offline.

**Build:**
- Buy a $300-500 mini-PC, identical setup to brain #1
- Install brain #2 in a different location (different power circuit at minimum, ideally different building)
- Configure load balancer (HAProxy or just round-robin DNS) in front of both
- Health checks remove a dead brain from rotation automatically
- Both brains run same model, same config — perfectly interchangeable
- Capacity doubles to ~100 customers

**Time:** 1 week

---

### 2. Encrypted offsite backup

**Why:** Watchdog rollback handles "Orbi broke." Offsite backup handles "their hard drive died" or "their building burned down."

**Build:**
- Daily incremental backup of customer's data folder (NOT model weights, NOT modules — just data)
- Encrypted client-side with a key only the customer knows (Frank can't read it)
- Stored on Backblaze B2 or Cloudflare R2 (~$5/TB/mo)
- Restore flow: customer's box dies → install Orbi on new box → enter restore key → data pulled and decrypted
- Default: ON (with explicit consent in onboarding)
- Privacy: customer can turn it off if they want zero-cloud

**Time:** 2 weeks

---

### 3. Google Calendar integration (FIRST integration)

**Why:** Most-asked-for integration. Customers want Orbi to book appointments into their existing calendar, not a new one.

**Build:**
- OAuth flow: customer connects their Google account
- Module reads available slots, books appointments via Google Calendar API
- Two-way sync: appointments booked elsewhere show up in Orbi's awareness
- Conflict detection ("I see you have a dentist appointment at 2 — should I look for a different time?")
- Owner toggles which calendars Orbi can read vs write

**Time:** 2 weeks

---

### 4. Quickbooks integration

**Why:** Second most-asked integration. Customers want Orbi to create invoices and track expenses.

**Build:**
- OAuth flow with Quickbooks Online
- Orbi can create invoices ("send John a $250 invoice for the repair")
- Orbi can log expenses ("logged $45 for gas")
- Owner reviews and approves before send (configurable)

**Time:** 2 weeks

---

### 5. Custom RAG (customer's own documents)

**Why:** Many businesses have a binder of policies, FAQs, technical specs, etc. They want Orbi to know that content.

**Build:**
- Owner uploads PDFs, Word docs, or pastes text in the dashboard
- Backend chunks the content, generates embeddings (sentence-transformers, local)
- Stored as vector DB on customer's box (Chroma or Qdrant lite)
- When a query comes in, similar chunks are retrieved and prepended to the LLM prompt
- Owner sees what was uploaded, can delete documents anytime
- No data leaves their box

**Time:** 2 weeks

---

### 6. SMS/Email automation

**Why:** Customers want Orbi to send appointment reminders, follow-up texts, "thanks for your order" emails.

**Build:**
- Triggers: appointment booked, lead captured, X days since last contact, custom triggers
- Templates owner can edit ("Hi {name}, this is {business} confirming your appointment {date}...")
- Throttling so customers don't get spammed
- Unsubscribe handling for SMS (legal requirement)
- Uses Twilio for SMS, Resend for email

**Time:** 2 weeks

---

### 7. Audit log + compliance basics

**Why:** Bigger customers (medical, legal) will ask "can you show me a log of every change and access?" Required for HIPAA-adjacent or any regulated industry.

**Build:**
- Every change (config edit, module add, login, Frank's remote sessions) logged with timestamp + actor + what changed
- Logs immutable (append-only file, signed daily)
- Owner can export logs as CSV
- Privacy pages: privacy policy, terms of service, data processing agreement
- GDPR-style "delete my data" button in dashboard
- Per-customer data residency option (their box only, nothing in any cloud)

**Time:** 2 weeks

---

### 8. Performance metrics + customer success dashboard

**Why:** At 25+ customers, you need data to spot trends ("customers using Chef Mode have 40% higher retention").

**Build:**
- Per-customer metrics: queries/day, calls/day, message capture rate, response time avg
- Aggregate metrics across all customers
- Cohort analysis (this month's signups vs last month's)
- Churn warning signals (decreasing usage = likely to cancel)
- Frank sees in admin dashboard

**Time:** 1 week

---

## Phase 3 sequencing

Customer demand drives the order. Typical order:

1. Second brain (before any single brain becomes a bottleneck) — week 1
2. Google Calendar integration (highest demand) — weeks 2-3
3. Encrypted offsite backup (peace of mind, sales hook for bigger customers) — weeks 4-5
4. Quickbooks integration (second highest demand) — weeks 6-7
5. Custom RAG (sales hook for professional services) — weeks 8-9
6. SMS/Email automation — weeks 10-11
7. Audit log + compliance (unblocks medical/legal sales) — week 12
8. Performance dashboard (sharpens decisions going into Phase 4) — week 13

---

## What Phase 3 is NOT

- Not native iOS/Android apps. Phase 4.
- Not vertical-specific bundles. Phase 4.
- Not white-label / reseller. Phase 4.
- Not a CRM (use the customer's CRM via integration, don't compete with theirs).

---

## Phase 3 success metrics

- Customer count grows to 40-60
- Monthly recurring revenue $6,000-$9,000
- Add-on revenue (integrations, modules) starts hitting $1,500-$3,000/mo
- Frank works 30-35 hours/week (down from 50-60)
- Customer NPS measurable and above 50
- At least one big-ticket customer (medical/legal) on the books
