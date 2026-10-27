/*
 * 進階後台頁面共用的小功能 (列印、複製網址、顯示 / 隱藏區塊)。
 * 各頁面以 data-action 觸發，不使用行內事件 (CSP)。
 */
(function () {
    'use strict';

    Kiosk.on(document, 'click', {
        'print-page': () => window.print(),
        'copy-text': async (el) => {
            const text = el.dataset.text || '';
            try {
                await navigator.clipboard.writeText(text);
                el.textContent = '已複製';
            } catch (err) {
                window.prompt('請手動複製：', text);
            }
        },
        'toggle-target': (el) => {
            const target = document.getElementById(el.dataset.target);
            if (target) target.classList.toggle('d-none');
        },
    });

    // <select data-show-when="值" data-show-target="id">：選到特定值時顯示對應區塊
    document.addEventListener('change', (ev) => {
        const el = ev.target;
        if (!(el instanceof HTMLSelectElement) || !el.dataset.toggleGroup) return;
        document.querySelectorAll(`[data-group="${el.dataset.toggleGroup}"]`).forEach((box) => {
            const values = (box.dataset.when || '').split(',');
            box.classList.toggle('d-none', !values.includes(el.value));
        });
    });
})();
