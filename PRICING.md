# Orbi Web — Pricing

## v1 launch pricing (KEEP IT SIMPLE)

### Monthly tiers

| Tier | Monthly | What's included |
|------|---------|-----------------|
| **Chat Only** | $79 | PWA web chatbot, business memory, message capture. No phone. |
| **Standard** | $149 | Everything in Chat Only + 24/7 AI phone receptionist (200 min/mo included, $0.10/min after) |
| **Local-Only Premium** | $179 | Everything in Standard, but their brain runs entirely on their own hardware. No cloud LLM ever. |

### Setup fees

| Tier | Setup |
|------|-------|
| Chat Only | $199 |
| Standard | $349 |
| Local-Only Premium | $799 + hardware cost passed through |

Setup fees cover Frank's install labor (4-6 hours) and filter out tire-kickers.

---

## Why these numbers

| Comparable | Their price | Orbi advantage |
|------------|-------------|----------------|
| Squarespace + Wix (website hosting only) | $30-50/mo | Orbi adds AI on top for nearly the same total |
| Ruby Receptionists (human phone answering) | $235-$450/mo | Orbi is half the price and works 24/7 |
| AnswerConnect (virtual receptionist) | $90-280/mo | Same value, Orbi knows their business better over time |
| ChatGPT Plus | $20/mo | Orbi includes ChatGPT-level chat + knows the business |
| Toast/Square ordering | $79-$165/mo | Orbi handles ordering as a bonus, not the main feature |

A small business currently paying $300-500/mo across separate tools can replace 2-3 of them with one $149/mo Orbi subscription.

---

## Frank's costs per customer per month

| Item | Cost |
|------|------|
| Twilio phone number | $1.15 |
| Phone call minutes (~100 calls × 2min) | $10-15 |
| HuggingFace failover usage | <$0.10 (covered by $20 credit) |
| Brain machine electricity (amortized) | $1-2 |
| Cloudflared tunnel | $0 |
| Stripe processing (2.9% + 30¢ on $149) | $4.62 |
| **Total hard cost** | **~$17-22/mo** |

Gross margin per Standard customer: ~$127/mo (85%)

---

## Revenue projections

| Customers | Mix | Monthly recurring | + Avg setup ($349) | Annual run-rate |
|-----------|-----|------------------|---------------------|-----------------|
| 5 | mostly Standard | $700 | + $1,400 (one-time) | $10,000 |
| 10 | mostly Standard | $1,400 | + $1,000/mo (new) | $20,000 |
| 25 | mixed | $3,500 | + $1,500/mo (new) | $50,000 |
| 50 | mixed | $7,500 | + $2,000/mo (new) | $100,000+ |
| 100 | mixed | $15,000 | + $3,000/mo (new) | $200,000+ |

At 25 customers, Frank is making a real living. At 50, he's running a small business.

---

## Add-ons (Phase 3+, NOT for v1)

Premium modules and integrations sold à la carte. Sample pricing:

| Add-on | Monthly |
|--------|---------|
| Google Calendar integration | $20 |
| Quickbooks sync | $25 |
| Shopify / POS sync | $20 |
| Email/SMS follow-up automation | $15 + usage |
| Custom training on their PDFs/docs | $30 |
| Premium module (Chef Mode, Recipes, etc.) | $5-15 each |
| Additional phone number | $5 |
| Extra staff login | $10 each |

Rule: never charge more than $50/mo total add-ons on top of the base tier. Customers should feel they're adding value, not getting nickel-and-dimed.

---

## Pricing rules

1. **No annual contracts at launch.** Month-to-month, cancel anytime. Removes the buying friction.
2. **No free trial.** Setup fee filters serious customers. If they won't pay $199 setup, they won't be a good customer.
3. **30-day money-back guarantee.** On the monthly fee, not the setup fee. Removes risk perception without losing the install labor.
4. **Lock in early customers.** First 10 customers: lifetime $99 Standard (instead of $149). Creates evangelists and gets feedback.
5. **Don't discount on demand.** Discounts come from saying "first 10 customers" or "introductory pricing through [date]." Never negotiate one-off — it trains buyers to ask for discounts.

---

## Pitch script (what Frank says in the meeting)

> "Right now you're probably paying for a website, maybe an answering service, maybe an ordering app, and a few software subscriptions. I'm guessing $300-500 a month across all of them. Here's what Orbi does: it's your website AI chat, it answers your phone 24/7 in a real voice, it remembers every customer that calls, and it texts you when someone places an order or wants a callback. One tool. $149 a month. $349 to set it up, which I'll do for you. Month-to-month, cancel anytime. If you don't love it in the first 30 days, I'll refund the monthly fee. Want to try it for a month?"

That pitch closes about half the people who hear it if delivered to the right kind of small business.
