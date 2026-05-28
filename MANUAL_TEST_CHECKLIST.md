# Orbi manual test checklist

Things `test_capabilities.py` **can't** catch. Walk this checklist before every release / demo / customer install.

Tick the box if it works. If something fails, file it as a bug and FIX before shipping.

> Run `python3 customer_install/test_capabilities.py` first — that takes 2 min and catches most server bugs. The list below is the human-eye stuff that comes after.

---

## ☐ Pre-flight (do this FIRST every time)

- [ ] **Pull latest:** `cd /home/frank/orbi_web && git pull` — make sure you're on the code you intend to test
- [ ] **Restart Orbi:** kill the running `orbi.py` and start it back up so new code loads
- [ ] **Hard-refresh browser:** `Ctrl+Shift+R` on the dashboard to clear cached JS/CSS
- [ ] **Run automation:** `python3 customer_install/test_capabilities.py` — must be all green before continuing

---

## ☐ Owner dashboard — visual + interaction (5 min)

Open `http://localhost:5050/owner` and log in.

- [ ] Page lands on **Ask Orbi** tab (not Messages)
- [ ] Top bar shows business name + green "Online" pill
- [ ] All tabs in the nav are visible: Messages, Ask Orbi, My Day, Voicemails, Email, Contacts, Files, Business, Settings, Help
- [ ] **Staff tab is hidden** unless you're logged in as the owner role
- [ ] Click each tab once — none crash, none stay empty forever
- [ ] On a phone-sized window (drag the browser narrow), tabs scroll horizontally, content reflows readably
- [ ] Universal search bar at top — type "joe" → suggestions drop down

---

## ☐ Ask Orbi — chat UI (3 min)

In the Ask Orbi tab:

- [ ] Type a message → press Enter → message bubble appears immediately
- [ ] "thinking..." indicator shows up while waiting for response
- [ ] Long replies wrap properly, don't overflow horizontally
- [ ] Code blocks in replies render in a monospaced font
- [ ] Scroll-to-bottom button appears when you scroll up; clicking it jumps to latest
- [ ] **Voice button (mic icon):** click → permission prompt the first time → mic indicator turns red
- [ ] Speak a sentence → it transcribes into the input → auto-sends
- [ ] Orbi's reply is **spoken back out loud** (TTS)
- [ ] Click **Stop** button (square icon) while Orbi is speaking → audio cuts off cleanly
- [ ] Voice button again → mic turns OFF

---

## ☐ Reminders — end-to-end (4 min)

Keep the dashboard open in one window so you can see the toast.

- [ ] In Ask Orbi: *"remind me in 1 minute to test the toast"* — replies with confirmation
- [ ] Wait 60-75 seconds. **Toast slides in from bottom-right** with chime sound, yellow left border, "⏰ Reminder for {you}"
- [ ] Toast text matches what you asked for
- [ ] Click the toast → dismisses cleanly
- [ ] Open **My Day** tab → the test reminder shows as fired (not pending)
- [ ] Set another with bad time: *"remind me to call Bob at"* → Orbi asks **what time**, does NOT save
- [ ] *"who's on my staff"* → shows your actual staff list (or "just you" if no staff)

---

## ☐ Help tab (1 min)

- [ ] Click **Help** tab → markdown renders as formatted page with tables and headers (not raw `#` and `**` text)
- [ ] Type "calendar" in the search bar → other rows hide, calendar-related ones stay
- [ ] Clear the search → everything comes back
- [ ] Click **"Walk me through it"** button → switches to Ask Orbi tab, input is pre-filled with "Walk me through what you can do."

---

## ☐ Push notifications (mobile — 5 min)

Open the dashboard URL on your phone's browser.

- [ ] First load: browser prompts to allow notifications → allow it
- [ ] Add the dashboard to your phone's home screen (PWA install)
- [ ] Open the installed PWA from home screen → it loads
- [ ] On your computer: trigger a notification (set a reminder for 1 min, or send a test message)
- [ ] Within 30 seconds your **phone gets a notification** even with the screen off
- [ ] Tap the notification → opens the PWA on the right page

---

## ☐ Visitor chat widget (3 min)

Open `http://localhost:5050/` in an **incognito** browser window (no owner login).

- [ ] Floating chat button (bottom-right corner) is visible on the landing page
- [ ] Click it → chat panel slides up smoothly
- [ ] Welcome card shows business name + tagline + 3-5 quick-action chips
- [ ] Click a quick chip → message sends, Orbi replies
- [ ] Type *"are you open today?"* → Orbi answers from your business hours
- [ ] Type *"can I get a quote for X?"* → Orbi asks for your name + contact info (lead capture)
- [ ] Provide a name + phone → check the **owner dashboard Messages tab** → new lead appears with your test info
- [ ] Close + reopen widget → previous chat history is gone (visitor sessions are fresh each time)
- [ ] Click the X to close → widget retracts, floating button returns

---

