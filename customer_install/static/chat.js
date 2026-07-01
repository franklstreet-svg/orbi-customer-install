/* Orbi public chat widget — floating launcher, sticky mic + speaker, anti-echo */

(function () {
  'use strict';

  // ------------------------------------------------------------------
  // DOM references
  // ------------------------------------------------------------------
  const launcher       = document.getElementById('orbi-launcher');
  const launcherBadge  = document.getElementById('launcher-badge');
  const panel          = document.getElementById('orbi-panel');
  const panelClose     = document.getElementById('panel-close');
  const clearBtn       = document.getElementById('clear-btn');
  const businessName   = document.getElementById('business-name');
  const landingBusiness = document.getElementById('landing-business');
  const landingTagline  = document.getElementById('landing-tagline');
  const connStatus     = document.getElementById('connection-status');
  const stateBar       = document.getElementById('state-bar');
  const chatArea       = document.getElementById('chat-area');
  const input          = document.getElementById('input');
  const sendBtn        = document.getElementById('send-btn');
  const micToggle      = document.getElementById('mic-toggle');
  const speakerToggle  = document.getElementById('speaker-toggle');
  const avatar         = document.getElementById('orbi-avatar');
  const welcomeEl      = document.getElementById('welcome');
  const scrollBtn      = document.getElementById('scroll-bottom-btn');
  const scrollBadge    = document.getElementById('scroll-bottom-badge');

  // ------------------------------------------------------------------
  // Persistent state (survives reloads on the same device)
  // ------------------------------------------------------------------
  const PREFS_KEY = 'orbi_prefs_v1';
  const prefs = loadPrefs();
  function loadPrefs() {
    try {
      return Object.assign({ micOn: false, speakerOn: true, panelOpen: false },
                           JSON.parse(localStorage.getItem(PREFS_KEY) || '{}'));
    } catch { return { micOn: false, speakerOn: true, panelOpen: false }; }
  }
  function savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch {}
  }

  // ------------------------------------------------------------------
  // Conversation state (in-memory only, never persisted)
  // ------------------------------------------------------------------
  // Chat history is keyed only by parent-page origin so the conversation
  // continues as the visitor navigates between pages of a multi-page site
  // (purblum.com /menu → /order → /contact …). sessionStorage is per-tab,
  // so:
  //   • Same tab, any page on the same site → history persists ✓
  //   • Tab closes (or browser restart) → sessionStorage wiped → fresh
  //     greeting on next visit ✓
  //
  // Earlier this key also included `_=<timestamp>` from embed.js's cache-
  // buster, which inadvertently rotated on every page load and made Orby
  // re-greet from the top on every navigation. Removed 2026-06-03.
  //
  // Visitor profile (name + phone) lives separately in localStorage so
  // Orby still recognizes the visitor on their next visit.
  const _qs = new URLSearchParams(window.location.search);
  const HIST_KEY = 'orbi_chat_history__' +
    (_qs.get('parent') || window.location.origin);
  function _loadHistory() {
    try {
      const raw = sessionStorage.getItem(HIST_KEY);
      if (!raw) return [];
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.slice(-40) : [];
    } catch { return []; }
  }
  function _saveHistory(h) {
    try { sessionStorage.setItem(HIST_KEY, JSON.stringify(h.slice(-40))); } catch {}
  }
  let history = _loadHistory();
  let visitor = loadVisitorInfo();
  let unread  = 0;

  // ------------------------------------------------------------------
  // Embed detection — if we're inside an iframe from embed.js, behave
  // accordingly: hide our own launcher (the parent has its own) and
  // pipe unread/open/close events to the parent window.
  // ------------------------------------------------------------------
  const IS_EMBED = (window.location.search || '').includes('embed=1') ||
                   (window.self !== window.top);
  const EMBED_PARENT_ORIGIN = (function () {
    try { return window.parent.location.origin; } catch { return '*'; }
  })();

  function notifyParent(type, data = {}) {
    if (!IS_EMBED || window.parent === window.self) return;
    try { window.parent.postMessage({ type, ...data }, '*'); } catch {}
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    // Mobile audio gate — any direct tap inside the iframe is a user-
    // gesture context, which is what mobile browsers need to start audio.
    // If we have a queued first speech (the proactive greeting from before
    // the user interacted), drain it here.
    const _drainOnFirstTap = () => {
      _unlockMobileAudio();
      if (_pendingFirstSpeech && prefs.speakerOn) {
        const txt = _pendingFirstSpeech;
        _pendingFirstSpeech = null;
        try { Promise.resolve(speak(txt)).finally(_markGreetingDone); } catch { _markGreetingDone(); }
      }
    };
    document.addEventListener('click', _drainOnFirstTap, { once: true });
    document.addEventListener('touchstart', _drainOnFirstTap, { once: true });

    if (IS_EMBED) {
      // Embed iframe — auto-open panel, hide standalone launcher (parent
      // page already triggered this via its own gesture).
      launcher.style.display = 'none';
      document.querySelector('.landing')?.remove();
      document.body.classList.add('fullpage-chat');
      panel.classList.add('open', 'embed-mode');
      panel.setAttribute('aria-hidden', 'false');
      panelClose.addEventListener('click', () => notifyParent('orbi:close'));
    } else {
      // Standalone (visiting / directly in a browser/desktop app window).
      // Keep launcher visible and landing card shown until user clicks
      // "Talk to Orby" — that click is the user-gesture context that
      // unlocks audio so the proactive greeting actually plays out loud.
      panel.classList.add('standalone-mode');
      panelClose.style.display = 'none';
    }
    setupLauncher();
    setupComposer();
    setupToggles();
    setupVoice();
    setupClearButton();
    setupScrollWatch();
    setupNetworkWatch();
    loadBusinessGreeting();
    applyPrefs();
    // If we restored history from localStorage (visitor navigating to a
    // new page on the same site), re-render the past bubbles so the
    // conversation visually continues instead of looking like a fresh start.
    if (history.length > 0) {
      document.getElementById('welcome')?.remove();
      history.forEach((m) => {
        if (m.role === 'user' || m.role === 'assistant') addBubble(m.role, m.content);
      });
    }
    // Frank 2026-06-23: mic stays OFF by default. Auto-turning mic on
    // after the greeting failed on iOS anyway (post-await, no gesture)
    // and confused customers — many don't realize the mic light is
    // already on. Customer taps the mic icon when they want to talk;
    // that tap unlocks audio + turns speaker on + starts the mic in
    // one gesture (see micToggle handler in setupToggles).
    if (IS_EMBED) {
      setTimeout(() => {
        if (!prefs.speakerOn) setSpeakerOn(true);
      }, 100);
    }
  });

  function setupClearButton() {
    clearBtn?.addEventListener('click', () => {
      if (history.length === 0) return;
      if (!confirm('Clear this conversation?')) return;
      history = [];
      _saveHistory(history);  // also clear localStorage so it doesn't rehydrate
      chatArea.innerHTML = '';
      const welcome = document.createElement('div');
      welcome.className = 'welcome';
      welcome.id = 'welcome';
      welcome.innerHTML = `
        <div class="welcome-bubble">
          <h2 id="welcome-title">Hi! How can I help?</h2>
          <p id="welcome-sub">Ask me anything — hours, services, prices, or anything else.</p>
        </div>
        <div class="quick-actions" id="quick-actions"></div>`;
      chatArea.appendChild(welcome);
      stopSpeaking();
      loadBusinessGreeting();
    });
  }

  function setupScrollWatch() {
    let pendingMessages = 0;
    chatArea.addEventListener('scroll', () => {
      const atBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < 40;
      if (atBottom) {
        scrollBtn.hidden = true;
        scrollBadge.hidden = true;
        pendingMessages = 0;
      }
    });
    scrollBtn?.addEventListener('click', () => {
      chatArea.scrollTo({ top: chatArea.scrollHeight, behavior: 'smooth' });
      scrollBtn.hidden = true;
      scrollBadge.hidden = true;
      pendingMessages = 0;
    });
    // Expose helper so message renderer can update
    window.__orbiOnNewMessage = (fromAssistant) => {
      const atBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < 60;
      if (atBottom) {
        chatArea.scrollTop = chatArea.scrollHeight;
      } else if (fromAssistant) {
        pendingMessages++;
        scrollBtn.hidden = false;
        scrollBadge.hidden = false;
        scrollBadge.textContent = pendingMessages > 9 ? '9+' : String(pendingMessages);
      }
    };
  }

  function setupNetworkWatch() {
    window.addEventListener('online', () => {
      console.log('[Orbi] back online');
      const banner = document.querySelector('.offline-banner');
      if (banner) banner.remove();
    });
    window.addEventListener('offline', () => {
      console.log('[Orbi] gone offline');
      showOfflineBanner("You're offline. Orby will reconnect when your internet is back.");
    });
  }

  function applyPrefs() {
    // Standalone mode requires a user-gesture (the launcher click) to
    // open the panel — otherwise the browser blocks audio playback and
    // the proactive greeting never plays out loud. Skip the auto-reopen
    // here; the user clicks "Talk to Orbi" to start every session.
    if (prefs.panelOpen && IS_EMBED) openPanel(false);
    setSpeakerOn(prefs.speakerOn, /*persist*/ false);
    if (prefs.micOn && IS_EMBED) setMicOn(true, /*persist*/ false);
  }

  // ------------------------------------------------------------------
  // Launcher button (open/close panel)
  // ------------------------------------------------------------------
  function setupLauncher() {
    launcher.addEventListener('click', () => {
      if (panel.classList.contains('open')) closePanel();
      else openPanel();
    });
    panelClose.addEventListener('click', closePanel);
  }

  async function openPanel(persist = true) {
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    launcher.classList.add('open');
    launcher.setAttribute('aria-label', 'Close chat with Orby');
    // In standalone mode, take over the screen (drop the landing card
    // + go fullscreen). Embed mode is already auto-fullpage at boot.
    if (!IS_EMBED) {
      document.querySelector('.landing')?.remove();
      document.body.classList.add('fullpage-chat');
    }
    unread = 0;
    updateBadge();
    setTimeout(() => input.focus(), 200);
    if (persist) { prefs.panelOpen = true; savePrefs(); }
    // CRITICAL: do these SYNCHRONOUSLY first. We're inside a user-gesture
    // handler (the launcher click), and the audio unlock + speaker-on
    // must run in this gesture context for the browser to allow playback.
    // Any `await` here would drop the gesture context and silently block
    // the proactive greeting.
    _unlockMobileAudio();
    if (!prefs.speakerOn) setSpeakerOn(true);
    // Frank 2026-06-23: mic stays OFF on panel open. Customer taps the
    // mic icon when they want to talk — that tap is the gesture that
    // also turns on the speaker (already on by default here) and primes
    // audio. Defaulting mic OFF avoids the confusing "is the mic on
    // already? do I need to tap it?" UX and gives us the gesture iOS
    // requires before recording.
  }

  // Mobile audio unlock — two-pronged approach:
  //
  //   1. A persistent HTMLAudioElement that gets unlocked ONCE in a
  //      gesture handler via a silent-WAV play. iOS keys audio unlock
  //      to specific elements — once THIS element is unlocked, we
  //      keep reusing it (swap .src instead of creating new Audio).
  //
  //   2. A Web Audio API AudioContext kept resumed as a backup for
  //      replies that arrive while the mic recognition was running
  //      (iOS PlayAndRecord mode can otherwise route audio to the
  //      receiver/earpiece or silently fail).
  //
  // The persistent-element path is the primary; WebAudio is fallback.
  let _audioCtx = null;
  let _audioUnlocked = false;
  let _currentSource = null;  // active WebAudio BufferSource (so stopSpeaking can cancel)
  const _audioEl = new Audio();
  _audioEl.preload = 'auto';
  // CRITICAL on iOS Safari:
  //  - playsInline = true: without this, iOS tries to go fullscreen
  //    (treats it as video) and silently refuses to play.
  //  - in-DOM: detached <audio> elements (created with `new Audio()`
  //    but never appended) are blocked on iOS. Append to body, but
  //    keep it visually hidden.
  _audioEl.setAttribute('playsinline', '');
  _audioEl.setAttribute('webkit-playsinline', '');
  _audioEl.playsInline = true;
  _audioEl.style.cssText = 'position:absolute;width:0;height:0;visibility:hidden;pointer-events:none;';
  if (document.body) {
    document.body.appendChild(_audioEl);
  } else {
    // Body not parsed yet; attach as soon as it is
    document.addEventListener('DOMContentLoaded',
      () => document.body.appendChild(_audioEl), { once: true });
  }

  // Frank 2026-06-23: was single-shot (return early if _audioUnlocked).
  // iOS routinely re-suspends the AudioContext after a tab switch / idle
  // window, and the single-shot pattern meant we never re-armed it,
  // causing the "type 4-5 times to wake her up" flake. Now: run every
  // gesture, idempotent, cheap. Sets _audioUnlocked true on first
  // success and starts a heartbeat to prevent iOS re-suspension.
  function _unlockMobileAudio() {
    try {
      // (1) Persistent <audio> element: prime with silent WAV in the
      // current gesture context. Once this play succeeds, the element
      // stays unlocked for the page lifetime no matter how many src
      // swaps we do.
      // Use the server's TTS endpoint with a single-character silent input
// instead of a tiny base64 WAV — iOS sometimes refuses to commit the
// audio unlock from a 36-byte WAV but accepts it from a real streaming
// MP3 of any duration. The space-only text produces near-silent audio.
_audioEl.src = '/tts?text=%20&silent=1';
      _audioEl.volume = 0;
      const p = _audioEl.play();
      if (p && typeof p.then === 'function') {
        p.catch(() => {});
      }

      // (2) AudioContext fallback path.
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) {
        if (!_audioCtx) _audioCtx = new Ctx();
        if (_audioCtx.state === 'suspended') {
          _audioCtx.resume().catch(() => {});
        }
        const buffer = _audioCtx.createBuffer(1, 1, 22050);
        const source = _audioCtx.createBufferSource();
        source.buffer = buffer;
        source.connect(_audioCtx.destination);
        try { source.start(0); } catch {}
      }

      _audioUnlocked = true;
      _startAudioHeartbeat();
    } catch {}
  }
  // Once the AudioContext is alive, keep it alive with a 1-sample silent
  // buffer every 3 seconds. iOS treats this as continuous output and
  // never suspends the session. Prevents "she's silent after I haven't
  // touched the panel for 30 seconds" — common in voice mode.
  let _heartbeatTimer = null;
  function _startAudioHeartbeat() {
    if (_heartbeatTimer || !_audioCtx) return;
    _heartbeatTimer = setInterval(() => {
      const ctx = _audioCtx;
      if (!ctx || ctx.state !== 'running') return;
      try {
        const buf = ctx.createBuffer(1, 1, 22050);
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        src.start(0);
      } catch {}
    }, 3000);
  }

  // Mobile audio gate — Orby's first message (the proactive greeting) is
  // generated BEFORE the user has clicked anything inside the iframe, so
  // mobile browsers block the playback. We queue the text here; the next
  // time we get a user-gesture context (the mic permission "Allow" click
  // firing recognition.onstart, OR any direct tap inside the iframe), we
  // play the queued text from THAT context.
  let _pendingFirstSpeech = null;

  // Boot sequencer — mic auto-on waits until the proactive greeting has
  // finished speaking (or 8s failsafe). This keeps the status flow correct:
  // "Speaking..." appears first, then "Listening". Without this gate, mic
  // turns on at boot and the user sees "Listening" before she's said hi.
  let _greetingDoneResolve = null;
  const _greetingDone = new Promise(resolve => {
    _greetingDoneResolve = resolve;
    setTimeout(() => { if (_greetingDoneResolve) { _greetingDoneResolve(); _greetingDoneResolve = null; } }, 8000);
  });
  function _markGreetingDone() {
    if (_greetingDoneResolve) { _greetingDoneResolve(); _greetingDoneResolve = null; }
  }

  function closePanel(persist = true) {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
    launcher.classList.remove('open');
    launcher.setAttribute('aria-label', 'Open chat with Orby');
    // Closing the panel does NOT turn off mic/speaker toggles — they stay sticky
    // as Frank requested. Only stop active listening so we're not eavesdropping.
    stopListening();
    stopSpeaking();
    if (persist) { prefs.panelOpen = false; savePrefs(); }
  }

  function updateBadge() {
    if (unread > 0) {
      launcherBadge.textContent = unread > 9 ? '9+' : String(unread);
      launcherBadge.hidden = false;
    } else {
      launcherBadge.hidden = true;
    }
  }

  // ------------------------------------------------------------------
  // Mic + Speaker toggles (sticky)
  // ------------------------------------------------------------------
  function setupToggles() {
    // CRITICAL on iOS: both toggle taps are direct user gestures — the
    // ONLY windows during which we can prime the audio element so iOS
    // Safari trusts it to play unattended later. We unlock here even
    // when the user is turning the toggle OFF, because the next tap to
    // turn it back on still benefits from already-unlocked audio.
    micToggle.addEventListener('click', () => {
      // Frank 2026-06-23: one tap = full voice conversation. Unlock
      // audio, force speaker on (else her replies are silent), then
      // toggle mic. Speaker stays sticky after mic is later turned off.
      _unlockMobileAudio();
      const turningOn = !prefs.micOn;
      if (turningOn && !prefs.speakerOn) setSpeakerOn(true);
      // If this is the FIRST mic-on tap in this conversation, deliver
      // the spoken+typed welcome before turning the mic on. That way
      // Orby actually GREETS the visitor in her voice (per Frank's
      // request) instead of sitting there with a canned silent welcome
      // bubble. Subsequent mic taps don't re-greet.
      if (turningOn && history.length === 0 && !_welcomeDelivered) {
        deliverSpokenWelcome().finally(() => setMicOn(true));
      } else {
        setMicOn(turningOn);
      }
    });
    speakerToggle.addEventListener('click', () => {
      _unlockMobileAudio();   // gesture-time unlock
      setSpeakerOn(!prefs.speakerOn);
      // After unlock + speaker on, play a tiny inaudible chirp through
      // the persistent audio element so iOS records "this element has
      // been allowed to play audio." All subsequent speak() calls reuse
      // the same element via src-swap and ride that permission.
      if (prefs.speakerOn) {
        try {
          // Use the server's TTS endpoint with a single-character silent input
// instead of a tiny base64 WAV — iOS sometimes refuses to commit the
// audio unlock from a 36-byte WAV but accepts it from a real streaming
// MP3 of any duration. The space-only text produces near-silent audio.
_audioEl.src = '/tts?text=%20&silent=1';
          _audioEl.volume = 0;
          const p = _audioEl.play();
          if (p && typeof p.then === 'function') p.catch(() => {});
        } catch {}
      }
    });
  }

  function setMicOn(on, persist = true) {
    prefs.micOn = on;
    micToggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    micToggle.title = 'Microphone (' + (on ? 'on' : 'off') + ')';
    if (on) {
      // Don't start listening if Orbi is currently speaking (anti-echo)
      if (!isSpeaking) startListening();
    } else {
      stopListening();
    }
    if (persist) savePrefs();
  }

  function setSpeakerOn(on, persist = true) {
    prefs.speakerOn = on;
    speakerToggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    speakerToggle.title = 'Speaker (' + (on ? 'on' : 'off') + ')';
    if (!on) stopSpeaking();
    if (persist) savePrefs();
    // If we just turned the speaker ON and there's a queued first-greeting
    // that never got to play (because speaker was off when greeting fired,
    // or because the browser blocked autoplay), drain it now. This is the
    // path that gets the proactive greeting actually heard the moment the
    // speaker becomes available.
    if (on && _pendingFirstSpeech) {
      const txt = _pendingFirstSpeech;
      _pendingFirstSpeech = null;
      try { Promise.resolve(speak(txt)).finally(_markGreetingDone); } catch { _markGreetingDone(); }
    }
  }

  // ------------------------------------------------------------------
  // State bar
  // ------------------------------------------------------------------
  function setStateBar(text, cls) {
    if (!text) {
      stateBar.hidden = true;
      stateBar.className = 'orbi-state-bar';
      return;
    }
    stateBar.hidden = false;
    stateBar.className = 'orbi-state-bar ' + (cls || '');
    stateBar.textContent = text;
  }

  // ------------------------------------------------------------------
  // Composer (text)
  // ------------------------------------------------------------------
  function setupComposer() {
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
      sendBtn.disabled = !input.value.trim();
    });
    input.addEventListener('keydown', (e) => {
      // Every keystroke is a user gesture — keep iOS audio session alive
      // even between Orby's replies in typed-only conversations.
      try { _unlockMobileAudio(); } catch {}
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send(input.value);
      }
    });
    sendBtn.addEventListener('click', () => {
      // Send button is a guaranteed user gesture — opportune moment to
      // unlock audio so Orby's reply will actually play on iOS.
      _unlockMobileAudio();
      send(input.value);
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
      // Esc clears input or stops speech
      if (e.key === 'Escape') {
        if (isSpeaking) {
          stopSpeaking();
        } else if (document.activeElement === input && input.value) {
          input.value = '';
          input.style.height = 'auto';
          sendBtn.disabled = true;
        }
      }
      // Ctrl/Cmd+L clears chat
      if ((e.ctrlKey || e.metaKey) && e.key === 'l' && !document.activeElement?.matches('input, textarea')) {
        e.preventDefault();
        clearBtn?.click();
      }
    });
  }

  // ------------------------------------------------------------------
  // Send a message (text or transcribed voice) and play TTS reply if speaker on
  // ------------------------------------------------------------------
  let isSending = false;
  // Sales-bot scrape orchestration: when the LLM emits <<SCRAPE:url>>,
  // we kick off /api/public/sales_scrape, poll for completion, then send
  // a silent "continue" turn carrying prospect_url. From then on, every
  // /chat in this session forwards prospect_url so the LLM keeps the
  // prospect's business in its context.
  let _pendingProspectUrl = null;

  async function send(text, opts) {
    opts = opts || {};
    text = (text || '').trim();
    if (!text || isSending) return;
    // We're inside a user-gesture handler here (sendBtn click or Enter
    // keydown). Unlock mobile audio BEFORE the async fetch below so the
    // TTS playback on the reply can sound. After the await, we'd be out
    // of gesture context and any first-time unlock would be too late.
    _unlockMobileAudio();
    isSending = true;
    sendBtn.disabled = true;

    welcomeEl?.remove();
    clearInterim();
    // In silent mode (sales-scrape continuation), skip the user-bubble so
    // the visitor never sees "(continue)" — but DO push to history so the
    // LLM sees the conversation continuing.
    if (!opts.silent) {
      addBubble('user', text);
    }
    history.push({ role: 'user', content: text });
    _saveHistory(history);

    if (!opts.silent) {
      input.value = '';
      input.style.height = 'auto';
    }

    setStateBar('Thinking...', 'thinking');
    const thinkingEl = addThinking();

    try {
      // If this chat shell was loaded inside an embed iframe, the parent
      // page's origin was passed as ?parent=... — forward it to the
      // backend as X-Embed-Parent so it can route to the correct
      // customer's profile (instead of defaulting to MOAS).
      // Also forward ?demo_as=<slug> if present — that's Frank's demo
      // mode where he scrapes a prospect's site and loads their profile.
      const _params = new URLSearchParams(window.location.search);
      const embedParent = _params.get('parent') || '';
      const demoAs = _params.get('demo_as') || '';
      const headers = { 'Content-Type': 'application/json' };
      if (embedParent) headers['X-Embed-Parent'] = embedParent;
      if (demoAs) headers['X-Demo-As'] = demoAs;
      const body = {
        message: text,
        history: history.slice(-20),
        visitor: visitor,
      };
      // Sales-bot context: once we've scraped a prospect's site, every
      // subsequent /chat carries the URL so the server can attach the
      // PROSPECT BUSINESS section to the system prompt.
      if (_pendingProspectUrl) {
        body.prospect_url = _pendingProspectUrl;
      }
      const res = await fetch('/chat', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(body)
      });
      const data = await res.json();
      thinkingEl.remove();

      const reply = data.reply || "I couldn't reach my AI right now. Please try again.";
      addBubble('assistant', reply, { tier: data.tier });
      history.push({ role: 'assistant', content: reply });
      _saveHistory(history);
      updateConnectionPill(data.tier);

      // SALES SCRAPE trigger — if the bot emitted <<SCRAPE:url>>, the
      // server stripped the marker and surfaced the URL. Fire off the
      // scrape + poll + silent continuation.
      if (data.scrape_request && data.scrape_request.url) {
        _orchestrateSalesScrape(data.scrape_request.url);
      }

      // SALES NAVIGATE trigger — bot emitted <<NAV:url>> at the end
      // of the buy flow. The visitor must end up on a FULL-PAGE legal
      // page (not the chat iframe), and then on Stripe's full-page
      // checkout. Stripe refuses to render in iframes (X-Frame-Options:
      // DENY), so a navigation that lands in the iframe is broken.
      //
      // Cross-origin nav: the iframe is on orbi.twickell.com and the
      // parent is twickell.com / purblum.com / etc. window.top.location
      // assignment can silently fail in some strict browsers, leaving
      // the iframe to take the navigation instead. The reliable path
      // is to postMessage to embed.js (which runs in the parent page
      // and can navigate its own window with no cross-origin friction).
      if (data.navigate_request && data.navigate_request.url) {
        const navUrl = data.navigate_request.url;
        // Give the bot's last sentence ~2.5s to speak before nav.
        setTimeout(() => {
          // Primary path: ask the parent embed.js to do the navigation.
          // The parent listens for orbi:navigate and runs
          // window.location.href = url in its own origin.
          try {
            notifyParent('orbi:navigate', { url: navUrl });
          } catch {}
          // Fallback for standalone (non-embed) chat — when there is
          // no parent embed.js listener, the iframe IS the top window,
          // so this just navigates the page normally.
          if (window.top === window.self) {
            try { window.top.location.assign(navUrl); } catch {}
          }
        }, 2500);
      }

      // Live cart panel — server returns order_summary when the chat
      // looks like an order. Show items + subtotal + tax + total so the
      // customer SEES what's being captured + the math, not just hears
      // it from Orby.
      if (data.order_summary && data.order_summary.subtotal > 0) {
        _renderCartPanel(data.order_summary);
      }

      if (!panel.classList.contains('open')) {
        unread += 1;
        updateBadge();
      }
      // Always notify the parent (embed) about new assistant messages,
      // so the badge on its launcher updates
      if (IS_EMBED) notifyParent('orbi:unread', { count: unread });

      if (data.billing_warning === 'billing_issue') {
        showOfflineBanner('There is a billing issue on this account — please contact the owner.');
      }

      maybeCaptureContactInfo(text);

      // Speak the reply if speaker is on
      if (prefs.speakerOn) {
        await speak(reply);
      } else {
        setStateBar(null);
        // Re-arm listening if mic was on
        if (prefs.micOn) startListening();
      }
    } catch (err) {
      thinkingEl.remove();
      setStateBar(null);
      // Silent mode (sales-scrape continuation) — never surface an
      // "I'm offline" bubble for the auto-fired (continue) message.
      // It's a server-to-server hop the visitor shouldn't see.
      if (opts.silent) {
        console.warn('[Orbi] silent send failed (suppressed):', err);
      } else {
        const fallback = "I'm offline right now — please check your connection or try again.";
        addBubble('assistant', fallback, { tier: 'none' });
        if (prefs.speakerOn) speak(fallback);
        else if (prefs.micOn) startListening();
      }
    } finally {
      isSending = false;
      sendBtn.disabled = !input.value.trim();
      // Restore focus to input so user can keep typing
      // (but only on desktop — on mobile this would re-open the keyboard annoyingly)
      if (window.matchMedia('(min-width: 481px)').matches) {
        input.focus();
      }
    }
  }

  // ------------------------------------------------------------------
  // Sales-bot scrape orchestration (twickell.com only)
  // ------------------------------------------------------------------
  // Flow: bot emits <<SCRAPE:url>> in its reply (stripped server-side
  // and surfaced as data.scrape_request). Client kicks off the public
  // scrape endpoint, polls until done (~30-90s for a typical site),
  // sets _pendingProspectUrl so subsequent /chat calls carry the URL,
  // then triggers a silent "continue" turn so the bot can deliver its
  // tailored pitch using the freshly-scraped PROSPECT BUSINESS context.
  async function _orchestrateSalesScrape(url) {
    try {
      setStateBar('Scanning your site...', 'thinking');
      const startRes = await fetch('/api/public/sales_scrape', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const startData = await startRes.json();
      if (!startData.ok) {
        setStateBar(null);
        addBubble('assistant',
          startData.error === 'rate_limited'
            ? "I've already looked at one site recently — give me a few minutes before I scan another. Or tell me what kind of business you run and I'll recommend a fit."
            : "I couldn't reach that site. Want to tell me what kind of business you run instead?",
          { tier: 'local' });
        return;
      }
      const jobId = startData.job_id;
      // Poll every 3s for up to ~3 minutes
      for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 3000));
        let sData;
        try {
          const sRes = await fetch(`/api/public/sales_scrape_status/${jobId}`);
          sData = await sRes.json();
        } catch { continue; }
        const status = sData && sData.status;
        if (status === 'done') {
          _pendingProspectUrl = url;
          setStateBar(null);
          // Silent continuation — server sees this in history but the
          // visitor never sees "(continue)" in their chat panel.
          send('(continue — scrape complete)', { silent: true });
          return;
        } else if (status === 'error') {
          setStateBar(null);
          addBubble('assistant',
            "Sorry, I had trouble loading that site. Want to tell me what kind of business you run instead?",
            { tier: 'local' });
          return;
        }
        // still running, keep waiting
      }
      // Timeout after ~3 min
      setStateBar(null);
      addBubble('assistant',
        "That's taking longer than I expected. Want to tell me what kind of business you run instead?",
        { tier: 'local' });
    } catch (e) {
      console.error('[Orbi] sales scrape orchestration failed:', e);
      setStateBar(null);
    }
  }

  // ------------------------------------------------------------------
  // Speech recognition (mic)
  // ------------------------------------------------------------------
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  let isListening = false;
  let interimBubble = null;
  let restartTimer = null;
  let wantsListening = false;  // user's intent — survives auto-restart loops

  function setupVoice() {
    if (!Recognition) {
      micToggle.disabled = true;
      micToggle.title = 'Voice input not supported in this browser';
      micToggle.style.opacity = 0.4;
      return;
    }
    recognition = new Recognition();
    recognition.lang = 'en-US';
    // continuous=false is COUNTERINTUITIVE but more reliable. Chrome's
    // continuous mode silently dies after ~30s of silence, or when the
    // tab loses focus, or randomly — and the death doesn't reliably
    // trigger onend on all builds. With continuous=false, recognition
    // returns one utterance at a time, fires onend cleanly, and the
    // restart loop below keeps a fresh recognizer running. Net effect:
    // mic stays on far more reliably across long conversations.
    recognition.continuous = false;
    recognition.interimResults = true;
    // Ask the browser for top-3 alternative transcriptions. We use the
    // most confident one — picks up better than the default top-1 when
    // the audio is borderline (background noise, fast talkers).
    recognition.maxAlternatives = 3;

    recognition.onstart = () => {
      isListening = true;
      micToggle.classList.add('listening');
      setStateBar('Listening — speak any time...', 'listening');
      // Do NOT drain the speech queue here. speak() calls stopListening()
      // as its anti-echo step — which would immediately kill the mic we
      // JUST started. The document-level tap listener handles draining
      // when the user taps inside the chat (input field, send button, etc.)
      // — that's a safer gesture context that doesn't conflict with mic.
    };

    recognition.onend = () => {
      isListening = false;
      // Keep the visual "listening" badge ON during the auto-restart gap
      // so the user doesn't see flicker between utterances and think the
      // mic is off. We'll genuinely remove it only when the user toggles
      // mic off OR Orby starts speaking.
      if (!wantsListening || isSpeaking) {
        micToggle.classList.remove('listening');
        setStateBar(null);
      }
      // Auto-restart with a 250ms gap. Chrome's recognition.start() will
      // throw "already running" if called too quickly after stop() — 50ms
      // wasn't enough on many builds. 250ms reliably works across
      // Chrome / Edge / Brave / Safari and is still imperceptible to
      // someone holding a normal conversation.
      if (wantsListening && !isSpeaking) {
        clearTimeout(restartTimer);
        restartTimer = setTimeout(() => {
          if (wantsListening && !isSpeaking) safeStart();
        }, 250);
      }
    };

    recognition.onerror = (e) => {
      // 'aborted' fires on intentional stop — ignore it
      if (e.error === 'aborted' || e.error === 'no-speech') return;
      console.warn('mic error:', e.error);
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        setMicOn(false);
        setStateBar('Microphone blocked — tap the mic icon to retry.', 'error');
      }
    };

    recognition.onresult = (event) => {
      let interim = '';
      let finalText = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        // Pick the most CONFIDENT alternative from the top-3, not just
        // the first one. Catches partial-drop cases where the second
        // candidate has the full phrase.
        let best = event.results[i][0];
        for (let k = 1; k < event.results[i].length; k++) {
          if ((event.results[i][k].confidence || 0) > (best.confidence || 0)) {
            best = event.results[i][k];
          }
        }
        const transcript = best.transcript;
        if (event.results[i].isFinal) finalText += transcript;
        else interim += transcript;
      }
      if (interim) showInterim(interim);
      if (finalText.trim()) {
        clearInterim();
        send(finalText);
      }
    };
  }

  function startListening() {
    if (!recognition || isListening || isSpeaking) return;
    wantsListening = true;
    safeStart();
  }

  function safeStart() {
    try { recognition.start(); }
    catch (e) { /* already running — ignore */ }
  }

  function stopListening() {
    wantsListening = false;
    clearTimeout(restartTimer);
    if (recognition) {
      try {
        if (typeof recognition.abort === 'function') recognition.abort();
        else if (isListening) recognition.stop();
      } catch {}
    }
    isListening = false;
    clearInterim();
    micToggle.classList.remove('listening', 'muted-while-speaking');
    if (stateBar.classList.contains('listening')) setStateBar(null);
  }

  function showInterim(text) {
    if (!interimBubble) {
      interimBubble = document.createElement('div');
      interimBubble.className = 'message interim';
      chatArea.appendChild(interimBubble);
    }
    interimBubble.textContent = text;
    chatArea.scrollTop = chatArea.scrollHeight;
  }

  function clearInterim() {
    if (interimBubble) {
      interimBubble.remove();
      interimBubble = null;
    }
  }

  // ------------------------------------------------------------------
  // Spoken welcome — Frank 2026-06-23. When the user taps the mic for the
  // first time, Orby greets them out loud + types the same line into the
  // chat so it appears as her first message. Replaces the old static
  // "Hi! How can I help?" placeholder bubble that just sat there silently.
  // ------------------------------------------------------------------
  let _welcomeDelivered = false;
  async function deliverSpokenWelcome() {
    if (_welcomeDelivered) return;
    _welcomeDelivered = true;
    const welcomeText = "Hi, welcome to myOrby. How can I help you today?";
    // Remove the static placeholder card now that real conversation starts
    welcomeEl?.remove();
    // Create the bubble empty, then type characters in while speech plays
    const div = document.createElement('div');
    div.className = 'message assistant';
    const body = document.createElement('div');
    body.className = 'message-body';
    div.appendChild(body);
    chatArea.appendChild(div);
    chatArea.scrollTop = chatArea.scrollHeight;
    // Speak in parallel with typing — both kick off now
    const speechPromise = (prefs.speakerOn ? speak(welcomeText) : Promise.resolve());
    // Type ~30 chars/sec — slow enough to read along with her voice
    for (let i = 0; i < welcomeText.length; i++) {
      body.textContent = welcomeText.slice(0, i + 1);
      chatArea.scrollTop = chatArea.scrollHeight;
      await new Promise(r => setTimeout(r, 33));
    }
    // Push into chat history so the LLM sees her opening turn and
    // continues the conversation from there.
    history.push({ role: 'assistant', content: welcomeText });
    _saveHistory(history);
    // Wait for speech to finish (so anti-echo guard works correctly)
    try { await speechPromise; } catch {}
  }

  // ------------------------------------------------------------------
  // Text-to-speech — Edge TTS (server-side neural voices). Sounds natural,
  // much better than browser SpeechSynthesis.
  //
  // Anti-echo: stop listening BEFORE audio plays, resume AFTER it ends.
  // ------------------------------------------------------------------
  let isSpeaking = false;
  let currentAudio = null;
  let currentAudioUrl = null;
  const ORBI_TTS_VOICE = 'en-US-AvaNeural';
  // Browser fallback (only if server TTS fails)
  const browserSynth = window.speechSynthesis;

  async function speak(text) {
    if (!text) {
      if (prefs.micOn && !isSpeaking) startListening();
      return;
    }
    stopSpeaking();

    // ANTI-ECHO: cut the mic BEFORE audio starts
    const wasMicOn = prefs.micOn;
    if (wasMicOn) {
      stopListening();
      micToggle.classList.add('muted-while-speaking');
    }

    isSpeaking = true;
    avatar.classList.add('speaking');
    setStateBar('Speaking...', 'speaking');

    const cleanText = stripForSpeech(text);

    try {
      // STREAMING: point the audio element at the /tts GET URL. The
      // server is already a streaming Flask Response (yields MP3
      // chunks from edge_tts.stream()). When we use audio.src = url,
      // the browser plays each chunk as it arrives — audio starts
      // within ~1s. The prior fetch + blob path waited for the WHOLE
      // synthesis to finish before play(), so long replies showed text
      // for 30-60s before audio started.
      //
      // GET caps text at ~2000 chars (URL length safety); the server
      // /tts handler also caps at 12000 internally. The LLM's replies
      // are typically <500 chars so this is non-binding in practice.
      const ttsUrl = '/tts?voice=' + encodeURIComponent(ORBI_TTS_VOICE)
        + '&text=' + encodeURIComponent(cleanText.slice(0, 2000));

      if (_audioUnlocked) {
        await new Promise((resolve) => {
          const done = () => {
            _audioEl.onended = null;
            _audioEl.onerror = null;
            resolve();
          };
          _audioEl.onended = done;
          _audioEl.onerror = done;
          _audioEl.src = ttsUrl;
          _audioEl.volume = 1;
          _audioEl.muted = false;
          _audioEl.play().catch(done);
        });
      } else {
        // Pre-gesture (rare — first reply before any tap inside chat).
        // Fresh Audio() may fail to play on iOS; the persistent element
        // path above handles every subsequent reply once the user taps.
        currentAudio = new Audio(ttsUrl);
        currentAudio.preload = 'auto';
        await new Promise((resolve) => {
          currentAudio.onended = resolve;
          currentAudio.onerror = resolve;
          currentAudio.play().catch(resolve);
        });
      }
    } catch (err) {
      console.warn('[Orbi] server TTS failed, falling back to browser voice:', err);
      // Last-resort fallback so the assistant still speaks something
      if (browserSynth) {
        const utter = new SpeechSynthesisUtterance(cleanText);
        utter.rate = 1.02;
        await new Promise((resolve) => {
          utter.onend = resolve;
          utter.onerror = resolve;
          browserSynth.speak(utter);
        });
      }
    }

    isSpeaking = false;
    currentAudio = null;
    avatar.classList.remove('speaking');
    micToggle.classList.remove('muted-while-speaking');
    setStateBar(null);

    // Anti-echo release: give the speaker a moment to settle, then re-arm
    // mic. 100ms was too tight — Chrome's SpeechRecognition can fail
    // silently when the audio playback hasn't fully released the device.
    // 400ms is the floor that reliably works across Chrome / Edge / Brave
    // / Safari for both desktop and laptop mics.
    //
    // Plus: if the first attempt doesn't actually take (no onstart event
    // within 600ms), retry once. Without the retry, a silently-failed
    // start leaves the user staring at a dead mic with no signal.
    if (wasMicOn && prefs.micOn) {
      const armMic = () => {
        if (!prefs.micOn || isSpeaking) return;
        startListening();
        // Watchdog: if startListening didn't actually start the mic
        // within 600ms, try one more time. This catches the silent-fail
        // case Chrome hits after long sessions.
        setTimeout(() => {
          if (prefs.micOn && !isSpeaking && !isListening && wantsListening) {
            console.log('[Orbi] mic restart watchdog firing — retrying start');
            try { recognition && recognition.stop(); } catch {}
            setTimeout(() => {
              if (prefs.micOn && !isSpeaking) startListening();
            }, 350);
          }
        }, 600);
      };
      setTimeout(armMic, 400);
    }
  }

  function stopSpeaking() {
    if (_currentSource) {
      try { _currentSource.stop(); } catch {}
      _currentSource = null;
    }
    try { _audioEl.pause(); } catch {}
    if (currentAudio) {
      try { currentAudio.pause(); currentAudio.currentTime = 0; } catch {}
      currentAudio = null;
    }
    if (currentAudioUrl) {
      URL.revokeObjectURL(currentAudioUrl);
      currentAudioUrl = null;
    }
    if (browserSynth && (browserSynth.speaking || browserSynth.pending)) {
      try { browserSynth.cancel(); } catch {}
    }
    isSpeaking = false;
    avatar.classList.remove('speaking');
    micToggle.classList.remove('muted-while-speaking');
  }

  function stripForSpeech(t) {
    // Clean text BEFORE it goes to TTS so she speaks like a human, not a
    // markdown parser. Without these substitutions she reads markdown
    // marks literally ("hashtag hashtag pricing") and reads "$129" as
    // "dollar one twenty nine" instead of "one hundred twenty-nine dollars".
    return String(t || '')
      // Markdown bold/italic markers
      .replace(/\*\*?/g, '')
      // Line-start bullet markers
      .replace(/^[-•]\s+/gm, '')
      // ALL hashes — markdown headings (## Pricing) and inline tags
      // (#seat). TTS reads each one as the word "hashtag" otherwise.
      .replace(/#+/g, '')
      // Inline code backticks
      .replace(/`+/g, '')
      // System tier hints we don't want spoken
      .replace(/—\s*backup mode\s*—/gi, '')
      .replace(/—\s*offline mode\s*—/gi, '')
      // Currency — Edge TTS reads "$129" as "dollar one twenty nine".
      // Rewrite as natural English: "$129" → "129 dollars",
      // "$29.99" → "29 dollars and 99 cents", "$0.99" → "99 cents".
      .replace(/\$(\d+)\.(\d{2})\b/g, (_m, d, c) => {
        const di = parseInt(d, 10);
        const ci = parseInt(c, 10);
        if (di === 0) return `${ci} cents`;
        if (ci === 0) return `${di} dollars`;
        return `${di} dollars and ${ci} cents`;
      })
      .replace(/\$(\d+)\b/g, (_m, d) => `${d} dollars`)
      // Per-unit slashes — "$29/seat/mo" reads as "slash seat slash mo"
      // by default. Rewrite the common units as "per X".
      .replace(/\s*\/\s*mo\b/gi, ' per month')
      .replace(/\s*\/\s*month\b/gi, ' per month')
      .replace(/\s*\/\s*yr\b/gi, ' per year')
      .replace(/\s*\/\s*year\b/gi, ' per year')
      .replace(/\s*\/\s*seat\b/gi, ' per seat')
      .replace(/\s*\/\s*user\b/gi, ' per user')
      .replace(/\s*\/\s*day\b/gi, ' per day')
      .replace(/\s*\/\s*hr\b/gi, ' per hour')
      .replace(/\s*\/\s*hour\b/gi, ' per hour')
      .replace(/\s*\/\s*wk\b/gi, ' per week')
      .replace(/\s*\/\s*week\b/gi, ' per week')
      // Collapse any double-spaces the substitutions introduced
      .replace(/\s{2,}/g, ' ')
      .trim();
  }

  // ------------------------------------------------------------------
  // Bubble rendering
  // ------------------------------------------------------------------
  function addBubble(role, text, opts = {}) {
    const div = document.createElement('div');
    div.className = 'message ' + role +
      (opts.tier && opts.tier !== 'brain' && opts.tier !== 'none' ? ' degraded' : '');

    // Content
    const body = document.createElement('div');
    body.className = 'message-body';
    body.textContent = text;
    div.appendChild(body);

    // Tier hint — only show when we're ACTUALLY degraded. Healthy tiers:
    // brain, phone_llm (Scaleway Llama 3.3 70B direct), local (intentional
    // fast paths like time/date/canned cap_overview), learned (learning
    // loop hits). Only huggingface counts as "backup mode" (brain fallback).
    if (opts.tier
        && opts.tier !== 'brain'
        && opts.tier !== 'phone_llm'
        && opts.tier !== 'local'
        && opts.tier !== 'learned'
        && opts.tier !== 'learning_loop'
        && opts.tier !== 'none') {
      const hint = document.createElement('div');
      hint.className = 'message-tier-hint';
      hint.textContent = opts.tier === 'huggingface' ? '— backup mode —' : '— offline mode —';
      div.appendChild(hint);
    }

    // Timestamp (subtle, on hover)
    const time = document.createElement('span');
    time.className = 'message-time';
    time.textContent = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    div.appendChild(time);

    // Copy button — only on assistant messages
    if (role === 'assistant') {
      const copy = document.createElement('button');
      copy.className = 'message-copy-btn';
      copy.title = 'Copy';
      copy.setAttribute('aria-label', 'Copy message');
      copy.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(text);
          copy.classList.add('copied');
          copy.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>';
          setTimeout(() => {
            copy.classList.remove('copied');
            copy.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>';
          }, 1500);
        } catch (e) {
          console.warn('clipboard write failed:', e);
        }
      });
      div.appendChild(copy);
    }

    chatArea.appendChild(div);
    // Scroll-aware: if user is at bottom, scroll. If scrolled up, badge the
    // floating scroll button (handled in setupScrollWatch).
    if (window.__orbiOnNewMessage) {
      window.__orbiOnNewMessage(role === 'assistant');
    } else {
      chatArea.scrollTop = chatArea.scrollHeight;
    }
    return div;
  }

  function addThinking() {
    const div = document.createElement('div');
    div.className = 'message assistant';
    div.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    chatArea.appendChild(div);
    chatArea.scrollTop = chatArea.scrollHeight;
    return div;
  }

  function showOfflineBanner(msg) {
    if (panel.querySelector('.offline-banner')) return;
    const banner = document.createElement('div');
    banner.className = 'offline-banner';
    banner.textContent = msg;
    panel.querySelector('.orbi-header').after(banner);
  }

  function updateConnectionPill(tier) {
    // Frank 2026-06-23: 'local' is an intentional fast path (time, date,
    // capability overview, etc.) — it's not a failure, so it shouldn't
    // show as 'Offline mode'. Only flag true degradation (huggingface
    // fallback) or no-connectivity (none).
    if (!tier || tier === 'brain' || tier === 'phone_llm' || tier === 'local'
        || tier === 'learned' || tier === 'learning_loop') {
      connStatus.className = 'status';
      connStatus.textContent = '● Online';
    } else if (tier === 'huggingface') {
      connStatus.className = 'status degraded';
      connStatus.textContent = '● Backup mode';
    } else if (tier === 'none') {
      connStatus.className = 'status offline';
      connStatus.textContent = '● No connection';
    }
  }

  // ------------------------------------------------------------------
  // Business greeting
  // ------------------------------------------------------------------
  async function loadBusinessGreeting() {
    try {
      // Same X-Embed-Parent / X-Demo-As trick as the /chat fetch — so the
      // backend loads the right customer profile for the widget header.
      const _qs2 = new URLSearchParams(window.location.search);
      const embedParent = _qs2.get('parent') || '';
      const demoAs = _qs2.get('demo_as') || '';
      const summaryHeaders = {};
      if (embedParent) summaryHeaders['X-Embed-Parent'] = embedParent;
      if (demoAs) summaryHeaders['X-Demo-As'] = demoAs;
      const r = await fetch('/api/public/business_summary', { headers: summaryHeaders });
      if (r.ok) {
        const data = await r.json();
        if (data.name) {
          businessName.textContent = data.name;
          landingBusiness.textContent = 'Welcome to ' + data.name;
          document.title = data.name;
        }
        if (data.tagline) landingTagline.textContent = data.tagline;
        if (data.name) {
          document.getElementById('welcome-title').textContent =
            `Hi! I'm Orby at ${data.name}.`;
        }
        renderQuickActions(data.quick_actions || []);
        // Proactive greeting — Orbi speaks first when the chat opens
        // so the visitor knows she's awake and ready. _fireProactiveGreeting
        // guards against double-greeting on widget reopen by checking
        // history. Skip if conversation history already exists (visitor
        // navigated to a new page on the same site).
        if (history.length === 0) {
          _fireProactiveGreeting(data.name || 'us');
        } else {
          _markGreetingDone();
        }
      } else {
        _markGreetingDone();
      }
    } catch {
      renderQuickActions([]);
      _markGreetingDone();
    }

    try {
      const h = await fetch('/health').then(r => r.json());
      if (h.business_name && !businessName.textContent.length) {
        businessName.textContent = h.business_name;
      }
    } catch {}
  }

  function renderQuickActions(actions) {
    const container = document.getElementById('quick-actions');
    if (!actions.length) {
      actions = ['Are you open now?', 'Where are you located?', 'What do you offer?'];
    }
    container.innerHTML = actions
      .map((a) => `<button class="quick-chip" data-text="${esc(a)}">${esc(a)}</button>`)
      .join('');
    container.querySelectorAll('.quick-chip').forEach((chip) => {
      chip.addEventListener('click', () => send(chip.dataset.text));
    });
  }

  // ------------------------------------------------------------------
  // Visitor info
  // ------------------------------------------------------------------
  // Live cart panel — renders inside the chat scroll area when the server
  // returns order_summary. Math is server-computed (deterministic); the
  // widget just displays. Stays at the top of the chat so the visitor can
  // see what's been captured + the running total as the conversation goes.
  function _renderCartPanel(summary) {
    let panel = document.getElementById('orbi-cart-panel');
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'orbi-cart-panel';
      panel.style.cssText = `
        position: sticky;
        top: 0;
        z-index: 10;
        background: #fff;
        border: 1px solid #d4d8e0;
        border-radius: 10px;
        margin: 8px 12px 12px;
        padding: 10px 12px;
        font-size: 13px;
        line-height: 1.5;
        color: #1a2240;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
      `;
      // Insert at the TOP of the chat scroll area so it stays visible
      chatArea.parentNode.insertBefore(panel, chatArea);
    }
    const rows = (summary.lines || []).map(li => {
      const safeName = String(li.name).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
      return `<div style="display:flex;justify-content:space-between;gap:8px">
        <span>${li.qty}× ${safeName}</span>
        <span style="color:#444">$${Number(li.line_total).toFixed(2)}</span>
      </div>`;
    }).join('');
    const taxLine = summary.tax_rate_pct
      ? `<div style="display:flex;justify-content:space-between;color:#666;font-size:12px;margin-top:2px">
          <span>Tax (${summary.tax_rate_pct.toFixed(2)}%)</span>
          <span>$${Number(summary.tax).toFixed(2)}</span>
        </div>`
      : '';
    const totalLine = summary.tax_rate_pct
      ? `<div style="display:flex;justify-content:space-between;font-weight:700;color:#1a2240;border-top:1px solid #e6e8ec;padding-top:6px;margin-top:6px">
          <span>Total</span>
          <span>$${Number(summary.total).toFixed(2)}</span>
        </div>`
      : '';
    panel.innerHTML = `
      <div style="font-weight:700;color:#666;font-size:11px;letter-spacing:0.5px;margin-bottom:6px">YOUR ORDER</div>
      ${rows}
      <div style="display:flex;justify-content:space-between;margin-top:6px;padding-top:6px;border-top:1px dashed #e6e8ec;color:#1a2240;font-weight:600">
        <span>Subtotal</span>
        <span>$${Number(summary.subtotal).toFixed(2)}</span>
      </div>
      ${taxLine}
      ${totalLine}
      <button id="orbi-cart-submit" style="
        width:100%;
        margin-top:10px;
        padding:9px 12px;
        background:#1a8e3a;
        color:#fff;
        border:none;
        border-radius:6px;
        font-size:13px;
        font-weight:700;
        cursor:pointer;
        letter-spacing:0.3px;
      ">Place Order</button>
    `;
    document.getElementById('orbi-cart-submit')?.addEventListener('click', _submitCartFromChat);
  }

  // Submit the current cart through web_driver — backend extracts order
  // from the chat history, builds the cart, drives purblum.com, confirms.
  async function _submitCartFromChat() {
    const btn = document.getElementById('orbi-cart-submit');
    if (!btn) return;

    // Before submitting, make sure we have the customer's name + phone.
    // If not collected from the conversation, show a small inline form
    // right in the cart panel so they can fill it before we hit the API.
    if (!visitor.phone) {
      const panel = document.getElementById('orbi-cart-panel');
      if (panel && !document.getElementById('orbi-contact-form')) {
        const form = document.createElement('div');
        form.id = 'orbi-contact-form';
        form.style.cssText = 'margin-top:10px;padding-top:10px;border-top:1px dashed #e6e8ec;';
        form.innerHTML = `
          <div style="font-size:12px;color:#555;margin-bottom:6px">We need your name and phone number to send the order:</div>
          <input id="orbi-cf-name" placeholder="Your name" style="width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #ccc;border-radius:5px;font-size:13px;margin-bottom:6px">
          <input id="orbi-cf-phone" placeholder="Phone number" type="tel" style="width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #ccc;border-radius:5px;font-size:13px;margin-bottom:8px">
          <button id="orbi-cf-submit" style="width:100%;padding:8px;background:#1a8e3a;color:#fff;border:none;border-radius:5px;font-size:13px;font-weight:700;cursor:pointer">Send Order</button>
        `;
        panel.appendChild(form);
        btn.style.display = 'none';
        document.getElementById('orbi-cf-submit').addEventListener('click', () => {
          const name  = (document.getElementById('orbi-cf-name').value || '').trim();
          const phone = (document.getElementById('orbi-cf-phone').value || '').trim();
          if (!phone) { document.getElementById('orbi-cf-phone').style.borderColor = '#c00'; return; }
          if (name && !visitor.name)  { visitor.name  = name;  }
          if (phone && !visitor.phone){ visitor.phone = phone; }
          saveVisitorInfo();
          form.remove();
          btn.style.display = '';
          _submitCartFromChat();
        });
      }
      return;
    }

    btn.disabled = true;
    btn.style.opacity = '0.7';
    btn.textContent = 'Submitting…';
    const embedParent = new URLSearchParams(window.location.search).get('parent') || '';
    const headers = { 'Content-Type': 'application/json' };
    if (embedParent) headers['X-Embed-Parent'] = embedParent;
    try {
      const res = await fetch('/api/public/chat/submit_order', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
          history: history.slice(-30),
          visitor: visitor,
        }),
      });
      const data = await res.json();
      if (data.ok) {
        btn.textContent = `✓ Order ${data.order_id || 'placed'}`;
        btn.style.background = '#136d2c';
        // Echo a confirmation bubble in the chat
        addBubble('assistant', `Order's in! Confirmation ${data.order_id || ''}. The kitchen has it.`);
        history.push({role: 'assistant', content: `Order's in. Confirmation ${data.order_id || ''}.`});
        _saveHistory(history);
      } else {
        btn.textContent = 'Couldn\'t submit — tap to retry';
        btn.style.background = '#a02020';
        btn.disabled = false;
        addBubble('assistant', `Hit a snag submitting that — ${data.error || 'try again in a sec'}.`);
      }
    } catch (e) {
      btn.textContent = 'Network error — tap to retry';
      btn.style.background = '#a02020';
      btn.disabled = false;
    }
  }

  // Proactive greeting — Orby speaks first when the chat opens, instead
  // of sitting silent waiting for the visitor to type. Personalized for
  // returning visitors via the persisted visitor profile (name).
  function _fireProactiveGreeting(businessName) {
    // Guard: if a greeting already exists in history (e.g. user reopened
    // the widget mid-session), don't double-greet.
    if (history.some(m => m.role === 'assistant')) { _markGreetingDone(); return; }
    const hr = new Date().getHours();
    const tod = hr < 12 ? "Good morning" : hr < 17 ? "Good afternoon" : "Good evening";
    const v = (typeof visitor === 'object' && visitor) ? visitor : {};
    const name = (v.name || '').trim();
    let greeting;
    if (name) {
      // Returning visitor — single short line so the mic re-arms fast
      // for natural conversation. Long greetings = long anti-echo mute.
      greeting = `Hi ${name}! What can I help with?`;
    } else {
      // First-time visitor — one short sentence. The avatar + business
      // name in the header already shows who she is, so the greeting
      // doesn't need to re-introduce. Special case: when the business
      // IS Orbi (her own website / sales bot), saying "Orbi at Orbi"
      // sounds dumb. Drop the "at X" suffix in that case.
      const isOrbiSite = /^(vola|myorby|orbi|myorbi)$/i.test((businessName || '').trim());
      greeting = isOrbiSite
        ? `Hi! I'm Orby — how can I help?`
        : `Hi! I'm Orby at ${businessName} — how can I help?`;
    }
    // Remove the welcome bubble (replaced by Orby's actual first message)
    document.getElementById('welcome')?.remove();
    addBubble('assistant', greeting);
    history.push({ role: 'assistant', content: greeting });
    _saveHistory(history);
    // ALWAYS queue the greeting for TTS, regardless of current speakerOn —
    // because the auto-enable timeout (~100ms after DOM ready) may flip
    // speaker on AFTER this point. If we gate on prefs.speakerOn here,
    // a slow business-summary fetch finishes before the speaker auto-on,
    // and the greeting never queues at all. The drains below (first tap,
    // speaker toggle) consult prefs.speakerOn at drain time instead.
    _pendingFirstSpeech = greeting;
    if (prefs.speakerOn) {
      _pendingFirstSpeech = null;
      try { Promise.resolve(speak(greeting)).finally(_markGreetingDone); } catch { _markGreetingDone(); }
    }
    // If speakerOn is false right now, leave _pendingFirstSpeech queued —
    // the setSpeakerOn drain (auto-on at boot or user toggle) picks it up
    // and resolves the greeting gate at that point.
  }

  // Visitor profile (name + phone + email) lives in localStorage keyed per
  // customer site so it PERSISTS across browser sessions. Chat history is
  // wiped at tab-close, but Orby still recognizes returning visitors by
  // their name + phone — same model as the phone-side caller recognition.
  const VISITOR_KEY = 'orbi_visitor__' +
    (new URLSearchParams(window.location.search).get('parent') || window.location.origin);
  function loadVisitorInfo() {
    try { return JSON.parse(localStorage.getItem(VISITOR_KEY) || '{}'); }
    catch { return {}; }
  }
  function saveVisitorInfo() {
    try { localStorage.setItem(VISITOR_KEY, JSON.stringify(visitor)); } catch {}
  }
  function maybeCaptureContactInfo(text) {
    // Phone: 555-1234, (555) 555-1234, +1 555 555 1234, etc.
    const phoneMatch = text.match(/(?:\+?1[-.\s]?)?\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})/);
    // Email: anything that looks like an email
    const emailMatch = text.match(/[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/i);
    // Name patterns — both "my name is" and "this is X" and "I'm X"
    const nameMatch  = text.match(/(?:my name'?s?|i am|i'?m|this is|it'?s|call me)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)/);
    let changed = false;
    if (phoneMatch && !visitor.phone) { visitor.phone = phoneMatch[0]; changed = true; }
    if (emailMatch && !visitor.email) { visitor.email = emailMatch[0]; changed = true; }
    if (nameMatch  && !visitor.name)  { visitor.name  = nameMatch[1]; changed = true; }
    if (changed) saveVisitorInfo();
  }

  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
})();
