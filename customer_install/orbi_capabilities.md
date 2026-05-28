# What Orby Can Do

Orby is your AI for three jobs: **your phone receptionist**, **your website chat host**, and **your personal assistant** on your own computer. Everything below works from the *Ask Orby* tab in this dashboard — type a request or use the microphone.

Tip: phrases in *italics* are real examples you can copy and paste, or just say out loud.

---

## Quick start — your first 10 minutes

1. Type *"Show me my day"* in **Ask Orby** — she'll pull together calendar + tasks + unread messages.
2. Say *"What can you do?"* and she'll walk you through the rest of this guide section by section.
3. Open the **Business** tab and confirm your hours, services, and FAQ are right — that's what she tells customers.
4. Open the **Settings** tab → **Phone** to provision your business line (if you haven't already).
5. Drop the website widget snippet from **Settings → Website** onto your site so visitors can chat.

---

## Calendar

| Want to… | Say… |
|---|---|
| See today | *"What's on my calendar today?"* |
| See the week | *"Show me this week"* |
| Add an event | *"Book a haircut with Maria tomorrow at 2"* |
| Move an event | *"Move my 3pm with Joe to Friday at 10"* |
| Cancel | *"Cancel my Tuesday 1pm"* |
| Find a free slot | *"When am I free Thursday afternoon?"* |
| Check someone else's | *"Is Kathy free Friday morning?"* (only works for staff in your system) |

Calendar lives on your machine. Connect Google or Outlook from **Settings → Integrations** if you want it to sync.

## Tasks & reminders

| Want to… | Say… |
|---|---|
| Add a task | *"Remind me to call the supplier Friday"* |
| List open tasks | *"What's still on my list?"* |
| Mark done | *"Done with the supplier call"* |
| Recurring | *"Remind me every Monday at 9 to send the staff schedule"* |

## Contacts

| Want to… | Say… |
|---|---|
| Find someone | *"What's Bill Henry's phone?"* |
| Add | *"Save Bill Henry, billh@example.com, 555-0142, electrician"* |
| Update | *"Change Bill's phone to 555-0199"* |
| Search by tag | *"Show me all my suppliers"* |
| Recent | *"Who have I emailed this week?"* |

## Email

| Want to… | Say… |
|---|---|
| Triage | *"What's important in my inbox?"* — she summarizes + flags |
| Draft a reply | *"Reply to the message from Pam — say I'll be there at 4"* |
| Compose | *"Email Bill Henry: the parts came in, ready when you are"* |
| Search | *"Find the email from last month about the lease"* |
| Auto-reply | Turn on **Settings → Email → Auto-reply** so Orby answers routine messages in your voice |

Connect Gmail or Outlook from **Settings → Integrations**.

## Files & documents

| Want to… | Say… |
|---|---|
| Write a letter | *"Write a thank-you letter to the Smith family for their order"* |
| Make a spreadsheet | *"Build me a spreadsheet of last month's expenses"* |
| Open a file | *"Open the lease.pdf"* |
| Search files | *"Find the invoice from Acme Plumbing"* |
| OCR | Drop a photo or scanned PDF in **Files** — she reads the text |

All files live in your `Orby/` folder. Nothing leaves your computer.

## Web & search

| Want to… | Say… |
|---|---|
| Quick lookup | *"What's the sales tax in Reno?"* |
| Fetch a page | *"Read me what's on twickell.com/about"* |
| Compare options | *"Compare prices for Stihl chainsaws at Home Depot and Lowe's"* |
| Local | *"Find a notary near 89509"* |

## Voice mode (hands-free)

- Click the mic icon, or say *"Hey Orby"* (if wake-word is on)
- She'll listen, answer with voice, and keep her ear open for follow-ups
- Say *"goodbye"* or *"I'm good"* when you're done

## Catalog & inventory

If you sell things, drop a CSV / Excel / Google Sheet in **Files → Catalog** and Orby will:

- Answer customer questions about products + prices
- Tell you what's running low when you ask
- *"How many size-9 boots do we have?"*
- *"What's the price on the framed Tahoe print?"*

## Revenue & money (Stripe)

If Stripe is connected (**Settings → Integrations**):

- *"How much did we make last week?"*
- *"Show me today's transactions"*
- *"Top 5 customers by revenue"*

Read-only — Orby never creates charges or refunds.

---

# What customers can do (your website + phone)

These are the things visitors and callers can do without you lifting a finger:

| When they… | Orby will… |
|---|---|
| Visit your website | Greet them, answer questions about hours/services/prices/location |
| Ask something she doesn't know | Capture their name + best contact method, then ask YOU via your preferred channel — and once you answer, she delivers it back AND remembers it for next time. **This is the learning loop — your knowledge compounds.** |
| Want to book | Schedule an appointment in your real calendar (if booking is enabled) |
| Want a callback | Take a message and text/email you the details |
| Call your phone | Same thing in voice — she answers in a polite human voice, takes the message, summarizes by text |

---

# Staff features (when you add team members)

Each staff member gets their own login from **Staff** tab. They see:

- Their own calendar, tasks, contacts
- Shared business data (catalog, customer messages, leads)
- Customer chat handoff requests
- Their email (limited to assigned accounts)

Owner-only stuff (billing, integrations, staff management) is hidden from staff.

---

# Settings worth knowing about

- **Personality** — pick how Orby talks (friendly / formal / brisk). Affects every channel.
- **Style learning** — Orby watches how YOU write emails and matches your voice over time.
- **Notifications** — choose how she pings you when something needs attention.
- **Backups** — your data snapshots to `Orby/backups/` automatically. You can also export a full ZIP from **Settings → Data**.
- **Voice** — pick Orby's voice (12 to choose from). Test it from Settings.
- **Privacy** — every conversation, lead, and document stays on this computer. Nothing goes to a cloud unless you turn on a specific integration (Gmail, Outlook, Stripe, etc.).

---

# When something's wrong

- **Orby seems slow** — check the connection light in the header. Green = full brain, yellow = backup brain, red = offline (still works locally, just less smart).
- **She got something wrong** — type *"that's wrong, the actual answer is X"*. She'll learn it and apply it next time.
- **You need to start fresh** — **Settings → Data → Reset chat memory**. Doesn't touch your calendar, contacts, or files.
- **The phone or chat went down** — Orby's watchdog should restart her automatically. If it doesn't, run `orbi-restart` from Terminal/PowerShell.

---

# What Orby WON'T do

So you know where the edges are:

- She won't invent personal data about your people / customers / suppliers — if she doesn't know, she asks you.
- She won't charge customers or move money on your behalf (Stripe is read-only).
- She won't email or post on your behalf without you confirming the draft first (unless you turn on auto-reply for routine inbox triage).
- She doesn't browse private accounts that you haven't connected — no peeking at your bank or socials unless you wire them up.

---

*This guide is updated as Orby grows. Last updated: 2026-05-27.*
