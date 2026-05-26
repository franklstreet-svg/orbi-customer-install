# Orbi Web — Module Inventory

## v1 default install (ships with EVERY customer)

Only 4 modules. The brain stays lean. Customer learns one thing: "ask Orbi anything about your business."

| Module | Purpose |
|--------|---------|
| `business_info` | Hours, address, services, menu, prices, FAQ. The owner fills this in during onboarding. |
| `memory` | Three-tier (short / mid / long) so Orbi remembers conversations and learns over time |
| `notes` | Free-form notes the owner adds ("Maria has nut allergy", "Tuesday delivery only") |
| `messages` | Captured leads, voicemails, order requests. The owner reviews these in the dashboard. |

That's it. Everything else (calendar, finance, inventory, CRM) the customer uses their existing tools for — until Phase 3 integrations bridge them.

---

## Phase 2 add-ons (sold à la carte after first 5 customers)

These exist as premium modules customers can ADD if they want them. Default: off.

| Module | Who buys it | Suggested price |
|--------|-------------|----------------|
| Chef Mode | Restaurants, caterers | $10/mo |
| Recipes | Restaurants, bakers | $5/mo |
| Inventory | Shops, restaurants | $15/mo |
| Fleet Tracking | Plumbers, delivery, landscapers | $15/mo |
| Emergency Info (first aid) | Gyms, daycares, schools, restaurants | $10/mo |
| Health Knowledge | Medical/dental offices | $15/mo |
| Bedtime Stories | Daycares, kid-focused businesses | $5/mo |

---

## Phase 3 integration add-ons (after 25 customers)

These are not "modules" in the Orby sense — they're real integrations with other software.

| Integration | Who buys it | Price |
|-------------|-------------|-------|
| Google Calendar | Anyone with appointments | $20/mo |
| Quickbooks sync | Anyone doing invoicing | $25/mo |
| Shopify / POS sync | Retailers, restaurants | $20/mo |
| Email automation (Resend) | Anyone with a customer list | $15/mo + usage |
| SMS automation (Twilio) | Anyone sending appointment reminders | $10/mo + usage |
| Custom RAG (their PDFs) | Anyone with custom docs | $30/mo |

---

## Phase 4 vertical packs (after 50 customers)

Pre-configured bundles aimed at specific industries.

| Pack | Includes |
|------|----------|
| Restaurant Pack | Chef Mode + Recipes + Inventory + Google Calendar + POS sync |
| Contractor Pack | Fleet + Quickbooks + SMS reminders + custom RAG (codes) |
| Medical Pack | Health Knowledge + Google Calendar + SMS reminders + HIPAA-style config |
| Salon Pack | Google Calendar + SMS reminders + Recipes (for color formulas) |

Bundled at a discount vs buying separately. Easy to sell to a specific industry.

---

## Archived (lives in Personal Orby, NOT shipped with Orbi Web)

These modules are valuable for Frank's personal use but don't translate to most business customers. They stay in `/home/frank/orby_5050/modules/` and are NOT ported to Orbi Web's customer install.

- `fitness`
- `meal_plan` (personal meal planning, not restaurant recipes)
- `mood`
- `travel`
- All other purely personal modules from the original 56

If a Phase 4 customer specifically asks for one of these, it can be ported as a one-off custom install.

---

## Pro module decision (FINAL)

The original 23 "pro" modules (medical_pro, legal_pro, framing_pro, contractor_pro, etc.) are **NOT shipped as standalone modules.** Decision rationale:

1. As thin shells they don't hold enough info to be useful
2. Their actual answer-content falls back to the LLM anyway
3. Stuffing 23 pro contexts onto the brain causes drift / hallucination

**Replacement strategy:**
- Pro KNOWLEDGE (codes, drug interactions, statute summaries) moves to brain RAG. One central reference library Frank maintains.
- Pro WORKFLOW (job trackers, client matter lists, patient charts) becomes simple modules only when a specific customer needs them, built as part of their onboarding.

Result: 23 modules become 0 default modules + a growing brain RAG library + occasional custom modules per customer.

---

## Module rules going forward

1. **Default install ships 4 modules. Period.** New defaults need a strong reason.
2. **Premium modules only ship when bought.** Customer adds via dashboard, you install via remote update.
3. **Brain never holds module content.** Modules are storage. Brain is reasoning.
4. **Module updates roll out through the same staged update system as the core.** Canary customers first.
5. **Customer can disable any module without breaking Orbi.** Each module is optional, not load-bearing.
