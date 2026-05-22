/**
 * FH6 Dashboard – Service Worker
 *
 * Strategy:
 *  - Precache the app shell (index.html, car_names.js) on install.
 *  - Cache-first for map tiles (/maptiles/…) and CDN assets (Leaflet).
 *  - Network-first with cache fallback for everything else.
 *  - WebSocket connections bypass service workers natively; no special handling needed.
 */

const CACHE = "fh6-v1";

/** Resources to warm-cache on install. */
const PRECACHE = ["/", "/car_names.js"];

// ── Lifecycle ──────────────────────────────────────────────────────────────

self.addEventListener("install", (evt) => {
    evt.waitUntil(
        caches
            .open(CACHE)
            .then((c) => c.addAll(PRECACHE))
            .then(() => self.skipWaiting()),
    );
});

self.addEventListener("activate", (evt) => {
    evt.waitUntil(
        caches
            .keys()
            .then((keys) =>
                Promise.all(
                    keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)),
                ),
            )
            .then(() => self.clients.claim()),
    );
});

// ── Fetch ──────────────────────────────────────────────────────────────────

self.addEventListener("fetch", (evt) => {
    const { request } = evt;

    // Only handle GET requests (WebSocket upgrades are not GET and bypass SW anyway).
    if (request.method !== "GET") return;

    const url = new URL(request.url);

    const isTile = url.pathname.startsWith("/maptiles/");
    const isCarNames = url.pathname === "/car_names.js";
    const isCDN = url.hostname === "unpkg.com";

    if (isTile || isCarNames || isCDN) {
        // Cache-first: these assets are static and large; avoid redundant network hits.
        evt.respondWith(cacheFirst(request));
        return;
    }

    // Network-first for the app shell so updates are reflected immediately.
    evt.respondWith(networkFirst(request));
});

// ── Strategies ─────────────────────────────────────────────────────────────

async function cacheFirst(request) {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    // Cache opaque (CDN cross-origin) and normal success responses.
    if (response.ok || response.type === "opaque") {
        cache.put(request, response.clone());
    }
    return response;
}

async function networkFirst(request) {
    const cache = await caches.open(CACHE);
    try {
        const response = await fetch(request);
        if (response.ok) {
            cache.put(request, response.clone());
        }
        return response;
    } catch {
        const cached = await cache.match(request);
        if (cached) return cached;
        // Return a minimal offline shell so the page still loads.
        return new Response("Offline – reconnect to the FH6 server.", {
            status: 503,
            headers: { "Content-Type": "text/plain" },
        });
    }
}
