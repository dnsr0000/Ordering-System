/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：促銷預覽、金額計算 (與後端 process_checkout 相同的進位規則)、優惠券 / 點數。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js。
 * 金額的算法 (含點數上限 Math.floor(remaining + 0.5)) 在 shared/totals.js，這裡只負責讀畫面上的值與寫回畫面。
 */
import {
    $, api, html, isGuest, menuItems, pageData, pro, rewardConfig, setText, state, toggleRow, ui,
} from './core.js';
import { computeTotals as sharedComputeTotals, deliveryFee as sharedDeliveryFee } from '../shared/totals.js';
import { createPreviewScheduler, payloadLine } from '../shared/orderpayload.js';
import { createPromoVerifier, renderPromoMessage } from '../shared/promo.js';

// ================================================================ 促銷活動預覽 (實際以後端結帳計算為準)
const preview = createPreviewScheduler(runPromotionPreview, 300);

export function schedulePromotionPreview() {
    preview.schedule();
}

export async function runPromotionPreview(isCurrent = () => true) {
    if (!state.cart.length) {
        state.promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
        state.eta = 0;
        renderEta();
        calculateTotal();
        return;
    }
    refreshEta(isCurrent);
    try {
        const res = await api('/api/promotions/preview', {
            json: { items: state.cart.map(payloadLine), channel: pageData.table ? 'table' : 'kiosk', order_type: $('orderType').value },
        });
        if (isCurrent()) state.promotion = res;
    } catch (err) {
        if (isCurrent()) state.promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
    }
    calculateTotal();
}

// ================================================================ 預估等候時間 (系統設定 → 廚房看板；線上端不顯示)
export function renderEta() {
    const row = $('cart-eta-row');
    if (!row) return;
    row.classList.toggle('d-none', !(state.eta > 0));
    setText('cart-eta', state.eta || 0);
}

export async function refreshEta(isCurrent = () => true) {
    if (!pro.etaEnabled) return;
    try {
        const res = await api('/api/eta', { json: { items: state.cart.map(payloadLine) } });
        if (isCurrent()) state.eta = Number(res.minutes) || 0;
    } catch (err) {
        if (isCurrent()) state.eta = 0;
    }
    renderEta();
}

// ================================================================ 金額計算 (shared/totals.js；這裡讀取畫面上的點數 / 用餐方式 / 外送區域)
export function computeTotals() {
    const subtotal = state.cart.reduce((sum, l) => sum + l.price * l.quantity, 0);
    const toggle = $('usePointsToggle');
    const input = $('pointsToUse');
    const pointsOn = !isGuest && rewardConfig.isEnabled && toggle && toggle.checked;
    const dineIn = pageData.table || $('orderType').value === '內用';
    const t = sharedComputeTotals({
        subtotal,
        promotionDiscount: state.promotion.discount,
        blocksCoupon: state.promotion.blocks_coupon,
        couponDiscount: state.appliedPromoDiscount,
        tierPct: pro.tierDiscount,
        points: pointsOn ? {
            enabled: true, available: state.userPoints, requested: input.value, ppd: rewardConfig.pointsPerDollar,
            maxDiscount: rewardConfig.maxDiscount,
        } : null,
        servicePct: pro.serviceChargePercent,
        serviceApplies: Boolean(pro.serviceChargeDineIn && dineIn),
        deliveryFee: deliveryFee(subtotal),
        spendPerPoint: rewardConfig.spendPerPoint,
        earnsPoints: !isGuest,
    });
    // 輸入的點數超過上限時直接修正輸入框
    if (pointsOn && String(t.pointsRequested) !== input.value) input.value = t.pointsRequested;
    return t;
}

/** 外送到府的外送費 (與後端相同：依配送區域，商品小計達免運門檻免收) */
export function deliveryFee(subtotal) {
    if (!pro.homeDelivery || !$('orderType') || $('orderType').value !== '外送到府') return 0;
    const opt = $('deliveryZone') && $('deliveryZone').selectedOptions[0];
    if (!opt) return 0;
    return sharedDeliveryFee(subtotal, opt.dataset.fee, pro.homeDelivery.freeOver);
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
    toggleCarbon('cart-carbon-row', 'cart-carbon');
    return t;
}

/** 購物車熱量合計 (有標示熱量的餐點才計入；後台關閉熱量顯示時隱藏) */
export function cartCalories() {
    return state.cart.reduce((sum, l) => sum + (Number((menuItems.get(Number(l.menu_item_id)) || {}).calories) || 0) * l.quantity, 0);
}

/** 購物車碳足跡合計 (g CO₂e，參考值；有標示的餐點才計入) */
export function cartCarbon() {
    return state.cart.reduce((sum, l) => sum + (Number((menuItems.get(Number(l.menu_item_id)) || {}).carbon) || 0) * l.quantity, 0);
}

export function carbonLabel(grams) {
    if (!(grams > 0)) return '';
    if (grams >= 1000) return `${String(Number((grams / 1000).toFixed(2)))} kg CO₂e`;
    return `${Math.round(grams)} g CO₂e`;
}

export function toggleCarbon(rowId, valueId) {
    const row = $(rowId);
    if (!row) return;
    const grams = pro.showCarbon ? cartCarbon() : 0;
    row.classList.toggle('d-none', !(grams > 0));
    setText(valueId, carbonLabel(grams));
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
        Kiosk.setHtml(select, html`<option value="" disabled selected>${ui.no_coupons || '-- 尚無可用優惠券 --'}</option>`);
        if (badge) Kiosk.setHtml(badge, html`<a href="/rewards" class="text-warning text-decoration-none"><i class="bi bi-gift"></i> ${ui.go_rewards || '去商城兌換'}</a>`);
        return;
    }
    const current = state.activePromoCode;
    Kiosk.setHtml(select, [
        html`<option value="">${ui.pick_coupon || '-- 選擇我的優惠券 --'}</option>`,
        list.map((c) => html`<option value="${c.code}" ${c.code === current ? 'selected' : ''}>${c.title} (${ui.code_label || '代碼'}: ${c.code})${c.min_spend > 0 ? ` - ${ui.rw_min_spend || '消費滿 $'}${Math.round(c.min_spend)}${ui.rw_usable || '可用'}` : ''}</option>`),
    ]);
    if (badge) Kiosk.setHtml(badge, html`<span class="badge bg-warning text-dark py-0">${list.length} ${ui.coupons_available || '張可用'}</span>`);
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

const promo = createPromoVerifier('/api/verify_promo');

export function showPromoMessage(ok, message) {
    renderPromoMessage($('promo-msg'), ok, message, 'small mt-1');
}

/** 驗證優惠碼 (shared/promo.js 帶請求序號：連續輸入時只採用最新一次的結果)；失敗或連線錯誤時清掉折扣 */
export async function verifyPromo(code, { silent = false } = {}) {
    const subtotal = state.cart.reduce((sum, l) => sum + l.price * l.quantity, 0);
    const res = await promo.verify({ promo_code: code, subtotal: Math.round(subtotal) });
    if (!res) return;   // 已有更新的驗證取代
    state.activePromoCode = res.valid ? code : '';
    state.appliedPromoDiscount = res.discount;
    if (!silent || !res.valid) showPromoMessage(res.valid, res.message);
    calculateTotal();
}

export function applyPromoCode() {
    const code = $('promoInput').value.trim();
    if (!code) {
        promo.invalidate();
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
