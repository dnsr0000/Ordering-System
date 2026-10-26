/*
 * 廚房出單看板 (KDS)：顯示今日製作中的訂單，完成出餐後推進為「已出餐」。
 * 訂單變動由 SSE 通知 (Kiosk.onOrdersChanged)，收到後重新抓取 /api/kitchen_orders。
 */
(function () {
    'use strict';

    const { html } = Kiosk;
    const grid = document.getElementById('ordersGrid');
    const noOrdersMessage = document.getElementById('noOrdersMessage');
    const countBadge = document.getElementById('pendingCountBadge');
    const completeAllBtn = document.getElementById('completeAllBtn');
    const soundToggleBtn = document.getElementById('soundToggleBtn');
    const soundStatusText = document.getElementById('soundStatusText');

    const cfg = Kiosk.pageData();
    const stationId = cfg.stationId || null;
    const warnMinutes = Number(cfg.warnMinutes) || 8;
    const lateMinutes = Number(cfg.lateMinutes) || 15;
    let audioEnabled = cfg.sound !== false;
    let audioCtx = null;
    let isFirstLoad = true;
    let currentOrders = [];
    const knownOrderIds = new Set();

    // ---------------------------------------------------------------- 提示音
    // 取得或喚醒 AudioContext (瀏覽器需使用者互動後才允許播放聲音)
    function getAudioContext() {
        if (!audioCtx) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (AudioContextClass) audioCtx = new AudioContextClass();
        }
        if (audioCtx && audioCtx.state === 'suspended') {
            audioCtx.resume().catch(() => { /* 尚未互動，下次點擊再試 */ });
        }
        return audioCtx;
    }

    // 點擊頁面任何地方即解鎖音效權限
    document.addEventListener('click', getAudioContext, { once: true });

    function playTone(ctx, frequency, start, end) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(frequency, start);
        gain.gain.setValueAtTime(0.4, start);
        gain.gain.exponentialRampToValueAtTime(0.001, end);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(start);
        osc.stop(end);
    }

    // 清脆雙音叮咚 (E5 → G5) 提示音
    function playChime() {
        if (!audioEnabled) return;
        try {
            const ctx = getAudioContext();
            if (!ctx) return;
            const now = ctx.currentTime;
            playTone(ctx, 659.25, now, now + 0.35);
            playTone(ctx, 783.99, now + 0.18, now + 0.7);
        } catch (err) {
            console.warn('音效播放失敗:', err);
        }
    }

    function toggleSound() {
        audioEnabled = !audioEnabled;
        getAudioContext();
        soundStatusText.textContent = audioEnabled ? '已開啟' : '已靜音';
        soundToggleBtn.classList.toggle('btn-outline-warning', audioEnabled);
        soundToggleBtn.classList.toggle('btn-outline-secondary', !audioEnabled);
        if (audioEnabled) playChime(); // 開啟時試播一次
    }

    // ---------------------------------------------------------------- 畫面
    const pad = (n) => String(n).padStart(3, '0');

    function waitStyle(minutes) {
        if (minutes >= lateMinutes) return { border: 'border-time-danger', badge: 'bg-danger', late: true };
        if (minutes >= warnMinutes) return { border: 'border-time-warning', badge: 'bg-warning text-dark' };
        return { border: 'border-time-normal', badge: 'bg-success' };
    }

    function orderCard(o, nowSeconds) {
        const waitMinutes = Math.max(0, Math.floor((nowSeconds - o.timestamp) / 60));
        const style = waitStyle(waitMinutes);
        const takeOut = o.order_type === '外帶' ? html`
            <div class="mb-2">
                <span class="badge ${o.need_cutlery ? 'bg-success' : 'bg-secondary'} small">${o.need_cutlery ? '要餐具' : '不用餐具'}</span>
                ${o.note ? html`<div class="small text-warning mt-1 kds-note">備註：${o.note}</div>` : ''}
            </div>` : '';
        const items = o.items.map((item) => html`
            <div class="item-row py-2 ${item.done || !Number(item.quantity) ? 'opacity-50 text-decoration-line-through' : ''}">
                <div class="d-flex justify-content-between align-items-center gap-2">
                    <span class="fs-5 fw-bold text-white kds-item-name">${item.name}
                        ${item.station && !stationId ? html`<span class="badge bg-dark border border-secondary small ms-1">${item.station}</span>` : ''}</span>
                    <span class="badge bg-light text-dark fs-6 px-2">x ${Number(item.quantity) || 0}</span>
                </div>
                ${Number(item.voided) ? html`<div class="badge bg-danger mt-1 kds-voided"><i class="bi bi-x-octagon me-1"></i>已作廢 ${Number(item.voided)} 份</div>` : ''}
                ${item.customization ? html`
                    <div class="text-danger fw-bold small mt-1">
                        <i class="bi bi-arrow-return-right me-1"></i>${item.customization}
                    </div>` : ''}
            </div>`);

        return html`
            <div class="col-12 col-md-6 col-lg-4 col-xl-3" data-order-id="${o.id}">
                <div class="kds-card p-3 h-100 shadow-lg ${style.border} ${o.is_rush ? 'kds-rush' : ''}">
                    ${o.is_rush ? html`<div class="badge bg-danger w-100 fs-6 mb-2 kds-rush-badge"><i class="bi bi-lightning-charge-fill me-1"></i>加急</div>` : ''}
                    ${o.pickup_at ? html`<div class="badge bg-info text-dark w-100 fs-6 mb-2 kds-pickup-at"><i class="bi bi-clock me-1"></i>預約 ${o.pickup_at} 取餐</div>` : ''}
                    <div class="d-flex justify-content-between align-items-center mb-2 pb-2 border-bottom border-secondary">
                        <div>
                            <h3 class="fw-bold text-warning mb-0 kds-pickup-number">#${pad(o.pickup_number)}</h3>
                            <small class="text-white-50">單號 #${o.id} • <i class="bi bi-person me-1"></i>${o.table_number}</small>
                        </div>
                        <div class="text-end">
                            <span class="badge bg-primary mb-1">${o.order_type}</span><br>
                            <span class="badge ${style.badge}"><i class="bi bi-clock me-1"></i>${style.late ? '逾時 ' : '已等 '}${waitMinutes} 分鐘</span>
                        </div>
                    </div>
                    ${takeOut}
                    <div class="flex-grow-1 my-2 kds-items">${items}</div>
                    <div class="pt-2 mt-auto d-flex gap-2">
                        <button type="button" class="btn btn-outline-danger btn-sm" data-action="toggle-rush" data-id="${o.id}" data-rush="${o.is_rush ? 1 : 0}"
                                title="${o.is_rush ? '取消加急' : '設為加急'}" aria-label="加急"><i class="bi bi-lightning-charge"></i></button>
                        <button type="button" class="btn btn-complete flex-grow-1 shadow" data-action="complete-order" data-id="${o.id}">
                            <i class="bi bi-check-circle-fill me-1"></i> ${stationId ? '本站完成' : '完成出餐'}
                        </button>
                    </div>
                </div>
            </div>`;
    }

    function render() {
        countBadge.textContent = currentOrders.length;
        completeAllBtn.disabled = currentOrders.length === 0;
        noOrdersMessage.classList.toggle('d-none', currentOrders.length > 0);
        const nowSeconds = Date.now() / 1000;
        Kiosk.setHtml(grid, currentOrders.map((o) => orderCard(o, nowSeconds)));
    }

    function applyOrders(orders) {
        // 新進訂單 (非第一次載入) 播放提示音
        let hasNewOrder = false;
        const ids = new Set(orders.map((o) => o.id));
        orders.forEach((o) => {
            if (!knownOrderIds.has(o.id)) {
                if (!isFirstLoad) hasNewOrder = true;
                knownOrderIds.add(o.id);
            }
        });
        [...knownOrderIds].forEach((id) => { if (!ids.has(id)) knownOrderIds.delete(id); });
        isFirstLoad = false;
        currentOrders = orders;
        render();
        if (hasNewOrder) playChime();
    }

    async function loadOrders() {
        try {
            const data = await Kiosk.api(stationId ? `/api/kitchen_orders?station=${stationId}` : '/api/kitchen_orders');
            applyOrders(data.orders || []);
        } catch (err) {
            console.warn('KDS 載入失敗:', err.message);
        }
    }

    // ---------------------------------------------------------------- 操作
    async function completeOrder(btn) {
        getAudioContext();
        const orderId = Number(btn.dataset.id);
        btn.disabled = true;
        try {
            if (stationId) {
                await Kiosk.api(`/api/kitchen/orders/${orderId}/station_done`, { json: { station_id: stationId } });
            } else {
                await Kiosk.api(`/api/orders/${orderId}/status`, { json: { status: 'Completed' } });
            }
            knownOrderIds.delete(orderId);
        } catch (err) {
            alert(`❌ ${err.message}`);
        }
        loadOrders();
    }

    async function completeAll() {
        getAudioContext();
        const count = currentOrders.length;
        if (count === 0) {
            alert('目前沒有待製作的訂單！');
            return;
        }
        if (!window.confirm(`⚠️ 確定要將目前畫面上共 ${count} 筆待製作訂單【全部標記為已出餐】嗎？`)) return;

        const original = completeAllBtn.innerHTML;
        completeAllBtn.disabled = true;
        Kiosk.setHtml(completeAllBtn, html`<span class="spinner-border spinner-border-sm me-1"></span> 處理中...`);
        try {
            await Kiosk.api('/api/kitchen_complete_all', { method: 'POST' });
            knownOrderIds.clear();
        } catch (err) {
            alert(`❌ ${err.message}`);
        } finally {
            completeAllBtn.innerHTML = original;
            loadOrders();
        }
    }

    async function toggleRush(btn) {
        try {
            await Kiosk.api(`/api/kitchen/orders/${btn.dataset.id}/rush`, { json: { is_rush: btn.dataset.rush !== '1' } });
        } catch (err) {
            alert(`❌ ${err.message}`);
        }
        loadOrders();
    }

    Kiosk.on(document, 'click', {
        'complete-order': (el) => completeOrder(el),
        'toggle-rush': (el) => toggleRush(el),
        'complete-all': () => completeAll(),
        'toggle-sound': () => toggleSound(),
    });

    Kiosk.startClock(document.getElementById('clock'));
    setInterval(render, 30000); // 定期更新「已等 N 分鐘」與逾時顏色
    loadOrders();
    Kiosk.onOrdersChanged(loadOrders);
})();
