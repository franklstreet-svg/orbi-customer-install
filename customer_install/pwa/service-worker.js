/* ============================================================================
   Orbi PWA Service Worker
   ----------------------------------------------------------------------------
   What this does, in plain English:

   1. INSTALL  – When the browser first picks this file up, we pre-download the
                 "app shell" (the dashboard page, its CSS, its JS, the manifest
                 and the icons) and stash them in a cache named ORBI_CACHE_V1.
                 That way, even if the phone has no signal later, Orbi still
                 opens.

   2. FETCH    – Every network request the dashboard makes flows through here
                 so we can decide where to get the answer from:

                   - /api/*  ........ NETWORK FIRST.  Owners need fresh data
                                      (new messages, calendar, todos). We try
                                      the network first; only if it fails do
                                      we fall back to whatever the cache has.

                   - static assets .. CACHE FIRST.  CSS / JS / icons rarely
                                      change, so we serve them from cache for
                                      instant load and quietly refresh in the
                                      background.

                   - everything else  NETWORK with cache fallback.

   3. ACTIVATE – When a NEW version of this file ships (we bump the cache
                 version string), we wipe out any older caches so the user
                 doesn't drag around stale files forever.

   Bump ORBI_CACHE_V1 to ORBI_CACHE_V2 (etc.) whenever you ship a breaking
   change to the dashboard so phones pick up the new shell on next open.
   ========================================================================= */

// Bumped from v1 → v2 after the dashboard got top-bar search, briefing banner,
// follow-up card, voicemails tab, OCR scan button, and the integrations panel.
// Older clients with v1 cache will purge it on activate and re-fetch.
const ORBI_CACHE_V1 = "orbi-cache-v3";

// The "app shell" — minimum files needed for the dashboard to render offline.
const SHELL_ASSETS = [
  "/owner",
  "/static/dashboard.css",
  "/static/dashboard.js",
  "/pwa/manifest.json",
  "/pwa/register-sw.js",
  "/pwa/icons/icon-192.png",
  "/pwa/icons/icon-512.png"
];

// ---------------------------------------------------------------------------
// INSTALL — pre-cache the shell
// ---------------------------------------------------------------------------
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(ORBI_CACHE_V1).then((cache) => {
      // addAll is atomic — if any one file fails the whole install fails.
      // Use Promise.allSettled so one missing icon doesn't break installation.
      return Promise.allSettled(
        SHELL_ASSETS.map((url) =>
          cache.add(new Request(url, { cache: "reload" })).catch((err) => {
            console.warn("[orbi-sw] could not pre-cache", url, err);
          })
        )
      );
    })
  );
  // Activate the new service worker immediately instead of waiting for all
  // tabs to close.
  self.skipWaiting();
});

// ---------------------------------------------------------------------------
// ACTIVATE — purge old caches
// ---------------------------------------------------------------------------
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys
          .filter((key) => key.startsWith("orbi-cache-") && key !== ORBI_CACHE_V1)
          .map((key) => {
            console.log("[orbi-sw] purging old cache:", key);
            return caches.delete(key);
          })
      );
    }).then(() => self.clients.claim())
  );
});

// ---------------------------------------------------------------------------
// FETCH — route requests
// ---------------------------------------------------------------------------
self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Only handle GETs. POSTs (sending a message, saving a setting) should
  // always hit the network; if it fails, the dashboard will surface the error.
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Same-origin only — never try to cache third-party scripts.
  if (url.origin !== self.location.origin) return;

  // ---- /api/* : NETWORK FIRST -------------------------------------------
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirst(req));
    return;
  }

  // ---- /tts and /stt : pass through (never cache audio) -----------------
  if (url.pathname.startsWith("/tts") || url.pathname.startsWith("/stt")) {
    return; // let the browser handle it normally
  }

  // ---- static assets : CACHE FIRST --------------------------------------
  if (
    url.pathname.startsWith("/static/") ||
    url.pathname.startsWith("/pwa/")  ||
    url.pathname === "/owner"          ||
    url.pathname === "/"               ||
    url.pathname === "/favicon.ico"
  ) {
    event.respondWith(cacheFirst(req));
    return;
  }

  // ---- everything else : try network, fall back to cache ----------------
  event.respondWith(
    fetch(req).catch(() => caches.match(req))
  );
});

// ---------------------------------------------------------------------------
// Strategy helpers
// ---------------------------------------------------------------------------
async function networkFirst(req) {
  const cache = await caches.open(ORBI_CACHE_V1);
  try {
    const fresh = await fetch(req);
    // Only cache successful responses
    if (fresh && fresh.status === 200) {
      cache.put(req, fresh.clone());
    }
    return fresh;
  } catch (err) {
    const cached = await cache.match(req);
    if (cached) return cached;
    // Final fallback — a minimal JSON error so the dashboard can show
    // "you're offline" instead of a generic browser failure page.
    return new Response(
      JSON.stringify({ ok: false, offline: true, error: "no network" }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    );
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(ORBI_CACHE_V1);
  const cached = await cache.match(req);
  if (cached) {
    // Refresh in the background ("stale-while-revalidate")
    fetch(req).then((fresh) => {
      if (fresh && fresh.status === 200) cache.put(req, fresh.clone());
    }).catch(() => { /* offline — keep the cached copy */ });
    return cached;
  }
  // Not in cache yet — fetch and store.
  try {
    const fresh = await fetch(req);
    if (fresh && fresh.status === 200) cache.put(req, fresh.clone());
    return fresh;
  } catch (err) {
    // Last-resort: if they asked for the dashboard shell and we have nothing,
    // hand back a tiny offline page.
    if (req.mode === "navigate") {
      return new Response(
        "<h1>Orbi is offline</h1><p>You'll see fresh data again as soon as your phone reconnects.</p>",
        { status: 503, headers: { "Content-Type": "text/html" } }
      );
    }
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Message channel — lets the dashboard tell us to skip waiting on a new
// version (used by the "update available, tap to reload" pattern).
// ---------------------------------------------------------------------------
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});
