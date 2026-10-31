/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：結帳預覽 / 送出 / 錯誤處理 / 斷線暫存 / 收據、桌邊帳單、我的訂單。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import {
    $, REWARD_DISPLAY_LABEL, api, html, isGuest, isOnline, modal, pageData, pro, setText, state, toggleRow, ui,
} from './core.js';
import { lineDescription, saveCart, updateCartUI } from './cart.js';
import { calculateTotal, computeTotals, setUserPoints, toggleCalories, verifyPromo } from './totals.js';

export async function openTableBill() {
    try {
        const res = await api('/api/table/bill');
        Kiosk.setHtml($('tableBillBody'), res.orders.length ? html`
            ${res.orders.map((o) => html`<div class="border-bottom py-1"><div class="fw-bold small">${ui.pickup} #${o.pickup_number} • $${o.total_price}</div>
                ${o.items.map((i) => html`<div class="small text-secondary">${i.name} x${i.quantity}</div>`)}</div>`)}
            <div class="fs-5 fw-bold text-end mt-2">${ui.payable} <span id="tableBillTotal">$${res.total}</span></div>` : html`<div class="text-secondary">${ui.no_unpaid_orders}</div>`);
        $('tablePayBtn').disabled = !res.orders.length || !res.online_pay;
        modal('tableBillModal').show();
    } catch (err) {
        alert(`❌ ${err.message}`);
    }
}

export async function payTableBill() {
    $('tablePayBtn').disabled = true;
    try {
        const res = await api('/api/table/pay', { method: 'POST', json: {} });
        window.location.href = res.payment_url;
    } catch (err) {
        alert(`❌ ${err.message}`);
        $('tablePayBtn').disabled = false;
    }
}

// ================================================================ 結帳
export function takeoutInfo() {
    const orderType = $('orderType').value;
    const takeout = orderType === '外帶' || orderType === '外送到府';
    return {
        orderType,
        needCutlery: takeout ? $('needCutlery').checked : false,
        note: takeout ? ($('takeoutNote').value || '').trim() : '',
    };
}

export async function openPreview() {
    if (!state.cart.length) {
        alert(ui.cart_empty_error);
        return;
    }
    if (isOnline && !pageData.pickupNode) {
        alert(ui.select_branch_first);
        return;
    }
    const soldOut = state.cart.find((l) => l.is_sold_out);
    if (soldOut) {
        alert(`⚠️ ${ui.sold_out_cart_error} (${soldOut.name})`);
        return;
    }
    if ($('paymentMethod').value === '儲值金') {
        const t0 = computeTotals();
        if ((Number(pro.walletBalance) || 0) < t0.final) {
            alert(`⚠️ ${ui.wallet_insufficient} ($${Number(pro.walletBalance) || 0} / $${t0.final})`);
            return;
        }
    }
    // 購物車可能在套用優惠碼後變動 (百分比折扣、低消門檻)：送出前重新驗證一次
    if (state.activePromoCode) await verifyPromo(state.activePromoCode, { silent: true });

    const t = calculateTotal();
    const info = takeoutInfo();
    Kiosk.setHtml($('preview-items'), html`
        <ul class="list-group">
            ${state.cart.map((l) => {
                const desc = lineDescription(l);
                return html`
                <li class="list-group-item bg-dark text-white border-secondary d-flex justify-content-between align-items-center">
                    <div>
                        <div>${l.is_reward ? `${REWARD_DISPLAY_LABEL} ` : ''}${l.name} x${l.quantity}</div>
                        ${desc ? html`<small class="text-info">${desc}</small>` : ''}
                    </div>
                    <span class="fw-bold">$${Math.round(l.price * l.quantity)}</span>
                </li>`;
            })}
        </ul>
        <div class="mt-3 p-3 rounded bg-secondary bg-opacity-10 border border-secondary">
            <div class="small text-white-50">${pageData.table ? ui.table_order_notice : ui.takeout_settings}</div>
            <div class="fw-bold text-white">${pageData.table ? `${ui.table_order_notice} ${pageData.table} • ${ui.dine_in} • ${ui.table_order_delivery}`
                : (info.orderType === '外帶' ? (info.needCutlery ? ui.need_cutlery : ui.no_cutlery) : info.orderType === '外送到府' ? ui.home_delivery : ui.dine_in)}</div>
            ${info.note ? html`<div class="small text-warning mt-2">${ui.note}：${info.note}</div>` : ''}
        </div>`);
    toggleRow('preview-promotion-row', 'preview-promotion', t.promotionDiscount);
    toggleRow('preview-tier-row', 'preview-tier', t.tier);
    toggleRow('preview-service-row', 'preview-service', t.service);
    toggleRow('preview-delivery-row', 'preview-delivery', t.delivery);
    setText('preview-subtotal', Math.round(t.subtotal));
    setText('preview-promo', Math.round(t.promo));
    setText('preview-points', Math.round(t.pointsDiscount));
    setText('preview-final', checkoutPayload().expected_total);   // 伺服器回報過新金額時顯示新金額
    setText('preview-earned', t.earned);
    toggleCalories('preview-calories-row', 'preview-calories');
    const pickupSel = $('pickupTime');
    const pickupLabel = pickupSel && pickupSel.value ? pickupSel.options[pickupSel.selectedIndex].text : '';
    $('preview-pickup-row').classList.toggle('d-none', !pickupLabel);
    setText('preview-pickup', pickupLabel);
    modal('confirmModal').show();
}