## ☐ Cross-browser visual sanity (8 min)

Open the dashboard in each browser, log in, click through every tab once.

- [ ] **Chrome** — no console errors (F12 → Console tab)
- [ ] **Safari** (Mac) or **Edge** (Windows) — no obvious layout breaks
- [ ] **Firefox** — no obvious layout breaks
- [ ] **Mobile Safari** (iPhone) — usable, tap targets big enough
- [ ] **Mobile Chrome** (Android) — usable

---

## ☐ Phone receptionist (Twilio — 10 min, only if Twilio is configured)

- [ ] Settings → Phone → confirm a Twilio number is provisioned, status = active
- [ ] Call the Twilio number from your cell phone
- [ ] Within 3 rings, Orbi answers in a natural voice
- [ ] She introduces the business by name
- [ ] Say *"What are your hours?"* → she answers from your business profile
- [ ] Say *"I want to leave a message"* → she takes the message
- [ ] Hang up
- [ ] Within 2 min, owner dashboard **Voicemails** tab shows the message, transcribed
- [ ] Within 2 min, **a text/email notification** of the voicemail reaches the owner

---

## ☐ Onboarding wizard (15 min — for new-customer flow simulation)

Spin up a brand-new install with a fresh `ORBI_DIR` so you actually see the wizard.

- [ ] Visit `/owner` on the fresh install — onboarding wizard auto-opens
- [ ] Step 1 (website scrape): paste a real business URL → wizard fetches + previews extracted info
- [ ] Step 2-6 (gap questions): the wizard asks you what's missing → typing answers fills them in
- [ ] Step 7 (Twilio + Cloudflare): clear yes/no prompts, no broken steps
- [ ] After finishing → dashboard fully populated with the business info you provided
- [ ] Ask Orbi *"what's our address?"* → returns the value you just entered

---

## ☐ Brain proxy (the refund-loophole closer — 5 min)

This needs to happen on **Frank's brain server** + a customer install pointing at it.

- [ ] Brain server up: `curl https://billing.orbi.frank.com/health` → 200
- [ ] Customer install's `config.json` has `brain.url` and `brain.api_key`
- [ ] Customer Orbi sends a chat message → Frank's brain server logs show the `/v1/chat/completions` request from that api_key
- [ ] **Mark the customer inactive** via admin endpoint: `curl -X POST -H "X-Admin-Token: ..." https://billing.orbi.frank.com/api/admin/deactivate/<api_key>`
- [ ] Customer chat retry → response is **402 / "subscription not active"** banner in the customer dashboard
- [ ] Reactivate → chat works again
- [ ] In one month, log into the brain server, check the `usage` table — customer's chat count is roughly what they actually sent

---

## ☐ Watchdog + self-healing (5 min)

- [ ] On the install: `sudo systemctl status orbi orbi-watchdog` → both active
- [ ] `kill -9 $(pgrep -f orbi.py)` → Orbi process dies
- [ ] Within 10 seconds, **systemd restarts it** (verify with `ps`)
- [ ] Dashboard recovers — refresh the browser, everything still works
- [ ] Check `/opt/orbi/data/watchdog.log` → restart event recorded

---

## ☐ Backups + restore (3 min, do this at least once per quarter)

- [ ] Settings → Data → **Export full backup** → ZIP file downloads
- [ ] Open the ZIP → confirm it contains: business_info.json, contacts/, calendar/, reminders/, messages/, files/
- [ ] **Test restore on a throwaway install:** move the ZIP to a fresh install dir, restart, log in → all your data is there

---

## ☐ Demo flow (the prospect's first 5 minutes — DO THIS BEFORE EVERY DEMO)

Pretend you're a small business owner who just landed on the site.

- [ ] Open the landing page in a fresh browser (no cookies)
- [ ] Floating chat opens, looks polished, business name correct
- [ ] Ask Orbi a question your real business gets every week — she answers it well
- [ ] Ask her something she shouldn't know — she **captures your contact and says she'll get back to you** (NOT make something up)
- [ ] As the owner: pretend to answer her — the lead reaches you, you respond, the answer flows back to the original "visitor"

If any of those feel awkward, **don't demo today**. Fix it first.

---

## When you spot a failure

1. Take a screenshot
2. Note **exact steps to reproduce** (the more precise the faster the fix)
3. Note which **browser + device** you were on
4. Paste it into a chat with Claude → I'll fix it
5. Re-run `test_capabilities.py` after the fix to confirm no regression

---

## Cadence

| Cadence | What to run |
|---|---|
| **Every push to main** | `test_capabilities.py` only (~2 min) |
| **Before each demo or customer install** | This whole checklist (~30-60 min) |
| **Weekly** | `test_capabilities.py` + Owner dashboard + Reminders + Visitor chat sections |
| **Quarterly** | The whole list, including Backups + Watchdog |

---

*Last updated: 2026-05-27. Tick boxes are intentional — copy this file to a fresh checklist per release if you want to keep records.*
