# Orby Bug Log — Confirmed Fixes Only

Each entry records a bug that was **verified fixed** (symptom gone on a test device).
Failed attempts are omitted — those are in git commit history.
Update this file after every confirmed fix. See SOURCE_OF_TRUTH.md for the rule.

---

## Chat Widget — Voice / STT / Mic

### BUG-001 — Double audio on first greeting (echo overlap)
**Date fixed:** 2026-07-01
**File:** `customer_install/static/chat.js` — mic click handler

**Symptom:** On first mic click, Orby's greeting played twice simultaneously — two voices overlapping.

**Root cause:** `setSpeakerOn(true)` drains `_pendingFirstSpeech` and calls `speak(greetingText)`. Then `deliverSpokenWelcome()` calls `speak(welcomeText)` a split-second later. Both calls compete over the same `_audioEl` audio element. Each `speak()` registers its own `done` closure on `_audioEl.onended`. When the first speak is interrupted (stopSpeaking), its `done1` closure fires and clears `done2` off `_audioEl.onended` — so the second speech never resolves, leaving `_suppressSTT=true` forever and the mic permanently deaf.

**Fix:** Null `_pendingFirstSpeech` BEFORE calling `setSpeakerOn(true)` in the welcome path. The drain finds nothing — `deliverSpokenWelcome` is the only caller of `speak()`.
```javascript
// mic click handler — welcome path
_pendingFirstSpeech = null;        // ← null FIRST
if (!prefs.speakerOn) setSpeakerOn(true);  // drain finds null, skips
setMicOn(true);
deliverSpokenWelcome();
```

---

### BUG-002 — `wantsListening` never set when mic clicked during proactive greeting
**Date fixed:** 2026-07-01
**File:** `customer_install/static/chat.js` — `startListening()`

**Symptom:** Mic worked once (first utterance processed), then died permanently after that.

**Root cause:** `startListening()` checked `isSpeaking` BEFORE setting `wantsListening=true`. If the proactive greeting was still playing (`isSpeaking=true`) when the user clicked the mic, the function returned early and `wantsListening` stayed `false` forever. Every `onend` restart checks `if (wantsListening && !isSpeaking)` — all silently failed.

**Fix:** Set `wantsListening = true` as the very first line in `startListening()`, before any early-return guard.
```javascript
function startListening() {
    wantsListening = true;  // always set intent first
    if (!recognition || isListening || isSpeaking) return;
    safeStart();
}
```

---

### BUG-003 — `recognition.start()` never called in Chrome gesture window (normal flow)
**Date fixed:** 2026-07-01
**File:** `customer_install/static/chat.js` — mic click handler else branch

**Symptom:** After a fresh page refresh, clicking mic showed it as "on" and displayed "Listening" in the status bar, but Orby was completely deaf. Toggling mic off and on once fixed it.

**Root cause:** Chrome's iframe SpeechRecognition requires the VERY FIRST `recognition.start()` call to happen inside a user-gesture event handler. All subsequent restarts (from timers, `onend`, `armMic`) can be outside the gesture. The `history.length === 0` welcome path (which had the `safeStart()` call) NEVER runs in normal use because `_fireProactiveGreeting()` adds the server greeting to `history` before the user clicks the mic. The `else` branch — the path that actually runs — had no `safeStart()` call at all, so Chrome never got its gesture-window first call. Every post-TTS restart was silently rejected.

**Fix:** Add `safeStart()` directly in the `else` branch of the mic click handler, called synchronously inside the click event (gesture window).
```javascript
} else {
    if (turningOn && !prefs.speakerOn) setSpeakerOn(true);
    setMicOn(turningOn);
    if (turningOn && !isListening) safeStart();  // gesture window — must be here
}
```

---

### BUG-004 — STT echo during proactive greeting (Orby hears herself)
**Date fixed:** 2026-07-01
**File:** `customer_install/static/chat.js` — `recognition.onresult`

**Symptom:** While Orby was speaking her greeting, the mic picked up her own voice. STT transcribed it and in some cases sent it to the LLM as a user message, putting the widget in a stuck "thinking" state.

**Root cause:** When the mic was turned on while the proactive greeting was playing, recognition ran concurrently with TTS (no anti-echo, because `wasMicOn=false` at the time the proactive `speak()` started). Any STT result that fired just as `isSpeaking` cleared could slip through to `send()`.

**Fix:** In `recognition.onresult`, check `isSpeaking` directly instead of a separate `_suppressSTT` flag. Any result that arrives while Orby is speaking is discarded. Covers the proactive greeting and all future TTS automatically.
```javascript
recognition.onresult = (event) => {
    if (isSpeaking) { clearInterim(); return; }
    // ... normal result handling
};
```

