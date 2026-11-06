/*
 * 換頁時保留選單的捲動位置 (後台側欄、系統設定左欄…)：點選單是整頁載入，原本每次都會跳回最上面。
 * 用法：可捲動的選單容器加 data-keep-scroll="名稱"；位置存在 sessionStorage (每個瀏覽器分頁各自記)。
 * 由 _admin_sidebar.html 在側欄之後立即載入 (側欄不會先閃到頂端)；之後才出現的選單在 DOMContentLoaded 時處理。
 * 沒有記錄時 (第一次進來)，若目前頁面的選項不在選單可視範圍內，只在選單內捲到看得見它 (不捲動整頁)。
 */
(function () {
    'use strict';
    const PREFIX = 'keepScroll:';

    function revealActive(el) {
        const active = el.querySelector('.active');
        if (!active || el.scrollHeight <= el.clientHeight) return;
        const box = el.getBoundingClientRect();
        const item = active.getBoundingClientRect();
        if (item.top < box.top || item.bottom > box.bottom) el.scrollTop += item.top - box.top - box.height / 3;
    }

    function keep(el) {
        if (el.dataset.keepScrollReady) return;
        el.dataset.keepScrollReady = '1';
        const key = PREFIX + el.dataset.keepScroll;
        let saved = null;
        try { saved = sessionStorage.getItem(key); } catch (err) { /* 無痕模式等不能存取時略過 */ }
        if (saved !== null) el.scrollTop = Number(saved) || 0;
        else revealActive(el);
        const save = () => {
            try { sessionStorage.setItem(key, String(Math.round(el.scrollTop))); } catch (err) { /* 略過 */ }
        };
        el.addEventListener('click', save, true);   // 點選單項目、準備換頁前
        window.addEventListener('pagehide', save);   // 其他方式離開頁面 (表單送出、上一頁…)
    }

    const scan = () => document.querySelectorAll('[data-keep-scroll]').forEach(keep);
    scan();
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scan);
})();
