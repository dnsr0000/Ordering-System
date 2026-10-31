/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：8. 結帳送出、混合付款、外送單、改價 / 整單折扣、收據。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import { $, api, html, modalOf, num, pad3, posCfg, setHtml, state, SUBMIT_LABEL } from './core.js';
import { repriceCart } from './menu.js';
import { renderCart } from './cart.js';
import { applyServerPrices, clearMember, refreshBoundMember } from './member.js';
import {
    broadcastPaid, invoicePayload, isHomeDelivery, onInvoiceTypeChange, onOrderTypeChange, recalcTotals, resetPromo,
    updateCashChange,
} from './totals.js';
import { syncLiveState } from './live.js';

// ======================================================================
// 8. 結帳送出與收據
// ======================================================================
export function payloadLine(l) {
    return {
        menu_item_id: l.menu_item_id,
        quantity: l.quantity,
        modifier_ids: l.modifier_ids,
        combo_option_id: l.combo_option_id,
        combo_choices: l.combo_choices || [],
        pass_id: l.pass_id || 0,
        is_add_on: l.is_add_on,
        is_reward: l.is_reward,
        customization: l.customization,
        size: l.size || '',
        override_price: l.override_price,
    };
}

export function setSubmitting(on) {
    state.submitting = on;
    const btn = $('posSubmitBtn');
    btn.disabled = on;
    setHtml(btn, on ? html`<span class="spinner-border spinner-border-sm me-2"></span>結帳處理中...` : SUBMIT_LABEL);
}

export function resetTerminal() {
    state.cart = [];
    resetPromo();          // 先重置優惠券，下一位顧客才不會繼承上一單的折扣
    clearMember();         // 內含 renderCart()
    $('posTableInput').value = '';
    $('posOrderNote').value = '';
    $('posCashReceived').value = '';
    state.orderDiscount = null;
    ['posCardAuth', 'posCardLast4', 'posInvoiceValue', 'posDeliveryRef', 'posHomeAddress', 'posHomePhone']
        .forEach((id) => { if ($(id)) $(id).value = ''; });
    if (isHomeDelivery()) { $('posTypeDineIn').checked = true; onOrderTypeChange(); }
    if ($('posInvoiceType')) { $('posInvoiceType').value = ''; onInvoiceTypeChange(); }
    if ($('posRush')) $('posRush').checked = false;
    updateCashChange();
}

