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

## How to use this file

- **One entry per confirmed fix.** If you try three things and only the third works, write only the third.
- **Root cause first.** The symptom is what you see; the root cause is why it happened. Both matter.
- **Keep code snippets short** — just enough to show what changed.
- **Date the fix** so you know how old the entry is when revisiting.
