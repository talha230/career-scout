/*
 * Jarvis service worker — T068.
 *
 * - Hashed build assets: cache-first (a new build has new names).
 * - Page loads: network-first, falling back to the cached app shell offline.
 * - GET /api: network-first; offline, the last saved copy is served with an
 *   `X-Jarvis-Cached-At` header so the UI can say it is a saved copy. A stale
 *   job list shown as if it were current would misrepresent what is open.
 * - Anything that is not a GET is never cached or replayed. A write — above
 *   all an approval — happens against the live server or not at all.
 */
const SHELL = "jarvis-shell-v1";
const API = "jarvis-api-v1";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(["/", "/manifest.webmanifest", "/icon-192.png"])));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL && k !== API).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

async function apiNetworkFirst(request) {
  const cache = await caches.open(API);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch {
    const saved = await cache.match(request);
    if (!saved) throw new Error("offline and no saved copy");
    const headers = new Headers(saved.headers);
    headers.set("X-Jarvis-Cached-At", saved.headers.get("date") || "unknown");
    return new Response(await saved.blob(), { status: saved.status, headers });
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    event.respondWith(apiNetworkFirst(request));
  } else if (url.pathname.startsWith("/assets/")) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(SHELL).then((c) => c.put(request, copy));
        return response;
      }))
    );
  } else if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/")));
  }
});
