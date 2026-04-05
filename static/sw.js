const CACHE = 'vf-v1';
const STATIC = ['/', '/manifest.json'];

self.addEventListener('install', e => {
    e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
    self.skipWaiting();
});

self.addEventListener('activate', e => {
    e.waitUntil(caches.keys().then(keys =>
        Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ));
});

self.addEventListener('fetch', e => {
    const url = e.request.url;
    // Don't cache API calls
    if (['/login', '/register', '/generate', '/status', '/download'].some(p => url.includes(p))) return;
    if (e.request.method !== 'GET') return;
    e.respondWith(
        caches.match(e.request).then(r => r || fetch(e.request))
    );
});
