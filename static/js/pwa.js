/*
 * PWA：註冊 Service Worker (加到主畫面、斷線時顯示離線頁)。
 * Service Worker 只能在 https 或 localhost 使用；門市以 http://區網IP 開啟時瀏覽器不允許，會自動略過。
 */
(function () {
    'use strict';
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch((err) => console.warn('Service Worker 註冊失敗:', err.message));
    });
})();
