/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：9~10. 掛單 / 取單、即時庫存與當日訂單、桌邊呼叫、桌位結帳與平面圖。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import {
    $, api, displayName, HOLD_STORAGE_KEY, html, itemMap, modalOf, num, pad3, posCfg, setHtml, state, storageGet,
    storageSet,
} from './core.js';
import { renderCart } from './cart.js';
import { bindMember, clearMember } from './member.js';
import { invoicePayload, onOrderTypeChange, resetPromo } from './totals.js';
import { showReceipt } from './checkout.js';

// ======================================================================
// 9. 掛單 / 取單
// ======================================================================
export const getHeld = () => {
    const list = storageGet(HOLD_STORAGE_KEY, []);
    return Array.isArray(list) ? list : [];
};

export function saveHeld(list) {
    storageSet(HOLD_STORAGE_KEY, list);
    $('holdCountBadge').textContent = list.length;
}

export function holdOrder() {
    if (state.cart.length === 0) {
        alert('⚠️ 目前沒有餐點可掛單！');
        return;
    }
    const list = getHeld();
    const table = ($('posTableInput').value || '').trim();
    list.push({
        id: Date.now(),
        label: table || (state.member ? state.member.name : `掛單 #${list.length + 1}`),
        time: new Date().toTimeString().split(' ')[0],
        cart: state.cart,
        member: state.member,
        table,
        orderType: $('posTypeTakeout').checked ? '外帶' : '內用',
        note: ($('posOrderNote').value || '').trim(),
        promoCode: state.promoCode,   // 取回時一律重新驗證，不沿用舊折抵金額
    });
    saveHeld(list);
    state.cart = [];
    resetPromo();
    clearMember();
    $('posTableInput').value = '';
    $('posOrderNote').value = '';
}

export function openHoldModal() {
    const list = getHeld();
    setHtml($('posHoldModalBody'), list.length === 0
        ? html`<div class="text-center text-white-50 py-4">目前沒有任何暫存掛單</div>`
        : list.map((h, idx) => html`
            <div class="p-3 rounded bg-dark border border-secondary mb-2 d-flex justify-content-between align-items-center">
                <div>
                    <div class="fw-bold text-warning">${h.label} <small class="text-white-50 ms-2">${h.time}</small></div>
                    <div class="small text-white-50">${(h.cart || []).map((l) => `${displayName(l)} x${num(l.quantity)}`).join('、')}</div>
                </div>
                <div class="d-flex gap-2">
                    <button type="button" class="btn btn-sm btn-warning fw-bold text-dark" data-action="recallHold" data-idx="${idx}">取回結帳</button>
                    <button type="button" class="btn btn-sm btn-outline-danger" data-action="removeHold" data-idx="${idx}" aria-label="刪除掛單"><i class="bi bi-trash"></i></button>
                </div>
            </div>`));
    modalOf('posHoldModal').show();
}

export function recallHold(idx) {
    const list = getHeld();
    const target = list[idx];
    if (!target) return;
    // 購物車還有餐點時拒絕取單，避免畫面上的餐點被覆蓋遺失
    if (state.cart.length > 0) {
        alert('⚠️ 目前購物車尚有餐點，請先結帳、掛單或清空後再取回掛單！');
        return;
    }
    resetPromo();
    state.cart = Array.isArray(target.cart) ? target.cart : [];
    if (target.member) bindMember(target.member);
    else clearMember();
    $('posTableInput').value = target.table || '';
    $('posOrderNote').value = target.note || '';
    $(target.orderType === '外帶' ? 'posTypeTakeout' : 'posTypeDineIn').checked = true;
    onOrderTypeChange();
    if (target.promoCode) {   // 先填入代碼，由 renderCart() 依目前小計與會員重新驗證
        state.promoCode = target.promoCode;
        $('posPromoInput').value = target.promoCode;
    }
    list.splice(idx, 1);
    saveHeld(list);
    renderCart();
    modalOf('posHoldModal').hide();
}

export function removeHold(idx) {
    const list = getHeld();
    list.splice(idx, 1);
    saveHeld(list);
    openHoldModal();
}

// ======================================================================
// 10. 即時庫存與當日訂單 (SSE 通知後重新抓取)
// ======================================================================
export const STATUS_TEXT = { Pending: '製作中', Completed: '已出餐', PickedUp: '已取餐', Cancelled: '已取消' };
export const STATUS_BADGES = {
    Pending: html`<span class="badge bg-warning text-dark">⏳ 製作中</span>`,
    Completed: html`<span class="badge bg-success">✅ 待取餐</span>`,
    PickedUp: html`<span class="badge bg-secondary">📦 已取餐</span>`,
    Cancelled: html`<span class="badge bg-danger">❌ 已取消</span>`,
};

