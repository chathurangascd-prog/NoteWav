// NoteWav AI — Service Worker
// Caches the app "shell" (HTML/CSS/JS/icons) so the app still opens
// (even if showing stale content) when there's no network. API calls
// (/process-note, /tts, /ocr, /library/*) are NEVER cached — they
// always need a live network request, so those are excluded here.

const CACHE_NAME = 'notewav-shell-v9'; // bumped after cloneNode-based PDF fix (root cause: Mermaid's embedded stylesheet)
const SHELL_FILES = [
    '/',
    '/static/styles.css',
    '/static/script.js',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((names) =>
            Promise.all(
                names
                    .filter((name) => name !== CACHE_NAME)
                    .map((name) => caches.delete(name))
            )
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Never cache API/data routes — always go to the network for these.
    const isApiRoute = ['/process-note', '/tts', '/ocr', '/library', '/health']
        .some((path) => url.pathname.startsWith(path));
    if (isApiRoute || event.request.method !== 'GET') {
        return; // let the browser handle it normally
    }

    // App shell files: cache-first, falling back to network, and
    // updating the cache in the background when possible.
    event.respondWith(
        caches.match(event.request).then((cached) => {
            const networkFetch = fetch(event.request)
                .then((response) => {
                    if (response && response.ok) {
                        const clone = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
                    }
                    return response;
                })
                .catch(() => cached); // offline — fall back to cache

            return cached || networkFetch;
        })
    );
});