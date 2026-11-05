/*
 * POS 作廢 / 部分退款 / 單品作廢、加急、桌位分開結帳與換桌 (ES module；對話框在 templates/_pos_pro_modals.html)。
 */
import { $, api, html, modalOf, num, posCfg, setHtml, state } from './core.js';
import { syncLiveState } from './live.js';
import { paymentDetails, withApproval } from './pro.js';

const cfg = posCfg;

// ======================================================================
// 作廢 / 部分退款
// ======================================================================
let refundOrder = null;

function openRefund(orderId, mode) {
    refundOrder = (state.liveOrders || []).find((o) => Number(o.id) === Number(orderId));
    if (!refundOrder) return;
    const unpaid = refundOrder.payment_status === 'unpaid';
    $('posRefundOrderId').textContent = refundOrder.id;
    $('posRefundMethod').textContent = refundOrder.payment_method;
    $('posRefundMax').textContent = num(refundOrder.refundable);
    $('posRefundAmount').value = '';
    $('posRefundReason').value = '';
    $('posRefundItemQty').value = 1;
    const items = (refundOrder.items || []).filter((it) => num(it.quantity) > 0 && it.id);
    setHtml($('posRefundItemSel'), items.map((it) => html`<option value="${it.id}" data-max="${num(it.quantity)}">${it.name} x${num(it.quantity)} ($${num(it.price)})</option>`));
    const tenders = Object.entries(refundOrder.tenders || {});
    setHtml($('posRefundTender'), tenders.map(([m, amt]) => html`<option value="${m}">退回：${m} (可退 $${num(amt)})</option>`));
    $('posRefundPartial').disabled = unpaid;
    $('posRefundVoid').disabled = unpaid;   // 桌邊未結帳的整單取消用訂單卡片上的「取消」
    (mode === 'item' || unpaid ? $('posRefundItem') : $('posRefundVoid')).checked = true;
    onRefundModeChange();
    modalOf('posRefundModal').show();
}

function onRefundModeChange() {
    const partial = $('posRefundPartial').checked;
    const item = $('posRefundItem').checked;
    $('posRefundAmount').classList.toggle('d-none', !partial);
    $('posRefundItemRow').classList.toggle('d-none', !item);
    const tenders = refundOrder && Object.keys(refundOrder.tenders || {}).length > 0;
    $('posRefundTender').classList.toggle('d-none', !(tenders && (partial || item)));
    $('posRefundModeLabel').textContent = partial ? '部分退款' : (item ? '作廢品項' : '作廢');
}
document.querySelectorAll('input[name="posRefundMode"]').forEach((r) => r.addEventListener('change', onRefundModeChange));

$('posRefundForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (!refundOrder) return;
    const partial = $('posRefundPartial').checked;
    const reason = $('posRefundReason').value.trim();
    const id = refundOrder.id;
    try {
        let message;
        const method = $('posRefundTender').classList.contains('d-none') ? '' : $('posRefundTender').value;
        if ($('posRefundItem').checked) {
            const body = { item_id: num($('posRefundItemSel').value), quantity: num($('posRefundItemQty').value), reason, method };
            const res = await withApproval((extra) => api(`/api/pos/orders/${id}/void_item`, { json: { ...body, ...extra } }));
            message = `訂單 #${id}：${res.message}`;
        } else if (partial) {
            const amount = num($('posRefundAmount').value);
            const res = await withApproval((extra) => api(`/api/pos/orders/${id}/refund`, { json: { amount, reason, method, ...extra } }));
            message = res.message;
        } else {
            await withApproval((extra) => api(`/api/orders/${id}/status`, { json: { status: 'Cancelled', reason, ...extra } }));
            message = `訂單 #${id} 已作廢 (庫存、點數已回補，已收款會建立退款紀錄)`;
        }
        modalOf('posRefundModal').hide();
        alert(`✅ ${message}`);
    } catch (err) {
        if (!err.cancelled) alert(`❌ ${err.message}`);
    }
    syncLiveState();
});

async function toggleRush(btn) {
    try {
        await api(`/api/pos/orders/${btn.dataset.id}/rush`, { json: { is_rush: btn.dataset.rush !== '1' } });
    } catch (err) {
        alert(`❌ ${err.message}`);
    }
    syncLiveState();
}

// ======================================================================
// 桌位分開結帳 / 換桌
// ======================================================================
let splitTable = null;
const PAY_OPTIONS = (cfg.methods || ['Cash']).filter((m) => ['Cash', 'Credit Card', 'Line Pay', '街口支付', 'Apple Pay'].includes(m));

function selectedOrders() {
    return [...document.querySelectorAll('.split-order:checked')].map((el) => Number(el.value));
}

function selectedTotal() {
    if (!splitTable) return 0;
    const ids = selectedOrders();
    return splitTable.orders.filter((o) => ids.includes(o.id)).reduce((s, o) => s + num(o.total_price), 0);
}

function paymentRowHtml(amount) {
    return html`
        <div class="row g-1 mb-1 split-pay-row">
            <div class="col-4"><select class="form-select form-select-sm split-method" aria-label="付款方式">
                ${PAY_OPTIONS.map((m) => html`<option value="${m}">${m}</option>`)}</select></div>
            <div class="col-3"><input type="number" class="form-control form-control-sm split-amount" min="1" value="${amount}" aria-label="金額"></div>
            <div class="col-3"><input type="number" class="form-control form-control-sm split-received" min="0" placeholder="現金實收" aria-label="實收"></div>
            <div class="col-2"><button type="button" class="btn btn-sm btn-outline-danger w-100" data-action="splitRemoveRow" aria-label="移除"><i class="bi bi-x"></i></button></div>
        </div>`;
}

