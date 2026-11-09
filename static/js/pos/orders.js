/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：當日訂單的「到店付款」收款。
 * 線上點餐 (不收線上付款 ONLINE_PAYMENT=0) 與團體預購 (取餐時現場付款) 的訂單 pay_at_pickup=true：
 * 當日訂單列表顯示「到店付款・未收款」並提供「收款」按鈕，付款方式 / 實收 / 發票與桌位結帳相同，
 * 刷卡授權碼與 LINE Pay 付款碼沿用 pro.js 的 paymentDetails，送出 /api/pos/orders/<id>/settle。
 * 收款視窗由本模組動態建立 (不寫行內 script)，所有動態內容以 Kiosk.html (自動跳脫) 寫入。
 */
import { $, api, html, modalOf, num, posCfg, setHtml, state } from './core.js';
import { syncLiveState, TABLE_PAYMENTS } from './live.js';
import { paymentDetails } from './pro.js';
import { invoicePayload } from '../shared/invoice.js';

const INVOICE_TYPES = [['', '發票：預設'], ['print', '紙本證明聯'], ['mobile', '手機條碼'], ['company', '打統編'], ['donate', '捐贈']];
const MODAL_ID = 'posPickupPayModal';
let payOrder = null;      // 正在收款的訂單 (當日訂單列表的資料)
let submitting = false;   // 送出中：連點兩下不會收款兩次

/** 當日訂單卡片的付款狀態標籤：到店付款未收款 / 桌邊未結帳 */
export function unpaidBadge(o) {
    if (o.pay_at_pickup && o.payment_status === 'unpaid') {
        return html`<span class="badge bg-warning text-dark me-1 pos-pay-at-pickup">到店付款・未收款</span>`;
    }
    return o.payment_status === 'unpaid' ? html`<span class="badge bg-warning text-dark me-1">未結帳</span>` : '';
}

/** 到店付款且尚未收款、未取消的訂單：「收款」按鈕 */
export function pickupPayButton(o) {
    if (!o.pay_at_pickup || o.payment_status !== 'unpaid' || o.status === 'Cancelled') return '';
    return html`<button type="button" class="btn btn-sm btn-warning fw-bold text-dark" data-action="openPickupPay" data-id="${o.id}">
        <i class="bi bi-cash-coin me-1"></i>收款</button>`;
}

function ensureModal() {
    let el = $(MODAL_ID);
    if (el) return el;
    el = document.createElement('div');
    el.className = 'modal fade no-print pro-modal';
    el.id = MODAL_ID;
    el.tabIndex = -1;
    el.setAttribute('aria-hidden', 'true');
    el.setAttribute('aria-labelledby', 'posPickupPayTitle');
    setHtml(el, html`
        <div class="modal-dialog modal-dialog-centered">
            <form class="modal-content" id="posPickupPayForm" autocomplete="off">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title fw-bold text-warning" id="posPickupPayTitle"><i class="bi bi-cash-coin me-2"></i>到店付款收款</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="關閉"></button>
                </div>
                <div class="modal-body">
                    <div class="small text-white-50 mb-2" id="posPickupPayInfo"></div>
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="text-white-50">應收</span>
                        <span class="fw-bold text-warning fs-4">$<span id="posPickupPayTotal">0</span></span>
                    </div>
                    <div class="row g-1 mb-2">
                        <div class="col-6">
                            <select class="form-select form-select-sm" id="posPickupPayMethod" aria-label="付款方式">
                                ${TABLE_PAYMENTS.map(([v, label]) => html`<option value="${v}">${label}</option>`)}
                            </select>
                        </div>
                        <div class="col-6">
                            <input type="number" class="form-control form-control-sm" id="posPickupPayCash" min="0" placeholder="實收 (空白=剛好)" aria-label="實收現金">
                        </div>
                    </div>
                    ${posCfg.invoiceEnabled ? html`<div class="row g-1 mb-2">
                        <div class="col-6"><select class="form-select form-select-sm" id="posPickupPayInvType" aria-label="發票類型">${INVOICE_TYPES.map(([v, label]) => html`<option value="${v}">${label}</option>`)}</select></div>
                        <div class="col-6"><input type="text" class="form-control form-control-sm" id="posPickupPayInvValue" maxlength="16" autocomplete="off" placeholder="載具 / 統編 / 愛心碼" aria-label="載具 / 統編 / 愛心碼"></div>
                    </div>` : ''}
                    <div class="small text-info mb-1" id="posPickupPayChange"></div>
                    <div class="small text-danger" id="posPickupPayMsg"></div>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">取消</button>
                    <button type="submit" class="btn btn-warning fw-bold text-dark" id="posPickupPaySubmit">確認收款</button>
                </div>
            </form>
        </div>`);
    document.body.appendChild(el);
    el.querySelector('form').addEventListener('submit', (ev) => {
        ev.preventDefault();
        submitPickupPay();
    });
    el.addEventListener('input', updateChange);
    el.addEventListener('change', updateChange);
    return el;
}

