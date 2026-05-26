/* Orbi Embed Widget
 * -----------------
 * One-line install on any external website:
 *
 *   <script src="https://YOUR-TUNNEL.example.com/static/embed.js" defer></script>
 *
 * This injects a floating chat launcher in the bottom-right corner of the
 * customer's site. Clicking it opens an iframe pointing at our hosted
 * chat shell (which is the same /static/chat.html we serve to direct PWA
 * users). All conversation, voice, mic toggles work inside the iframe.
 *
 * The embed script lives on the SAME ORIGIN as the Orbi install — so when
 * twickell.com loads it, the script comes from twickell.orbi.frank.com
 * (their tunnel URL). The iframe is same-origin to the script, which means
 * cookies / session / mic permission all work cleanly.
 */

(function () {
  'use strict';

  if (window.__orbiEmbedLoaded) return;
  window.__orbiEmbedLoaded = true;

  // The script tag itself tells us where Orbi is hosted.
  // <script src="https://twickell.orbi.frank.com/static/embed.js"> → origin is the tunnel
  const scriptEl =
    document.currentScript ||
    Array.from(document.scripts).find(s => /\/embed\.js(\?|$)/.test(s.src));
  if (!scriptEl) {
    console.warn('[Orbi] embed.js could not locate its own script tag');
    return;
  }
  const origin = new URL(scriptEl.src).origin;
  const chatSrc = origin + '/?embed=1';

  // ----- Styles (scoped, no global leakage) -----
  const css = `
    #orbi-embed-launcher {
      position: fixed; bottom: 20px; right: 20px;
      width: 60px; height: 60px;
      border-radius: 50%;
      background: linear-gradient(135deg, #4f8cff 0%, #8b5cf6 100%);
      color: white;
      border: none;
      cursor: pointer;
      box-shadow: 0 8px 24px rgba(79,140,255,0.35);
      display: flex; align-items: center; justify-content: center;
      z-index: 2147483646;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
      font-family: system-ui, sans-serif;
    }
    #orbi-embed-launcher:hover {
      transform: scale(1.06);
      box-shadow: 0 10px 30px rgba(79,140,255,0.45);
    }
    #orbi-embed-launcher svg { width: 26px; height: 26px; fill: white; }
    #orbi-embed-launcher.open .icon-chat { display: none; }
    #orbi-embed-launcher .icon-close { display: none; }
    #orbi-embed-launcher.open .icon-close { display: block; }
    #orbi-embed-badge {
      position: absolute; top: -2px; right: -2px;
      background: #ff5555; color: white;
      font-size: 11px; font-weight: 700;
      min-width: 18px; height: 18px;
      border-radius: 9px; padding: 0 5px;
      display: flex; align-items: center; justify-content: center;
      border: 2px solid white;
    }
    #orbi-embed-frame {
      position: fixed; bottom: 92px; right: 20px;
      width: 380px; max-width: calc(100vw - 24px);
      height: 600px; max-height: calc(100vh - 120px);
      border: none;
      border-radius: 16px;
      box-shadow: 0 12px 48px rgba(0,0,0,0.25);
      background: #0b0f1a;
      z-index: 2147483647;
      opacity: 0;
      transform: translateY(20px) scale(0.95);
      pointer-events: none;
      transition: opacity 0.18s ease, transform 0.18s ease;
    }
    #orbi-embed-frame.open {
      opacity: 1;
      transform: translateY(0) scale(1);
      pointer-events: auto;
    }
    @media (max-width: 480px) {
      #orbi-embed-frame {
        bottom: 0; right: 0; left: 0; top: 0;
        width: 100vw; height: 100vh; max-width: none; max-height: 100vh;
        border-radius: 0;
      }
      #orbi-embed-launcher.open { display: none; }
    }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  // ----- DOM -----
  const launcher = document.createElement('button');
  launcher.id = 'orbi-embed-launcher';
  launcher.setAttribute('aria-label', 'Open chat with Orbi');
  launcher.innerHTML = `
    <svg class="icon-chat"  viewBox="0 0 24 24"><path d="M20 2H4a2 2 0 0 0-2 2v16l4-4h14a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zM7 9h10v2H7V9zm0 4h7v2H7v-2z"/></svg>
    <svg class="icon-close" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
    <span id="orbi-embed-badge" style="display:none;">1</span>
  `;
  document.body.appendChild(launcher);

  const frame = document.createElement('iframe');
  frame.id = 'orbi-embed-frame';
  frame.allow = 'microphone; clipboard-read; clipboard-write; autoplay';
  // We lazy-load the iframe source on first open to keep page load light
  frame.dataset.src = chatSrc;
  frame.title = 'Orbi chat assistant';
  document.body.appendChild(frame);

  // ----- Behavior -----
  let opened = false;
  let unread = 0;
  const badge = launcher.querySelector('#orbi-embed-badge');

  function openWidget() {
    if (!frame.src) frame.src = frame.dataset.src;
    frame.classList.add('open');
    launcher.classList.add('open');
    opened = true;
    setBadge(0);
  }
  function closeWidget() {
    frame.classList.remove('open');
    launcher.classList.remove('open');
    opened = false;
  }
  function setBadge(n) {
    unread = n;
    if (n > 0) {
      badge.textContent = n > 9 ? '9+' : String(n);
      badge.style.display = 'flex';
    } else {
      badge.style.display = 'none';
    }
  }
  launcher.addEventListener('click', () => opened ? closeWidget() : openWidget());

  // Allow the chat shell inside the iframe to talk back to us
  // (for unread badge updates and "close me" requests)
  window.addEventListener('message', (e) => {
    if (e.origin !== origin) return;
    const msg = e.data || {};
    if (msg.type === 'orbi:unread')  setBadge(Number(msg.count) || 0);
    if (msg.type === 'orbi:close')   closeWidget();
    if (msg.type === 'orbi:open')    openWidget();
  });

  // Expose a tiny API for the host page
  window.Orbi = {
    open: openWidget,
    close: closeWidget,
    isOpen: () => opened,
    setBadge,
  };

  console.log('[Orbi] embed widget loaded from', origin);
})();