export async function submitOrder() {
    if (state.submitting) return;
    if (state.cart.length === 0) {
        alert('⚠️ 收銀點餐清單為空，請先點選餐點！');
        return;
    }
    if (state.deliveryMode) {
        submitDeliveryOrder();
        return;
    }
    const payMethod = $('posPaymentMethod').value;
    // 上一次送出時伺服器回報金額不同、收銀已看過新金額：同一份內容再送出時以新金額為準
    const cartKey = JSON.stringify([state.cart.map(payloadLine), state.member && state.member.id, state.promoCode,
        state.orderDiscount, $('posPointsInput') && $('posPointsInput').value, payMethod,
        $('posTypeTakeout').checked, isHomeDelivery() && $('posHomeZone').value]);
    const finalTotal = state.acceptedTotal && state.acceptedTotal.key === cartKey ? state.acceptedTotal.total : state.finalTotal;
    if (payMethod === '混合付款' && !state.tenders) {   // 先分配各種付款方式的金額，確認後再送出
        openTenders(finalTotal);
        return;
    }
    const received = num($('posCashReceived').value);
    if (payMethod === 'Cash' && received > 0 && received < finalTotal) {
        alert(`⚠️ 實收現金 ($${received}) 小於應收總額 ($${finalTotal})！`);
        return;
    }
    if (payMethod === '儲值金') {
        if (!state.member) {
            alert('⚠️ 使用儲值金付款請先綁定會員！');
            return;
        }
        if (num(state.member.wallet_balance) < finalTotal) {
            alert(`⚠️ 儲值金餘額 $${num(state.member.wallet_balance)} 不足 (應付 $${finalTotal})，請先儲值或改用其他付款方式！`);
            return;
        }
    }
    const orderType = isHomeDelivery() ? '外送到府' : ($('posTypeTakeout').checked ? '外帶' : '內用');
    const toggle = $('posUsePointsToggle');
    const usePoints = state.member && toggle && toggle.checked ? num($('posPointsInput').value) : 0;
    const payload = {
        items: state.cart.map(payloadLine),
        user_id: state.member ? state.member.id : 0,
        table_number: ($('posTableInput').value || '').trim(),
        order_type: orderType,
        payment_method: payMethod,
        need_cutlery: orderType === '外帶' ? $('posNeedCutlery').checked : false,
        note: ($('posOrderNote').value || '').trim(),
        promo_code: state.promoCode,
        use_points: usePoints,
        cash_received: payMethod === '混合付款'
            ? ((state.tenders || []).find((t) => t.method === 'Cash') || {}).received || 0
            : (received > 0 ? received : finalTotal),
        expected_total: finalTotal,
        order_discount: state.orderDiscount,
        is_rush: Boolean($('posRush') && $('posRush').checked),
        invoice: invoicePayload(),
        delivery: isHomeDelivery() ? { address: ($('posHomeAddress').value || '').trim(),
                                      phone: ($('posHomePhone').value || '').trim(), zone: $('posHomeZone').value } : null,
        payment_info: payMethod === 'Credit Card'
            ? { auth_code: ($('posCardAuth').value || '').trim(), card_last4: ($('posCardLast4').value || '').trim() } : {},
    };
    if (payMethod === '混合付款') payload.payment_info = { tenders: state.tenders };
    if (payMethod === 'Credit Card' && posCfg.cardRequireAuth && !payload.payment_info.auth_code) {
        alert('⚠️ 請輸入刷卡機簽單上的授權碼！');
        $('posCardAuth').focus();
        return;
    }
    if (payMethod === '禮物卡') {
        const code = window.prompt(`禮物卡扣款 $${finalTotal}：請刷卡或輸入 16 碼卡號`, '');
        if (!code) return;
        payload.payment_info = { gift_code: code.trim() };
    }
    // 同一份結帳內容重送 (例如連線逾時後再按一次) 沿用同一個 client_order_id，內容改變才換新的
    const sig = JSON.stringify(payload);
    if (!state.checkout || state.checkout.sig !== sig) state.checkout = { id: Kiosk.newOrderId(), sig };

    if (payMethod === 'Line Pay' && posCfg.linePayIntegrated) {
        const key = await window.PosPro.askLinePayKey(finalTotal);
        if (!key) return;
        payload.payment_info = { one_time_key: key };
    }
    setSubmitting(true);
    try {
        // 主管核准 (改價、折扣…) 與會員身分驗證 (儲值金 / 點數 / 寄杯) 都可能要求補資料後重送
        const res = await window.PosPro.withApproval((extra) => window.PosPro.withMemberVerify(
            (verify) => api('/api/pos/checkout', { json: { ...payload, ...extra, ...verify, client_order_id: state.checkout.id } })));
        state.checkout = null;
        if (state.member && res.wallet_balance !== undefined) state.member.wallet_balance = res.wallet_balance;
        showReceipt(res, res.replayed ? '此筆訂單先前已結帳 • 收據預覽' : '結帳成功 • 收據預覽');
        broadcastPaid(res);
        resetTerminal();
    } catch (err) {
        if (err.data && err.data.payment_failed) state.checkout = null;   // 付款失敗的訂單已取消，下次換新的單號
        if (!err.cancelled) alert(`❌ ${err.message}`);
        if (err.data && (err.data.coupon_unavailable || err.data.points_changed)) {
            await refreshBoundMember(Boolean(err.data.coupon_unavailable));
        }
        if (err.data && err.data.price_changed) applyServerPrices(err.data, cartKey);
    } finally {
        state.tenders = null;   // 混合付款每次結帳重新分配
        setSubmitting(false);
        syncLiveState();
    }
}

// ---------------------------------------------------------------- 混合付款
export const TENDER_METHODS = () => [...$('posPaymentMethod').options].map((o) => o.value)
    .filter((m) => m !== '混合付款' && !(m === 'Line Pay' && posCfg.linePayIntegrated));

export function tenderRowHtml(method, amount) {
    return html`<div class="row g-1 mb-1 pos-tender-row">
        <div class="col-4"><select class="form-select form-select-sm tender-method" aria-label="付款方式">
            ${TENDER_METHODS().map((m) => html`<option value="${m}" ${m === method ? 'selected' : ''}>${m}</option>`)}</select></div>
        <div class="col-3"><input type="number" class="form-control form-control-sm tender-amount" min="1" value="${amount}" aria-label="金額"></div>
        <div class="col-4"><input type="text" class="form-control form-control-sm tender-extra" aria-label="實收 / 卡號 / 授權碼"></div>
        <div class="col-1"><button type="button" class="btn btn-sm btn-outline-secondary" data-action="removeTender" aria-label="移除"><i class="bi bi-x"></i></button></div>
    </div>`;
}

