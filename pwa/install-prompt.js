// Orbi PWA install prompt
// Drop this <script> into chat.html. Handles "Add to Home Screen" on Android/Chrome.
// iPhone users get a manual hint because iOS Safari doesn't fire beforeinstallprompt.

(function () {
  'use strict';

  // Register the service worker
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker
        .register('/pwa/service-worker.js')
        .catch((err) => console.warn('Orbi SW registration failed:', err));
    });
  }

  let deferredPrompt = null;
  const dismissedKey = 'orbi_install_dismissed_at';
  const dismissalTtl = 1000 * 60 * 60 * 24 * 14; // 14 days

  function recentlyDismissed() {
    const t = localStorage.getItem(dismissedKey);
    if (!t) return false;
    return Date.now() - parseInt(t, 10) < dismissalTtl;
  }

  function showInstallBanner(onInstall) {
    if (document.getElementById('orbi-install-banner')) return;
    const banner = document.createElement('div');
    banner.id = 'orbi-install-banner';
    banner.innerHTML = `
      <style>
        #orbi-install-banner {
          position: fixed; left: 12px; right: 12px; bottom: 12px;
          background: #1a2236; color: #eaf0ff;
          border: 1px solid #2c3957;
          border-radius: 12px; padding: 14px 16px;
          display: flex; align-items: center; gap: 12px;
          box-shadow: 0 6px 24px rgba(0,0,0,0.35);
          z-index: 9999; font-family: system-ui, sans-serif;
        }
        #orbi-install-banner .text { flex: 1; font-size: 14px; line-height: 1.35; }
        #orbi-install-banner button {
          padding: 8px 14px; border-radius: 8px; border: none;
          font-weight: 600; cursor: pointer; font-size: 14px;
        }
        #orbi-install-banner .install { background: #4f8cff; color: white; }
        #orbi-install-banner .dismiss { background: transparent; color: #8fa3c7; }
      </style>
      <div class="text">Install Orbi on your phone for one-tap access.</div>
      <button class="install">Install</button>
      <button class="dismiss" aria-label="Dismiss">Not now</button>
    `;
    document.body.appendChild(banner);
    banner.querySelector('.install').addEventListener('click', onInstall);
    banner.querySelector('.dismiss').addEventListener('click', () => {
      localStorage.setItem(dismissedKey, Date.now().toString());
      banner.remove();
    });
  }

  function showIOSHint() {
    if (recentlyDismissed()) return;
    if (!/iphone|ipad|ipod/i.test(navigator.userAgent)) return;
    if (window.matchMedia('(display-mode: standalone)').matches) return;
    setTimeout(() => {
      showInstallBanner(() => {
        const hint = document.createElement('div');
        hint.innerHTML = `
          <div style="position:fixed;inset:0;background:rgba(0,0,0,0.75);
            display:flex;align-items:center;justify-content:center;z-index:10000;
            font-family:system-ui,sans-serif;padding:24px;">
            <div style="background:#1a2236;color:#eaf0ff;padding:24px;
              border-radius:12px;max-width:340px;text-align:center;line-height:1.5;">
              <div style="font-size:48px;margin-bottom:8px;">&#x1F4F1;</div>
              <h3 style="margin:0 0 12px;">Install Orbi</h3>
              <p style="margin:0 0 14px;font-size:14px;">
                Tap the <strong>Share</strong> button at the bottom of Safari,
                then choose <strong>Add to Home Screen</strong>.
              </p>
              <button onclick="this.closest('div').parentElement.remove()"
                style="padding:10px 20px;background:#4f8cff;color:white;
                border:none;border-radius:8px;font-weight:600;cursor:pointer;">
                Got it
              </button>
            </div>
          </div>`;
        document.body.appendChild(hint);
        document.getElementById('orbi-install-banner')?.remove();
      });
    }, 6000);
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    if (recentlyDismissed()) return;
    deferredPrompt = e;
    showInstallBanner(async () => {
      if (!deferredPrompt) return;
      deferredPrompt.prompt();
      const choice = await deferredPrompt.userChoice;
      if (choice.outcome === 'dismissed') {
        localStorage.setItem(dismissedKey, Date.now().toString());
      }
      deferredPrompt = null;
      document.getElementById('orbi-install-banner')?.remove();
    });
  });

  window.addEventListener('appinstalled', () => {
    localStorage.removeItem(dismissedKey);
    document.getElementById('orbi-install-banner')?.remove();
  });

  document.addEventListener('DOMContentLoaded', showIOSHint);
})();