export function applyStock(menuItems) {
    menuItems.forEach((m) => {
        const it = itemMap.get(Number(m.id));
        const soldOut = Boolean(m.is_sold_out) || num(m.stock) <= 0;
        if (it) {
            it.stock = m.stock;
            it.is_available = !soldOut;
        }
        const card = $(`pos-card-${m.id}`);
        if (card) {
            card.dataset.soldout = soldOut ? '1' : '0';
            card.classList.toggle('pos-item-soldout', soldOut);
        }
        const badge = $(`pos-stock-badge-${m.id}`);
        if (badge) {
            badge.textContent = soldOut ? '已售完' : `庫存 ${m.stock}`;
            badge.className = `badge pos-xxs ${soldOut ? 'bg-danger' : (num(m.stock) <= 5 ? 'bg-warning text-dark' : 'bg-dark border border-secondary text-white-50')}`;
        }
    });
}

export async function syncLiveState() {
    let res;
    try {
        res = await api('/api/pos/today_orders');
    } catch (err) {
        return;   // 同步失敗略過，下一次通知會再抓
    }
    if (Array.isArray(res.menu_items)) {
        applyStock(res.menu_items);
        if (state.cart.length) renderCart({ revalidate: false });   // 加購品售完時重新整理推薦列
    }
    if (Array.isArray(res.orders)) {
        state.liveOrders = res.orders;
        $('topTodayOrderCount').textContent = res.orders.filter((o) => o.status !== 'Cancelled').length;
        renderLiveOrders();
    }
    if (Array.isArray(res.service_calls)) renderServiceCalls(res.service_calls);
    if (Array.isArray(res.unpaid_tables)) {
        state.unpaidTables = res.unpaid_tables;
        if ($('topUnpaidTableCount')) $('topUnpaidTableCount').textContent = res.unpaid_tables.length;
        renderTables();
        if (state.tablesView === 'floor' && $('posTablesModal').classList.contains('show')) loadFloor();
    }
}

// ---------------------------------------------------------------- 桌邊呼叫服務
export let knownCallIds = new Set();

export function callBeep() {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        osc.frequency.value = 880;
        osc.connect(ctx.destination);
        osc.start();
        setTimeout(() => { osc.stop(); ctx.close(); }, 250);
    } catch (err) { /* 瀏覽器不允許自動播放聲音時略過 */ }
}

export function renderServiceCalls(calls) {
    const btn = $('posCallsBtn');
    if (!btn) return;
    $('posCallsCount').textContent = calls.length;
    btn.classList.toggle('btn-danger', calls.length > 0);
    btn.classList.toggle('btn-outline-secondary', calls.length === 0);
    const fresh = calls.some((c) => !knownCallIds.has(c.id));
    knownCallIds = new Set(calls.map((c) => c.id));
    if (fresh) callBeep();
    setHtml($('posCallsList'), calls.length ? calls.map((c) => html`
        <div class="d-flex align-items-center justify-content-between border-bottom border-secondary py-2 pos-call-row" data-call-id="${c.id}">
            <div>
                <span class="badge bg-info text-dark fs-6 me-2">${c.table} 桌</span>
                <span class="fw-bold">${c.label}</span>
                ${c.note ? html`<div class="small text-white-50">${c.note}</div>` : ''}
                <div class="small ${c.minutes >= 5 ? 'text-danger' : 'text-white-50'}">${c.at} (${c.minutes} 分鐘前)</div>
            </div>
            <button type="button" class="btn btn-sm btn-success" data-action="serviceCallDone" data-id="${c.id}">已處理</button>
        </div>`) : html`<div class="text-center text-white-50 py-4">目前沒有呼叫</div>`);
}

export async function serviceCallDone(id) {
    try {
        const res = await api(`/api/pos/service_calls/${id}/done`, { method: 'POST' });
        renderServiceCalls(res.service_calls || []);
    } catch (err) {
        alert(`❌ ${err.message}`);
    }
}

// ---------------------------------------------------------------- 桌位結帳 (桌邊掃碼點餐)
export const PAY_LABELS = { Cash: '現金', 'Credit Card': '信用卡', 'Line Pay': 'LINE Pay', 街口支付: '街口支付', 'Apple Pay': 'Apple Pay' };
export const TABLE_PAYMENTS = (posCfg.methods || ['Cash']).filter((m) => PAY_LABELS[m]).map((m) => [m, PAY_LABELS[m]]);

