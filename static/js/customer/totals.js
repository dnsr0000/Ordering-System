/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：促銷預覽、金額計算 (與後端 process_checkout 相同的進位規則)、優惠券 / 點數。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import {
    $, api, html, isGuest, menuItems, pageData, pro, rewardConfig, setText, state, toggleRow, ui,
} from './core.js';

// ================================================================ 促銷活動預覽 (實際以後端結帳計算為準)
export function schedulePromotionPreview() {
    clearTimeout(state.promotionTimer);
    state.promotionTimer = setTimeout(runPromotionPreview, 300);
}

export async function runPromotionPreview() {
    const seq = ++state.promotionSeq;
    if (!state.cart.length) {
        state.promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
        calculateTotal();
        return;
    }
    try {
        const res = await api('/api/promotions/preview', {
            json: { items: state.cart.map(itemPayload), channel: pageData.table ? 'table' : 'kiosk', order_type: $('orderType').value },
        });
        if (seq === state.promotionSeq) state.promotion = res;
    } catch (err) {
        if (seq === state.promotionSeq) state.promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
    }
    calculateTotal();
}

export function itemPayload(l) {
    return {
        menu_item_id: l.menu_item_id,
        quantity: l.quantity,
        modifier_ids: l.modifier_ids,
        combo_option_id: l.combo_option_id,
        is_add_on: l.is_add_on,
        is_reward: l.is_reward,
        customization: l.customization,
        size: l.size || '',
    };
}

// ================================================================ 金額計算 (與後端 process_checkout 相同的進位規則)
export function computeTotals() {
    const subtotal = state.cart.reduce((sum, l) => sum + l.price * l.quantity, 0);
    const promotionDiscount = Math.min(Number(state.promotion.discount) || 0, subtotal);
    const afterPromotion = Math.max(0, subtotal - promotionDiscount);
    const promo = state.promotion.blocks_coupon ? 0 : Math.min(state.appliedPromoDiscount, afterPromotion);
    let remaining = Math.max(0, afterPromotion - promo);
    const tierPct = Number(pro.tierDiscount) || 0;
    const tier = tierPct > 0 ? remaining * Math.min(100, tierPct) / 100 : 0;
    remaining = Math.max(0, remaining - tier);
    let pointsUsed = 0;
    let pointsDiscount = 0;
    const toggle = $('usePointsToggle');
    if (!isGuest && rewardConfig.isEnabled && toggle && toggle.checked) {
        const input = $('pointsToUse');
        const ppd = Math.max(1, Number(rewardConfig.pointsPerDollar) || 1);
        let maxCash = Math.floor(remaining);
        if (rewardConfig.maxDiscount > 0) maxCash = Math.min(maxCash, rewardConfig.maxDiscount);
        const maxPoints = Math.min(state.userPoints, maxCash * ppd);
        let val = Math.floor(Number(input.value) || 0);
        val = Math.min(Math.max(val, 0), maxPoints);
        if (String(val) !== input.value) input.value = val;
        pointsDiscount = Math.min(Math.floor(val / ppd), maxCash);
        pointsUsed = pointsDiscount * ppd;
    }
    const afterDiscounts = Math.max(0, remaining - pointsDiscount);
    const servicePct = Number(pro.serviceChargePercent) || 0;
    const dineIn = pageData.table || $('orderType').value === '內用';
    const service = servicePct > 0 && pro.serviceChargeDineIn && dineIn ? Math.floor(afterDiscounts * servicePct / 100 + 0.5) : 0;
    const delivery = deliveryFee(subtotal);
    const final = Math.floor(afterDiscounts + 0.5) + service + delivery;
    const earned = isGuest ? 0 : Math.floor((final - delivery) / Math.max(1, Number(rewardConfig.spendPerPoint) || 100));
    return { subtotal, promo, pointsUsed, pointsDiscount, final, earned, promotionDiscount, tier, service, delivery };
}

/** 外送到府的外送費 (與後端相同：依配送區域，商品小計達免運門檻免收) */
export function deliveryFee(subtotal) {
    if (!pro.homeDelivery || !$('orderType') || $('orderType').value !== '外送到府') return 0;
    const opt = $('deliveryZone') && $('deliveryZone').selectedOptions[0];
    if (!opt) return 0;
    const freeOver = Number(pro.homeDelivery.freeOver) || 0;
    return freeOver && subtotal >= freeOver ? 0 : Number(opt.dataset.fee) || 0;
}

