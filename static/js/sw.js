/*
 * Service Worker：有版本參數 (?v=) 的靜態檔快取優先、沒有的 (模組 import) 網路優先；頁面一律走網路 (菜單、價格、庫存需即時)，
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
        const save = (resp) => {
            if (resp.ok) {
                const copy = resp.clone();
                caches.open(CACHE).then((cache) => cache.put(req, copy));
            }
            return resp;
        };
        // 離線時：帶版本參數的網址也可以用預先快取的同一個檔案 (預先快取清單沒有 ?v=)
        const offline = () => caches.match(req, { ignoreSearch: true });
        if (url.searchParams.has('v')) {
            // 有版本參數 (?v=)：內容不會變，快取優先
            event.respondWith(caches.match(req).then((hit) => hit || fetch(req).then(save).catch(offline)));
        } else {
            // 沒有版本參數 (ES module 的相對 import 如 ./customer/core.js)：網路優先，離線才用快取 ——
            // 快取優先時，部署後第一次開啟會拿到舊模組配新主程式，整頁無法執行
            event.respondWith(fetch(req).then(save).catch(offline));
        }
    }
});
