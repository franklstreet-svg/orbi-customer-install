# Integrations that need third-party approval before they go live

These are the integrations we WANT to ship in Orbi but cannot turn on
ourselves — Meta and Intuit require formal app review processes that
take 1-4 weeks of waiting. Start these applications today so by the
time we ship Orbi v1 publicly, the approvals are mostly through.

For each: Frank does the application; once approved, Frank pastes the
credentials into the customer install's `config.json` under the noted
key. Then the connector wakes up — no code changes needed.

---

## 1. Facebook Messenger + Instagram DMs (Meta)

**Estimated wait:** 1-3 weeks
**Required for:** restaurants, retail, anyone whose customers DM them

### Steps for Frank

1. Go to **developers.facebook.com** → log in with your business Facebook account
2. Click **"My Apps"** → **"Create App"**
3. App type: **"Business"**
4. App name: `Orbi for Small Business`
5. Business contact email: your email
6. Once the app exists, in the left sidebar:
   - Click **"Add Product"**
   - Add **"Messenger"** product
   - Add **"Instagram Graph API"** product
7. Go to **App Review → Permissions and Features**
8. Request these permissions:
   - `pages_messaging` (send/receive Page messages — for Messenger)
   - `pages_messaging_subscriptions` (subscribed event webhooks)
   - `pages_show_list`
   - `instagram_basic`
   - `instagram_manage_messages` (Instagram DMs — separately reviewed)
9. **Important:** Meta requires a **screencast** showing your app
   correctly using each permission. You'll need a working Orbi demo
   to record this. We can shoot the videos together once the connector
   code lights up.
10. Submit for review. They reply in 1-3 weeks (usually).

### Once approved

Paste your **App ID** and **App Secret** into customer_install/config.json
under a new section:

```json
"meta_oauth": {
  "app_id": "...",
  "app_secret": "..."
}
```

Then the (not-yet-built) Messenger/Instagram connectors will work.

---

## 2. WhatsApp Business

**Estimated wait:** 2-6 weeks (harder than Messenger)
**Required for:** internationally-focused businesses, customer support
**Skip if:** your customers don't use WhatsApp heavily

### Steps for Frank

1. Same Meta developer dashboard as above
2. Add the **"WhatsApp"** product to your existing Orbi app
3. Apply for **WhatsApp Business API** access
4. Meta requires **business verification** — you must verify your
   business is real via documents (tax ID, business registration, etc.)
5. Once verified, you get a phone number provisioned + access tokens

Recommendation: skip WhatsApp for v1 unless a specific customer asks
for it. Build it after we have proof of customer demand.

---

## 3. QuickBooks (Intuit)

**Estimated wait:** 2-4 weeks
**Required for:** businesses that bookkeep in QuickBooks
**Value:** Orbi can read invoices, payments, customer balances

### Steps for Frank

1. Go to **developer.intuit.com**
2. Sign in / create account → **My Apps** → **Create an app**
3. App type: **"QuickBooks Online and Payments"**
4. App name: `Orbi for Small Business`
5. Pick scopes: `com.intuit.quickbooks.accounting`
6. Sandbox credentials are issued immediately — you can BUILD against
   the sandbox without waiting.
7. To use it with REAL customer data: apply for **Production**
   - Go to your app → **Production** tab → **Get production keys**
   - Fill out the app review form (Intuit asks about your business,
     intended use, security practices)
   - Intuit responds in 2-4 weeks

### Once approved

Paste **Client ID** and **Client Secret** into:

```json
"quickbooks_oauth": {
  "client_id": "...",
  "client_secret": "..."
}
```

Then the QuickBooks connector (build TBD) will work.

---

## Order to start applications today (in order of value to customers)

| Application | Start today? | Why |
|---|---|---|
| **Meta — Messenger + Instagram** | ✅ Yes | Reaches the broadest customer base (restaurants/retail) |
| **Intuit — QuickBooks** | ✅ Yes | High-value for any business with accounting |
| **WhatsApp Business** | ⏳ Wait | Niche for U.S. SMB — skip unless a customer asks |

While Meta + Intuit reviews are pending, focus our code time on:
- Polishing the connectors we already built
- Customer-acquisition site (twickell.com → orbi-website)
- Stripe checkout flow for Orbi itself
- Onboarding wizard for new customer installs