function renderPayRows(amounts) {
    setHtml($('posSplitPayments'), amounts.map((a) => paymentRowHtml(a)));
    updateSplitRemain();
}

function updateSplitRemain() {
    const total = selectedTotal();
    $('posSplitTotal').textContent = total;
    let paid = 0;
    document.querySelectorAll('.split-pay-row').forEach((row) => { paid += num(row.querySelector('.split-amount').value); });
    const remain = total - paid;
    const el = $('posSplitRemain');
    el.className = `small mb-2 ${remain === 0 ? 'text-success' : 'text-danger'}`;
    el.textContent = remain === 0 ? '付款金額合計正確' : `付款合計 $${paid}，${remain > 0 ? `尚差 $${remain}` : `多了 $${-remain}`}`;
}

async function openSplit(tableId) {
    splitTable = (state.unpaidTables || []).find((t) => String(t.table_id) === String(tableId));
    if (!splitTable) return;
    $('posSplitTable').textContent = splitTable.name;
    $('posSplitMsg').textContent = '';
    setHtml($('posSplitOrders'), splitTable.orders.map((o) => html`
        <div class="form-check">
            <input class="form-check-input split-order" type="checkbox" value="${o.id}" id="split-o-${o.id}" checked>
            <label class="form-check-label small" for="split-o-${o.id}">
                #${Kiosk.pad3(o.pickup_number || o.id)} ${o.created_at} — $${num(o.total_price)}
                <span class="text-white-50">(${o.items.map((it) => `${it.name}x${it.quantity}`).join('、')})</span>
            </label>
        </div>`));
    renderPayRows([selectedTotal()]);
    try {
        const res = await api('/api/pos/tables');
        setHtml($('posMoveTarget'), res.tables.filter((t) => String(t.id) !== String(tableId))
            .map((t) => html`<option value="${t.id}">${t.name}</option>`));
    } catch (err) { /* 換桌清單載入失敗不影響結帳 */ }
    modalOf('posSplitModal').show();
}

function splitEqual(n) {
    const total = selectedTotal();
    const base = Math.floor(total / n);
    const amounts = Array.from({ length: n }, (_, i) => base + (i < total - base * n ? 1 : 0));
    renderPayRows(amounts);
}

$('posSplitOrders').addEventListener('change', () => {
    const rows = document.querySelectorAll('.split-pay-row');
    if (rows.length === 1) rows[0].querySelector('.split-amount').value = selectedTotal();
    updateSplitRemain();
});
$('posSplitPayments').addEventListener('input', updateSplitRemain);

$('posSplitForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (!splitTable) return;
    const orderIds = selectedOrders();
    if (!orderIds.length) {
        $('posSplitMsg').textContent = '請至少勾選一張訂單';
        return;
    }
    const payments = [];
    for (const row of document.querySelectorAll('.split-pay-row')) {
        const method = row.querySelector('.split-method').value;
        const amount = num(row.querySelector('.split-amount').value);
        const p = { method, amount, received: method === 'Cash' ? num(row.querySelector('.split-received').value) : 0 };
        const extra = await paymentDetails(method, amount);
        if (extra === null) return;
        payments.push({ ...p, ...extra });
    }
    const invType = $('posSplitInvoiceType') ? $('posSplitInvoiceType').value : '';
    const invValue = $('posSplitInvoiceValue') ? $('posSplitInvoiceValue').value.trim() : '';
    const invoice = invType ? { type: invType, carrier: invValue, buyer_id: invValue, love_code: invValue } : null;
    try {
        const res = await api(`/api/pos/tables/${splitTable.table_id}/settle`, { json: { order_ids: orderIds, payments, invoice } });
        modalOf('posSplitModal').hide();
        const detail = res.payments.map((p) => `${p.method} $${p.amount}${p.change ? ` (找零 $${p.change})` : ''}`).join('、');
        alert(`✅ ${res.table} 桌結帳 ${res.orders.length} 張單，共 $${res.total}：${detail}`);
    } catch (err) {
        $('posSplitMsg').textContent = err.message;
    }
    syncLiveState();
});

async function moveTable() {
    if (!splitTable) return;
    const target = $('posMoveTarget').value;
    if (!target) return;
    const orderIds = selectedOrders();
    try {
        const res = await api(`/api/pos/tables/${splitTable.table_id}/move`, { json: { to_table_id: Number(target), order_ids: orderIds } });
        modalOf('posSplitModal').hide();
        alert(`✅ 已將 ${res.moved.length} 張訂單從 ${res.from} 桌移到 ${res.to} 桌`);
    } catch (err) {
        $('posSplitMsg').textContent = err.message;
    }
    syncLiveState();
}

Kiosk.on(document, 'click', {
    openRefund: (el) => openRefund(el.dataset.id, el.dataset.mode),
    toggleRush: (el) => toggleRush(el),
    openSplit: (el) => openSplit(el.dataset.id),
    splitEqual: (el) => splitEqual(Number(el.dataset.n)),
    splitAddRow: () => {
        const holder = document.createElement('div');
        setHtml(holder, paymentRowHtml(0));
        $('posSplitPayments').appendChild(holder.firstElementChild);
        updateSplitRemain();
    },
    splitRemoveRow: (el) => {
        const row = el.closest('.split-pay-row');
        if (row && document.querySelectorAll('.split-pay-row').length > 1) row.remove();
        updateSplitRemain();
    },
    moveTable: () => moveTable(),
});