---

### BUG-005 — Stale service worker serves old `chat.js` after every push
**Date:** Ongoing (mitigation in place)
**File:** `customer_install/pwa/service-worker.js`

**Symptom:** Pushing a new `chat.js` had no effect on the live site. Old behavior persisted even after hard refresh.

**Root cause:** The PWA service worker uses cache-first for `/static/`. New deployments are invisible until the cache version string is bumped, forcing the SW to install fresh and purge the old cache.

**Mitigation:** Bump `ORBI_CACHE_V1` constant (`"orbi-cache-vN"`) in `service-worker.js` with every push that changes `chat.js`, `chat.css`, `embed.js`, or any other file under `/static/` or `/pwa/`.

**If a browser is still stuck (SW update didn't take):** Chrome DevTools → Application → Service Workers → Unregister → Ctrl+Shift+R.

---

---

## Sales Bot — prompts / chat.js / vola.py

### BUG-006 — STT misreads "Orby" as "Orbi", "Orbee", "Orbie"
**Date fixed:** 2026-07-01
**File:** `customer_install/static/chat.js` — `recognition.onresult`

**Symptom:** When a visitor said "Orby" out loud, Chrome STT transcribed it as "Orbi", "Orbee", or "Orbie". The LLM received the misspelling and sometimes echoed it back in its reply.

**Root cause:** Chrome SpeechRecognition phonetically interprets "Orby" as a long-E ending. The transcript was sent to the LLM verbatim with the wrong spelling.

**Fix:** Normalize the final transcript before `send()` using a regex replace. Also added an explicit spelling rule to `prompts.py` so the LLM knows to write ORBY in its own output.
```javascript
finalText = finalText
  .replace(/\bOrb(?:i|ee|ie|ey)\b/g, 'Orby')
  .replace(/\bORB(?:I|EE|IE|EY)\b/g, 'ORBY')
  .replace(/\borb(?:i|ee|ie|ey)\b/g, 'orby');
```

---

### BUG-007 — LLM outputs internal phase notes and parenthetical plans to the customer
**Date fixed:** 2026-07-01
**File:** `customer_install/prompts.py` — sales brief template

**Symptom:** Orby would send messages like "(After getting their name, I'll ask about their website...)" or "(Standing by while I review your site — we'll get Orby perfectly matched to your needs.)" or "🚨 Key reminders for this phase:" directly in the customer-facing chat window.

**Root cause:** The sales prompt uses phase labels, note blocks, and good/bad examples as instructional scaffolding. Llama/Qwen sometimes mirrors the prompt's structural formatting back as output, treating internal annotations as text to emit.

**Fix:** Added an explicit ban at the top of the prompt listing every form of internal leakage: phase names, note blocks, reminder lists, and parenthetical "here's what I'm doing next" narration. Second fix applied when parenthetical plan-narration persisted despite the first rule.
```
🚨 NEVER OUTPUT INTERNAL NOTES — Never write phase names, note blocks,
reminder lists, or parenthetical plans for what you'll do next.
If it sounds like a stage direction or a memo to yourself, cut it.
```

---

### BUG-008 — STT breaks domain URLs with spaces; Orby scrapes only the last word
**Date fixed:** 2026-07-01
**File:** `customer_install/prompts.py` — STT mishear rule

**Symptom:** Visitor said "SCS plan room dot com" (voice). STT returned "SCS plan room.com". Orby extracted only "room.com" as the URL to scrape — picked up just the last space-separated fragment containing a dot extension.

**Root cause:** LLM saw "SCS plan room.com" and treated it as three separate words, taking the `.com`-suffixed word ("room.com") as the domain.

**Fix:** Added a rule to the STT MISHEAR section: when a URL comes in with spaces, reconstruct it by stripping spaces between the parts and keeping the extension. "SCS plan room.com" → "scsplanroom.com".

---

### BUG-009 — User message during scrape triggers duplicate SCRAPE marker, hits rate limit
**Date fixed:** 2026-07-01
**File:** `customer_install/static/chat.js` — `_orchestrateSalesScrape()`

**Symptom:** Orby said "Cool, looking at scsplanroom.com now..." and started the scrape. Visitor typed "hello" a minute later while waiting. Orby replied with "I've already looked at one site recently — give me a few minutes before I scan another." Conversation was broken. Visitor had to start over.

**Root cause:** When the visitor sent "hello" mid-scrape, `send()` hit the LLM with the conversation history. The LLM saw the prior "Cool, looking at X now..." turn with no scrape result yet and re-emitted `<<SCRAPE:X>>`. The second `/api/public/sales_scrape` call hit the 10-minute per-IP rate limit and returned the hardcoded error message. Meanwhile the first scrape eventually finished and sent its silent `(continue)`, producing the correct reveal — but the conversation was already broken.

**Fix:** Added `_scrapeInProgress` flag. While scraping, any user message gets a local "Still scanning your site — just another moment..." response and never reaches the LLM. The original poll loop continues and fires the silent `(continue)` when done.
```javascript
if (_scrapeInProgress && !opts.silent) {
  addBubble('assistant', "Still scanning your site — just another moment...", { tier: 'local' });
  return;
}
```

---

### BUG-010 — TTS mispronounces comma-separated dollar amounts
**Date fixed:** 2026-07-01
**File:** `customer_install/vola.py` — `tts()` route

**Symptom:** Orby said "$2,294.49" as "two dollars" [pause] "two hundred ninety-four" [pause] "zero dollars and forty-nine cents" instead of "two thousand two hundred ninety-four dollars and forty-nine cents."

**Root cause:** TTS engines treat a comma as a natural pause boundary. "$2,294.49" was parsed as three tokens: "$2", "294", "$0.49".

**Fix:** Strip commas from inside dollar amounts before sending text to the TTS engine. Regex runs on the text inside `tts()` before it reaches either Kokoro or edge_tts.
```python
import re as _re_tts
text = _re_tts.sub(
    r'\$(\d{1,3}(?:,\d{3})+(?:\.\d+)?)',
    lambda m: '$' + m.group(1).replace(',', ''),
    text,
)
```

---

## Owner Chat — /api/owner/chat

### BUG-011 — /api/owner/chat returns 500 on every request (KeyError: data_dir)
**Date fixed:** 2026-07-01
**File:** `customer_install/vola.py` — `owner_chat()` route + `_try_widget_install_chat()`

**Symptom:** Every POST to `/api/owner/chat` returned HTTP 500 "Something broke". Construction module testing, owner dashboard chat — all broken.

**Root cause (1):** `_try_widget_install_chat()` called `data_dir = Path(user_rec["data_dir"])` unconditionally at the top of the function, before any message-trigger check. `user_rec` from `auth.require_user()` only has username/role/status — no `data_dir`. Crash happened on every request.

**Root cause (2):** `_try_widget_install_chat()` also called `_session_get()` and `_session_set()` which were never defined anywhere in vola.py (written but stub was missing).

**Fix:**
1. In `owner_chat()`, inject `user_rec.setdefault("data_dir", str(DATA_DIR))` and `user_rec.setdefault("user_dir", str(user_dir))` after computing them.
2. Added `_session_get()` / `_session_set()` as module-level helpers backed by `_widget_sessions: dict[str, dict]` — simple in-memory store keyed by username.

---

### BUG-012 — CO/review/portal signing URLs default to port 5050 (Frank's personal Orby)
**Date fixed:** 2026-07-01
**File:** `customer_install/vola.py` — `_co_sign_url()` and related URL builders

**Symptom:** Change order signing links, review links, and client portal links all pointed to `http://127.0.0.1:5050/...` instead of the configured tunnel URL or port 6000 (dev).

**Root cause:** URL builder functions called `CONFIG.get("tunnel_url")` but the tunnel URL is nested at `CONFIG["server"]["tunnel_url"]`. The key lookup returned `None` and fell back to the hardcoded `"http://127.0.0.1:5050"` literal.

**Fix:** Updated all 5 occurrences to check `(CONFIG.get("server") or {}).get("tunnel_url")` first, then fall back to the flat key, then dynamic port from config, then 5050. Signing links now correctly use `https://orbi.twickell.com/...`.

---

### BUG-013 — Daily log crew parsing ignores "and" as separator
**Date fixed:** 2026-07-01
**File:** `customer_install/vola.py` — `_parse_daily_log_message()`

**Symptom:** "crew: Mike and Jose" only captured "Mike". "Jose" was left in the work description text.

**Root cause:** Crew regex split on `[+,&]` but not the word "and". "Mike and Jose" matched "Mike" (first name before the unsupported separator) and stopped.

**Fix:** Added `|\band\b` to both the crew capture regex and the split pattern.
```python
_re.search(r"crew[:\s]+((?:[A-Z][a-z]+(?:\s*(?:[+,&]|\band\b)\s*[A-Z][a-z]+)*))", ...)
crew = [n.strip() for n in _re.split(r"[+,&]|\band\b", raw) if n.strip()]
```

---

## How to use this file

- **One entry per confirmed fix.** If you try three things and only the third works, write only the third.
- **Root cause first.** The symptom is what you see; the root cause is why it happened. Both matter.
- **Keep code snippets short** — just enough to show what changed.
- **Date the fix** so you know how old the entry is when revisiting.
