/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：6~7. 優惠券 / 點數折抵 / 金額計算、用餐方式 / 付款方式 / 找零 / 發票、客顯螢幕。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import {
    $, api, cartRewardPoints, cartSubtotal, channel, html, num, posCfg, publicCoupons, rewardConfig, setHtml, state,
} from './core.js';
import { repriceCart } from './menu.js';

// ======================================================================
// 6. 優惠券 / 點數折抵 / 金額計算
// ======================================================================
export const minSpendText = (c) => (num(c.min_spend) > 0 ? ` (滿$${Math.round(num(c.min_spend))})` : '');

export function populateCouponSelect() {
    const sel = $('posMemberCouponSelect');
    const memberCoupons = (state.member && state.member.coupons) || [];
    setHtml(sel, [
        html`<option value="">-- 選擇優惠券 --</option>`,
        memberCoupons.map((c) => html`<option value="${c.code}">${c.title}${minSpendText(c)}</option>`),
        publicCoupons.map((c) => html`<option value="${c.code}">[公開] ${c.label ? `[${c.label}] ` : ''}${c.title}${minSpendText(c)}</option>`),
    ]);
    sel.value = state.promoCode && [...sel.options].some((o) => o.value === state.promoCode) ? state.promoCode : '';
}

export function setPromoMsg(ok, text) {
    const el = $('posPromoMsg');
    el.className = `small mb-1 pos-xs ${ok ? 'text-success' : 'text-danger'}`;
    setHtml(el, text ? html`<i class="bi ${ok ? 'bi-check-circle' : 'bi-x-circle'} me-1"></i>${text}` : '');
}

/** 清掉優惠券狀態，並讓進行中的驗證請求失效 */
export function resetPromo() {
    state.promoSeq++;
    state.promoCode = '';
    state.promoDiscount = 0;
    $('posPromoInput').value = '';
    $('posPromoMsg').textContent = '';
    $('posMemberCouponSelect').value = '';
}

export function applyPromo() {
    const code = ($('posPromoInput').value || '').trim();
    if (!code) {
        state.promoSeq++;
        state.promoCode = '';
        state.promoDiscount = 0;
        $('posPromoMsg').textContent = '';
        recalcTotals();
        return;
    }
    runPromoCheck(code);
}

/** 購物車變動後重新驗證已套用的優惠券 (門檻與百分比折扣都會隨小計改變) */
export function revalidatePromo() {
    if (state.promoCode && state.cart.length) runPromoCheck(state.promoCode);
}

export async function runPromoCheck(code) {
    const seq = ++state.promoSeq;
    try {
        const res = await api('/api/pos/verify_promo', {
            json: { promo_code: code, subtotal: cartSubtotal(), user_id: state.member ? state.member.id : 0 },
        });
        if (seq !== state.promoSeq) return;   // 已有更新的請求或已被重置，丟棄過期結果
        state.promoCode = res.valid ? code : '';
        state.promoDiscount = res.valid ? num(res.discount) : 0;
        setPromoMsg(res.valid, res.message);
        recalcTotals();
    } catch (err) {
        // 網路異常時保留原狀；結帳時後端仍會重新驗證優惠券
        if (seq === state.promoSeq) setPromoMsg(false, err.message);
    }
}

export function maxPointsCash(remaining) {
    let maxCash = Math.floor(remaining);
    if (num(rewardConfig.maxDiscount) > 0) maxCash = Math.min(maxCash, num(rewardConfig.maxDiscount));
    return maxCash;
}

export function togglePoints() {
    const toggle = $('posUsePointsToggle');
    const input = $('posPointsInput');
    if (!toggle || !input) return;
    if (toggle.checked && state.member) {
        input.disabled = false;
        const ppd = Math.max(1, num(rewardConfig.pointsPerDollar) || 1);
        const maxCash = maxPointsCash(Math.max(0, cartSubtotal() - state.promoDiscount));
        const availPts = Math.max(0, state.member.points - cartRewardPoints());
        const maxPts = Math.min(availPts, maxCash * ppd);
        input.value = Math.floor(maxPts / ppd) * ppd;
    } else {
        input.disabled = true;
        input.value = 0;
    }
    recalcTotals();
}