export function syncTenderRows() {
    const total = num($('posTenderTotal').textContent);
    let sum = 0;
    document.querySelectorAll('#posTenderRows .pos-tender-row').forEach((row) => {
        sum += num(row.querySelector('.tender-amount').value);
        const m = row.querySelector('.tender-method').value;
        const extra = row.querySelector('.tender-extra');
        extra.placeholder = m === 'Cash' ? '實收 (選填)' : (m === '禮物卡' ? '禮物卡卡號' : (m === 'Credit Card' ? '授權碼/末四碼' : ''));
        extra.disabled = !['Cash', '禮物卡', 'Credit Card'].includes(m);
    });
    const remain = total - sum;
    $('posTenderRemain').textContent = `$${remain}`;
    $('posTenderRemain').className = `fw-bold ${remain === 0 ? 'text-success' : 'text-danger'}`;
    $('posTenderSubmit').disabled = remain !== 0;
}

export function openTenders(total) {
    $('posTenderTotal').textContent = total;
    const methods = TENDER_METHODS();
    setHtml($('posTenderRows'), [tenderRowHtml(methods[0] || 'Cash', total), tenderRowHtml(methods[1] || methods[0], 0)]);
    syncTenderRows();
    modalOf('posTenderModal').show();
}

export function addTender() {
    $('posTenderRows').insertAdjacentHTML('beforeend', String(tenderRowHtml(TENDER_METHODS()[0], 0)));
    syncTenderRows();
}

export function readTenders() {
    return [...document.querySelectorAll('#posTenderRows .pos-tender-row')].map((row) => {
        const method = row.querySelector('.tender-method').value;
        const amount = num(row.querySelector('.tender-amount').value);
        const extra = (row.querySelector('.tender-extra').value || '').trim();
        const t = { method, amount };
        if (method === 'Cash') t.received = num(extra) || amount;
        if (method === '禮物卡') t.gift_code = extra;
        if (method === 'Credit Card') {
            const [auth, last4] = extra.split('/').map((x) => (x || '').trim());
            t.auth_code = auth || '';
            t.card_last4 = last4 || '';
        }
        return t;
    });
}

export async function submitDeliveryOrder() {
    const ref = ($('posDeliveryRef').value || '').trim();
    if (!ref) {
        alert('⚠️ 請輸入外送平台的訂單編號！');
        $('posDeliveryRef').focus();
        return;
    }
    const payload = {
        items: state.cart.map(payloadLine), platform: $('posDeliveryPlatform').value, external_ref: ref,
        note: ($('posOrderNote').value || '').trim(),
    };
    const sig = JSON.stringify(payload);
    if (!state.checkout || state.checkout.sig !== sig) state.checkout = { id: Kiosk.newOrderId(), sig };
    setSubmitting(true);
    try {
        const res = await api('/api/pos/delivery_order', { json: { ...payload, client_order_id: state.checkout.id } });
        state.checkout = null;
        showReceipt(res, '外送單已登錄 • 收據預覽');
        resetTerminal();
    } catch (err) {
        alert(`❌ ${err.message}`);
    } finally {
        setSubmitting(false);
        syncLiveState();
    }
}

export function toggleDeliveryMode() {
    state.deliveryMode = !state.deliveryMode;
    $('posDeliveryPanel').classList.toggle('d-none', !state.deliveryMode);
    $('posOrderInfoBox').classList.toggle('opacity-50', state.deliveryMode);
    const btn = $('posDeliveryBtn');
    if (btn) btn.classList.toggle('active', state.deliveryMode);
    setHtml($('posSubmitBtn'), state.deliveryMode ? html`<i class="bi bi-bicycle me-1"></i> 登錄外送單 (F2)` : SUBMIT_LABEL);
    repriceCart();
}

// ---------------------------------------------------------------- 改價 / 整單折扣
export let priceIdx = null;
export function openPriceModal(idx) {
    const line = state.cart[idx];
    if (!line) return;
    priceIdx = idx;
    $('posPriceItemName').textContent = line.name;
    $('posPriceOriginal').textContent = line.original_price !== undefined ? line.original_price : line.price;
    $('posPriceValue').value = line.price;
    modalOf('posPriceModal').show();
}

