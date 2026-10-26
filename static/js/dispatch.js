/* 外送派車：訂單變動時自動重新整理 (正在輸入時不打斷；連線建立時的第一個通知不算) */
(function () {
    'use strict';

    const loadedAt = Date.now();

    function reloadIfIdle() {
        if (Date.now() - loadedAt < 5000) return;
        const active = document.activeElement;
        if (active && ['INPUT', 'SELECT', 'TEXTAREA'].includes(active.tagName)) return;
        window.location.reload();
    }

    try { Kiosk.onOrdersChanged(reloadIfIdle); } catch (err) { /* 不支援 SSE */ }
    setInterval(reloadIfIdle, 60000);
})();
