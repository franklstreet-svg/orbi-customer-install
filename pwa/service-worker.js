// Orbi PWA service worker
// Caches the app shell so it loads instantly and works offline (with degraded mode).

const CACHE_VERSION = 'orbi-v1';
const SHELL = [
  '/',
  '/static/chat.html',
  '/static/chat.css',
  '/static/chat.js',
  '/pwa/manifest.json',
  '/pwa/icons/icon-192.png',
  '/pwa/icons/icon-512.png',
  '/pwa/offline.html'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Never cache POSTs or non-http schemes
  if (req.method !== 'GET' || !req.url.startsWith('http')) return;

  // API calls: network-first, fall back to a polite offline JSON response
  if (req.url.includes('/api/') || req.url.includes('/chat')) {
    event.respondWith(
      fetch(req).catch(() =>
        new Response(
          JSON.stringify({
            offline: true,
            reply:
              "I'm offline right now — your internet may be down. I'll be back as soon as the connection returns."
          }),
          { headers: { 'Content-Type': 'application/json' } }
        )
      )
    );
    return;
  }

  // Static assets: cache-first
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req)
        .then((res) => {
          // Cache successful same-origin responses for next time
          if (res.ok && new URL(req.url).origin === self.location.origin) {
            const copy = res.clone();
            caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match('/pwa/offline.html'));
    })
  );
});

// Push notification (used by owner dashboard for new messages/leads)
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};
  const title = data.title || 'Orbi';
  const options = {
    body: data.body || 'You have a new message.',
    icon: '/pwa/icons/icon-192.png',
    badge: '/pwa/icons/icon-192.png',
    tag: data.tag || 'orbi-notification',
    data: { url: data.url || '/owner' }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/owner';
  event.waitUntil(clients.openWindow(url));
});