export function serviceChargeApplies() {
    const pct = num(posCfg.serviceChargePercent);
    return pct > 0 && !state.deliveryMode && channel() === 'dine_in'
        && (posCfg.serviceChargeScope || []).includes('pos_dine_in');
}

/** 與後端相同的計算順序：小計 → 促銷 → 優惠券 → 會員折扣 → 整單折扣 → 點數 → 服務費 */
export function recalcTotals() {
    const subtotal = cartSubtotal();
    const promotion = Math.min(num(state.promotion.discount), subtotal);
    const afterPromotion = Math.max(0, subtotal - promotion);
    const coupon = state.promotion.blocks_coupon ? 0 : Math.min(state.promoDiscount, afterPromotion);
    let remaining = Math.max(0, afterPromotion - coupon);
    const tierPct = state.member ? num(state.member.tier_discount) : 0;
    const tierDiscount = tierPct > 0 ? remaining * Math.min(100, tierPct) / 100 : 0;
    remaining = Math.max(0, remaining - tierDiscount);
    let manual = 0;
    if (state.orderDiscount) {
        const v = num(state.orderDiscount.value);
        manual = state.orderDiscount.type === 'percent' ? remaining * Math.min(100, v) / 100 : Math.min(v, remaining);
        remaining = Math.max(0, remaining - manual);
    }
    let pointsDiscount = 0;
    const toggle = $('posUsePointsToggle');
    const input = $('posPointsInput');
    if (state.member && rewardConfig.isEnabled && toggle && toggle.checked && input) {
        const ppd = Math.max(1, num(rewardConfig.pointsPerDollar) || 1);
        const maxCash = maxPointsCash(remaining);
        const availPts = Math.max(0, state.member.points - cartRewardPoints());
        const val = Math.min(Math.max(0, num(input.value)), Math.min(availPts, maxCash * ppd));
        input.value = val;
        pointsDiscount = Math.min(Math.floor(val / ppd), maxCash);
    }
    if (state.member) $('posMemberPoints').textContent = Math.max(0, state.member.points - cartRewardPoints());
    const afterDiscounts = Math.max(0, remaining - pointsDiscount);
    const service = serviceChargeApplies() ? Math.floor(afterDiscounts * num(posCfg.serviceChargePercent) / 100 + 0.5) : 0;

    // 外送到府：依配送區域加外送費 (商品小計達免運門檻免收)
    const delivery = homeDeliveryFee(subtotal);
    // 與後端相同：應付金額四捨五入到整數元
    state.finalTotal = Math.floor(afterDiscounts + 0.5) + service + delivery;
    state.totals = { subtotal, promotion, coupon, tierDiscount, manual, pointsDiscount, service, delivery };
    $('posSubtotal').textContent = Math.round(subtotal);
    $('posPromoDiscount').textContent = Math.round(coupon);
    $('posPointsDiscount').textContent = Math.round(pointsDiscount);
    const toggleRow = (id, valueId, value) => {
        const row = $(id);
        if (!row) return;
        row.classList.toggle('d-none', !(value > 0));
        $(valueId).textContent = Math.round(value);
    };
    toggleRow('posPromotionRow', 'posPromotionDiscount', promotion);
    if ($('posPromotionNames')) $('posPromotionNames').textContent = (state.promotion.names || []).join('、');
    toggleRow('posTierRow', 'posTierDiscount', tierDiscount);
    toggleRow('posManualRow', 'posManualDiscount', manual);
    toggleRow('posServiceRow', 'posServiceCharge', service);
    toggleRow('posDeliveryFeeRow', 'posDeliveryFee', delivery);
    if ($('posServicePercent')) $('posServicePercent').textContent = num(posCfg.serviceChargePercent);
    const gifts = state.promotion.gifts || [];
    setHtml($('posGiftList'), gifts.map((gft) => html`<div><i class="bi bi-gift me-1"></i>贈送 ${gft.name} x${gft.quantity} (${gft.promotion})</div>`));
    $('posFinalTotal').textContent = state.finalTotal;
    updateCashChange();
}

// ======================================================================
// 7. 用餐方式 / 付款方式 / 現金找零
// ======================================================================
export function isHomeDelivery() {
    return Boolean($('posTypeHome') && $('posTypeHome').checked);
}

