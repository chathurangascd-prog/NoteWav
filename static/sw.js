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
// the background to update the cache for NEXT time. Switched to
// NETWORK-FIRST: when online, always fetch the live version first
// (and refresh the cache with it) — the cache is now purely an
// OFFLINE fallback, not something that can make the app show stale
// content while online.
//
// FIX #2 (Aug 18, 2026 — daily quote card, and other fresh-deploy
// content, STILL sometimes only appearing after a manual page
// refresh, even with network-first above): "network-first" was
// correct in INTENT, but fetch(event.request) here still goes through
// the BROWSER's own separate HTTP disk cache first — a completely
// different cache from this Service Worker's own `caches` storage.
// Flask serves static files (script.js, styles.css, etc.) without an
// explicit "never cache" instruction, so the browser was sometimes
// silently satisfying this "network" fetch from its own disk cache
// instead of truly hitting the network — even though, from this
// Service Worker's code's point of view, it looked like a normal
// fetch. A manual browser refresh (Cmd/Ctrl+R) forces the browser to
// actually revalidate with the server, which is exactly why refreshing
// "fixed" it. Passing { cache: 'no-store' } tells the browser to skip
// its own HTTP cache entirely for this request, so "network-first"
// now genuinely means "always ask the real server," matching the
// original intent of the FIX above.

const CACHE_NAME = 'notewav-shell-v16'; // bumped: network-first fetches now bypass the browser's own HTTP cache too, not just this SW's cache
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

    // App shell files: NETWORK-FIRST, and now genuinely bypassing the
    // browser's own HTTP cache (see FIX #2 above) — { cache: 'no-store' }
    // forces a real round-trip to the server every time, so anyone
    // online always sees the latest deployed version on their very
    // next request, not "whichever version the browser happened to
    // have cached." Only falls back to THIS Service Worker's own
    // cached copy if the network request genuinely fails (offline, or
    // — for generated audio files that have since been cleaned up
    // server-side — a non-ok response like 404).
    event.respondWith(
        fetch(event.request, { cache: 'no-store' })
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