# First Customer Playbook

Plain-English playbook for when a real human says "I'm interested in Orby" — from first email through their first month using it.

---

## 1. Their first email arrives

Likely subject lines from the Get Started buttons:
- "Orby Small Business"
- "Orby Medium Business"
- "Orby Large Business"
- "Orby Enterprise"

They might add some text, or it might be empty.

### Your reply template (copy + edit)

```
Subject: Re: Orby [TIER NAME] — let's get you set up

Hi [their first name from the email],

Thanks for reaching out about Orby [TIER NAME].

Quick reality check before we go further — Orby works best for:
- Owners of small offices, shops, or service businesses
- People who answer their own phone or website chat today
- People who'd rather their customer data live on THEIR computer than in someone else's cloud

Two questions before I send a payment link:

1. What's your business? (Just a name + what you do — e.g. "Reno
   Mountain Plumbing, residential & light commercial.")

2. What's the ONE problem you'd most want Orby to solve in the first week?
   (Missing calls? Spam emails? Forgotten reminders? Something else?)

If we're a fit, the next step is:
- I send you a Stripe payment link for [$99 / $149 / $249 / $399]/month
- The moment you pay, you get an email with a download link + a one-time
  install token
- Installer runs on your computer in about 10 minutes
- You can ask Orby anything within a minute of install completing
- Cancel anytime; no contracts, no setup fee

Reply when you have a moment.

— Frank
My Orby AI Solutions
orbiaisolutions@gmail.com
```

### Why ask two questions first

- Confirms it's a real human (filters tire-kickers)
- Tells you whether they're actually a fit (you can save your time + theirs by saying "you'd be better off with X" if they're not)
- Their "one problem" answer is the demo you give them — show them THAT working before anything else

---

## 2. They reply with their info

If they pass the fit check, send:

```
Subject: Orby [TIER NAME] — payment link inside

[Name],

Thanks. [Business name] sounds like a good fit — Orby's strongest at exactly
the "[their one problem]" use case.

Here's your payment link:
[PASTE STRIPE PAYMENT LINK]

When you pay, Stripe sends you a receipt and my system emails you a download
link + a one-time install token within about a minute. If you don't see the
token email in 5 min, check spam (it comes from franklstreet@yahoo.com).

After install, you log into your dashboard, drop a few of your business
details into the Business tab, and you're live. I'll send a 15-minute
"first day" guide along with the payment.

Any time questions come up, just reply to this thread.

— Frank
```

---

## 3. They pay, the install email goes out, they install

This is automatic from your side. The Stripe webhook fires → install email → they click → installer runs.

**Watch for these failure modes on Day 1:**

| Symptom | Fix |
|---|---|
| They don't get the install email | `journalctl -u stripe-webhook -f` — token will be there, copy it and email manually |
| Installer says "can't reach billing server" | brain.twickell.com tunnel is down — check `systemctl status cloudflared` |
| Installer says "invalid token" | Token already used. Refund + reissue from Stripe → new webhook fires → new token |
| Install runs but dashboard won't open | Port 5050 already in use OR Windows firewall blocking — turn it off temporarily |
| They installed but Orby doesn't answer | Brain proxy down — check `systemctl status stripe-webhook` |

---

## 4. Their first week — what to actively watch

**Day 1:** Send them this:

```
Subject: Your Orby is installed — here are the 5 things to try in your first hour

Welcome aboard.

Open your dashboard (the installer should have opened it for you, or use
the desktop shortcut). Once you're logged in:

1. Click the Business tab. Fill in your hours, services, FAQ, address,
   phone. Anything you put here, Orby will tell customers.

2. Click Ask Orby (your home tab). Type "what's on my calendar today" —
   she should answer (probably empty).

3. Try: "remind me in 5 minutes to test the loud reminder" — wait 5 min,
   you should hear a chime and see a big yellow banner.

4. Open Settings → Integrations → "Other email" → Add your email account.
   For Yahoo/iCloud/AOL/Fastmail you'll need an "app password" — the dialog
   tells you where to get one.

5. Open the Help tab. Search for any feature. Or just ask Orby
   "walk me through booking a calendar appointment" and she'll teach you.

Hit me back with anything that feels broken, weird, or "she really should
have done that better."

— Frank
```

**Day 3-5:** Check in. They'll have hit at least one friction point.

```
Subject: How's Orby treating you?

[Name], no agenda — just a 30-second check-in.

Two questions:

1. What's working well?
2. What's pissing you off?

Both answers are useful. The "pissing you off" answers especially — that's
how Orby gets better fast.

— Frank
```

**Day 14:** First billing reminder. They'll be charged Day 30. Pre-empt:

```
Subject: Orby — first month wrap-up + what's next

[Name], two weeks in.

[Specific observation from their usage — "you've captured 14 leads",
"your inbox went from 200 unread to 12 important", whatever's true]

You'll renew automatically on [DATE]. If you want to change tier or pause,
just reply. If you want a longer-term annual rate (save 15%) we can switch
you at the renewal too.

What's the next thing you want her to do that she's not doing yet?

— Frank
```

---

## 5. Saying NO to bad-fit customers

Don't take everyone. If someone:
- Wants Orby to do something illegal (impersonate a doctor, send unsolicited bulk mail, etc.)
- Won't pay until they "see it work" (no free trials — say so)
- Wants enterprise-grade SLAs (they're not your buyer)
- Has 50+ employees (Connect blueprint, not Orby — different conversation)

Say:

```
Hey [Name], thanks for the interest. Orby probably isn't the right fit for
you — it's built specifically for [1-3 person small offices / etc.]. You'd
likely be better off with [their actual fit alternative].

I'll send your contact info to [alternative] if that'd be useful, or just
leave you to it.

— Frank
```

Saying no early protects them, you, and the brand. Saying yes to the
wrong fit = refund disputes + bad word-of-mouth.

---

## 6. When you get to customer #5

Three things change:

1. **Stop hand-customizing emails.** Build a Notion/Google Doc with the
   templates above and just paste.

2. **Track every customer in a spreadsheet.** Name, business, tier,
   start date, churn date, "what they actually use Orby for." This
   becomes your product roadmap.

3. **Move the brain proxy off your home machine.** A $10/mo VPS
   becomes worth it once you're earning $300+/mo from customers, AND
   when your home internet going down means 5 customers are down.

---

## What I (Claude) can build for you when these arrive

When customer #1 emails:
- Auto-reply template that pulls from your business_info
- "Customer interest" tab in the dashboard that tracks each lead

When customer #5 is on:
- Customer-portal subdomain so they can manage their own subscription
- Self-serve "change tier" flow inside the dashboard
- Notification when a payment fails

When customer #20 is on:
- Migrate the brain proxy + billing to a VPS together
- HF Pro token + the model-tier routing flips on for paying customers