export function openTables() {
    syncLiveState();
    modalOf('posTablesModal').show();
    if (state.tablesView === 'floor') loadFloor();
}

// 平面圖：每桌狀態 (空桌 / 用餐中 / 呼叫中 / 即將訂位)；點用餐中的桌 → 切回清單並捲到那一桌
export async function loadFloor() {
    try {
        const res = await api('/api/pos/floor');
        state.floor = res;
        const sel = $('posFloorArea');
        const current = sel.value;
        setHtml(sel, [html`<option value="">全部區域</option>`, (res.areas || []).map((a) => html`<option value="${a}">${a}</option>`)]);
        sel.value = (res.areas || []).includes(current) ? current : '';
        drawFloor();
    } catch (err) {
        setHtml($('posFloorPlan'), html`<div class="text-danger small">${err.message}</div>`);
    }
}

export function drawFloor() {
    if (!state.floor || !window.FloorPlan) return;
    window.FloorPlan.render($('posFloorPlan'), state.floor.tables || [], {
        area: $('posFloorArea').value,
        onPick: (id) => {
            const t = (state.floor.tables || []).find((x) => x.id === id);
            if (!t || !t.orders) return;
            setTablesView('list');
            const card = document.querySelector(`#posTablesGrid [data-table-id="${id}"]`);
            if (card) card.scrollIntoView({ block: 'center' });
        },
    });
}

export function setTablesView(view) {
    state.tablesView = view;
    $('posFloorBox').classList.toggle('d-none', view !== 'floor');
    $('posTablesGrid').classList.toggle('d-none', view === 'floor');
    $('posTablesListBtn').classList.toggle('active', view !== 'floor');
    $('posTablesFloorBtn').classList.toggle('active', view === 'floor');
    if (view === 'floor') loadFloor();
}

export function renderTables() {
    const grid = $('posTablesGrid');
    if (!grid) return;
    const tables = state.unpaidTables || [];
    if (tables.length === 0) {
        setHtml(grid, html`<div class="col-12 text-center text-white-50 py-5">目前沒有未結帳的桌位</div>`);
        return;
    }
    setHtml(grid, tables.map((t) => html`
        <div class="col-md-6 col-lg-4">
            <div class="p-3 rounded bg-dark border border-info h-100 d-flex flex-column justify-content-between pos-table-card" data-table-id="${t.table_id}">
                <div>
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <h5 class="fw-bold text-info mb-0">${t.name} 桌</h5>
                        <span class="badge bg-warning text-dark">${t.orders.length} 張未結帳</span>
                    </div>
                    <div class="border-top border-bottom border-secondary py-2 mb-2 pos-live-items">
                        ${t.orders.map((o) => html`
                            <div class="small text-white-50 mt-1">取餐 #${pad3(o.pickup_number || o.id)} • ${o.created_at} • ${STATUS_TEXT[o.status] || o.status}</div>
                            ${o.items.map((it) => html`
                                <div class="d-flex justify-content-between small">
                                    <span class="text-white">${it.name} x${num(it.quantity)}</span>
                                    <span class="text-white-50">$${Math.round(num(it.price) * num(it.quantity))}</span>
                                </div>`)}`)}
                    </div>
                </div>
                <div>
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="text-white-50">應收</span>
                        <span class="fw-bold text-warning fs-4">$${num(t.total)}</span>
                    </div>
                    <div class="row g-1 mb-2">
                        <div class="col-6">
                            <select class="form-select form-select-sm table-pay-method" aria-label="付款方式">
                                ${TABLE_PAYMENTS.map(([v, label]) => html`<option value="${v}">${label}</option>`)}
                            </select>
                        </div>
                        <div class="col-6">
                            <input type="number" class="form-control form-control-sm table-cash" min="0" placeholder="實收 (空白=剛好)" aria-label="實收現金">
                        </div>
                    </div>
                    <div class="small text-info mb-2 table-change"></div>
                    <div class="d-flex gap-1">
                        <button type="button" class="btn btn-info fw-bold text-dark flex-grow-1" data-action="settleTable" data-id="${t.table_id}">
                            <i class="bi bi-cash-coin me-1"></i>整桌結帳
                        </button>
                        <button type="button" class="btn btn-outline-info fw-bold" data-action="openSplit" data-id="${t.table_id}" title="分開結帳 / 換桌">分開 / 換桌</button>
                    </div>
                </div>
            </div>
        </div>`));
}

