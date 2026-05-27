/* Orbi Booking Embed Widget
 * -------------------------
 * Drop-in "Book a time with me" button + floating modal. The customer
 * pastes ONE script tag on their own website:
 *
 *   <script src="https://<owner>.orbi.frank.com/static/booking-embed.js"
 *           data-username="frank"
 *           data-button-text="Book a time with me"
 *           data-color="#4f8cff"></script>
 *
 * The script tag's own src tells us where Orbi is hosted (same trick as
 * embed.js). We then inject:
 *   - a floating button in the bottom-right of the host page
 *   - on click, a centered modal containing an iframe pointing at
 *     /book?u=<username>&embed=1 on the Orbi origin.
 *
 * Same-origin script + iframe (both on the tunnel) means cookies and
 * session handling Just Work; the host page (twickell.com, etc.) is
 * untouched and never sees the visitor's form data.
 */

(function () {
  'use strict';

  if (window.__orbiBookingEmbedLoaded) return;
  window.__orbiBookingEmbedLoaded = true;

  // ---- Locate our own script tag ----
  const scriptEl =
    document.currentScript ||
    Array.from(document.scripts).find(s =>
      /\/booking-embed\.js(\?|$)/.test(s.src));
  if (!scriptEl) {
    console.warn('[Orbi booking] could not locate own script tag');
    return;
  }

  const origin = new URL(scriptEl.src).origin;
  const username = (scriptEl.getAttribute('data-username') || '').trim();
  if (!username) {
    console.warn('[Orbi booking] missing data-username attribute');
    return;
  }
  const buttonText = scriptEl.getAttribute('data-button-text') ||
                     'Book a time with me';
  const color      = scriptEl.getAttribute('data-color') || '#4f8cff';

  const bookSrc = origin + '/book?u=' + encodeURIComponent(username) + '&embed=1';

  // ---- Styles (scoped) ----
  const css = `
    #orbi-book-launcher {
      position: fixed; bottom: 20px; right: 20px;
      padding: 12px 20px;
      border-radius: 999px;
      background: ${color};
      color: white;
      border: none;
      cursor: pointer;
      box-shadow: 0 8px 24px rgba(0,0,0,0.25);
      display: inline-flex; align-items: center; gap: 8px;
      z-index: 2147483646;
      transition: transform 0.16s ease, box-shadow 0.16s ease, filter 0.16s ease;
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      font-size: 15px;
      font-weight: 600;
      line-height: 1;
    }
    #orbi-book-launcher:hover {
      transform: translateY(-1px);
      filter: brightness(1.06);
      box-shadow: 0 10px 30px rgba(0,0,0,0.3);
    }
    #orbi-book-launcher svg {
      width: 18px; height: 18px; fill: currentColor;
    }

    #orbi-book-backdrop {
      position: fixed; inset: 0;
      background: rgba(8, 12, 24, 0.6);
      backdrop-filter: blur(2px);
      z-index: 2147483647;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.18s ease;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    #orbi-book-backdrop.open {
      opacity: 1;
      pointer-events: auto;
    }

    #orbi-book-modal {
      position: relative;
      width: 560px;
      max-width: calc(100vw - 24px);
      height: 720px;
      max-height: calc(100vh - 48px);
      background: #0b0f1a;
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 24px 64px rgba(0,0,0,0.5);
      transform: translateY(10px) scale(0.98);
      transition: transform 0.18s ease;
    }
    #orbi-book-backdrop.open #orbi-book-modal {
      transform: translateY(0) scale(1);
    }

    #orbi-book-iframe {
      width: 100%;
      height: 100%;
      border: none;
      background: #0b0f1a;
    }

    #orbi-book-close {
      position: absolute;
      top: 10px; right: 10px;
      width: 32px; height: 32px;
      border-radius: 50%;
      background: rgba(255,255,255,0.1);
      border: none;
      color: white;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 2;
      transition: background 0.12s ease;
      font-family: system-ui, sans-serif;
    }
    #orbi-book-close:hover { background: rgba(255,255,255,0.2); }
    #orbi-book-close svg { width: 16px; height: 16px; fill: white; }

    @media (max-width: 560px) {
      #orbi-book-modal {
        width: 100vw;
        height: 100vh;
        max-height: 100vh;
        border-radius: 0;
      }
      #orbi-book-launcher {
        bottom: 14px; right: 14px;
        padding: 11px 16px;
        font-size: 14px;
      }
    }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  // ---- Launcher button ----
  const launcher = document.createElement('button');
  launcher.id = 'orbi-book-launcher';
  launcher.type = 'button';
  launcher.setAttribute('aria-label', buttonText);
  launcher.innerHTML = `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M19 4h-1V2h-2v2H8V2H6v2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 16H5V10h14v10zm0-12H5V6h14v2z"/>
    </svg>
    <span>${escapeHtml(buttonText)}</span>
  `;
  document.body.appendChild(launcher);

  // ---- Backdrop + modal + iframe ----
  const backdrop = document.createElement('div');
  backdrop.id = 'orbi-book-backdrop';
  backdrop.innerHTML = `
    <div id="orbi-book-modal" role="dialog" aria-modal="true"
         aria-label="Book a time">
      <button id="orbi-book-close" type="button" aria-label="Close booking">
        <svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
      </button>
      <iframe id="orbi-book-iframe"
              allow="clipboard-read; clipboard-write"
              title="Book a time"></iframe>
    </div>
  `;
  document.body.appendChild(backdrop);

  const iframe = backdrop.querySelector('#orbi-book-iframe');
  const closeBtn = backdrop.querySelector('#orbi-book-close');
  iframe.dataset.src = bookSrc;

  let opened = false;

  function openModal() {
    if (!iframe.src) iframe.src = iframe.dataset.src;
    backdrop.classList.add('open');
    document.body.style.overflow = 'hidden';
    opened = true;
  }
  function closeModal() {
    backdrop.classList.remove('open');
    document.body.style.overflow = '';
    opened = false;
  }

  launcher.addEventListener('click', openModal);
  closeBtn.addEventListener('click', closeModal);
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && opened) closeModal();
  });

  // Allow the booking page inside the iframe to ask us to close
  // (e.g. after a successful booking + tap on a "Done" button).
  window.addEventListener('message', (e) => {
    if (e.origin !== origin) return;
    const msg = e.data || {};
    if (msg.type === 'orbi:booking:close') closeModal();
    if (msg.type === 'orbi:booking:open')  openModal();
  });

  // Public API for the host page (e.g. open the modal from a link)
  window.OrbiBooking = {
    open:   openModal,
    close:  closeModal,
    isOpen: () => opened,
  };

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  console.log('[Orbi booking] embed loaded for', username, 'from', origin);
})();
