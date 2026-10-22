/* 列印桌卡頁 (table_qr_print.html) */
(() => {
    Kiosk.on(document, 'click', {
        print: () => window.print(),
    });
})();