export function applyLinePrice(ev) {
    ev.preventDefault();
    const line = state.cart[priceIdx];
    if (!line) return;
    const value = Math.max(0, Math.round(num($('posPriceValue').value)));
    if (line.original_price === undefined) line.original_price = line.price;
    if (value === line.original_price) {
        delete line.override_price;
        delete line.original_price;
    } else {
        line.override_price = value;
    }
    line.price = value;
    modalOf('posPriceModal').hide();
    renderCart();
}

export function resetLinePrice() {
    const line = state.cart[priceIdx];
    if (line && line.original_price !== undefined) {
        line.price = line.original_price;
        delete line.override_price;
        delete line.original_price;
    }
    modalOf('posPriceModal').hide();
    renderCart();
}

export function applyOrderDiscount(ev) {
    ev.preventDefault();
    const type = document.querySelector('input[name="posDiscountType"]:checked').value;
    const value = Math.round(num($('posDiscountValue').value));
    state.orderDiscount = value > 0 ? { type, value } : null;
    modalOf('posDiscountModal').hide();
    recalcTotals();
}

export function showReceipt(d, title) {
    $('rcpt-title').textContent = title;
    $('rcpt-pickup-no').textContent = `#${pad3(d.pickup_number || d.order_id)}`;
    $('rcpt-order-id').textContent = `#${d.order_id}`;
    $('rcpt-customer').textContent = d.table_number || d.user_name || '櫃檯POS';
    $('rcpt-order-type').textContent = d.order_type + (d.order_type === '外帶' ? (d.need_cutlery ? ' (附餐具)' : ' (免餐具)') : '');
    $('rcpt-cashier').textContent = d.cashier || 'POS';
    $('rcpt-time').textContent = d.created_at || new Date().toLocaleString();

    const noteBox = $('rcpt-note-box');
    noteBox.classList.toggle('d-none', !d.note);
    noteBox.textContent = d.note ? `備註：${d.note}` : '';

    setHtml($('rcpt-items-tbody'), (d.items || []).map((it) => html`
        <tr>
            <td>
                <div class="fw-bold">${it.name || it.item_name}</div>
                ${it.customization ? html`<div class="rcpt-custom">${it.customization}</div>` : ''}
            </td>
            <td class="text-center">x${num(it.quantity)}</td>
            <td class="text-end">$${num(it.subtotal) || num(it.price) * num(it.quantity)}</td>
        </tr>`));

    state.lastOrderId = d.order_id;
    const svc = num(d.service_charge);
    $('rcpt-service-row').classList.toggle('d-none', svc <= 0);
    $('rcpt-service').textContent = svc;
    const inv = d.invoice || null;
    $('rcpt-invoice-box').classList.toggle('d-none', !inv);
    if (inv) {
        $('rcpt-invoice-no').textContent = inv.invoice_no || (inv.status === 'pending' ? '開立中' : inv.status);
        $('rcpt-invoice-carrier').textContent = `${inv.carrier || ''}${inv.random_number ? ` / ${inv.random_number}` : ''}`;
    }
    $('rcpt-qr-box').classList.toggle('d-none', !d.track_qr);
    if (d.track_qr) $('rcpt-qr').src = d.track_qr;
    $('rcpt-subtotal').textContent = Math.round(num(d.subtotal) || num(d.total_price));
    $('rcpt-discount').textContent = Math.round(num(d.discount_amount));
    $('rcpt-total').textContent = Math.round(num(d.total_price));
    $('rcpt-pay-method').textContent = d.payment_method || 'Cash';
    $('rcpt-received').textContent = Math.round(d.cash_received !== undefined && d.cash_received !== null ? num(d.cash_received) : num(d.total_price));
    $('rcpt-change').textContent = Math.round(num(d.cash_change));

    const ptsBox = $('rcpt-points-box');
    const showPts = num(d.points_earned) > 0 || num(d.remaining_points) > 0;
    ptsBox.classList.toggle('d-none', !showPts);
    $('rcpt-earned-pts').textContent = `+${num(d.points_earned)} 點`;
    $('rcpt-remain-pts').textContent = `${num(d.remaining_points)} 點`;

    modalOf('posReceiptModal').show();
}