function updateChange() {
    if (!payOrder) return;
    const isCash = $('posPickupPayMethod').value === 'Cash';
    const cashInput = $('posPickupPayCash');
    cashInput.disabled = !isCash;
    const received = num(cashInput.value);
    const total = num(payOrder.total_price);
    $('posPickupPayChange').textContent = isCash && received
        ? (received >= total ? `找零 $${received - total}` : '實收金額不足') : '';
}

function openPickupPay(orderId) {
    payOrder = (state.liveOrders || []).find((o) => Number(o.id) === Number(orderId));
    if (!payOrder || !payOrder.pay_at_pickup) return;
    ensureModal();
    $('posPickupPayInfo').textContent = `${Kiosk.pickupNo(payOrder)} (單號 #${payOrder.id}) • ${payOrder.user_name || ''}`;
    $('posPickupPayTotal').textContent = num(payOrder.total_price);
    $('posPickupPayCash').value = '';   // 不沿用上一張單的實收、統編 / 載具
    if ($('posPickupPayInvType')) $('posPickupPayInvType').value = '';
    if ($('posPickupPayInvValue')) $('posPickupPayInvValue').value = '';
    $('posPickupPayMsg').textContent = '';
    $('posPickupPaySubmit').disabled = false;
    updateChange();
    modalOf(MODAL_ID).show();
}

async function submitPickupPay() {
    if (!payOrder || submitting) return;
    const order = payOrder;
    const method = $('posPickupPayMethod').value;
    const cash = method === 'Cash' ? num($('posPickupPayCash').value) : 0;
    const expectedTotal = num(order.total_price);   // 畫面金額供後端核對 (改單後金額已變動時拒絕)
    const invType = $('posPickupPayInvType');
    const invoice = invType ? invoicePayload(invType.value, $('posPickupPayInvValue').value) : null;
    const extra = await paymentDetails(method, expectedTotal);
    if (extra === null) {
        $('posPickupPayMsg').textContent = '已取消付款資料輸入 (授權碼 / 付款碼)，尚未收款';
        return;
    }
    submitting = true;
    $('posPickupPaySubmit').disabled = true;
    try {
        const res = await api(`/api/pos/orders/${encodeURIComponent(order.id)}/settle`, {
            json: { payment_method: method, cash_received: cash, ...extra, expected_total: expectedTotal, invoice },
        });
        modalOf(MODAL_ID).hide();
        payOrder = null;
        const total = res.total !== undefined ? res.total : expectedTotal;
        alert(`✅ 訂單 #${order.id} 收款完成：應收 $${total}${res.change ? `，實收 $${res.cash_received}，找零 $${res.change}` : ''}`);
    } catch (err) {
        $('posPickupPayMsg').textContent = err.message;
        $('posPickupPaySubmit').disabled = false;
    } finally {
        submitting = false;
    }
    syncLiveState();
}

Kiosk.on(document, 'click', {
    openPickupPay: (el) => openPickupPay(el.dataset.id),
});
