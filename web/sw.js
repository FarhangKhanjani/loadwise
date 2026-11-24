/* Service worker: keeps a copy of the app's own files so it opens instantly, even with a
 * slow connection. Data requests (/metrics, /food-log, ... with the API key) never go
 * through this cache: they always go to the server, so the numbers are never stale.
 *
 * Strategy for the app files: answer from the cache, and refresh the cache in the
 * background. After an update to web/, the phone shows the new version on the next start.
 */
const CACHE = 'loadwise-app-v1';
const FILES = ['./', 'index.html', 'app.css', 'app.js', 'manifest.webmanifest',
  'icon.svg', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const appFile = event.request.method === 'GET' && url.origin === self.location.origin
    && url.pathname.startsWith('/app/');
  if (!appFile) return;  // the browser handles it normally

  event.respondWith(caches.open(CACHE).then(async (cache) => {
    const cached = await cache.match(event.request, { ignoreSearch: true });
    const fresh = fetch(event.request)
      .then((response) => { if (response.ok) cache.put(event.request, response.clone()); return response; })
      .catch(() => cached);
    return cached || fresh;
  }));
});
