/*
 * 公開取餐叫號看板 (免登入)：左側顯示已出餐號碼並語音叫號，右側顯示製作中清單。
 * 訂單變動由 SSE 通知，收到後重新抓取 /api/counter_orders。
 */
(function () {
    'use strict';

    const { html } = Kiosk;
    const readyGrid = document.getElementById('readyOrdersGrid');
    const noReadyMessage = document.getElementById('noReadyMessage');
    const preparingList = document.getElementById('preparingList');
    const speechToggleBtn = document.getElementById('speechToggleBtn');
    const speechStatusText = document.getElementById('speechStatusText');

    let speechEnabled = true;
    let isFirstLoad = true;
    let lastReadyIds = new Set();
    let lastReadySignature = null;
    let lastPreparingSignature = null;

    const pad = (n) => String(n).padStart(3, '0');
    const signature = (orders) => orders.map((o) => o.id).join(',');

    // ---------------------------------------------------------------- 語音叫號
    function speak(text) {
        if (!speechEnabled || !window.speechSynthesis) return;
        try {
            window.speechSynthesis.cancel();
            const utterance = new SpeechSynthesisUtterance(text);
            utterance.lang = 'zh-TW';
            utterance.rate = 0.85;
            utterance.pitch = 1.0;
            window.speechSynthesis.speak(utterance);
        } catch (err) {
            console.warn('語音播放失敗:', err);
        }
    }

    function toggleSpeech() {
        speechEnabled = !speechEnabled;
        speechStatusText.textContent = speechEnabled ? '已開啟' : '已靜音';
        speechToggleBtn.classList.toggle('btn-outline-warning', speechEnabled);
        speechToggleBtn.classList.toggle('btn-outline-secondary', !speechEnabled);
        if (speechEnabled) speak('系統語音叫號已開啟');
    }

    // ---------------------------------------------------------------- 畫面
    function renderReady(orders) {
        const sig = signature(orders);
        if (sig === lastReadySignature) return; // 清單未變動就不重繪，避免閃爍
        lastReadySignature = sig;

        // 新出餐的號碼合併成一次語音播報 (第一次載入不播)
        const newlyReady = orders.filter((o) => !isFirstLoad && !lastReadyIds.has(o.id))
            .map((o) => `${pad(o.pickup_number)} 號`);
        if (newlyReady.length) speak(`請，取餐號碼 ${newlyReady.join('、')}，至櫃檯取餐`);
        lastReadyIds = new Set(orders.map((o) => o.id));

        noReadyMessage.classList.toggle('d-none', orders.length > 0);
        Kiosk.setHtml(readyGrid, orders.map((o) => html`
            <div class="col-12 col-sm-6 col-lg-4" data-order-id="${o.id}">
                <div class="number-card p-4 text-center">
                    <div class="text-white-50 small mb-1">取餐編號</div>
                    <div class="order-number-display">#${pad(o.pickup_number)}</div>
                    <small class="text-white-50 d-block">系統單號 #${o.id}</small>
                    <div class="badge bg-success text-dark fw-bold mt-2 px-3 py-1 text-wrap">${o.order_type} (${o.user_name})</div>
                </div>
            </div>`));
    }

    function renderPreparing(orders) {
        const sig = signature(orders);
        if (sig === lastPreparingSignature) return;
        lastPreparingSignature = sig;

        if (!orders.length) {
            Kiosk.setHtml(preparingList, html`<div class="text-white-50 text-center py-4 small">目前沒有製作中的訂單</div>`);
            return;
        }
        Kiosk.setHtml(preparingList, orders.map((o) => html`
            <div class="p-3 rounded bg-dark border border-secondary d-flex justify-content-between align-items-center" data-order-id="${o.id}">
                <div>
                    <span class="fw-bold text-warning fs-5 me-2">取餐 #${pad(o.pickup_number)}</span>
                    <small class="text-white-50 me-2">(單號 #${o.id})</small>
                    <span class="badge bg-primary">${o.order_type}</span>
                </div>
                <div class="text-white-50 small"><i class="bi bi-clock me-1"></i>${o.created_at}</div>
            </div>`));
    }

    async function loadBoard() {
        try {
            const data = await Kiosk.api('/api/counter_orders');
            renderReady(data.ready_orders || []);
            renderPreparing(data.preparing_orders || []);
            isFirstLoad = false;
        } catch (err) {
            console.warn('叫號看板同步失敗:', err.message);
        }
    }

    Kiosk.on(document, 'click', { 'toggle-speech': () => toggleSpeech() });
    Kiosk.startClock(document.getElementById('counter-clock'));
    loadBoard();
    Kiosk.onOrdersChanged(loadBoard);
})();
