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
      return Object.assign({ micOn: false, speakerOn: false, panelOpen: false },
                           JSON.parse(localStorage.getItem(PREFS_KEY) || '{}'));
    } catch { return { micOn: false, speakerOn: false, panelOpen: false }; }
  }
  function savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch {}
  }

  // ------------------------------------------------------------------
  // Conversation state (in-memory only, never persisted)
  // ------------------------------------------------------------------
  let history = [];
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
    // Visiting / directly OR in embed iframe — both get the full-page chat.
    // The floating launcher + landing card only exist for the demo embed-host page.
    launcher.style.display = 'none';
    document.querySelector('.landing')?.remove();
    document.body.classList.add('fullpage-chat');
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    if (IS_EMBED) {
      panel.classList.add('embed-mode');
      panelClose.addEventListener('click', () => notifyParent('orbi:close'));
    } else {
      panel.classList.add('standalone-mode');
      // No close button on the standalone page — it IS the chat
      panelClose.style.display = 'none';
    }
    setupComposer();
    setupToggles();
    setupVoice();
    setupClearButton();
    setupScrollWatch();
    setupNetworkWatch();
    loadBusinessGreeting();
    applyPrefs();
  });

  function setupClearButton() {
    clearBtn?.addEventListener('click', () => {
      if (history.length === 0) return;
      if (!confirm('Clear this conversation?')) return;
      history = [];
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
      showOfflineBanner("You're offline. Orbi will reconnect when your internet is back.");
    });
  }

  function applyPrefs() {
    if (prefs.panelOpen) openPanel(false);
    setSpeakerOn(prefs.speakerOn, /*persist*/ false);
    if (prefs.micOn) setMicOn(true, /*persist*/ false);
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

  function openPanel(persist = true) {
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    launcher.classList.add('open');
    launcher.setAttribute('aria-label', 'Close chat with Orbi');
    unread = 0;
    updateBadge();
    setTimeout(() => input.focus(), 200);
    if (persist) { prefs.panelOpen = true; savePrefs(); }
  }

  function closePanel(persist = true) {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
    launcher.classList.remove('open');
    launcher.setAttribute('aria-label', 'Open chat with Orbi');
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
    micToggle.addEventListener('click', () => setMicOn(!prefs.micOn));
    speakerToggle.addEventListener('click', () => setSpeakerOn(!prefs.speakerOn));
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
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send(input.value);
      }
    });
    sendBtn.addEventListener('click', () => send(input.value));

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

  async function send(text) {
    text = (text || '').trim();
    if (!text || isSending) return;
    isSending = true;
    sendBtn.disabled = true;

    welcomeEl?.remove();
    clearInterim();
    addBubble('user', text);
    history.push({ role: 'user', content: text });

    input.value = '';
    input.style.height = 'auto';

    setStateBar('Thinking...', 'thinking');
    const thinkingEl = addThinking();

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history: history.slice(-20),
          visitor: visitor
        })
      });
      const data = await res.json();
      thinkingEl.remove();

      const reply = data.reply || "I couldn't reach my AI right now. Please try again.";
      addBubble('assistant', reply, { tier: data.tier });
      history.push({ role: 'assistant', content: reply });
      updateConnectionPill(data.tier);

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
        speak(reply);
      } else {
        setStateBar(null);
        // Re-arm listening if mic was on
        if (prefs.micOn) startListening();
      }
    } catch (err) {
      thinkingEl.remove();
      const fallback = "I'm offline right now — please check your connection or try again.";
      addBubble('assistant', fallback, { tier: 'none' });
      setStateBar(null);
      if (prefs.speakerOn) speak(fallback);
      else if (prefs.micOn) startListening();
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
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onstart = () => {
      isListening = true;
      micToggle.classList.add('listening');
      setStateBar('Listening — speak any time...', 'listening');
    };

    recognition.onend = () => {
      isListening = false;
      micToggle.classList.remove('listening');
      // Auto-restart if the user still wants listening and we're not in a
      // speaking-mute window (Chrome stops recognition after silence).
      if (wantsListening && !isSpeaking) {
        clearTimeout(restartTimer);
        restartTimer = setTimeout(() => {
          if (wantsListening && !isSpeaking) safeStart();
        }, 300);
      } else {
        setStateBar(null);
      }
    };

    recognition.onerror = (e) => {
      // 'aborted' fires on intentional stop — ignore it
      if (e.error === 'aborted' || e.error === 'no-speech') return;
      console.warn('mic error:', e.error);
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        setMicOn(false);
        alert('Microphone permission was denied. To use voice, please allow microphone access in your browser settings.');
      }
    };

    recognition.onresult = (event) => {
      let interim = '';
      let finalText = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
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
    if (recognition && isListening) {
      try { recognition.stop(); } catch {}
    }
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
  // Text-to-speech — Edge TTS (server-side neural voices). Sounds natural,
  // much better than browser SpeechSynthesis.
  //
  // Anti-echo: stop listening BEFORE audio plays, resume AFTER it ends.
  // ------------------------------------------------------------------
  let isSpeaking = false;
  let currentAudio = null;
  let currentAudioUrl = null;
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
      const res = await fetch('/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: cleanText })
      });
      if (!res.ok) throw new Error('tts http ' + res.status);
      const blob = await res.blob();
      currentAudioUrl = URL.createObjectURL(blob);
      currentAudio = new Audio(currentAudioUrl);
      currentAudio.preload = 'auto';

      await new Promise((resolve) => {
        const done = () => {
          if (currentAudioUrl) {
            URL.revokeObjectURL(currentAudioUrl);
            currentAudioUrl = null;
          }
          resolve();
        };
        currentAudio.onended = done;
        currentAudio.onerror = done;
        currentAudio.play().catch(done);
      });
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

    // Anti-echo release: give the speaker a moment to settle, then re-arm mic
    if (wasMicOn && prefs.micOn) {
      setTimeout(() => {
        if (prefs.micOn && !isSpeaking) startListening();
      }, 350);
    }
  }

  function stopSpeaking() {
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
    // Strip markdown bullets, asterisks, and "— backup mode —" tier hints
    return String(t || '')
      .replace(/\*\*?/g, '')
      .replace(/^[-•]\s+/gm, '')
      .replace(/—\s*backup mode\s*—/gi, '')
      .replace(/—\s*offline mode\s*—/gi, '')
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

    // Tier hint
    if (opts.tier && opts.tier !== 'brain' && opts.tier !== 'none') {
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
    if (!tier || tier === 'brain') {
      connStatus.className = 'status';
      connStatus.textContent = '● Online';
    } else if (tier === 'huggingface') {
      connStatus.className = 'status degraded';
      connStatus.textContent = '● Backup mode';
    } else if (tier === 'local') {
      connStatus.className = 'status offline';
      connStatus.textContent = '● Offline mode';
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
      const r = await fetch('/api/public/business_summary');
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
            `Hi! I'm Orbi at ${data.name}.`;
        }
        renderQuickActions(data.quick_actions || []);
      }
    } catch {
      renderQuickActions([]);
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
  function loadVisitorInfo() {
    try { return JSON.parse(sessionStorage.getItem('orbi_visitor') || '{}'); }
    catch { return {}; }
  }
  function saveVisitorInfo() {
    try { sessionStorage.setItem('orbi_visitor', JSON.stringify(visitor)); } catch {}
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
