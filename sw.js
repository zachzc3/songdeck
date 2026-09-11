/* Song Deck service worker — makes the page + data work with no signal.
 * Bump CACHE when you change index.html / manifest so clients refresh.
 * Note: audio still streams from YouTube and needs a connection to play.
 */
const CACHE = "songdeck-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return; // let YouTube etc. hit the network

  if (url.pathname.endsWith("songs.json")) {
    // network-first so edits to the deck land, cache as offline fallback
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // cache-first for the app shell
  e.respondWith(caches.match(e.request).then((res) => res || fetch(e.request)));
});