export function payloadKey(payload) {
    return JSON.stringify({ ...payload, client_order_id: '', expected_total: 0 });
}

export function checkoutPayload() {
    const t = computeTotals();
    const info = takeoutInfo();
    if (!state.checkoutOrderId) state.checkoutOrderId = Kiosk.newOrderId();
    const payload = {
        client_order_id: state.checkoutOrderId,
        items: state.cart.map((l) => ({
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
        })),
        payment_method: $('paymentMethod').value,
        order_type: info.orderType,
        need_cutlery: info.needCutlery,
        note: info.note,
        promo_code: state.activePromoCode,
        use_points: t.pointsUsed,
        invoice: invoicePayload(),
        pickup_at: $('pickupTime') ? $('pickupTime').value : '',
        delivery: info.orderType === '外送到府' ? {
            address: ($('deliveryAddress').value || '').trim(),
            phone: ($('deliveryPhone').value || '').trim(),
            zone: $('deliveryZone').value,
        } : null,
    };
    const key = payloadKey(payload);
    payload.expected_total = state.acceptedTotal && state.acceptedTotal.key === key ? state.acceptedTotal.total : t.final;
    return payload;
}

/** 伺服器回報金額與畫面不同 (剛改價等)：以伺服器的單價更新購物車；仍有差異時記住伺服器金額 (顧客確認後再送出) */
export function applyServerPrices(data) {
    const prices = data.line_prices || [];
    if (prices.length === state.cart.length) state.cart.forEach((l, i) => { l.price = Number(prices[i]) || 0; });
    saveCart();
    updateCartUI();
    const again = checkoutPayload();
    const total = Number(data.server_total) || 0;
    state.acceptedTotal = computeTotals().final === total ? null : { key: payloadKey(again), total };
}

export const INVOICE_PLACEHOLDERS = {
    mobile: ui.mobile_invoice_placeholder,
    company: ui.company_invoice_placeholder,
    donate: ui.donation_invoice_placeholder,
};
export function invoicePayload() {
    const sel = $('invoiceType');
    if (!sel || !sel.value) return null;
    const value = ($('invoiceValue').value || '').trim();
    if (sel.value === 'company') return { type: 'company', buyer_id: value };
    if (sel.value === 'donate') return { type: 'donate', love_code: value };
    return { type: sel.value, carrier: value };
}

export function handleCheckoutError(err) {
    const data = err.data || {};
    if (data.sold_out && Array.isArray(data.sold_out_items)) {
        state.cart.forEach((l) => { if (data.sold_out_items.includes(l.name)) l.is_sold_out = true; });
        saveCart();
        modal('confirmModal').hide();
        alert(`⚠️ ${ui.sold_out_cart_error}\n${data.sold_out_items.join(', ')}`);
        window.location.reload();
        return;
    }
    if (data.price_changed) {
        modal('confirmModal').hide();
        applyServerPrices(data);
        alert(`⚠️ ${ui.price_changed_notice}`);
        bootstrap.Offcanvas.getOrCreateInstance($('cart-sidebar')).show();
        return;
    }
    if (data.coupon_unavailable || data.points_changed) {
        // 優惠券或點數剛在其他裝置 / 門市 / 線上用掉：重新載入最新的點數與可用券，讓顧客確認金額後再送出
        modal('confirmModal').hide();
        alert(`⚠️ ${ui.discount_or_points_changed}`);
        window.location.reload();
        return;
    }
    if (data.insufficient_stock) {
        modal('confirmModal').hide();
        alert(`⚠️ ${ui.stock_unavailable}`);
        bootstrap.Offcanvas.getOrCreateInstance($('cart-sidebar')).show();
        return;
    }
    alert(ui.checkout_failed);
}

