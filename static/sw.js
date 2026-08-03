// NoteWav AI — Service Worker
// Caches the app "shell" (HTML/CSS/JS/icons) so the app still opens
// (even if showing stale content) when there's no network. API calls
// (/process-note, /tts, /ocr, /library/*, /announcements/*, /track,
// /admin/*, /auth/*, /user/*) are NEVER cached — they always need a
// live network request, so those are excluded here.
//
// FIX (users had to refresh 2-3 times to see ANY deployed change —
// e.g. the signed-in email not appearing right after logging in):
// the fetch handler used to be CACHE-FIRST — it served whatever was
// already cached immediately, and only fetched the network version in
// the background to update the cache for NEXT time. That meant every
// single deploy was invisible until a person reloaded the page TWICE
// (once to trigger the background cache update, once more to actually
// see it) — the exact "need to refresh several times" symptom.
// Switched to NETWORK-FIRST: when online, always fetch the live
// version first (and refresh the cache with it) — the cache is now
// purely an OFFLINE fallback, not something that can make the app show
// stale content while online.

const CACHE_NAME = 'notewav-shell-v15'; // bumped: cache fallback now also covers non-ok responses (offline/post-cleanup audio playback)
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

    // Never cache API/data/auth routes — always go to the network for these.
    const isApiRoute = ['/process-note', '/tts', '/ocr', '/library', '/health', '/announcements', '/track', '/admin', '/auth', '/user']
        .some((path) => url.pathname.startsWith(path));
    if (isApiRoute || event.request.method !== 'GET') {
        return; // let the browser handle it normally
    }

    // App shell files: NETWORK-FIRST. Try the live network copy first
    // (and refresh the cache with whatever comes back) — only fall
    // back to the cached copy if the network request actually fails
    // (i.e. genuinely offline). This guarantees anyone online always
    // sees the latest deployed version on their very next request, not
    // "whichever version happened to be cached last time."
    //
    // FIX (offline audio playback): generated audio files
    // (/static/output_*.mp3) live on the server for only 1 hour before
    // being cleaned up — after that, a normal fetch() succeeds but
    // returns a 404 (not a network error), which the old logic didn't
    // treat as a reason to fall back to cache. Now: if we're online but
    // got a non-ok response (404/500/etc), fall back to whatever this
    // browser already has cached for that exact URL — so audio a
    // person already played once keeps working, whether truly offline
    // or just after the server's own cleanup.
    event.respondWith(
        fetch(event.request)
            .then((response) => {
                if (response && response.ok) {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
                    return response;
                }
                return caches.match(event.request).then((cached) => cached || response);
            })
            .catch(() => caches.match(event.request))
    );
});