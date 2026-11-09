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
let refundSubmitting = false;   // 送出中：連點兩下「確認」不會退款 / 作廢兩次
let refundRequestId = '';       // 每次開窗一個 request_id，逾時重送時伺服器可辨識為同一筆

function openRefund(orderId, mode) {
    refundOrder = (state.liveOrders || []).find((o) => Number(o.id) === Number(orderId));
    if (!refundOrder) return;
    refundRequestId = Kiosk.newOrderId();
    const unpaid = refundOrder.payment_status === 'unpaid';
    // 已取餐的訂單：角色不能作廢時 (後端直接回 403、不走主管核准) 停用「作廢整單」，改用部分退款
    const noVoid = refundOrder.status === 'PickedUp' && cfg.canVoidPickedUp === false;
    const voidLabel = document.querySelector('label[for="posRefundVoid"]');
    if (voidLabel) voidLabel.title = noVoid ? '已取餐的訂單需由店長 / 負責人作廢；可改用部分退款 (主管核准)' : '';
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
    $('posRefundVoid').disabled = unpaid || noVoid;   // 桌邊未結帳的整單取消用訂單卡片上的「取消」
    (mode === 'item' || unpaid ? $('posRefundItem') : $(noVoid ? 'posRefundPartial' : 'posRefundVoid')).checked = true;
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
    if (!refundOrder || refundSubmitting) return;
    const partial = $('posRefundPartial').checked;
    const reason = $('posRefundReason').value.trim();
    const id = refundOrder.id;
    const request_id = refundRequestId;
    const submitBtn = $('posRefundForm').querySelector('button[type="submit"]');
    refundSubmitting = true;
    if (submitBtn) submitBtn.disabled = true;
    try {
        let message;
        const method = $('posRefundTender').classList.contains('d-none') ? '' : $('posRefundTender').value;
        if ($('posRefundItem').checked) {
            const body = { item_id: num($('posRefundItemSel').value), quantity: num($('posRefundItemQty').value), reason, method, request_id };
            const res = await withApproval((extra) => api(`/api/pos/orders/${id}/void_item`, { json: { ...body, ...extra } }));
            message = `訂單 #${id}：${res.message}`;
        } else if (partial) {
            const amount = num($('posRefundAmount').value);
            const res = await withApproval((extra) => api(`/api/pos/orders/${id}/refund`, { json: { amount, reason, method, request_id, ...extra } }));
            message = res.message;
        } else {
            await withApproval((extra) => api(`/api/orders/${id}/status`, { json: { status: 'Cancelled', reason, ...extra } }));
            message = `訂單 #${id} 已作廢 (庫存、點數已回補，已收款會建立退款紀錄)`;
        }
        modalOf('posRefundModal').hide();
        alert(`✅ ${message}`);
    } catch (err) {
        const pickedUp = err.status === 403 && !(err.data && err.data.need_approval) && $('posRefundVoid').checked
            && refundOrder && refundOrder.status === 'PickedUp';
        if (!err.cancelled) alert(`❌ ${err.message}${pickedUp ? '\n已取餐的訂單可改用「部分退款」(經主管核准)' : ''}`);
    } finally {
        refundSubmitting = false;
        if (submitBtn) submitBtn.disabled = false;
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
    if ($('posSplitInvoiceType')) $('posSplitInvoiceType').value = '';   // 不沿用上一桌的統編 / 載具
    if ($('posSplitInvoiceValue')) $('posSplitInvoiceValue').value = '';
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

/** LINE Pay 付款碼視窗完全關閉 (淡出動畫結束) 後才 resolve；Bootstrap 在淡出期間會忽略再次 show() */
function linePayModalHidden() {
    const el = $('posLinePayModal');
    if (!el || !el.style.display || el.style.display === 'none') return Promise.resolve();
    return new Promise((resolve) => el.addEventListener('hidden.bs.modal', () => resolve(), { once: true }));
}

/**
 * 桌位結帳 (整桌結帳 live.js 與分開結帳共用)：顧客正在手機線上結帳 (綠界付款中) 時後端回 409 online_pay_pending，
 * 店員按確定後帶 force 重送 (這個 409 在 LINE Pay 扣款之前，授權碼 / 付款碼可原樣重送)；按取消時錯誤帶 cancelled。
 */
export async function settleTableApi(tableId, json) {
    const url = `/api/pos/tables/${encodeURIComponent(tableId)}/settle`;
    try {
        return await api(url, { json });
    } catch (err) {
        if (!(err.data && err.data.online_pay_pending)) throw err;
        if (!confirm(err.message)) {
            err.cancelled = true;
            throw err;
        }
        return api(url, { json: { ...json, force: true } });
    }
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
        await linePayModalHidden();   // 上一位的付款碼視窗還在淡出時開不了下一個視窗，先等它關好
        const extra = await paymentDetails(method, amount);
        if (extra === null) {
            $('posSplitMsg').textContent = '已取消付款資料輸入 (授權碼 / 付款碼)，尚未結帳';
            return;
        }
        payments.push({ ...p, ...extra });
    }
    const invType = $('posSplitInvoiceType') ? $('posSplitInvoiceType').value : '';
    const invValue = $('posSplitInvoiceValue') ? $('posSplitInvoiceValue').value.trim() : '';
    const invoice = invType ? { type: invType, carrier: invValue, buyer_id: invValue, love_code: invValue } : null;
    try {
        const res = await settleTableApi(splitTable.table_id, { order_ids: orderIds, payments, invoice });
        modalOf('posSplitModal').hide();
        const detail = res.payments.map((p) => `${p.method} $${p.amount}${p.change ? ` (找零 $${p.change})` : ''}`).join('、');
        alert(`✅ ${res.table} 桌結帳 ${res.orders.length} 張單，共 $${res.total}：${detail}`);
    } catch (err) {
        $('posSplitMsg').textContent = err.cancelled ? '已取消，尚未結帳' : err.message;
    }
    syncLiveState();
});

async function moveTable() {
    if (!splitTable) return;
    const target = $('posMoveTarget').value;
    if (!target) return;
    const orderIds = selectedOrders();
    if (!orderIds.length) {   // 送出空清單時後端會把整桌都移走，與畫面上的選擇相反
        $('posSplitMsg').textContent = '請至少勾選一張訂單 (要整桌移動請全選)';
        return;
    }
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
