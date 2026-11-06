/*
 * 後台側欄：換頁時保留側欄的捲動位置 (每次點選單都是整頁載入，原本會跳回最上面)。
 * 位置存在 sessionStorage (每個瀏覽器分頁各自記)；由 _admin_sidebar.html 在側欄之後立即載入，畫面不會先閃到頂端。
 * 第一次進後台 (沒有記錄) 時，若目前頁面的選項不在可視範圍內，捲到看得見它。
 */
(function () {
    'use strict';
    const KEY = 'adminSidebarScroll';
    const body = document.querySelector('#adminSidebar .admin-sidebar-body');
    if (!body) return;

    let saved = null;
    try { saved = sessionStorage.getItem(KEY); } catch (err) { /* 無痕模式等不能存取時略過 */ }
    if (saved !== null) {
        body.scrollTop = Number(saved) || 0;
    } else {
        const active = body.querySelector('.admin-nav-link.active');
        if (active) active.scrollIntoView({ block: 'nearest' });
    }

    const save = () => {
        try { sessionStorage.setItem(KEY, String(Math.round(body.scrollTop))); } catch (err) { /* 略過 */ }
    };
    body.addEventListener('click', save, true);       // 點選單項目、準備換頁前
    window.addEventListener('pagehide', save);         // 其他方式離開頁面 (表單送出、上一頁…)
})();
