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

    const voice = Kiosk.pageData();
    const template = voice.template || '取餐號碼 {number}，請到櫃檯取餐';
    const repeat = Math.max(1, Number(voice.repeat) || 1);
    let speechEnabled = voice.enabled !== false;
    let lastCalledIds = new Set();
    let isFirstLoad = true;
    let lastReadyIds = new Set();
    let lastReadySignature = null;
    let lastPreparingSignature = null;

    const signature = (orders) => orders.map((o) => o.id).join(',');

    // 瀏覽器在使用者第一次點擊頁面前會忽略 speechSynthesis.speak()：掛牆的看板沒人點就不會出聲，
    // 所以未解鎖前顯示「待啟用」與提示，點一下畫面任何地方 (含語音按鈕) 才算開啟
    let speechUnlocked = Boolean(navigator.userActivation && navigator.userActivation.hasBeenActive);
    let unlockHint = null;

    // ---------------------------------------------------------------- 語音叫號
    function speak(text, times = 1) {
        if (!speechEnabled || !window.speechSynthesis) return;
        try {
            // 不先 cancel()：前一號還沒唸完時新號碼排在後面接著唸 (cancel 會截斷並清掉重複播報)
            for (let i = 0; i < times; i += 1) {
                const utterance = new SpeechSynthesisUtterance(text);
                utterance.lang = 'zh-TW';
                utterance.rate = 0.85;
                utterance.pitch = 1.0;
                window.speechSynthesis.speak(utterance);
            }
        } catch (err) {
            console.warn('語音播放失敗:', err);
        }
    }

    function renderCalled(list) {
        const box = document.getElementById('waitlistCalledBox');
        if (!box) return;
        box.classList.toggle('d-none', list.length === 0);
        const fresh = list.filter((w) => !isFirstLoad && !lastCalledIds.has(w.id));
        if (fresh.length) speak(`候位號碼 ${fresh.map((w) => w.label.split('').join(' ')).join('、')}，請至櫃檯入座`, repeat);
        lastCalledIds = new Set(list.map((w) => w.id));
        Kiosk.setHtml(document.getElementById('waitlistCalledList'), list.map((w) => html`
            <span class="badge bg-info text-dark fs-4 px-3 py-2">${w.label} (${w.party_size} 位)</span>`));
    }

    function renderSpeechStatus() {
        speechStatusText.textContent = !speechEnabled ? '已靜音' : (speechUnlocked ? '已開啟' : '待啟用 (點一下畫面)');
        speechToggleBtn.classList.toggle('btn-outline-warning', speechEnabled);
        speechToggleBtn.classList.toggle('btn-outline-secondary', !speechEnabled);
        if (unlockHint) unlockHint.classList.toggle('d-none', speechUnlocked || !speechEnabled);
    }

    function showUnlockHint() {
        if (speechUnlocked || !window.speechSynthesis) return;
        unlockHint = document.createElement('div');
        unlockHint.className = 'alert alert-warning fw-bold shadow position-fixed bottom-0 start-50 translate-middle-x mb-3 z-3 text-center';
        unlockHint.setAttribute('role', 'status');
        unlockHint.textContent = '點一下畫面任何地方即可啟用語音叫號';
        document.body.appendChild(unlockHint);
    }

    function unlockSpeech() {
        if (speechUnlocked) return;
        speechUnlocked = true;
        renderSpeechStatus();
        speak('系統語音叫號已開啟');   // 試念一句：確認有聲音
    }

    function toggleSpeech() {
        if (!speechUnlocked && speechEnabled) {   // 第一下是啟用語音，不是切成靜音
            unlockSpeech();
            return;
        }
        speechUnlocked = true;
        speechEnabled = !speechEnabled;
        if (!speechEnabled && window.speechSynthesis) window.speechSynthesis.cancel();   // 靜音：停止正在唸的叫號
        renderSpeechStatus();
        if (speechEnabled) speak('系統語音叫號已開啟');
    }

    // ---------------------------------------------------------------- 畫面
    function renderReady(orders) {
        const sig = signature(orders);
        if (sig === lastReadySignature) return; // 清單未變動就不重繪，避免閃爍
        lastReadySignature = sig;

        // 新出餐的號碼合併成一次語音播報 (第一次載入不播)
        const newlyReady = orders.filter((o) => !isFirstLoad && !lastReadyIds.has(o.id))
            .map((o) => `${Kiosk.pad3(o.pickup_number)} 號`);
        if (newlyReady.length) speak(template.replace('{number}', newlyReady.join('、')), repeat);
        lastReadyIds = new Set(orders.map((o) => o.id));

        noReadyMessage.classList.toggle('d-none', orders.length > 0);
        Kiosk.setHtml(readyGrid, orders.map((o) => html`
            <div class="col-12 col-sm-6 col-lg-4" data-order-id="${o.id}">
                <div class="number-card p-4 text-center">
                    <div class="text-white-50 small mb-1">取餐編號</div>
                    <div class="order-number-display">${Kiosk.pickupNo(o.pickup_number)}</div>
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
                    <span class="fw-bold text-warning fs-5 me-2">取餐 ${Kiosk.pickupNo(o.pickup_number)}</span>
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
            renderCalled(data.called_waitlist || []);
            isFirstLoad = false;
        } catch (err) {
            console.warn('叫號看板同步失敗:', err.message);
        }
    }

    showUnlockHint();
    renderSpeechStatus();
    Kiosk.on(document, 'click', { 'toggle-speech': () => toggleSpeech() });
    // 點畫面任何地方即解鎖語音 (語音按鈕由 toggleSpeech 處理，這裡之後再執行時已解鎖、不會重複)
    document.addEventListener('click', unlockSpeech, { once: true });
    Kiosk.startClock(document.getElementById('counter-clock'));
    loadBoard();
    Kiosk.onOrdersChanged(loadBoard);
})();