export function tableCardChanged(ev) {
    const card = ev.target.closest('.pos-table-card');
    if (!card) return;
    const t = (state.unpaidTables || []).find((x) => String(x.table_id) === card.dataset.tableId);
    const isCash = card.querySelector('.table-pay-method').value === 'Cash';
    const cashInput = card.querySelector('.table-cash');
    cashInput.disabled = !isCash;
    const received = num(cashInput.value);
    card.querySelector('.table-change').textContent = isCash && received
        ? (received >= num(t && t.total) ? `找零 $${received - num(t && t.total)}` : '實收金額不足') : '';
}

export async function settleTable(btn) {
    const card = btn.closest('.pos-table-card');
    const method = card.querySelector('.table-pay-method').value;
    const cash = method === 'Cash' ? num(card.querySelector('.table-cash').value) : 0;
    const t = (state.unpaidTables || []).find((x) => String(x.table_id) === String(btn.dataset.id));
    const extra = await window.PosPro.paymentDetails(method, t ? num(t.total) : 0);
    if (extra === null) return;
    btn.disabled = true;
    try {
        const res = await api(`/api/pos/tables/${encodeURIComponent(btn.dataset.id)}/settle`,
            { json: { payment_method: method, cash_received: cash, ...extra, invoice: invoicePayload() } });
        alert(`✅ ${res.table} 桌結帳完成：應收 $${res.total}${res.change ? `，實收 $${res.cash_received}，找零 $${res.change}` : ''}`);
    } catch (err) {
        alert(`❌ ${err.message}`);
        btn.disabled = false;
    }
    syncLiveState();
}

export function openLiveOrders() {
    syncLiveState();
    modalOf('posLiveOrdersModal').show();
}

export function filterLiveOrders(btn) {
    state.liveFilter = btn.dataset.status;
    document.querySelectorAll('#posLiveFilterGroup .btn').forEach((b) => b.classList.toggle('active', b === btn));
    renderLiveOrders();
}

