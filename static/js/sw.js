/*
 * Service Worker：靜態檔 (CSS / JS / 圖示) 快取優先；頁面一律走網路 (菜單、價格、庫存需即時)，
 * 斷線時改顯示離線頁 /offline。API 與訂單不快取。
 */
const CACHE = 'kiosk-static-v2';
const OFFLINE_URL = '/offline';
const PRECACHE = [OFFLINE_URL, '/static/vendor/bootstrap/bootstrap.min.css', '/static/vendor/bootstrap-icons/bootstrap-icons.min.css',
    '/static/css/theme.css', '/static/css/pro.css', '/static/js/common.js', '/static/js/kiosk-offline.js',
    '/static/vendor/bootstrap/bootstrap.bundle.min.js', '/static/img/icon-192.png'];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
    event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
        .then(() => self.clients.claim()));
});

// Web Push：餐點完成 / 外送出發 (app/services/push_service.py)；點通知開啟出餐進度頁
self.addEventListener('push', (event) => {
    let payload = {};
    try { payload = event.data ? event.data.json() : {}; } catch (err) { payload = { body: event.data ? event.data.text() : '' }; }
    const title = payload.title || '餐點通知';
    const options = { body: payload.body || '', tag: payload.tag || 'kiosk-order', icon: '/static/img/icon-192.png',
        badge: '/static/img/icon-192.png', vibrate: [300, 150, 300], data: { url: payload.url || '/' } };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || '/';
    event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
        const hit = wins.find((w) => w.url.includes(url));
        if (hit) return hit.focus();
        return self.clients.openWindow(url);
    }));
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);
    if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
    if (req.mode === 'navigate') {
        event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
        return;
    }
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((resp) => {
            if (resp.ok) {
                const copy = resp.clone();
                caches.open(CACHE).then((cache) => cache.put(req, copy));
            }
            return resp;
        })));
    }
});
