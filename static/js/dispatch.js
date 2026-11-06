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

    Kiosk.on(document, 'click', { 'select-all': (el) => { el.select(); } });
    // 整頁重新載入的代價高：斷線期間改為每分鐘一次；外送員回報位置不會觸發 SSE，另外每分鐘刷新
    Kiosk.onOrdersChanged(reloadIfIdle, { fallbackMs: 60000 });
    Kiosk.poll(reloadIfIdle, 60000);
})();