export function renderLiveOrders() {
    const grid = $('posLiveOrdersGrid');
    const list = state.liveFilter === 'all' ? state.liveOrders : state.liveOrders.filter((o) => o.status === state.liveFilter);
    if (list.length === 0) {
        setHtml(grid, html`<div class="col-12 text-center text-white-50 py-5">目前沒有符合篩選條件的當日訂單</div>`);
        return;
    }
    setHtml(grid, list.map((o) => html`
        <div class="col-md-6 col-lg-4">
            <div class="p-3 rounded bg-dark border border-secondary h-100 d-flex flex-column justify-content-between pos-live-order" data-order-id="${o.id}">
                <div>
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <h5 class="fw-bold text-warning mb-0">${o.pickup_pending ? '待分店確認' : `#${pad3(o.pickup_number || o.id)}`} <small class="text-white-50 fs-6">(單號 #${o.id})</small></h5>
                        <span>
                            ${o.pickup_at ? html`<span class="badge bg-info text-dark me-1 pos-pickup-at">預約 ${o.pickup_at}</span>` : ''}
                            ${o.delivery_status ? html`<span class="badge bg-warning text-dark me-1 pos-home-delivery">外送：${o.delivery_status_label}</span>` : ''}
                            ${o.payment_status === 'unpaid' ? html`<span class="badge bg-warning text-dark me-1">未結帳</span>` : ''}
                            ${STATUS_BADGES[o.status] || STATUS_BADGES.Pending}
                        </span>
                    </div>
                    <div class="small text-white-50 mb-2 d-flex justify-content-between">
                        <span><i class="bi bi-person me-1"></i>${o.user_name} • ${o.order_type}${o.pickup_node_name ? ` • ${o.pickup_node_name}` : ''}</span>
                        <span>${o.created_at}</span>
                    </div>
                    <div class="border-top border-bottom border-secondary py-2 mb-2 pos-live-items">
                        ${(o.items || []).map((it) => html`
                            <div class="d-flex justify-content-between small">
                                <span class="text-white">${it.name || it.item_name} x${num(it.quantity)}${num(it.voided_qty) ? html` <span class="badge bg-danger pos-voided">作廢 ${num(it.voided_qty)}</span>` : ''}</span>
                                <span class="text-white-50">$${Math.round(num(it.price) * num(it.quantity))}</span>
                            </div>
                            ${it.customization ? html`<div class="text-danger pos-cart-custom">${it.customization}</div>` : ''}`)}
                    </div>
                </div>
                <div class="d-flex justify-content-between align-items-center pt-1">
                    <div>
                        <span class="fw-bold text-white fs-5">$${Math.round(num(o.total_price))}</span>
                        <small class="text-white-50 ms-1">(${o.payment_method})</small>
                        ${num(o.refunded_amount) > 0 ? html`<span class="badge bg-danger ms-1">已退 $${num(o.refunded_amount)}</span>` : ''}
                        ${o.external_ref ? html`<span class="badge bg-warning text-dark ms-1">${o.external_ref}</span>` : ''}
                    </div>
                    <div class="d-flex flex-wrap gap-1 justify-content-end">
                        <button type="button" class="btn btn-sm btn-outline-light" data-action="reprint" data-id="${o.id}" title="補印收據" aria-label="補印收據">
                            <i class="bi bi-printer"></i>
                        </button>
                        ${o.invoice && o.invoice.invoice_no ? html`<a class="btn btn-sm btn-outline-light" href="/admin/invoices/${o.invoice.id}/print" target="_blank" rel="noopener" title="發票 ${o.invoice.invoice_no}" aria-label="列印發票"><i class="bi bi-receipt"></i></a>` : ''}
                        ${o.status === 'Pending' ? html`
                            <button type="button" class="btn btn-sm ${o.is_rush ? 'btn-danger' : 'btn-outline-danger'}" data-action="toggleRush" data-id="${o.id}" data-rush="${o.is_rush ? 1 : 0}" title="加急" aria-label="加急"><i class="bi bi-lightning-charge"></i></button>
                            <button type="button" class="btn btn-sm btn-success fw-bold" data-action="orderStatus" data-id="${o.id}" data-status="Completed">出餐</button>` : ''}
                        ${o.status === 'Completed' ? html`
                            <button type="button" class="btn btn-sm btn-info fw-bold text-dark" data-action="orderStatus" data-id="${o.id}" data-status="PickedUp">核銷取餐</button>` : ''}
                        ${o.status !== 'Cancelled' && o.payment_status === 'unpaid' && o.status !== 'PickedUp' ? html`
                            <button type="button" class="btn btn-sm btn-outline-danger" data-action="orderStatus" data-id="${o.id}" data-status="Cancelled">取消</button>` : ''}
                        ${o.status !== 'Cancelled' && o.payment_status !== 'unpaid' ? html`
                            <button type="button" class="btn btn-sm btn-outline-danger" data-action="openRefund" data-id="${o.id}">作廢/退款</button>` : ''}
                        ${o.status !== 'Cancelled' && o.payment_status === 'unpaid' ? html`
                            <button type="button" class="btn btn-sm btn-outline-info" data-action="openRefund" data-mode="item" data-id="${o.id}">改單</button>` : ''}
                    </div>
                </div>
            </div>
        </div>`));
}

export async function updateOrderStatus(orderId, status) {
    if (status === 'Cancelled' && !confirm(`確定要取消訂單 #${orderId} 並全額回滾庫存與紅利點數嗎？`)) return;
    try {
        await api(`/api/orders/${encodeURIComponent(orderId)}/status`, { json: { status } });
    } catch (err) {
        alert(`❌ ${err.message}`);
    }
    syncLiveState();
}

export function reprint(orderId) {
    const o = state.liveOrders.find((x) => Number(x.id) === Number(orderId));
    if (!o) return;
    showReceipt({
        order_id: o.id,
        pickup_number: o.pickup_number || o.id,
        table_number: o.user_name,
        order_type: o.order_type,
        need_cutlery: o.need_cutlery,
        note: o.note,
        created_at: o.created_at,
        cashier: o.cashier_username || '補印收據',
        subtotal: num(o.total_price) + num(o.discount_amount),
        discount_amount: o.discount_amount,
        total_price: o.total_price,
        payment_method: o.payment_method,
        cash_received: o.cash_received,
        cash_change: o.cash_change,
        items: o.items,
    }, '補印收據 • 收據預覽');
}

export function startLiveSync() {
    const dot = $('sse-status-dot');
    const setDot = (ok) => {
        dot.className = ok ? 'text-success' : 'text-warning';
        dot.textContent = ok ? '● 即時連線中' : '● 輪詢同步中';
    };
    if (!window.EventSource) {
        setDot(false);
        setInterval(syncLiveState, 5000);
        return;
    }
    const source = Kiosk.onOrdersChanged(syncLiveState);
    source.addEventListener('open', () => setDot(true));
    source.addEventListener('error', () => setDot(false));
}