export function homeDeliveryFee(subtotal) {
    if (!isHomeDelivery() || !posCfg.homeDelivery) return 0;
    const opt = $('posHomeZone').selectedOptions[0];
    const freeOver = num(posCfg.homeDelivery.freeOver);
    return !opt || (freeOver && subtotal >= freeOver) ? 0 : num(opt.dataset.fee);
}

export function onOrderTypeChange() {
    $('posCutleryBox').classList.toggle('d-none', !$('posTypeTakeout').checked && !isHomeDelivery());
    if ($('posHomeBox')) $('posHomeBox').classList.toggle('d-none', !isHomeDelivery());
    repriceCart();
}

export function setTable(value) {
    $('posTableInput').value = value;
    if (value === '外帶客') {
        $('posTypeTakeout').checked = true;
        onOrderTypeChange();
    }
}

export function onPaymentChange() {
    const method = $('posPaymentMethod').value;
    const isCash = method === 'Cash';
    $('posCashInputCol').classList.toggle('pos-invisible', !isCash);
    $('posQuickCashRow').classList.toggle('d-none', !isCash);
    $('posCardRow').classList.toggle('d-none', method !== 'Credit Card');
    $('posLinePayHint').classList.toggle('d-none', !(method === 'Line Pay' && posCfg.linePayIntegrated));
}

export const INVOICE_PLACEHOLDERS = { mobile: '手機條碼 (/ABC1234)', company: '統一編號 8 碼', donate: '愛心碼', citizen: '自然人憑證條碼' };
export function onInvoiceTypeChange() {
    const type = $('posInvoiceType').value;
    const input = $('posInvoiceValue');
    input.classList.toggle('d-none', !INVOICE_PLACEHOLDERS[type]);
    input.placeholder = INVOICE_PLACEHOLDERS[type] || '';
}

export function invoicePayload() {
    const sel = $('posInvoiceType');
    if (!sel || !sel.value) return null;
    const value = ($('posInvoiceValue').value || '').trim();
    const type = sel.value;
    if (type === 'company') return { type, buyer_id: value };
    if (type === 'donate') return { type, love_code: value };
    if (type === 'mobile' || type === 'citizen') return { type, carrier: value };
    return { type };
}

export function quickCash(mode) {
    const input = $('posCashReceived');
    if (mode === 'exact') input.value = state.finalTotal;
    else if (mode === '+100') input.value = num(input.value) + 100;
    else input.value = num(mode);
    updateCashChange();
}

// 客顯螢幕 (/pos/display，同一台電腦的第二個視窗)：購物車、應付金額、收款與找零即時同步
export const displayChannel = 'BroadcastChannel' in window ? new BroadcastChannel('pos-customer-display') : null;

export function broadcastCart() {
    if (!displayChannel) return;
    const isCash = $('posPaymentMethod') && $('posPaymentMethod').value === 'Cash';
    displayChannel.postMessage({
        kind: 'cart',
        total: state.finalTotal || 0,
        totals: state.totals || {},
        cash: isCash ? num($('posCashReceived').value) : 0,
        member: state.member ? { name: state.member.name, points: state.member.points } : null,
        lines: state.cart.map((l) => ({ name: l.name, detail: l.customization || '', quantity: l.quantity,
                                        subtotal: num(l.price) * num(l.quantity) })),
    });
}

export function broadcastPaid(res) {
    if (!displayChannel) return;
    displayChannel.postMessage({ kind: 'paid', pickup: res.pickup_number || res.order_id, change: num(res.cash_change) });
}

if (displayChannel) {
    displayChannel.onmessage = (ev) => { if (ev.data && ev.data.kind === 'hello') broadcastCart(); };
}

export function openCustomerDisplay() {
    window.open('/pos/display', 'pos-customer-display', 'popup,width=1024,height=768');
}

export function updateCashChange() {
    broadcastCart();
    const received = num($('posCashReceived').value);
    const badge = $('posChangeDisplay');
    const base = 'input-group-text bg-dark border-secondary fw-bold';
    if (received <= 0) {
        badge.className = `${base} text-white-50`;
        badge.textContent = '找零 $0';
    } else if (received < state.finalTotal) {
        badge.className = `${base} text-danger`;
        badge.textContent = `差 $${state.finalTotal - received}`;
    } else {
        badge.className = `${base} text-success`;
        badge.textContent = `找零 $${received - state.finalTotal}`;
    }
}
