/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：結帳預覽 / 送出 / 錯誤處理 / 斷線暫存 / 收據、桌邊帳單、我的訂單。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js，品項結構 / 發票 / 合併規則在 static/js/shared/。
 */
import {
    $, REWARD_TAG, api, html, isGuest, isOnline, menuItems, modal, modifierById, pageData, pro, setText, state, toggleRow, ui,
} from './core.js';
import { availableChoiceIds, itemName, lineAvailable, lineDescription, makeLine, persistCart, saveCart, updateCartUI } from './cart.js';
import { calculateTotal, computeTotals, setUserPoints, toggleCalories, verifyPromo, toggleCarbon } from './totals.js';
import { payloadLine } from '../shared/orderpayload.js';
import { applyLinePrices, mergeLine } from '../shared/cartline.js';
import { invoicePayload as sharedInvoicePayload } from '../shared/invoice.js';

const PAGE_DAY = new Date().toDateString();   // 頁面載入日 (預約取餐時段的「今天 / 明天」以此為準)

export async function openTableBill() {
    try {
        const res = await api('/api/table/bill');
        Kiosk.setHtml($('tableBillBody'), res.orders.length ? html`
            ${res.orders.map((o) => html`<div class="border-bottom py-1"><div class="fw-bold small">${ui.pickup_hash || '取餐 #'}${o.pickup_number} • $${o.total_price}</div>
                ${o.items.map((i) => html`<div class="small text-secondary">${i.name} x${i.quantity}</div>`)}</div>`)}
            <div class="fs-5 fw-bold text-end mt-2">${ui.total || '合計'} <span id="tableBillTotal">$${res.total}</span></div>` : html`<div class="text-secondary">${ui.table_bill_empty || '目前沒有需要結帳的訂單'}</div>`);
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
// ---------------------------------------------------------------- 禮物卡付款 (點餐機；系統設定 → 禮物卡 → 點餐機可用)
export function togglePaymentExtras() {
    if ($('giftCardBox')) $('giftCardBox').classList.toggle('d-none', $('paymentMethod').value !== '禮物卡');
}

export async function checkGiftCard() {
    const code = ($('giftCardCode').value || '').replace(/\D/g, '');
    state.giftCard = null;
    setText('giftCardBalance', '');
    try {
        const res = await api('/api/gift_cards/balance', { json: { code } });
        state.giftCard = { code, balance: Number(res.balance) || 0 };
        setText('giftCardBalance', (ui.gift_card_balance || '卡號 {c}：餘額 ${b}').replace('{c}', res.masked_code).replace('{b}', state.giftCard.balance));
    } catch (err) {
        setText('giftCardBalance', `⚠️ ${err.message}`);
    }
}

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
        alert(ui.cart_empty || '購物車為空');
        return;
    }
    if (isOnline && !pageData.pickupNode) {
        alert(ui.choose_branch_first || '請先在頁面上方選擇取餐分店，再進行結帳！');
        return;
    }
    // 預約取餐時段 (今天 / 明天) 是頁面載入時產生的：跨過午夜後重新載入，取得正確的時段與標籤
    if ($('pickupTime') && new Date().toDateString() !== PAGE_DAY) {
        alert(`⚠️ ${ui.pickup_slots_refreshed || '取餐時段已更新，請重新選擇取餐時間'}`);
        window.location.reload();
        return;
    }
    const soldOut = state.cart.find((l) => l.is_sold_out);
    if (soldOut) {
        alert(`⚠️ ${(ui.cart_has_sold_out || '購物車中有已售完餐點【{n}】，請先點擊「移除」後再結帳！').replace('{n}', itemName(soldOut.menu_item_id, soldOut.name))}`);
        return;
    }
    if ($('paymentMethod').value === '儲值金') {
        const t0 = computeTotals();
        if ((Number(pro.walletBalance) || 0) < t0.final) {
            alert(`⚠️ ${(ui.wallet_short || '儲值金餘額 ${a} 不足 (應付 ${b})，請改用其他付款方式或至櫃檯加值！').replace('{a}', Number(pro.walletBalance) || 0).replace('{b}', t0.final)}`);
            return;
        }
    }
    if ($('paymentMethod').value === '禮物卡') {
        if (!state.giftCard) {
            alert(`⚠️ ${ui.gift_card_required || '請先輸入禮物卡卡號並查餘額'}`);
            return;
        }
        const t0 = computeTotals();
        if (state.giftCard.balance < t0.final) {
            alert(`⚠️ ${(ui.gift_card_short || '禮物卡餘額 ${b} 不足 (應付 ${a})，請至櫃檯以混合付款補差額或改用其他付款方式').replace('{b}', state.giftCard.balance).replace('{a}', t0.final)}`);
            return;
        }
    }
    // 購物車可能在套用優惠碼後變動 (百分比折扣、低消門檻)：送出前重新驗證一次；連線失敗時中止 (保留優惠碼，不以原價開確認視窗 / 斷線暫存)
    if (state.activePromoCode && !(await verifyPromo(state.activePromoCode, { silent: true }))) return alert(`⚠️ ${ui.promo_verify_failed || '無法驗證優惠碼，請確認網路連線後再試'}`);
    const t = calculateTotal();
    const info = takeoutInfo();
    const cutlery = info.needCutlery ? (ui.need_cutlery || '需要餐具') : (ui.no_cutlery || '不需要餐具');
    const zone = $('deliveryZone') && $('deliveryZone').selectedIndex >= 0 ? $('deliveryZone').options[$('deliveryZone').selectedIndex].text : '';
    const address = info.orderType === '外送到府' ? [($('deliveryAddress').value || '').trim(), zone].filter(Boolean).join(' / ') : '';
    Kiosk.setHtml($('preview-items'), html`
        <ul class="list-group">
            ${state.cart.map((l) => {
                const desc = lineDescription(l);
                return html`
                <li class="list-group-item bg-dark text-white border-secondary d-flex justify-content-between align-items-center">
                    <div>
                        <div>${l.is_reward ? `${REWARD_TAG} ` : ''}${itemName(l.menu_item_id, l.name)} x${l.quantity}</div>
                        ${desc ? html`<small class="text-info">${desc}</small>` : ''}
                    </div>
                    <span class="fw-bold">${Kiosk.money(l.price * l.quantity)}</span>
                </li>`;
            })}
        </ul>
        <div class="mt-3 p-3 rounded bg-secondary bg-opacity-10 border border-secondary">
            <div class="small text-white-50">${pageData.table ? (ui.table_ordering || '桌邊點餐') : (ui.takeout_settings || '外帶設定')}</div>
            <div class="fw-bold text-white">${pageData.table ? `${pageData.table}${ui.table_dine_in || ' 桌 • 內用 • 用餐後至櫃檯結帳'}`
                : ({ '外帶': cutlery, '外送到府': `${orderTypeLabel(info.orderType)} • ${cutlery}` }[info.orderType] || (ui.dinein || '內用'))}</div>
            ${address ? html`<div class="small text-white mt-1">${ui.delivery_label || '外送到府：'}${address}</div>` : ''}
            ${info.note ? html`<div class="small text-warning mt-2">${ui.note_label || '備註：'}${info.note}</div>` : ''}
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
    toggleCarbon('preview-carbon-row', 'preview-carbon');
    const pickupSel = $('pickupTime');
    const pickupLabel = pickupSel && pickupSel.value ? pickupSel.options[pickupSel.selectedIndex].text : '';
    $('preview-pickup-row').classList.toggle('d-none', !pickupLabel);
    setText('preview-pickup', pickupLabel);
    $('preview-eta-row').classList.toggle('d-none', !(state.eta > 0) || Boolean(pickupLabel));
    setText('preview-eta', state.eta || 0);
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
        items: state.cart.map(payloadLine),
        payment_method: $('paymentMethod').value,
        payment_info: $('paymentMethod').value === '禮物卡' && state.giftCard ? { gift_code: state.giftCard.code } : undefined,
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
    applyLinePrices(state.cart, data.line_prices);
    persistCart();   // 只改單價：沿用結帳編號
    updateCartUI();
    const again = checkoutPayload();
    const total = Number(data.server_total) || 0;
    state.acceptedTotal = computeTotals().final === total ? null : { key: payloadKey(again), total };
}

/** 電子發票 (shared/invoice.js)：載具 / 統編 / 愛心碼 */
export function invoicePayload() {
    const sel = $('invoiceType');
    return sel ? sharedInvoicePayload(sel.value, $('invoiceValue').value) : null;
}

export function handleCheckoutError(err) {
    const data = err.data || {};
    if (data.sold_out && Array.isArray(data.sold_out_items)) {
        // 售完的可能是套餐的配餐 (sold_out_items 為配餐名稱)：依目前菜單 (含配餐) 一併標示；重新整理後由 normalizeLine 依新菜單標示
        state.cart.forEach((l) => { if (data.sold_out_items.includes(l.name) || !lineAvailable(l)) l.is_sold_out = true; });
        saveCart();
        modal('confirmModal').hide();
        alert(`⚠️ ${(ui.just_sold_out || '抱歉！以下餐點剛好售罄：\n【{n}】\n已為您更新菜單並在購物車中劃掉標記，請移除後再結帳！').replace('{n}', data.sold_out_items.join('、'))}`);
        window.location.reload();
        return;
    }
    if (data.price_changed) {
        modal('confirmModal').hide();
        applyServerPrices(data);
        alert(`⚠️ ${err.message}`);
        bootstrap.Offcanvas.getOrCreateInstance($('cart-sidebar')).show();
        return;
    }
    if (data.coupon_unavailable || data.points_changed || data.refresh_slots) {
        // 優惠券或點數剛在其他裝置 / 門市 / 線上用掉、預約時段已過或額滿：重新載入最新的點數、可用券與取餐時段，讓顧客確認後再送出
        modal('confirmModal').hide();
        alert(`⚠️ ${err.message}`);
        window.location.reload();
        return;
    }
    if (data.insufficient_stock) {
        modal('confirmModal').hide();
        alert(`⚠️ ${ui.stock_short_a || '點餐數量超過目前剩餘庫存：'}\n\n${err.message}\n\n${ui.stock_short_b || '請至購物車調整購買數量後再重新結帳！'}`);
        bootstrap.Offcanvas.getOrCreateInstance($('cart-sidebar')).show();
        return;
    }
    if (data.insufficient_gift) {
        state.giftCard = null;
        modal('confirmModal').hide();
        bootstrap.Offcanvas.getOrCreateInstance($('cart-sidebar')).show();
    }
    alert(err.message || ui.checkout_failed || '結帳失敗，請稍後再試！');
}

/** 人臉 / 門市 LINE 登入動用儲值金、點數、紅利或寄杯 (403 need_pin)：確認視窗內以遮蔽欄位輸入 PIN (共用螢幕不用 window.prompt)，以同一個 client_order_id 重送；關閉視窗 = 取消 (拋出原錯誤) */
async function submitOrder(pin = '') {
    try {
        return await api('/submit_order', { json: pin ? { ...checkoutPayload(), pin } : checkoutPayload() });
    } catch (err) {
        if (err.status !== 403 || !(err.data && err.data.need_pin)) throw err;
        const form = Object.assign(document.createElement('form'), { className: 'mt-3', autocomplete: 'off' });
        Kiosk.setHtml(form, html`<label class="form-label small text-warning mb-1" for="checkoutPin">${err.message} ${ui.enter_pin || '請輸入 PIN 碼：'}</label><div class="d-flex gap-1">
            <input class="form-control" type="password" id="checkoutPin" inputmode="numeric" maxlength="6" autocomplete="off" aria-label="${ui.pin || 'PIN 碼'}" required>
            <button class="btn btn-warning text-nowrap" type="submit">${ui.confirm || '確認'}</button></div>`);
        $('confirmModal').querySelector('.modal-body').append(form);
        form.querySelector('input').focus();
        const next = await new Promise((resolve) => {
            $('confirmModal').addEventListener('hidden.bs.modal', () => resolve(''), { once: true });
            form.addEventListener('submit', (ev) => { ev.preventDefault(); resolve(form.querySelector('input').value.trim()); });
        });
        form.remove();
        return next ? submitOrder(next) : Promise.reject(err);   // PIN 錯誤時再顯示一次輸入欄位
    }
}

export async function confirmCheckout() {
    if (!state.cart.length || state.submitting) return;
    state.submitting = true;
    const btn = $('confirmCheckoutBtn');
    btn.disabled = true;
    try {
        const data = await submitOrder();
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
    const reason = KioskOffline.blockReason(payload, { member: !isGuest });   // 會員價 / 贈點需要連線，補送一律是訪客單
    if (reason) {
        alert(`${err.message}\n\n（${reason}${ui.offline_blocked_suffix || '，請稍後再試或至櫃檯點餐'}）`);
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
    state.lastOrderId = data.order_id;
    if ($('receipt-send-msg')) $('receipt-send-msg').textContent = '';
    if (!isGuest) setUserPoints(data.remaining_points);
    setText('receipt-pickup', data.pickup_pending ? (ui.pending_branch || '待分店確認') : `#${Kiosk.pad3(data.pickup_number)}`);
    setText('receipt-order-id', `#${data.order_id}`);
    setText('receipt-node', data.pickup_node_name || '');
    $('receipt-node-row').classList.toggle('d-none', !data.pickup_node_name);
    $('receipt-pending-hint').classList.toggle('d-none', !data.pickup_pending);
    setText('receipt-user', data.user_name);
    setText('receipt-type', data.order_type === '外帶'
        ? `${orderTypeLabel(data.order_type)} • ${data.need_cutlery ? (ui.need_cutlery || '要餐具') : (ui.no_cutlery || '不用餐具')}` : orderTypeLabel(data.order_type));
    // 與付款方式下拉選單相同的翻譯文字 (templates/customer.html 的 pay_labels)
    setText('receipt-payment', { Cash: ui.pay_cash, 'Credit Card': ui.pay_card, '儲值金': ui.pay_wallet, '禮物卡': ui.pay_gift_card }[data.payment_method]
        || data.payment_method);
    setText('receipt-note', data.note || '');
    $('receipt-note-row').classList.toggle('d-none', !data.note);
    setText('receipt-delivery', data.delivery_address ? `${data.delivery_address} (${ui.delivery_fee || '外送費'} $${data.delivery_fee || 0})` : '');
    $('receipt-delivery-row').classList.toggle('d-none', !data.delivery_address);
    setText('receipt-pickup-at', data.pickup_at || '');
    $('receipt-pickup-at-row').classList.toggle('d-none', !data.pickup_at);
    setText('receipt-eta', data.eta || '');
    $('receipt-eta-row').classList.toggle('d-none', !data.eta);
    const svcRow = $('receipt-service-row');
    if (svcRow) {
        svcRow.classList.toggle('d-none', !(Number(data.service_charge) > 0));
        setText('receipt-service', Number(data.service_charge) || 0);
    }
    const invRow = $('receipt-invoice-row');
    if (invRow) {
        invRow.classList.toggle('d-none', !data.invoice);
        if (data.invoice) setText('receipt-invoice', data.invoice.invoice_no || `${data.invoice.carrier} ${ui.invoice_issuing || '(開立中)'}`);
    }
    const qrBox = $('receipt-qr-box');
    if (qrBox) {
        qrBox.classList.toggle('d-none', !data.track_qr);
        if (data.track_qr) $('receipt-qr').src = data.track_qr;
    }
    setText('receipt-subtotal', Math.round(data.subtotal));
    setText('receipt-discount', Math.round(data.discount_amount));
    setText('receipt-total', Math.round(data.total_price));
    setText('receipt-earned-points', `+${data.points_earned} ${ui.points_unit || '點'}`);
    setText('receipt-current-points', `${data.remaining_points} ${ui.points_unit || '點'}`);
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

// ---------------------------------------------------------------- 電子收據 (Email / LINE)
export async function sendReceipt(channel) {
    const orderId = state.lastOrderId;
    if (!orderId) return;
    const msg = $('receipt-send-msg');
    try {
        const body = channel === 'email'
            ? { email: ($('receiptEmail').value || '').trim(), remember: !!($('receiptRemember') && $('receiptRemember').checked) }
            : {};
        const res = await api(`/api/my_orders/${orderId}/receipt/${channel}`, { json: body });
        msg.textContent = `✅ ${res.message_i18n || res.message}`;
    } catch (err) {
        msg.textContent = `⚠️ ${err.message}`;
    }
}

export function finishOrder() {
    state.cart = [];
    saveCart();
    window.location.reload();
}

// ================================================================ 我的訂單 (狀態 / 付款徽章用 Kiosk.statusBadge / paymentBadge，文字依語言)
/** 用餐方式 / 訂單狀態 / 外送狀態的顯示文字 (後端的中文標籤為 fallback) */
export function orderTypeLabel(t) {
    return { '內用': ui.dinein, '外帶': ui.takeout, '外送到府': ui.home_delivery, '外送': ui.delivery_platform }[t] || t;
}

export function orderCardHtml(o) {
    const ptsUsed = o.points_used || 0;
    const expiredReorder = o.payment_status === 'expired';
    return html`
        <div class="card mb-3 border-secondary shadow-sm text-dark">
            <div class="card-header bg-light d-flex justify-content-between align-items-center">
                <div>
                    <span class="fw-bold text-success fs-5">${ui.order_hash || '訂單 #'}${o.user_order_no}</span>
                    ${Kiosk.statusBadge(o.status_code, ui[`status_${o.status_code}`] || o.status)}
                    <span class="badge bg-primary ms-1">${orderTypeLabel(o.order_type)}</span>
                    <span class="badge bg-warning text-dark fw-bold ms-1 order-pickup">${o.pickup_pending ? (ui.pickup_pending || '取餐號：待分店確認') : `${ui.pickup_hash || '取餐 #'}${Kiosk.pad3(o.pickup_number || o.order_id)}`}</span>
                    ${o.pickup_at ? html`<span class="badge bg-info text-dark ms-1 order-pickup-at"><i class="bi bi-clock me-1"></i>${ui.scheduled || '預約 '}${o.pickup_at}</span>` : ''}
                    ${o.eta ? html`<span class="badge bg-info text-dark ms-1 order-eta"><i class="bi bi-hourglass-split me-1"></i>${ui.eta_badge || '預計 '}${o.eta}</span>` : ''}
                    ${o.delivery_label ? html`<span class="badge bg-warning text-dark ms-1 order-delivery"><i class="bi bi-scooter me-1"></i>${ui[`delivery_${o.delivery_status}`] || o.delivery_label}</span>` : ''}
                    ${o.pickup_node_name ? html`<span class="badge bg-dark ms-1 order-node"><i class="bi bi-shop me-1"></i>${o.pickup_node_name}</span>` : ''}
                    ${Kiosk.PAYMENT_STATUS[o.payment_status] ? html`<span class="ms-1">${Kiosk.paymentBadge(o.payment_status, ui[`payment_${o.payment_status}`])}</span>` : ''}
                    ${o.payment_url ? html`<a class="btn btn-sm btn-warning fw-bold ms-2 order-pay" href="${o.payment_url}">${o.payment_note ? (ui.view_payment_info || '查看繳費資訊') : (ui.pay_online || '前往付款')}</a>` : ''}
                </div>
                <small class="text-muted">${o.created_at}</small>
            </div>
            <div class="card-body p-3">
                ${expiredReorder && o.can_reorder ? html`<p class="small text-warning mb-2 order-expired-hint">${ui.reorder_expired_hint || '付款期限已過，原訂單已取消；重新下單會以目前菜單價格建立新訂單。'}</p>` : ''}
                <table class="table table-sm mb-2 align-middle">
                    <thead><tr class="text-muted"><th>${ui.th_item || '品項'}</th><th>${ui.th_qty || '數量'}</th><th>${ui.th_price || '單價'}</th><th>${ui.th_subtotal || '小計'}</th></tr></thead>
                    <tbody>
                        ${(o.items || []).map((item) => html`
                            <tr>
                                <td><div>${item.name}</div>${item.customization ? html`<small class="text-danger fw-bold">${item.customization}</small>` : ''}</td>
                                <td>x ${item.quantity}</td>
                                <td>${Kiosk.money(item.price)}</td>
                                <td>${Kiosk.money(item.subtotal)}</td>
                            </tr>`)}
                    </tbody>
                </table>
                <div class="d-flex justify-content-between align-items-center pt-2 border-top">
                    <small class="text-muted">
                        ${ui.discount_summary_a || '總折扣折抵: -$'}${Math.round(o.discount_amount || 0)} ${ptsUsed > 0 ? (ui.discount_summary_pts || '(扣除 {n} 點)').replace('{n}', ptsUsed) : ''}${ui.discount_summary_b || ' | 獲得紅利: +'}${o.points_earned} ${ui.points_unit || '點'}
                    </small>
                    <div class="d-flex align-items-center gap-2">
                        ${o.can_reorder ? html`<button type="button" class="btn btn-sm btn-outline-success fw-bold order-reorder" data-action="reorder" data-id="${o.order_id}"><i class="bi bi-arrow-repeat me-1"></i>${ui[expiredReorder ? 'reorder_expired' : 'reorder'] || (expiredReorder ? '重新下單' : '再點一次')}</button>` : ''}
                        <div class="fw-bold text-danger fs-6">${ui.paid_label || '實付：$'}${Math.round(o.total_price)}</div>
                    </div>
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
            : html`<div class="text-center text-muted py-4">${ui.no_orders || '目前尚無任何訂單紀錄！'}</div>`);
    } catch (err) {
        Kiosk.setHtml(body, html`<div class="text-center text-danger py-4">${err.message}</div>`);
    }
}

export function showMyOrders() {
    fetchMyOrders();
    modal('historyModal').show();
}

/** 再點一次：伺服器還原該筆訂單的品項 (menu_item_id / 套餐 / 規格 / 加料)，以目前菜單重新加入購物車 */
export async function reorder(orderId) {
    let res;
    try {
        res = await api(`/api/reorder/${orderId}`);
    } catch (err) {
        alert(`❌ ${err.message}`);
        return;
    }
    const skipped = [...(res.skipped || [])];
    let added = 0;
    (res.lines || []).forEach((raw) => {
        const item = menuItems.get(Number(raw.menu_item_id));
        if (!item || !item.is_available) {
            skipped.push(item ? item.name : `#${raw.menu_item_id}`);
            return;
        }
        const combo = raw.combo_option_id ? (item.combos || []).find((c) => Number(c.id) === Number(raw.combo_option_id)) : null;
        // 套餐：固定配餐要有供應；對不回來 / 售完的任選改選第一個有供應的選項 (舊明細的該群組文字不帶回)
        const choiceIds = combo ? availableChoiceIds(combo, raw.combo_choices) : [];
        if ((raw.combo_option_id && !combo) || !choiceIds) {
            skipped.push(item.name);
            return;
        }
        const customization = String(raw.customization || '').split('、')
            .filter((p) => p && !(combo ? combo.choices || [] : []).some((g) => p.startsWith(`${g.group}:`))).join('、');
        const line = makeLine(item, {
            combo, isAddon: Boolean(raw.is_add_on), customization, size: raw.size || '',
            modifierIds: (raw.modifier_ids || []).map(Number).filter((id) => modifierById.has(id)), choiceIds,
        });
        line.quantity = Math.max(1, Number(raw.quantity) || 1);
        mergeLine(state.cart, line);
        added += line.quantity;
    });
    if (added) {
        saveCart();
        updateCartUI();
        modal('historyModal').hide();
    }
    const note = skipped.length ? `\n${ui.reorder_skipped || '略過 (已下架、售完或不適用)：'}${skipped.join('、')}` : '';
    alert(added ? `✅ ${(ui.reorder_added || '已加入 {n} 份到購物車').replace('{n}', added)}${note}` : `⚠️ ${ui.reorder_none || '沒有可加入的品項'}${note}`);
}
