/* Kolos — service worker (served at /sw.js, scope "/").
   Pages: network-first, fall back to the last saved copy, then to the offline screen.
   Static files and CDN scripts: stale-while-revalidate.
   POST requests are never touched. Saved pages are wiped on sign-out. */
const VERSION = 'v2';  // bump to refresh cached app files on installed PWAs
const STATIC_CACHE = `static-${VERSION}`;
const PAGES_CACHE = `pages-${VERSION}`;
const CDN_CACHE = `cdn-${VERSION}`;
const OFFLINE_URL = '/static/offline.html';

const PRECACHE = [
  OFFLINE_URL,
  '/static/css/app.css',
  '/static/js/app.js',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/apple-touch-icon.png',
];
const CDN_HOSTS = ['cdn.tailwindcss.com', 'cdn.jsdelivr.net'];
const NO_CACHE_PAGES = ['/login', '/register', '/logout'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(STATIC_CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  const keep = [STATIC_CACHE, PAGES_CACHE, CDN_CACHE];
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !keep.includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function staleWhileRevalidate(request, cacheName) {
  return caches.open(cacheName).then((cache) =>
    cache.match(request).then((cached) => {
      const network = fetch(request)
        .then((res) => {
          if (res && (res.ok || res.type === 'opaque')) cache.put(request, res.clone());
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
}

async function networkFirstPage(request) {
  const url = new URL(request.url);
  try {
    const res = await fetch(request);
    const cacheable = res.ok && !res.redirected && res.type === 'basic' && !NO_CACHE_PAGES.includes(url.pathname);
    if (cacheable) {
      // Skip pages that carry one-off flash toasts so they are not replayed offline.
      const text = await res.clone().text();
      if (!text.includes('class="toast-region"')) {
        const cache = await caches.open(PAGES_CACHE);
        await cache.put(request, new Response(text, { headers: res.headers }));
      }
    }
    return res;
  } catch (err) {
    const cached = await caches.match(request, { cacheName: PAGES_CACHE });
    return cached || caches.match(OFFLINE_URL);
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  const sameOrigin = url.origin === self.location.origin;

  if (sameOrigin && url.pathname === '/logout') {
    // Signing out must not leave the previous user's pages readable offline.
    event.respondWith(caches.delete(PAGES_CACHE).then(() => fetch(request)));
    return;
  }
  if (request.mode === 'navigate' && sameOrigin) {
    event.respondWith(networkFirstPage(request));
    return;
  }
  if (sameOrigin && url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidate(request, STATIC_CACHE));
    return;
  }
  if (CDN_HOSTS.includes(url.hostname)) {
    event.respondWith(staleWhileRevalidate(request, CDN_CACHE));
  }
});
