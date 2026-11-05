/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：6~7. 優惠券 / 點數折抵 / 金額計算、用餐方式 / 付款方式 / 找零 / 發票、客顯螢幕。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js。
 * 金額的算法 (含點數上限 Math.floor(remaining + 0.5))、優惠碼驗證與發票結構在 static/js/shared/，這裡只讀畫面上的值與寫回畫面。
 */
import {
    $, cartRewardPoints, cartSubtotal, channel, html, num, posCfg, publicCoupons, rewardConfig, setHtml, state,
} from './core.js';
import { repriceCart } from './menu.js';
import { computeTotals, deliveryFee, maxPointsCash } from '../shared/totals.js';
import { createPromoVerifier, renderPromoMessage } from '../shared/promo.js';
import { INVOICE_PLACEHOLDERS, invoicePayload as sharedInvoicePayload } from '../shared/invoice.js';

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

const promo = createPromoVerifier('/api/pos/verify_promo');

export function setPromoMsg(ok, text) {
    renderPromoMessage($('posPromoMsg'), ok, text, 'small mb-1 pos-xs');
}

/** 清掉優惠券狀態，並讓進行中的驗證請求失效 */
export function resetPromo() {
    promo.invalidate();
    state.promoCode = '';
    state.promoDiscount = 0;
    $('posPromoInput').value = '';
    $('posPromoMsg').textContent = '';
    $('posMemberCouponSelect').value = '';
}

export function applyPromo() {
    const code = ($('posPromoInput').value || '').trim();
    if (!code) {
        promo.invalidate();
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

/** 驗證優惠碼 (shared/promo.js 帶請求序號：只採用最新一次的結果)；網路異常時保留原狀，結帳時後端仍會重新驗證 */
export async function runPromoCheck(code) {
    const res = await promo.verify({ promo_code: code, subtotal: cartSubtotal(), user_id: state.member ? state.member.id : 0 });
    if (!res) return;   // 已有更新的請求或已被重置，丟棄過期結果
    if (res.error) {
        setPromoMsg(false, res.message);
        return;
    }
    state.promoCode = res.valid ? code : '';
    state.promoDiscount = res.discount;
    setPromoMsg(res.valid, res.message);
    recalcTotals();
}

export function togglePoints() {
    const toggle = $('posUsePointsToggle');
    const input = $('posPointsInput');
    if (!toggle || !input) return;
    if (toggle.checked && state.member) {
        input.disabled = false;
        const ppd = Math.max(1, num(rewardConfig.pointsPerDollar) || 1);
        const maxCash = maxPointsCash(Math.max(0, cartSubtotal() - state.promoDiscount), rewardConfig.maxDiscount);
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

/** 與後端相同的計算順序：小計 → 促銷 → 優惠券 → 會員折扣 → 整單折扣 → 點數 → 服務費 (shared/totals.js) */
export function recalcTotals() {
    const subtotal = cartSubtotal();
    const toggle = $('posUsePointsToggle');
    const input = $('posPointsInput');
    const pointsOn = Boolean(state.member && rewardConfig.isEnabled && toggle && toggle.checked && input);
    const t = computeTotals({
        subtotal,
        promotionDiscount: state.promotion.discount,
        blocksCoupon: state.promotion.blocks_coupon,
        couponDiscount: state.promoDiscount,
        tierPct: state.member ? state.member.tier_discount : 0,
        manual: state.orderDiscount,
        points: pointsOn ? {
            enabled: true, available: state.member.points - cartRewardPoints(), requested: input.value,
            ppd: rewardConfig.pointsPerDollar, maxDiscount: rewardConfig.maxDiscount,
        } : null,
        servicePct: posCfg.serviceChargePercent,
        serviceApplies: serviceChargeApplies(),
        deliveryFee: homeDeliveryFee(subtotal),   // 外送到府：依配送區域加外送費 (商品小計達免運門檻免收)
        earnsPoints: false,
    });
    if (pointsOn) input.value = t.pointsRequested;
    if (state.member) $('posMemberPoints').textContent = Math.max(0, state.member.points - cartRewardPoints());
    state.finalTotal = t.final;
    state.totals = {
        subtotal, promotion: t.promotionDiscount, coupon: t.promo, tierDiscount: t.tier, manual: t.manual, pointsDiscount: t.pointsDiscount,
        service: t.service, delivery: t.delivery,
    };
    $('posSubtotal').textContent = Math.round(subtotal);
    $('posPromoDiscount').textContent = Math.round(t.promo);
    $('posPointsDiscount').textContent = Math.round(t.pointsDiscount);
    Kiosk.toggleRow('posPromotionRow', 'posPromotionDiscount', t.promotionDiscount);
    if ($('posPromotionNames')) $('posPromotionNames').textContent = (state.promotion.names || []).join('、');
    Kiosk.toggleRow('posTierRow', 'posTierDiscount', t.tier);
    Kiosk.toggleRow('posManualRow', 'posManualDiscount', t.manual);
    Kiosk.toggleRow('posServiceRow', 'posServiceCharge', t.service);
    Kiosk.toggleRow('posDeliveryFeeRow', 'posDeliveryFee', t.delivery);
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
    return opt ? deliveryFee(subtotal, opt.dataset.fee, posCfg.homeDelivery.freeOver) : 0;
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

export function onInvoiceTypeChange() {
    const type = $('posInvoiceType').value;
    const input = $('posInvoiceValue');
    input.classList.toggle('d-none', !INVOICE_PLACEHOLDERS[type]);
    input.placeholder = INVOICE_PLACEHOLDERS[type] || '';
}

/** 電子發票 (shared/invoice.js)：載具 / 統編 / 愛心碼 */
export function invoicePayload() {
    const sel = $('posInvoiceType');
    return sel ? sharedInvoicePayload(sel.value, $('posInvoiceValue').value) : null;
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
