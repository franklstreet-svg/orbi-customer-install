/* ============================================================================
   Orbi — Service Worker registration + "Add to Home Screen" helper
   ----------------------------------------------------------------------------
   Include in dashboard.html with:
       <script src="/pwa/register-sw.js" defer></script>

   What it does:
     1. Registers /pwa/service-worker.js so the dashboard works offline.
     2. Captures the browser's `beforeinstallprompt` event (Chrome / Edge /
        Android) and stashes it so we can fire it later from a button click.
     3. Exposes window.orbiInstallPrompt() — call this from any "Install on
        this device" button in the dashboard.
     4. Detects iOS Safari, where the prompt doesn't exist, and shows the
        manual "Share → Add to Home Screen" steps instead.
   ========================================================================= */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // 1. Register the service worker
  // -------------------------------------------------------------------------
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker
        .register("/pwa/service-worker.js", { scope: "/" })
        .then(function (reg) {
          console.log("[orbi] service worker registered, scope:", reg.scope);

          // Watch for an updated SW waiting in the wings.
          reg.addEventListener("updatefound", function () {
            var newWorker = reg.installing;
            if (!newWorker) return;
            newWorker.addEventListener("statechange", function () {
              if (
                newWorker.state === "installed" &&
                navigator.serviceWorker.controller
              ) {
                // New version available — surface a soft hint if the page
                // exposes one. The dashboard can listen for this event.
                window.dispatchEvent(new CustomEvent("orbi:update-ready"));
              }
            });
          });
        })
        .catch(function (err) {
          console.warn("[orbi] service worker registration failed:", err);
        });
    });
  }

  // -------------------------------------------------------------------------
  // 2. Capture the install prompt
  // -------------------------------------------------------------------------
  var deferredPrompt = null;

  window.addEventListener("beforeinstallprompt", function (e) {
    // Stop Chrome from showing its own mini-infobar; we'll trigger it from
    // our own button.
    e.preventDefault();
    deferredPrompt = e;
    window.dispatchEvent(new CustomEvent("orbi:install-available"));
    console.log("[orbi] install prompt captured");
  });

  window.addEventListener("appinstalled", function () {
    deferredPrompt = null;
    console.log("[orbi] PWA installed");
    window.dispatchEvent(new CustomEvent("orbi:installed"));
  });

  // -------------------------------------------------------------------------
  // 3. Platform detection
  // -------------------------------------------------------------------------
  function isIos() {
    var ua = window.navigator.userAgent || "";
    // iPad on iOS 13+ reports as Mac — sniff for touch as a tie-breaker.
    var iPadOS13 =
      /Mac/.test(ua) && "ontouchend" in document;
    return /iPad|iPhone|iPod/.test(ua) || iPadOS13;
  }

  function isStandalone() {
    return (
      window.matchMedia("(display-mode: standalone)").matches ||
      window.navigator.standalone === true
    );
  }

  // -------------------------------------------------------------------------
  // 4. The function the dashboard's "Install on this device" button calls
  // -------------------------------------------------------------------------
  window.orbiInstallPrompt = function () {
    // Already installed and running standalone — nothing to do.
    if (isStandalone()) {
      alert(
        "Orbi is already installed on this device. You're using the installed app right now."
      );
      return;
    }

    // iOS Safari — no prompt API, show manual steps.
    if (isIos()) {
      alert(
        "To install Orbi on your iPhone or iPad:\n\n" +
        "1. Tap the Share button at the bottom of Safari " +
        "(the square with the arrow pointing up).\n" +
        "2. Scroll down and tap \"Add to Home Screen\".\n" +
        "3. Tap \"Add\" in the top right.\n\n" +
        "Orbi will appear as an app icon on your home screen."
      );
      return;
    }

    // Chrome / Edge / Android — fire the captured prompt.
    if (deferredPrompt) {
      deferredPrompt.prompt();
      deferredPrompt.userChoice.then(function (choice) {
        console.log("[orbi] install choice:", choice.outcome);
        deferredPrompt = null;
      });
      return;
    }

    // Browser doesn't support installation or the prompt hasn't fired yet.
    alert(
      "Your browser doesn't offer one-tap install for Orbi.\n\n" +
      "In Chrome or Edge, open the menu (⋮ in the top right) " +
      "and look for \"Install Orbi\" or \"Add to Home screen\"."
    );
  };

  // -------------------------------------------------------------------------
  // 5. Helper: dashboard can read this to decide whether to show the button
  // -------------------------------------------------------------------------
  window.orbiInstallState = function () {
    return {
      installed: isStandalone(),
      ios: isIos(),
      promptReady: !!deferredPrompt
    };
  };
})();