export async function confirmCheckout() {
    if (!state.cart.length || state.submitting) return;
    state.submitting = true;
    const btn = $('confirmCheckoutBtn');
    btn.disabled = true;
    try {
        const data = await api('/submit_order', { json: checkoutPayload() });
        if (data.payment_url) {  // 線上點餐：先付款，付款完成後訂單才送到分店
            state.cart = [];
            saveCart();
            window.location.href = data.payment_url;
            return;
        }
        modal('confirmModal').hide();
        const sidebar = bootstrap.Offcanvas.getInstance($('cart-sidebar'));
        if (sidebar) sidebar.hide();
        showReceipt(data);
    } catch (err) {
        if (window.KioskOffline && window.KioskOffline.shouldQueue(err)) {
            queueOffline(err);
            return;
        }
        handleCheckoutError(err);
    } finally {
        state.submitting = false;
        btn.disabled = false;
    }
}

/** 連不到門市主機：可暫存的訂單 (現金、未用點數 / 券) 存在點餐機，連線恢復後自動補送 */
export function queueOffline(err) {
    const payload = checkoutPayload();
    const reason = KioskOffline.blockReason(payload);
    if (reason) {
        alert(`${err.message}\n\n（${reason}，請稍後再試或至櫃檯點餐）`);
        return;
    }
    const summary = state.cart.map((l) => `${l.name}×${l.quantity}`).join('、');
    const entry = KioskOffline.enqueue(payload, computeTotals().final, summary);
    state.cart = [];
    state.checkoutOrderId = null;
    saveCart();
    updateCartUI();
    modal('confirmModal').hide();
    const sidebar = bootstrap.Offcanvas.getInstance($('cart-sidebar'));
    if (sidebar) sidebar.hide();
    KioskOffline.showReceipt(entry);
}

export function showReceipt(data) {
    if (!isGuest) setUserPoints(data.remaining_points);
    setText('receipt-pickup', data.pickup_pending ? ui.pickup_pending : `#${String(data.pickup_number).padStart(3, '0')}`);
    setText('receipt-order-id', `#${data.order_id}`);
    setText('receipt-node', data.pickup_node_name || '');
    $('receipt-node-row').classList.toggle('d-none', !data.pickup_node_name);
    $('receipt-pending-hint').classList.toggle('d-none', !data.pickup_pending);
    setText('receipt-user', data.user_name);
    const typeLabel = data.order_type === '內用' ? ui.dine_in : data.order_type === '外帶' ? ui.take_out
        : data.order_type === '外送到府' ? ui.home_delivery : data.order_type;
    setText('receipt-type', data.order_type === '外帶'
        ? `${typeLabel} • ${data.need_cutlery ? ui.need_cutlery : ui.no_cutlery}` : typeLabel);
    setText('receipt-payment', data.payment_method);
    setText('receipt-note', data.note || '');
    $('receipt-note-row').classList.toggle('d-none', !data.note);
    setText('receipt-delivery', data.delivery_address ? `${data.delivery_address} (${ui.delivery_fee} $${data.delivery_fee || 0})` : '');
    $('receipt-delivery-row').classList.toggle('d-none', !data.delivery_address);
    setText('receipt-pickup-at', data.pickup_at || '');
    $('receipt-pickup-at-row').classList.toggle('d-none', !data.pickup_at);
    const svcRow = $('receipt-service-row');
    if (svcRow) {
        svcRow.classList.toggle('d-none', !(Number(data.service_charge) > 0));
        setText('receipt-service', Number(data.service_charge) || 0);
    }
    const invRow = $('receipt-invoice-row');
    if (invRow) {
        invRow.classList.toggle('d-none', !data.invoice);
        if (data.invoice) setText('receipt-invoice', data.invoice.invoice_no || `${data.invoice.carrier} (${ui.issuing_invoice})`);
    }
    const qrBox = $('receipt-qr-box');
    if (qrBox) {
        qrBox.classList.toggle('d-none', !data.track_qr);
        if (data.track_qr) $('receipt-qr').src = data.track_qr;
    }
    setText('receipt-subtotal', Math.round(data.subtotal));
    setText('receipt-discount', Math.round(data.discount_amount));
    setText('receipt-total', Math.round(data.total_price));
    setText('receipt-earned-points', `+${data.points_earned} ${ui.points}`);
    setText('receipt-current-points', `${data.remaining_points} ${ui.points}`);
    Kiosk.setHtml($('receipt-items-body'), (data.items || []).map((item) => html`
        <tr>
            <td>
                <div>${item.name}</div>
                ${item.customization ? html`<small class="text-danger fw-bold"><i class="bi bi-tag-fill"></i> ${item.customization}</small>` : ''}
            </td>
            <td>x ${item.quantity}</td>
            <td>$${item.price}</td>
            <td>$${item.subtotal}</td>
        </tr>`));
    modal('receiptModal').show();
}

export function finishOrder() {
    state.cart = [];
    saveCart();
    window.location.reload();
}

