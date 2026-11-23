/**
 * ClipFlow Service Worker
 * Provides offline resilience, app-shell caching, and asset performance.
 * Security: Strict network-only policy for authentication, private APIs, and user data.
 */

const CACHE_VERSION = 'clipflow-v1';
const STATIC_CACHE = `clipflow-static-${CACHE_VERSION}`;

// Pre-cached static assets required for the app shell
const PRECACHE_ASSETS = [
  '/offline.html',
  '/manifest.json',
  '/brand/favicon.svg',
  '/brand/logo.svg',
  '/brand/logo-light.svg',
  '/brand/icon-192.png',
  '/brand/icon-512.png',
  '/brand/icon-maskable-192.png',
  '/brand/icon-maskable-512.png',
  '/assets/gradient-ascii.js'
];

// Patterns and endpoints that must NEVER be cached (strictly network-only)
const NEVER_CACHE_PREFIXES = [
  '/api/',          // Dynamic APIs, user data, sync, tokens, settings
  '/media/',        // Private user video and audio streams
  '/oauth/',        // OAuth redirects and callbacks
  '/auth/',         // Authentication endpoints
  '/admin/'         // Admin operations
];

// 1. Installation: cache app shell assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(async cache => {
      // Precache critical assets individually so one failure does not abort all
      for (const url of PRECACHE_ASSETS) {
        try {
          await cache.add(url);
        } catch (err) {
          console.warn(`[ClipFlow SW] Optional precache skipped for ${url}:`, err);
        }
      }
    }).then(() => self.skipWaiting())
  );
});

// 2. Activation: purge stale cache versions
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys.map(key => {
          if (key !== STATIC_CACHE) {
            console.log(`[ClipFlow SW] Purging old cache: ${key}`);
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// 3. Fetch handling
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Security Rule 1: Non-GET requests (POST, PUT, DELETE, PATCH) are NEVER cached
  if (req.method !== 'GET') {
    return;
  }

  // Security Rule 2: NEVER cache sensitive/private endpoints
  const isNeverCache = NEVER_CACHE_PREFIXES.some(prefix => url.pathname.startsWith(prefix));
  if (isNeverCache) {
    return; // Pass through to network with standard browser behavior
  }

  // Strategy A: Navigation requests (HTML pages) -> Network-first with offline fallback
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .catch(async () => {
          // If network is offline, attempt to return cached offline fallback page
          const cache = await caches.open(STATIC_CACHE);
          const cachedOffline = await cache.match('/offline.html');
          if (cachedOffline) {
            return cachedOffline;
          }
          return new Response(
            '<html><body><h1>Offline</h1><p>Please check your connection.</p></body></html>',
            { headers: { 'Content-Type': 'text/html' } }
          );
        })
    );
    return;
  }

  // Strategy B: Static Assets (Images, Icons, Fonts, JS, CSS) -> Cache-first with network fallback
  const isStaticAsset = (
    url.origin === self.location.origin && (
      url.pathname.startsWith('/brand/') ||
      url.pathname.startsWith('/assets/') ||
      url.pathname === '/manifest.json' ||
      /\.(png|jpg|jpeg|svg|webp|ico|woff2|woff|ttf|css|js)$/i.test(url.pathname)
    )
  ) || (
    url.hostname === 'fonts.googleapis.com' ||
    url.hostname === 'fonts.gstatic.com'
  );

  if (isStaticAsset) {
    event.respondWith(
      caches.match(req).then(cached => {
        if (cached) {
          // Stale-while-revalidate for background freshness
          fetch(req).then(networkResp => {
            if (networkResp && networkResp.status === 200) {
              caches.open(STATIC_CACHE).then(cache => cache.put(req, networkResp));
            }
          }).catch(() => { /* Offline; ignore background refresh */ });
          return cached;
        }

        // Cache miss: fetch from network and cache
        return fetch(req).then(networkResp => {
          if (networkResp && (networkResp.status === 200 || networkResp.type === 'opaque')) {
            const clone = networkResp.clone();
            caches.open(STATIC_CACHE).then(cache => cache.put(req, clone));
          }
          return networkResp;
        });
      })
    );
    return;
  }

  // Default: Network with graceful catch
  event.respondWith(
    fetch(req).catch(() => caches.match(req))
  );
});

// 4. Message events: skip waiting and clear on logout
self.addEventListener('message', event => {
  if (!event.data) return;

  if (event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }

  // When user logs out, purge any dynamic caches
  if (event.data.type === 'LOGOUT') {
    event.waitUntil(
      caches.keys().then(keys => {
        return Promise.all(keys.map(k => caches.delete(k)));
      }).then(() => self.clients.claim())
    );
  }
});
