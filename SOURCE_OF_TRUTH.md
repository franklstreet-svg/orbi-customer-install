# Orbi — Source of Truth

**Last updated:** 2026-06-20 (file-audit refresh — no policy changes since 6-18, but file counts, route lists, line counts, and the customer_install/owner_dashboard symlink claim were stale. Also captures the uncommitted edits that landed 6-19 on top of the 6-18 16:19 build but haven't been pushed to git yet.)
**Previous refresh:** 2026-06-18 (after Frank's external-customer install round — SCS / scsplanroom.com test surfaced new bugs around the welcome wizard, owner-name handling, and staff session writes. All fixed in source 6-18 and shipped in a fresh `.exe` (16:19) that's on Frank's thumb drive.)
**Owner:** Frank Street
**Legal entity:** FST LLC
**Product/company brand:** myOrbi (camelCase, "my" lowercase, "O" uppercase)
**AI character name:** Orbi (always — never "myOrbi" in her greetings or first-person voice)
**Status:** Pre-revenue. Stripe test mode. Kathy install bug rounds (1-6) complete from 6-08/6-10; on 6-18 Frank ran a second external test against scsplanroom.com (Sierra Contractors Source / Frank Hawbolt) which uncovered: scrape-derived "Sierra" name in prompts, brochure-style overview replies, email-derived "Frank L Street" display name, staff setup_initial_password overwriting business.owner_name, silent Orbi when scrape pre-fills business_info, an icon shipped with only 16/32 sizes (looked pixelated on Desktop), and cloudflared.exe holding a file lock that broke clean-reinstall. All seven fixed in source 6-18 and bundled into the 6-18 16:19 build.

---

## What Orbi is today

A **24/7 AI receptionist + personal assistant that runs on the customer's own computer**. Not SaaS. Not cloud-hosted. Local install, customer-owned data, no central database of customer conversations.

Three jobs in one product:

1. **Phone receptionist** — Frank's central Twilio account auto-provisions a phone number for each subscribing business at Stripe checkout. Inbound calls route through Frank's server (orbi-brain) to the customer's local Orbi for handling. Orbi answers in a natural voice, captures lead info, texts a receipt, emails the owner.
2. **Website chat widget** — A `<script>` tag the customer pastes on their site loads the embed widget from `orbi.twickell.com`. Widget routes to the customer's local Orbi via their `api_key`. Captures leads, answers FAQ from `business_info.json`.
3. **Personal AI assistant** — Calendar, tasks, contacts, document/PDF read/write, email drafts in the owner's voice (after Gmail/Outlook connect), persistent memory across sessions.

---

## Brand structure (clarified 2026-06-08 — three distinct labels)

| Label | What it is | Where it shows up |
|---|---|---|
| **Orbi** | AI character name | Her greetings, `personality.name`, prompts.py persona, TTS voice |
| **myOrbi** | Company/product brand | Marketing copy, logo wordmark, business_info.json `name` on the demo install, Stripe product name, footer brand block |
| **myorbi.AI** | Domain-styled form of company | Domain references, polished marketing copy emphasizing AI |
| **FST LLC** | Legal entity | Terms, refund, privacy, Stripe, Twilio, contracts, "by FST LLC" subtext under logo |

**Domain:** twickell.com (current). Target: myorbi.ai — NOT yet owned, ~$80-100/yr, deferred until first paid customer per the no-new-spend rule.

**Greeting rule:** Orbi always says "Hi, I'm Orbi" — never "myOrbi" in self-introduction. The company name only matters in marketing/legal contexts.

---

## Pricing (App Store model, locked 2026-06-08)

| Tier | Price | What it includes |
|---|---|---|
| **Personal** | $29.99/seat/mo | Personal assistant only. No phone, no website chat. Solo home use. |
| **Business** | $49.99/seat/mo | Universal base for any business. Phone receptionist + website chat + full assistant. |
| **+ Primary industry module** | +$49.99/mo | Industry-specific knowledge. Restaurant logic is integrated via the onboarding wizard + config (NOT a packaged standalone module — `ONBOARDING_STEPS_RESTAURANT` in `orbi.py:6004-6046`). Construction, Law, Medical, Auto, Salon planned. |
| **+ Sub-module** | +$24.99/mo | Finer-grained specialty. Marketing (`modules/marketing.py`) + Image sub-module (`image_gen.py` + `ad_gen.py` at install root, FLUX.1-schnell via HF) BUILT and wired. |
| **Founding member discount** | -33% Restaurant, -15% Business | Year 1 only. First 50 customers. |
| **Annual prepay** | -17% effective | Pay 10 months get 12. |

### The seat/brain/device model

**ONE SEAT = ONE Orbi = ONE shared brain accessible from up to 3 of the buyer's devices.**

- The BRAIN (data + memory + runtime) lives on ONE host computer.
- The other 2 devices (phone, tablet, second computer — ANY combination) are thin clients that LINK to the host. They don't have their own brain copy.
- Concrete examples of valid 1-seat setups: desktop+laptop+phone, laptop+iPad+phone, Mac+iPhone+iPad, desktop+iPad+Android phone.
- A buyer who has a desktop AND a laptop does NOT need 2 seats — those are 2 of their 3 device slots.
- A SEPARATE person who wants their OWN Orbi (different brain) = a second seat with its own host computer.
- One household PC can host multiple brains (one per seat). Family of 4 wanting separate Orbis = 4 seats, all 4 brains on the same household computer.

### Future: Cloud-hosted tier (NOT BUILDING YET)

For customers without a computer to host on. Suggested pricing: Personal Cloud $49.98/mo, Business Cloud $69.98/mo. Defer building until 5-10 local customers land. Three reasons not now: dilutes privacy moat, pre-revenue infrastructure commitment, segment validation needed.

---

## Architecture

```
 PUBLIC                            FRANK'S INFRASTRUCTURE        CUSTOMER'S COMPUTER
 ──────                            ─────────────────────         ───────────────────

 twickell.com  ◀────► Vercel ◀───► orbi-brain (Render)            Orbi (orbi.py)
   /index         (marketing)       billing.twickell.com           C:\Program Files\Orbi
   /orbi          (product)         - Stripe webhooks              - bundled Python 3.13
   /install-notice                  - install-token verify         - Flask on port 5050
   /terms                           - LLM proxy (HF Qwen 2.5 72B)  - Cloudflared tunnel
   /privacy                         - billing checks               - SQLite + JSON data
   /refund                          - fleet health                 - Twilio voice handler
                                                                   - edge-tts voice
 Embed widget   ◀──── Cloudflare ───► orbi.twickell.com           - Owner dashboard
   (on customer       named tunnel    (tunnels to Frank's          - Public chat widget
    websites)                          dev Orbi for sales bot)

 Phone calls   ◀────► Twilio ◀────► orbi-brain ◀──────────────► Customer's Orbi (TwiML)
   (caller dials                      routes by "To" number
    customer's
    business number)
```

### Frank's infrastructure (`orbi-brain` + `twickell_live`)

- **billing.twickell.com** — Flask app on Render. Stripe webhooks (create install tokens on checkout). Install-token verify endpoint (`/api/verify/<token>`). LLM proxy at `/v1/chat/completions` (auth via per-customer api_key, routes to HF Inference Qwen 2.5 72B). Billing status check endpoint. Fleet health stub.
- **orbi.twickell.com** — Cloudflared named tunnel → Frank's dev Orbi on port 6000. Serves the sales chat bot + the embed widget JS. Sales bot pitches Orbi to twickell.com visitors.
- **twickell.com** — Vercel-deployed static site (HTML/CSS/JS). Marketing pages, install-notice, terms, privacy, refund, sales-chat embed.

### Customer's local install (`customer_install/`)

- Single PyInstaller-bundled `.exe` (~136 MB) installed to `C:\Program Files\Orbi\` on Windows.
- Ships with **Windows Embeddable Python 3.13** + get-pip.py inside the bundle — customer never installs Python.
- Installer extracts embedded Python → patches `python313._pth` (enables site imports + adds install_dir to sys.path so `import audit`, `import modules` work) → installs pip → installs Flask + Twilio + edge-tts + all requirements directly into embedded Python (no venv — embeddable Python doesn't ship the venv module).
- Service registration via Startup-folder shortcut (not sc.exe — Python scripts can't be proper Windows services without nssm). `launcher.cmd` checks port 5050, spawns `pythonw.exe orbi.py` in background if not listening, waits up to 30s for port to come up, opens Chrome in `--app` mode at `http://localhost:5050/owner/login`.
- Data dir: `C:\Program Files\Orbi\data\` (encrypted nightly backup tarballs).
- Self-contained tunnel: bundled `cloudflared.exe` opens a public quick-tunnel so Twilio can webhook into the customer's home computer through their NAT.
- **Module gating via `config.json` → `enabled_modules`** (list of lowercase strings). All 37 module files in `customer_install/modules/` are present in every install, but only the modules listed here have their routes/aliases activated at runtime. Dev install currently runs `["marketing", "marketing_image"]`. Per-customer this list comes from the brain at activation time based on subscription tier + add-ons (see `orbi.py:180, :2146, :12500+`).

---

## Install flow (customer journey)

1. Customer visits **twickell.com**.
2. Sales-bot Orbi (running on Frank's dev port 6000 via Cloudflare tunnel) greets and walks discovery (Personal/Business/Restaurant → industry → website URL → name → email → phone → seats).
3. Sales bot emits `<<NAV:https://twickell.com/install-notice.html?from=buy&tier=...>>`.
4. **install-notice.html** — DEDICATED pre-purchase disclosure page. Trust-promise callout, install warning text (SmartScreen popup, McAfee warnings, click-through instructions). "Listen to Orbi read this to you" button hits `/tts`. Customer either clicks "Continue to terms & checkout" or "I'll wait until you're signed" (cert-ready waitlist).
5. **terms.html** — full ToS. Customer checks boxes + types name/email to agree.
6. Stripe Checkout (TEST or LIVE based on `STRIPE_MODE`). Customer pays.
7. Stripe webhook → `orbi-brain` creates install token + api_key + provisions Twilio phone number (for Business tier) + emails customer with download link.
8. Customer clicks email link → downloads `orbi-installer.exe` from `billing.twickell.com/download/<token>`.
9. Customer right-clicks `.exe` → "Run as administrator" (required for `C:\Program Files\` write).
10. Customer clicks through Windows SmartScreen and McAfee popups (the disclosure page warned them).
11. Installer prompts for token (manual paste — no clipboard auto-grab as of 2026-06-08). Customer pastes.
12. Installer verifies token with billing brain → marks token consumed → returns api_key + tier + enabled_modules + owner_email.
13. Installer extracts bundled Python → installs pip → installs requirements → writes config.json → creates desktop shortcut + Startup folder shortcut + launcher.cmd.
14. Customer double-clicks "Orbi" desktop icon → launcher.cmd starts orbi.py → Chrome opens to login.
15. First login: bootstrap credentials auto-populated → customer sets their own password.
16. Owner-chat panel appears. First message triggers `_try_onboarding` wizard:
    - Personal / business / restaurant track?
    - What should I call you?
    - **Do you have a website?** (URL → background scrape → auto-fills business_info.json)
    - Tone (friend / professional / playful / formal)
    - Companion mode (personal / business)
17. After onboarding, dashboard is fully usable. Phone receptionist live (if Business tier). Website chat embed code available in Settings.

---

## What's working today

- Stripe webhook → install token + api_key + Twilio number provisioning
- Email delivery with install link (Yahoo SMTP / Resend)
- Bundled Python install end-to-end on Windows (proved on Kathy's PC + Frank's external test PC)
- Token verify, marked-used flow (one-shot, idempotent)
- Embedded Python pip install of requirements (skip-venv path)
- Service install via Startup folder shortcut (no broken sc.exe service)
- **Native WebView2 window via pywebview** (Frank's "looks like a program, not a website" requirement). Launcher.cmd now tries `pythonw orbi_window.py` first, Chrome `--app` second, default browser last. orbi_window.py is at `customer_install/orbi_window.py`, untracked but bundled by the build script.
- Multi-user with per-user data folders, archive-not-delete (90-day purge), and three-mode auth (visitor/staff/owner). RBAC permission matrix in `ROLES_AND_PERMISSIONS.md`.
- Owner-chat first-run onboarding wizard (Personal/Restaurant tracks)
- **Welcome-on-first-chat (added 2026-06-18):** Even when the website scrape pre-fills business_info before the customer reaches the dashboard, Orbi now greets them on the FIRST chat — `_try_onboarding` carries a `welcome_shown` flag separate from `complete`, so a populated business_info no longer leaves her silent.
- Website scrape via site_scraper module (`/api/owner/onboarding/discover`)
- Brain LLM proxy with Cloudflare Mozilla UA workaround
- Sales bot on twickell.com with App Store pricing, install-notice routing, install-heads-up FAQ
- Marketing module + image sub-module (FLUX.1-schnell via HF)
- **web_agent module** (`customer_install/web_agent/`): Playwright-based Chrome browser automation with self-learning recipes (observe → record → replay → repair via brain). Dashboard "Web Tasks" tab exists. Chromium auto-download gated behind `ORBI_INSTALL_CHROMIUM=1` env var to keep install size down for v1.
- **Five learning modules** (`customer_install/modules/`): workflow_learner, preferences, glossary, schedule_patterns, thread_tone. Wired but unobtrusive until they have enough data.
- Daily encrypted local backup tarball
- Cloudflared quick-tunnel for inbound Twilio webhooks
- **Customer-workspace folder** `~\Orbi\` with welcome `README.txt`, plus "Orbi Workspace" desktop shortcut alongside the launcher. Start Menu entry. Both shortcuts use a full-size-set `orbi.ico` (16/24/32/48/64/128/256) generated from the PWA icon-512.png.

---

## What's NOT BUILT yet (honest list)

- **Multi-device sync** — the "3 devices on 1 brain" promise. Phone/tablet thin clients linking to host PC. Currently each install is standalone. ~2-3 weeks of work.
- **Phone/tablet native apps** — no iOS/Android client. Depends on the sync work above.
- **Remote update system** — Frank can't push fixes to existing customer installs. Every code change requires customer to reinstall. ~2-3 days to build (brain command queue + customer polling loop).
- **Auto-update** — Orbi doesn't pull source updates from brain. Built into `updater.py` skeleton but not wired up.
- **Code-signing certificate** — installer is unsigned. Every customer hits SmartScreen + AV popups. Disclosure page bridges the gap until cert is in place. OV cert ~$80-100/yr, EV ~$400/yr. Deferred until first paid customer revenue covers it.
- **Cloud-hosted tier** — for customers without a computer to host. Pricing decided ($+19.99/mo surcharge), not built. Defer until 5-10 local customers.
- **Native nssm bundling** — would let us register Orbi as a proper Windows service. Currently using Startup-folder shortcut as a workaround.
- **Real industry modules** — Restaurant module built. Construction, Law, Medical, Auto, Salon all "coming soon" — not started.
- **Onboarding mid-question scrape feedback** — the website scrape runs in background but Orbi doesn't tell the customer "I found 12 services from your site" mid-conversation.
- **Learning loop — partial.** The "Orbi doesn't know → captures caller contact → asks owner → stores answer → delivers back to original caller" pipeline. Real implementation in `customer_install/modules/learning_loop.py` (not a stub) and imported in `orbi.py:94`, but the full owner-notification + answer-routing pipeline is not yet end-to-end. ~26 hrs estimated remaining.

---

## Bug graveyard / lessons learned (2026-06-08 to 2026-06-18)

Each bullet is a real Windows install bug a customer or test PC surfaced. All fixed in source.

**Kathy install round (6-08 → 6-10):**

- **Cloudflare Bot Fight Mode blocks default Python urllib UA with HTTP 403.** Every brain call needs a Mozilla User-Agent header. Applied to `call_brain` in `llm_client.py` and the billing-check in `orbi.py`.
- **Windows Embeddable Python doesn't ship the `venv` module.** Skip the venv layer, install pip + requirements directly into the embedded Python's site-packages.
- **Embedded Python doesn't ship `setuptools` or `wheel` either.** Source-only packages (e.g. `http-ece` via `pywebpush`) need them. Fix: `pip install setuptools wheel` BEFORE `pip install -r requirements.txt`.
- **Embedded Python runs in ISOLATED mode with `._pth`** and doesn't auto-add the script's directory to sys.path. Fix: patch `python313._pth` during install to include the absolute install_dir path.
- **Em-dash in launcher.cmd comment broke ASCII encoding.** Silent zero-byte launcher.cmd. Fix: cp1252 encoding + catch UnicodeEncodeError.
- **sc.exe service registration silently doesn't work for Python scripts.** Fix: Startup-folder shortcut + launcher.cmd that spawns `pythonw.exe orbi.py` directly.
- **Install-token clipboard auto-grab caused cross-token confusion.** Fix: removed clipboard auto-grab, force manual paste.
- **Owner-chat dashboard has no anti-echo logic.** Mic picks up TTS, transcribes her own voice. Fix: `recognition.stop()` on submit, restart 400ms after speak() finishes.
- **Voice fallback regex picked Australian/UK voices** (Karen, Moira, Tessa, Nicky) on a clean Windows PC without Samantha/Ava installed. Fix: prefer en-US voices explicitly.

**SCS / Frank Hawbolt external test round (6-17 → 6-18):**

- **"Sierra" name bug.** Orbi greeted Frank as "Sierra" when installed against scsplanroom.com — the LLM-driven website scraper hallucinated the first word of the business name ("Sierra Contractors Source") as the owner's first name. Fix in `prompts.py` build_owner_prompt: trust `personality.owner_name` (explicit, typed at first login) over scraped `owner.name`; explicit heuristic in source rejects any scraped owner name that's a prefix of the business name. Plus prompt block forbidding the business-name-as-person mix-up with wrong/right examples.
- **Brochure-tone overview replies.** Asked "tell me about my business," Orbi dumped a markdown brochure with `### Business Information` headers, `**Name**:` bold field labels, and numbered service lists with bolded names. Default Qwen 2.5 72B behavior for "tell me about X" prompts. Fix in `prompts.py`: new "CONVERSATIONAL TONE WITH THE OWNER" block in build_owner_prompt forbidding markdown headers/bold for owner replies, requiring 2-4 sentence prose summaries with a "want me to walk through any of that?" tail, and listing the literal wrong example so the LLM has it as a contrast.
- **Email-derived owner display name.** `install_runtime.py` used `owner_email.split("@",1)[0].title()` which produced wrong-but-confident names like "Frank L Street" from `frank.l.street@…` — even when the actual business owner was someone else (Frank Hawbolt at scsplanroom.com). Fix: stop deriving owner.name from email at install time. Leave it blank; the first-login modal forces the customer to type the real name. Prompts fall back to "the owner" until they do.
- **Staff setup_initial_password overwrote business.owner_name.** When ANY user did their first-login password set, the endpoint wrote their display_name into business.personality.owner_name and business.owner_name — so adding a staff user and having them log in renamed the business's owner. Fix: gate the business writes to `user_rec["role"] == "owner"`. Staff sets their own user record only.
- **Orbi silent on first chat when scrape pre-filled business_info.** `_needs_onboarding()` returned False as soon as `business.name` was non-empty, so `_try_onboarding` short-circuited and Orbi sat waiting for the customer to say something. Fix: split state into `complete` (no more wizard) and `welcome_shown` (one-time greeting). First chat always returns a welcome that summarizes what was scraped and offers next-step choices, regardless of business_info state.
- **Desktop / Start Menu icon looked pixelated.** `orbi.ico` only had 16×16 and 32×32 sizes, so File Explorer upscaled to fill the larger Desktop icon slot. Fix: regenerate `customer_install/icons/orbi.ico` from `pwa/icons/icon-512.png` with sizes (16, 24, 32, 48, 64, 128, 256). Windows now picks the right resolution at any size.
- **Cloudflared file lock blocked clean reinstall.** TEST_INSTALL.bat killed pythonw but not cloudflared.exe, so the old install's cloudflared kept a handle on `bin\cloudflared.exe`. `rmdir /S /Q` threw "Access is denied," left half the prior install on disk, and the next install hung silently at "Installing Python dependencies" when pip ran into the partial install. Fix: TEST_INSTALL.bat now also taskkills `cloudflared.exe`, `orbi-installer.exe`, and `orbi.exe`, sleeps 3 seconds for handles to release, retries the rmdir on failure, and exits clean with a "reboot the test PC" message if it still can't fully clean up — instead of pressing on into a hung install.

**Build/dev environment lessons (6-18):**

- **`customer_install/owner_dashboard` is a symlink** → `../owner_dashboard` (one level up). The build script's `os.walk` crashes on the symlink when packed from a Windows path. Fix when staging the source tree to a Windows build dir: use `rsync -aL` (dereference symlinks) rather than `rsync -a`.
- **Build environment Python lives at `C:\Users\frank\AppData\Local\Programs\Python\Python310\python.exe`** with PyInstaller 6.20.0 pre-installed. WSL can invoke it via `cmd.exe /c "python.exe build_orbi_installer.py --target windows"` as long as cwd is a Windows-side path (UNC `\\wsl.localhost\…` paths are NOT supported by cmd.exe). Staging dir for this session is `/mnt/c/Users/frank/orbi-build/customer_install/`.
- **`REBUILD_ORBI.bat`** at `C:\Users\frank\Downloads\REBUILD_ORBI.bat` points at a STALE source (`Downloads\orbi-customer-install-main\...` from 6-09). It's NOT the path to run for current builds. The current build path is the staging dir above, fed by `rsync -aL` from `/home/frank/orbi_web/customer_install/`.

---

## File layout

```
~/orbi_web/                         (main dev workspace — git repo, pushed to GitHub. ⚠ Working tree currently dirty — see "Uncommitted source state" below.)
  customer_install/                 (what gets bundled into the .exe — runs on dev port 6000; ships as port 5050 on the customer .exe)
    orbi.py                         (the customer-side Flask app — 18,314 lines as of 6-19, all routes)
    prompts.py                      (LLM prompt templates — sales mode, owner mode, public mode)
    voice.py                        (Twilio phone TwiML + edge-tts)
    llm_client.py                   (LLM router with circuit breaker, Mozilla UA for Cloudflare)
    image_gen.py                    (image sub-module — FLUX.1-schnell via HF, wired in orbi.py:63)
    ad_gen.py                       (ad composition — orchestrates LLM + image_gen + PIL, orbi.py:64)
    auth.py                         (three-mode session tokens: visitor / staff / owner)
    users.py                        (multi-user registry, per-user data folders, archive-not-delete)
    onboarding.py                   (first-run wizard; restaurant variant lives here, not in modules/)
    orbi_window.py                  (pywebview WebView2 launcher — the "looks like a program" window)
    backup.py + watchdog.py + updater.py + scheduler.py + service_manager.py + error_reporter.py
                                    (runtime housekeeping)
    audit.py + pre_execute.py + rate_limit.py + safe_send.py + cross_search.py + universal_search.py
                                    (guardrails + search infrastructure)
    briefing.py birthdays.py booking.py caller_history.py chart_gen.py cloudflare_setup.py
      contextual_reminders.py customer_thread.py doc_convert.py email_inbox.py file_fetch.py
      follow_up.py friend_checkin.py gcal.py imap_smtp.py mail_merge.py notifications.py
      ocr.py phone_order.py pptx_gen.py review_responder.py sms_sender.py style_learner.py
      translation.py twilio_provision.py voice_notes.py voicemail.py wellbeing.py
      auto_categorize.py                                  (~30 helper modules at top level — feature-specific. `ls customer_install/*.py` is the live list.)
    site_scraper/                   (LLM-driven website scraper — crawler.py + llm_extract.py + merge.py + http_client.py + link_extractor.py + page_parser.py + storage.py)
    web_agent/                      (Playwright-based browser automation — controller.py + learner.py + page_observer.py + actions.py + session.py + recipes/)
    connectors/                     (third-party integration adapters — small)
    llm_local/                      (local-model harness, optional)
    bin/                            (runtime binaries — ffmpeg / cloudflared / piper that ship with the install)
    icons/                          (orbi.ico full size set 16/24/32/48/64/128/256 — fixed 6-18)
    pwa/                            (per-install PWA shell — manifest + service worker)
    desktop_shortcuts/              (.lnk and .url scaffolding for Start Menu + Desktop + Workspace)
    snapshots/                      (daily `.tar.gz` rolling snapshots — local-only)
    backups/                        (nightly encrypted `.tar.gz.enc` archives — local-only)
    data/                           (live runtime data — per-user folders, sessions, learned answers)
    data.preroll-*/                 (frozen pre-rollout data snapshots from 2026-06-06 — historical, can be archived/pruned)
    config.json                     (per-install config; `enabled_modules` gates which modules activate at runtime. Currently ["marketing","marketing_image"].)
    config.json.template            (defaults shipped in the installer)
    modules/                        (37 module files; ONLY the ones in config.enabled_modules light up)
      marketing.py                  (active — enabled in dev config)
      learning_loop.py              (PARTIAL — real impl, imported in orbi.py:94, full pipeline incomplete)
      memory.py business_info.py calendar.py contacts.py reminders.py
        notes.py tasks.py messages.py reviews.py mood.py gifts.py
        quick_capture.py workspace.py forms.py form_filler.py        (general personal-assistant modules — 15 files)
      workflow_learner.py preferences.py glossary.py
        schedule_patterns.py thread_tone.py                          (5 learning modules — wired, unobtrusive until they have data)
      bids.py change_orders.py closeout_pdf.py daily_logs.py
        invoice_pdf.py invoices.py line_items.py pricing.py
        projects.py proposal_pdf.py subcontractors.py wins.py
        clients.py catalog.py internal_messages.py                   (CONTRACTOR LEFTOVERS — 15 files; shelved 2026-05-31, code preserved but disabled at config level)
    static/                         (chat widget JS + CSS; dashboard.* are SYMLINKS → ~/orbi_web/owner_dashboard/)
    owner_dashboard                 (SYMLINK → ../owner_dashboard. ONE canonical source. No drift risk — previous "SECONDARY physical copy" note was wrong.)
    installer/
      install_runtime.py            (the bundled installer logic — runs inside the .exe, 83,747 bytes)
      build_orbi_installer.py       (build script — PyInstaller wrapper)
      _bundled_runtime/             (embedded Python 3.13 ZIP + get-pip.py)
      _bin_cache/                   (ffmpeg, cloudflared, piper binaries)
      dist/windows/orbi-installer.exe (what billing.twickell.com serves)
    install.sh                      (legacy Linux installer — predates the Windows .exe path. Not currently used.)
    requirements.txt
    orbi_capabilities.md            (high-level capability inventory for reference)
  owner_dashboard/                  (LIVE SOURCE — top-level. customer_install/static/dashboard.* + customer_install/owner_dashboard symlink BOTH point here. Single canonical copy.)
  billing/                          (⚠️ STALE — contains an older stripe_webhook.py (~74 KB, pre-Render) + the original twilio_central.py. The LIVE brain code is at ~/orbi-brain/. ~/orbi-brain/twilio_central.py is a symlink BACK to ~/orbi_web/billing/twilio_central.py, so that file IS live — do not edit billing/stripe_webhook.py as production.)
  brain/                            (early prototype brain_server.py + admin_server.py — superseded by ~/orbi-brain/)
  pwa/                              (manifest + offline shell for PWA install path — top-level copy)
  watchdog/                         (auto-restart guardian — runs under user-systemd as orbi-watchdog.service)
  cross_platform/                   (cross-platform build scaffolding — mac/, windows/, shared/, bin/, dist/ folders are all untracked WIP)
  usb_bundle/                       (offline-USB install path scaffolding)
  ROLES_AND_PERMISSIONS.md          (RBAC permission matrix — visitor / staff / owner)
  SOURCE_OF_TRUTH.md                (this file)
  ARCHITECTURE.md PRICING.md PHASE_*.md MANUAL_TEST_CHECKLIST.md
  FIRST_CUSTOMER_PLAYBOOK.md DEPLOY_CHECKLIST.md MODULE_INVENTORY.md
  README.md

~/orbi-brain/                       (THE LIVE BRAIN — Render-hosted at billing.twickell.com. Also running locally as orbi-stripe.service for dev.)
  stripe_webhook.py                 (Flask app — 2,535 lines as of 6-11. ACTIVE. Routes grouped by surface:
                                     Customer/install:   POST /webhook, GET /api/verify/<token>,
                                                         GET /download/<install_token>, GET /download/by-platform/<platform>
                                     LLM proxy + TTS:    POST /v1/chat/completions, POST /api/brain/tts,
                                                         GET /api/brain/usage/<api_key>
                                     Customer runtime:   GET /api/active/<api_key>, POST /api/heartbeat/<api_key>,
                                                         POST /api/error_report/<api_key>
                                     Twilio:             POST/GET /twilio/voice/<api_key>, POST/GET /twilio/sms/<api_key>
                                     Admin UI + ops:     GET /admin, GET /api/admin/customers,
                                                         POST /api/admin/activate/<api_key>, POST /api/admin/deactivate/<api_key>,
                                                         GET|POST /api/admin/modules/<api_key>,
                                                         GET /api/admin/fleet, GET /api/admin/fleet/inbox
                                     Legal + checkout:   GET|POST /agree/<key>, GET /agree-finalize/<key>,
                                                         GET /checkout/<key>
                                     Self-serve mgmt:    GET /manage, POST /manage/start
                                     Health:             GET /health)
  twilio_central.py                 (central Twilio account number-provisioning — SYMLINK → ~/orbi_web/billing/twilio_central.py. So that file IS live, even though billing/stripe_webhook.py beside it is stale.)
  stripe.env                        (Stripe keys, HF Qwen 2.5 72B token, Twilio creds — gitignored. Backup .bak from 6-05 also present.)
  installs.json (+ .bak files)      (install token store — DATA, careful)
  billing.db                        (per-customer billing state — sqlite, hot file)
  legal_acceptances.jsonl           (ToS + install-notice agreement audit log)
  customer_errors.jsonl             (fleet error reports)
  fleet_inbox.json                  (heartbeat inbox)
  create_stripe_products.py         (⚠️ OBSOLETE — defines stale Small/Medium/Large/Enterprise tiers
                                     at $99/$149/$249/$399. Do NOT re-run. Real pricing is hard-coded
                                     in stripe_webhook.py — Personal/Business/+Industry/+Sub.)

~/twickell_live/                    (the public website — Vercel-deployed, STATIC ONLY)
  website/
    index.html, orbi.html                  (marketing pages)
    install-notice.html                    (pre-purchase disclosure page — sales bot routes here first)
    install.html                           (post-checkout download hub w/ token clipboard auto-copy)
    install-help.html                      (public troubleshooting guide for Windows installer)
    terms.html, privacy.html, refund.html  (legal)
    admin-login.html, admin.html           (Frank's internal demo workspace — noindex)
    scs-login.html, scs-demo.html          (TEMPORARY preview for Sierra Contractors Source — noindex.
                                            Footer "🏗️ SCS Preview" link in index.html. Remove on Frank's signal.)
  vercel.json                       (deploy config: outputDirectory: "website")
  app.py                            (LOCAL DEV ONLY — Flask on port 5001 for TTS + demo chat.
                                     NOT deployed to Vercel. Don't confuse with the brain.)
  render.yaml                       (legacy — Render isn't used for twickell_live; brain is at ~/orbi-brain/)

~/ShadowBridge/bridge_rebuild_20260407/   (🔒 THE RELAY — port 8088 tool runner. UNTOUCHABLE.
                                           Copy from it, never modify. Locked by BACKUP_RULES_LOCKED.txt.)

~/.claude/projects/-home-frank/memory/   (persistent Claude memory — survives sessions)
  MEMORY.md                              (index)
  project_orbi_pricing.md                (pricing + seat/brain/device model)
  project_orbi_brand.md                  (Orbi vs myOrbi vs FST LLC distinction)
  project_install_warning_disclosure.md  (install-notice page policy)
  ... (~30 other memory files)
```

---

## Uncommitted source state (as of 2026-06-20 audit)

⚠ Working tree of `~/orbi_web/` is dirty. **No commits since the 6-18 22:57 SoT save.** All work below is local edits Frank should commit + push before they can ship to a build:

Modified (staged or unstaged):
- `customer_install/orbi.py` (last touched 6-19 00:04)
- `customer_install/imap_smtp.py` (6-19 13:13) — the freshest source edit
- `customer_install/prompts.py` (6-18 15:03, post the 16:19 build cut? confirm)
- `customer_install/llm_client.py` (6-13 16:38)
- `customer_install/voice.py`
- `customer_install/users.py`
- `customer_install/installer/install_runtime.py` (6-18 16:14)
- `customer_install/installer/build_orbi_installer.py`
- `customer_install/config.json.template`
- `customer_install/requirements.txt`
- `customer_install/static/chat.{html,css,js}` + `static/embed.js`
- `owner_dashboard/dashboard.{html,js}` (6-19 00:04 — UI iteration)
- `SOURCE_OF_TRUTH.md` (this file, this audit)

New, untracked (need git add or .gitignore decision):
- `ROLES_AND_PERMISSIONS.md` (newly added but never staged)
- `customer_install/error_reporter.py`
- `customer_install/icons/` (the full-size-set orbi.ico from 6-18)
- `customer_install/bin/` (runtime binaries)
- `customer_install/backups/` + `data.preroll-*/` (mostly belong in .gitignore — local data)
- `cross_platform/{bin,mac,shared,windows,dist,README.md}` (cross-platform build WIP)
- `brain/` (early prototype — may also belong in .gitignore now)

This means the live disk is AHEAD of what GitHub knows about. The 6-18 16:19 `.exe` on Frank's thumb drive was built from the 6-18 source. The 6-19 edits to `orbi.py`, `imap_smtp.py`, and the dashboard would need a fresh `.exe` to land on the test PC.

---

## Status / next steps (rolling)

**Most recent test round (2026-06-17 → 2026-06-18):**
- Frank ran an external test install against scsplanroom.com (Sierra Contractors Source / Frank Hawbolt persona) on his test PC via thumb drive.
- Round 1 surfaced: bad Sierra greeting + brochure-tone overview + Frank-L-Street email-derived name + bad icon.
- Round 2 (after first rebuild) surfaced: staff add overwriting owner_name + Orbi silent on first chat post-scrape + cloudflared file-lock blocking clean reinstall.
- All seven fixed in source 6-18 and bundled into the 6-18 16:19 build. New `.exe` (131 MB) + hardened TEST_INSTALL.bat + fresh token (`inst_aa5ee60d9b8555a40a47922a1b3206c6`) on thumb drive D: as of 17:47. Round 3 install pending.

**Since the 6-18 build cut (2026-06-19 → 2026-06-20):**
- Further edits to `orbi.py`, `imap_smtp.py`, the dashboard, and the chat/embed widgets — all uncommitted (see "Uncommitted source state" above).
- Audit pass on this SoT file (this update) — file counts, route lists, line counts, and the owner_dashboard symlink claim were corrected.
- Round 3 install on the test PC still pending. Either re-cut the `.exe` from current source if the 6-19 edits matter for the round-3 verification, or run round 3 against the 6-18 16:19 build first to verify the seven SCS fixes in isolation.

**Build workflow used this session (deviates from REBUILD_ORBI.bat):**

REBUILD_ORBI.bat points at a stale `Downloads\orbi-customer-install-main` source from 6-09. Instead, builds this session went:

1. Edit source in `/home/frank/orbi_web/customer_install/` (the canonical Linux source).
2. `rsync -aL --delete --exclude=…(big stuff)…` to `/mnt/c/Users/frank/orbi-build/customer_install/`.
3. From WSL: `cd /mnt/c/Users/frank/orbi-build/customer_install/installer && cmd.exe /c "python.exe build_orbi_installer.py --target windows"`.
4. Output: `…/dist/windows/orbi-installer.exe` → cmd.exe `copy /Y` to `D:\orbi-installer.exe`.
5. Frank walks the drive to the test PC, runs `D:\TEST_INSTALL.bat`.

Whole loop is ~2-3 minutes plus walking time. Faster than asking Frank to find a build target on Windows.

**Next iteration:**
1. Test PC round 3: clean reinstall with the 16:19 `.exe` after the hardened TEST_INSTALL.bat clears the cloudflared lock.
2. Verify the three "real quick" fixes from this session: staff add no longer renames owner, owner name no longer derived from email, Orbi greets on first chat.
3. Bring `orbi-install.log` back if anything misbehaves.

**Strategic next builds (post-SCS-round-3):**
1. Remote-update polling system (2-3 days). Brain command queue + customer polling loop + apply-handler. Means fixes reach existing installs without reinstall — would have saved every minute of today's thumb-drive walking.
2. Code-signing cert purchase (when first $$ lands). OV from KSoftware/Sectigo ~$80/yr.
3. Multi-tenant brain on one PC (2-3 weeks). For family-of-N installs.
4. Phone/tablet thin clients (depends on multi-device sync work).

---

## Hard rules (do not regress)

1. **Customer data lives on the customer's computer.** Not in any central database. Stateless brain calls only. Local-first is the moat.
2. **No new spend pre-revenue.** No paid SaaS, VPS, domains. (Exception: HF Inference Providers — Frank funds his HF account, used for Qwen 2.5 72B.)
3. **The relay (`~/ShadowBridge/bridge_rebuild_20260407`) is untouchable.** Copy from it, never modify.
4. **Per-seat means per-BRAIN, not per-device.** A seat covers up to 3 of the buyer's devices on one shared Orbi.
5. **Customer experience is double-click-and-done.** No PowerShell, no terminal, no env files, no GitHub. Everything is in the installer or in dashboard wizards.
6. **Code in git, data not.** Wiping/seeding data files requires explicit owner approval every time.
7. **Push every twickell_deploy fix to origin immediately.** Vercel auto-deploys from main; Frank verifies on the live site, not locally.
8. **Honest framing over cheerleading.** Don't claim "moats" for features competitors can copy in a sprint. Name strengths AND limits. Disclosure beats surprise.