// ================================================================ 我的訂單
export const STATUS_BADGES = { 製作中: 'bg-warning text-dark', 已出餐: 'bg-success', 已取餐: 'bg-secondary', 已取消: 'bg-danger' };
const STATUS_LABELS = { 製作中: ui.preparing, 已出餐: ui.completed, 已取餐: ui.picked_up, 已取消: ui.cancelled };

export const PAYMENT_BADGES = {
    pending: ['bg-warning text-dark', ui.payment_pending],
    paid: ['bg-success', ui.payment_paid],
    expired: ['bg-secondary', ui.payment_expired],
    refund_required: ['bg-danger', ui.refund_pending],
    refunded: ['bg-secondary', ui.refunded],
};

export function orderCardHtml(o) {
    const ptsUsed = o.points_used || 0;
    return html`
        <div class="card mb-3 border-secondary shadow-sm text-dark">
            <div class="card-header bg-light d-flex justify-content-between align-items-center">
                <div>
                    <span class="fw-bold text-success fs-5">${ui.order_number} #${o.user_order_no}</span>
                    <span class="badge ${STATUS_BADGES[o.status] || 'bg-danger'}">${STATUS_LABELS[o.status] || o.status}</span>
                    <span class="badge bg-primary ms-1">${o.order_type === '內用' ? ui.dine_in : o.order_type === '外帶' ? ui.take_out : o.order_type === '外送到府' ? ui.home_delivery : o.order_type}</span>
                    <span class="badge bg-warning text-dark fw-bold ms-1 order-pickup">${o.pickup_pending ? ui.pickup_pending : `${ui.pickup} #${String(o.pickup_number || o.order_id).padStart(3, '0')}`}</span>
                    ${o.pickup_at ? html`<span class="badge bg-info text-dark ms-1 order-pickup-at"><i class="bi bi-clock me-1"></i>${ui.scheduled} ${o.pickup_at}</span>` : ''}
                    ${o.delivery_label ? html`<span class="badge bg-warning text-dark ms-1 order-delivery"><i class="bi bi-scooter me-1"></i>${o.delivery_label}</span>` : ''}
                    ${o.pickup_node_name ? html`<span class="badge bg-dark ms-1 order-node"><i class="bi bi-shop me-1"></i>${o.pickup_node_name}</span>` : ''}
                    ${PAYMENT_BADGES[o.payment_status] ? html`<span class="badge ms-1 order-payment ${PAYMENT_BADGES[o.payment_status][0]}">${PAYMENT_BADGES[o.payment_status][1]}</span>` : ''}
                    ${o.payment_url ? html`<a class="btn btn-sm btn-warning fw-bold ms-2 order-pay" href="${o.payment_url}">${o.payment_note ? ui.view_payment_info : ui.go_to_payment}</a>` : ''}
                </div>
                <small class="text-muted">${o.created_at}</small>
            </div>
            <div class="card-body p-3">
                <table class="table table-sm mb-2 align-middle">
                    <thead><tr class="text-muted"><th>${ui.item}</th><th>${ui.quantity}</th><th>${ui.unit_price}</th><th>${ui.subtotal}</th></tr></thead>
                    <tbody>
                        ${(o.items || []).map((item) => html`
                            <tr>
                                <td><div>${item.name}</div>${item.customization ? html`<small class="text-danger fw-bold">${item.customization}</small>` : ''}</td>
                                <td>x ${item.quantity}</td>
                                <td>$${Math.round(item.price)}</td>
                                <td>$${Math.round(item.subtotal)}</td>
                            </tr>`)}
                    </tbody>
                </table>
                <div class="d-flex justify-content-between align-items-center pt-2 border-top">
                    <small class="text-muted">
                        ${ui.total_discount}: -$${Math.round(o.discount_amount || 0)} ${ptsUsed > 0 ? `(${ui.deducted_points} ${ptsUsed} ${ui.points})` : ''} | ${ui.earned_points}: +${o.points_earned} ${ui.points}
                    </small>
                    <div class="fw-bold text-danger fs-6">${ui.actual_paid}: $${Math.round(o.total_price)}</div>
                </div>
            </div>
        </div>`;
}

export async function fetchMyOrders() {
    const body = $('history-modal-body');
    try {
        const orders = await api('/my_orders');
        Kiosk.setHtml(body, Array.isArray(orders) && orders.length
            ? orders.map(orderCardHtml)
            : html`<div class="text-center text-muted py-4">${ui.no_orders}</div>`);
    } catch (err) {
        Kiosk.setHtml(body, html`<div class="text-center text-danger py-4">${err.message}</div>`);
    }
}

export function showMyOrders() {
    fetchMyOrders();
    modal('historyModal').show();
}