export function calculateTotal() {
    const t = computeTotals();
    toggleRow('cart-promotion-row', 'cart-promotion', t.promotionDiscount);
    setText('cart-promotion-names', (state.promotion.names || []).join('、'));
    toggleRow('cart-tier-row', 'cart-tier', t.tier);
    toggleRow('cart-service-row', 'cart-service', t.service);
    toggleRow('cart-delivery-row', 'cart-delivery', t.delivery);
    const gifts = $('cart-gifts');
    if (gifts) Kiosk.setHtml(gifts, (state.promotion.gifts || []).map((g) => html`<div><i class="bi bi-gift me-1"></i>${ui.gift || '贈品'}：${g.name} x${g.quantity}</div>`));
    setText('cart-subtotal', Math.round(t.subtotal));
    setText('cart-promo-discount', Math.round(t.promo));
    setText('cart-points-discount', Math.round(t.pointsDiscount));
    setText('cart-final', t.final);
    setText('cart-earned-points', t.earned);
    toggleCalories('cart-calories-row', 'cart-calories');
    return t;
}

/** 購物車熱量合計 (有標示熱量的餐點才計入；後台關閉熱量顯示時隱藏) */
export function cartCalories() {
    return state.cart.reduce((sum, l) => sum + (Number((menuItems.get(Number(l.menu_item_id)) || {}).calories) || 0) * l.quantity, 0);
}

export function toggleCalories(rowId, valueId) {
    const row = $(rowId);
    if (!row) return;
    const kcal = pro.showCalories ? cartCalories() : 0;
    row.classList.toggle('d-none', !(kcal > 0));
    setText(valueId, Math.round(kcal));
}

export function setUserPoints(points) {
    state.userPoints = Number(points) || 0;
    setText('nav-user-points', state.userPoints);
    setText('cart-user-points', state.userPoints);
}

// ================================================================ 優惠券 / 點數
export function renderCoupons(coupons) {
    const select = $('userCouponSelect');
    const badge = $('user-coupon-badge');
    if (!select) return;
    const list = coupons || [];
    if (!list.length) {
        Kiosk.setHtml(select, html`<option value="" disabled selected>${ui.no_coupon}</option>`);
        if (badge) Kiosk.setHtml(badge, html`<a href="/rewards" class="text-warning text-decoration-none"><i class="bi bi-gift"></i> ${ui.empty_coupon_store}</a>`);
        return;
    }
    const current = state.activePromoCode;
    Kiosk.setHtml(select, [
        html`<option value="">${ui.select_coupon}</option>`,
        list.map((c) => html`<option value="${c.code}" ${c.code === current ? 'selected' : ''}>${c.title} (${ui.coupon_code}: ${c.code})${c.min_spend > 0 ? ` - ${ui.min_spend} $${Math.round(c.min_spend)} ${ui.eligible}` : ''}</option>`),
    ]);
    if (badge) Kiosk.setHtml(badge, html`<span class="badge bg-warning text-dark py-0">${list.length} ${ui.available_coupons}</span>`);
}

export async function syncLatestUserCoupons() {
    if (isGuest) return;
    try {
        const data = await api('/api/user_available_coupons');
        setUserPoints(data.points);
        renderCoupons(data.coupons);
        calculateTotal();
    } catch (err) {
        /* 背景同步失敗不打擾顧客，下次開啟購物車會再試 */
    }
}

export function showPromoMessage(ok, message) {
    const el = $('promo-msg');
    el.className = `small mt-1 ${ok ? 'text-success' : 'text-danger'}`;
    Kiosk.setHtml(el, html`<i class="bi ${ok ? 'bi-check-circle' : 'bi-x-circle'}"></i> ${message}`);
}

export async function verifyPromo(code, { silent = false } = {}) {
    const subtotal = state.cart.reduce((sum, l) => sum + l.price * l.quantity, 0);
    try {
        const res = await api('/api/verify_promo', { json: { promo_code: code, subtotal: Math.round(subtotal) } });
        state.activePromoCode = res.valid ? code : '';
        state.appliedPromoDiscount = res.valid ? Number(res.discount) || 0 : 0;
        if (!silent || !res.valid) showPromoMessage(res.valid, res.valid ? ui.promo_applied : ui.promo_invalid);
    } catch (err) {
        state.activePromoCode = '';
        state.appliedPromoDiscount = 0;
        showPromoMessage(false, ui.promo_invalid);
    }
    calculateTotal();
}

export function applyPromoCode() {
    const code = $('promoInput').value.trim();
    if (!code) {
        state.activePromoCode = '';
        state.appliedPromoDiscount = 0;
        $('promo-msg').textContent = '';
        calculateTotal();
        return;
    }
    verifyPromo(code);
}

export function togglePoints() {
    const toggle = $('usePointsToggle');
    const input = $('pointsToUse');
    $('points-input-box').classList.toggle('d-none', !toggle.checked);
    // 開啟時預設帶入可使用的最大點數 (computeTotals 會再依上限修正)
    input.value = toggle.checked ? state.userPoints : 0;
    calculateTotal();
}
