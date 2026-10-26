/*
 * 點餐機首頁待機輪播：閒置設定秒數後全螢幕輪播推薦餐點 (系統設定 → 菜單顯示)，觸碰任一處關閉並重新計時。
 * 資料來自 page-data 的 attract：{seconds, message, slides: [{name, price, image}]}。
 */
(function () {
    'use strict';

    const cfg = (Kiosk.pageData() || {}).attract;
    const overlay = document.getElementById('attractOverlay');
    if (!cfg || !overlay || !(cfg.slides || []).length || !(cfg.seconds > 0)) return;

    const SLIDE_MS = 5000;
    let idleTimer = null;
    let slideTimer = null;
    let index = 0;

    function showSlide() {
        const s = cfg.slides[index % cfg.slides.length];
        document.getElementById('attractImg').src = s.image;
        document.getElementById('attractImg').alt = s.name;
        document.getElementById('attractName').textContent = s.name;
        document.getElementById('attractPrice').textContent = `$${s.price}`;
        index += 1;
    }

    function start() {
        if (document.querySelector('.modal.show')) { reset(); return; }
        index = 0;
        showSlide();
        overlay.classList.remove('d-none');
        overlay.dataset.active = '1';
        slideTimer = setInterval(showSlide, SLIDE_MS);
    }

    function stop() {
        clearInterval(slideTimer);
        overlay.classList.add('d-none');
        overlay.dataset.active = '';
    }

    function reset() {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(start, cfg.seconds * 1000);
    }

    function wake(ev) {
        if (overlay.dataset.active === '1') {
            ev.preventDefault();
            ev.stopPropagation();
            stop();
        }
        reset();
    }

    ['click', 'touchstart', 'keydown'].forEach((e) => document.addEventListener(e, wake, { capture: true }));
    ['mousemove', 'scroll'].forEach((e) => document.addEventListener(e, () => { if (overlay.dataset.active !== '1') reset(); }, { passive: true }));
    reset();
    window.KioskAttract = { start, stop };
})();
