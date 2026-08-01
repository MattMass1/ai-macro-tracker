/* Macro Tracker service worker.
 *
 * Strategy:
 *  - _next/static/*  → cache-first. Hashed filenames are immutable, so a
 *    cached chunk is never stale. This is what makes the shell load instantly
 *    from the phone.
 *  - navigations     → network-first with cached fallback. Fresh HTML when
 *    online; the last good shell when the network or backend is waking up.
 *  - everything else → network-only. API responses must stay fresh.
 */

const STATIC_CACHE = "macro-static-v1";
const SHELL_CACHE = "macro-shell-v1";

self.addEventListener("install", (event) => {
  // Don't force the old SW out until the new one is active.
  self.skipWaiting();
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(["/"])).catch(() => {}),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== STATIC_CACHE && key !== SHELL_CACHE)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Hashed static chunks: cache-first.
  if (url.pathname.startsWith("/_next/static/")) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(STATIC_CACHE).then((cache) => cache.put(request, clone));
          }
          return response;
        });
      }),
    );
    return;
  }

  // Navigations: network-first, cached shell as fallback.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put(request, clone));
          }
          return response;
        })
        .catch(() => caches.match(request).then((cached) => cached || caches.match("/"))),
    );
    return;
  }

  // Everything else (API, auth): network only.
});
