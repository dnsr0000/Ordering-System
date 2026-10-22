/*
 * 櫃檯取餐核銷：列出今日已出餐、等待取餐的訂單，按下「顧客已取餐」後推進為 PickedUp。
 * 核銷採樂觀更新 (先從畫面移除)，失敗時提示並重新整理清單。
 */
(function () {
    'use strict';

    const { html } = Kiosk;
    const grid = document.getElementById('pickupCardsGrid');
    const noOrdersMessage = document.getElementById('noOrdersMessage');
    const countBadge = document.getElementById('readyCountBadge');
    const quickForm = document.getElementById('quickPickupForm');
    const quickInput = document.getElementById('quickSearchInput');

    let currentOrders = [];
    const processingIds = new Set(); // 核銷中的訂單，防止重複點擊與重新整理時閃回

    const pad = (n) => String(n).padStart(3, '0');

    function updateSummary() {
        countBadge.textContent = currentOrders.length;
        noOrdersMessage.classList.toggle('d-none', currentOrders.length > 0);
    }

    function orderCard(o) {
        const items = o.items.map((it) => html`
            <div class="d-flex justify-content-between small text-white-50 mb-1">
                <span class="text-white fw-bold pickup-item-name">${it.name}</span>
                <span>x ${Number(it.quantity) || 0}</span>
            </div>
            ${it.customization ? html`<div class="text-danger small mb-1">${it.customization}</div>` : ''}`);

        return html`
            <div class="col-12 col-md-6 col-lg-4 col-xl-3 pickup-col" data-order-id="${o.id}">
                <div class="pickup-card p-3 h-100">
                    <div>
                        <div class="d-flex justify-content-between align-items-center mb-2 pb-2 border-bottom border-secondary">
                            <div>
                                <h2 class="fw-bold text-success mb-0 pickup-number">#${pad(o.pickup_number)}</h2>
                                <small class="text-white-50">單號 #${o.id} • <span class="pickup-user">${o.user_name}</span></small>
                            </div>
                            <span class="badge bg-primary fs-6">${o.order_type}</span>
                        </div>
                        ${o.note ? html`<div class="small text-warning">備註：${o.note}</div>` : ''}
                        <div class="my-3 pickup-items">${items}</div>
                    </div>
                    <div class="pt-2 border-top border-secondary">
                        <button type="button" class="btn btn-pickup-finish w-100" data-action="mark-picked-up" data-id="${o.id}">
                            <i class="bi bi-check-circle-fill me-1"></i> 顧客已取餐
                        </button>
                    </div>
                </div>
            </div>`;
    }

    function render() {
        Kiosk.setHtml(grid, currentOrders.map(orderCard));
        updateSummary();
    }

    async function loadOrders() {
        try {
            const data = await Kiosk.api('/api/pickup_orders');
            currentOrders = (data.orders || []).filter((o) => !processingIds.has(o.id));
            render();
        } catch (err) {
            console.warn('獲取訂單失敗:', err.message);
        }
    }

    async function markAsPickedUp(orderId) {
        if (processingIds.has(orderId)) return;
        processingIds.add(orderId);

        // 樂觀更新：先淡出卡片
        const col = grid.querySelector(`[data-order-id="${orderId}"]`);
        if (col) {
            col.classList.add('is-leaving');
            setTimeout(() => col.remove(), 150);
        }
        currentOrders = currentOrders.filter((o) => o.id !== orderId);
        updateSummary();

        try {
            await Kiosk.api(`/api/orders/${orderId}/status`, { json: { status: 'PickedUp' } });
            setTimeout(() => processingIds.delete(orderId), 1000);
        } catch (err) {
            alert(`❌ ${err.message}`);
            processingIds.delete(orderId);
            loadOrders();
        }
    }

    quickForm.addEventListener('submit', (ev) => {
        ev.preventDefault();
        const target = Number(quickInput.value.trim());
        if (!target) return;
        const matched = currentOrders.find((o) => Number(o.pickup_number) === target);
        if (matched) {
            markAsPickedUp(matched.id);
            quickInput.value = '';
        } else {
            alert(`查無取餐號碼 #${pad(target)} 之待領取訂單！`);
        }
    });

    Kiosk.on(grid, 'click', {
        'mark-picked-up': (el) => markAsPickedUp(Number(el.dataset.id)),
    });

    loadOrders();
    Kiosk.onOrdersChanged(loadOrders);
})();
