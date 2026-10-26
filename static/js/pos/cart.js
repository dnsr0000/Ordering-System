/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：4. 購物車與條件加購、促銷活動預覽。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import {
    $, api, cartRewardPoints, channel, displayName, html, itemMap, NON_MAIN_CATEGORIES, num, posCfg, setHtml, state,
    sumBy,
} from './core.js';
import { startConfigure } from './menu.js';
import { recalcTotals, resetPromo, revalidatePromo } from './totals.js';
import { payloadLine } from './checkout.js';

// ======================================================================
// 4. 購物車與條件加購 (每一份合格正價餐點最多搭配一份加購，與後端 _validate_addon_quota 相同)
// ======================================================================
export function lineQualifiesForAddon(line, addon) {
    if (!line || line.is_add_on || line.is_reward || line.can_addon === false) return false;
    const cat = (line.category || '未分類').trim();
    const trigger = addon.addon_trigger_type || 'any';
    const target = (addon.addon_trigger_target || '').trim().toLowerCase();
    if (trigger === 'category') return cat.toLowerCase() === (target || (addon.category || '').trim().toLowerCase());
    // 後台存餐點名稱，舊資料可能存餐點 ID；兩種都接受
    if (trigger === 'item') return String(line.menu_item_id) === target || line.name.trim().toLowerCase() === target;
    return !NON_MAIN_CATEGORIES.includes(cat);
}

export function mainUnits() {
    const units = [];
    state.cart.forEach((l) => {
        if (l.is_add_on || l.is_reward || l.can_addon === false) return;
        for (let k = 0; k < l.quantity; k++) units.push(l);
    });
    return units;
}

/** 二分圖配對：addonDefs 中每一份加購是否都能配到一份合格的正價餐點 */
export function addonsFeasible(addonDefs) {
    const mains = mainUnits();
    if (addonDefs.length > mains.length) return false;
    const owner = new Array(mains.length).fill(-1);
    const tryAssign = (a, seen) => {
        for (let m = 0; m < mains.length; m++) {
            if (seen[m] || !lineQualifiesForAddon(mains[m], addonDefs[a])) continue;
            seen[m] = true;
            if (owner[m] === -1 || tryAssign(owner[m], seen)) {
                owner[m] = a;
                return true;
            }
        }
        return false;
    };
    for (let a = 0; a < addonDefs.length; a++) {
        if (!tryAssign(a, new Array(mains.length).fill(false))) return false;
    }
    return true;
}

export function availableAddons() {
    return [...itemMap.values()].filter((it) => it.can_be_add_on && it.is_available);
}

export function cartAddonDefs() {
    const defs = [];
    state.cart.filter((l) => l.is_add_on).forEach((l) => {
        const def = itemMap.get(l.menu_item_id);
        if (def) for (let k = 0; k < l.quantity; k++) defs.push(def);
    });
    return defs;
}

/** 此加購品目前還能再加幾份 (不破壞既有加購配對) */
export function remainingAddonQuota(addon) {
    const current = cartAddonDefs();
    let n = 0;
    while (addonsFeasible(current.concat(new Array(n + 1).fill(addon)))) n++;
    return n;
}

export function triggerAddon(btn) {
    const item = itemMap.get(Number(btn.dataset.id));
    if (!item || !item.can_be_add_on || remainingAddonQuota(item) <= 0) {
        alert('⚠️ 加購名額已達上限！');
        return;
    }
    startConfigure({ item, combo: null, mode: 'addon' });
}

export const lineKey = (l) => JSON.stringify([l.menu_item_id, l.combo_option_id, l.customization, l.is_add_on, l.is_reward, l.modifier_ids,
    l.combo_choices || [], l.pass_id || 0]);

export function addLine(line) {
    const existing = state.cart.find((l) => lineKey(l) === lineKey(line));
    if (existing) existing.quantity += 1;
    else state.cart.push(line);
    renderCart();
}

export function changeQty(idx, delta) {
    const line = state.cart[idx];
    if (!line) return;
    if (delta > 0 && line.is_reward) {
        if (!state.member || (state.member.points - cartRewardPoints()) < line.points_cost) {
            alert('⚠️ 會員剩餘紅利點數不足，無法再增加兌換數量！');
            return;
        }
    }
    if (delta > 0 && line.is_add_on) {
        const def = itemMap.get(line.menu_item_id);
        if (!def || remainingAddonQuota(def) <= 0) {
            alert('⚠️ 加購名額已達上限！');
            return;
        }
    }
    line.quantity += delta;
    if (line.quantity <= 0) state.cart.splice(idx, 1);
    renderCart();
}

export function clearCart() {
    if (state.cart.length === 0) return;
    state.cart = [];
    resetPromo();
    renderCart();
}

/** 依購物車順序逐份保留加購，配不到合格餐點的加購自動減量 */
export function enforceAddonQuota() {
    const kept = [];
    state.cart.forEach((line) => {
        if (!line.is_add_on) return;
        const def = itemMap.get(line.menu_item_id);
        let allowed = 0;
        if (def) {
            for (let k = 0; k < line.quantity; k++) {
                if (!addonsFeasible(kept.concat([def]))) break;
                kept.push(def);
                allowed++;
            }
        }
        line.quantity = allowed;
    });
    state.cart = state.cart.filter((l) => l.quantity > 0);
}

export function renderCart({ revalidate = true } = {}) {
    enforceAddonQuota();
    const listEl = $('posCartList');
    const addonBox = $('posAddonRecommendBox');
    $('posCartCount').textContent = sumBy(state.cart, (l) => l.quantity);

    if (state.cart.length === 0) {
        setHtml(listEl, html`<div class="text-center text-white-50 py-4 small">尚未點選任何餐點</div>`);
        addonBox.textContent = '';
        recalcTotals();
        return;
    }

    setHtml(listEl, state.cart.map((line, idx) => html`
        <div class="pos-cart-row d-flex justify-content-between align-items-center py-1" data-cart-idx="${idx}">
            <div class="pe-2">
                <div class="fw-bold text-white small">
                    <span class="pos-cart-name">${displayName(line)}</span>
                    ${line.is_add_on ? html`<span class="badge bg-info text-dark ms-1 pos-xxs">加購</span>` : ''}
                    ${line.is_reward ? html`<span class="badge bg-warning text-dark ms-1 pos-xxs">-${line.points_cost}點</span>` : ''}
                </div>
                ${line.customization ? html`<div class="text-info pos-cart-custom">${line.customization}</div>` : ''}
                <div class="text-white-50 pos-xs">${line.original_price !== undefined ? html`<del class="me-1">$${line.original_price}</del>` : ''}$${line.price} x ${line.quantity} = <strong class="text-warning">$${line.price * line.quantity}</strong></div>
            </div>
            <div class="btn-group btn-group-sm flex-shrink-0">
                ${posCfg.allowPriceOverride && !line.is_reward ? html`<button type="button" class="btn btn-outline-warning px-2 py-0" data-action="priceLine" data-idx="${idx}" title="改價" aria-label="改價"><i class="bi bi-pencil"></i></button>` : ''}
                <button type="button" class="btn btn-outline-secondary text-white px-2 py-0" data-action="cartQty" data-idx="${idx}" data-delta="-1" aria-label="減少">-</button>
                <button type="button" class="btn btn-outline-secondary text-white px-2 py-0" data-action="cartQty" data-idx="${idx}" data-delta="1" aria-label="增加">+</button>
                <button type="button" class="btn btn-outline-danger px-2 py-0" data-action="cartQty" data-idx="${idx}" data-delta="-999" aria-label="移除"><i class="bi bi-x"></i></button>
            </div>
        </div>`));

    const eligible = availableAddons()
        .map((a) => ({ addon: a, quota: remainingAddonQuota(a) }))
        .filter((x) => x.quota > 0);
    if (eligible.length > 0) {
        setHtml(addonBox, html`
            <div class="p-2 rounded bg-dark border border-secondary mb-2">
                <div class="small fw-bold text-info mb-1 pos-cart-custom"><i class="bi bi-stars me-1"></i>可加購優惠品</div>
                <div class="d-flex gap-1 overflow-x-auto pb-1">
                    ${eligible.map(({ addon, quota }) => html`
                        <button type="button" class="btn btn-sm btn-outline-info py-0 px-2 flex-shrink-0 text-start pos-cart-custom"
                                data-action="addon" data-id="${addon.id}">
                            + ${addon.name} <strong class="text-warning">$${num(addon.add_on_price)}</strong> <small>(餘${quota})</small>
                        </button>`)}
                </div>
            </div>`);
    } else {
        addonBox.textContent = '';
    }

    recalcTotals();
    if (revalidate) revalidatePromo();
    schedulePromotionPreview();
}

// ---------------------------------------------------------------- 促銷活動預覽 (結帳時以後端計算為準)
export let promotionTimer = null;
export function schedulePromotionPreview() {
    clearTimeout(promotionTimer);
    promotionTimer = setTimeout(runPromotionPreview, 250);
}

export async function runPromotionPreview() {
    const seq = ++state.promotionSeq;
    if (!state.cart.length || state.deliveryMode) {
        state.promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
        recalcTotals();
        return;
    }
    try {
        const res = await api('/api/promotions/preview', {
            json: { items: state.cart.map(payloadLine), channel: 'pos', order_type: channel() === 'takeout' ? '外帶' : '內用',
                    user_id: state.member ? state.member.id : 0 },
        });
        if (seq !== state.promotionSeq) return;
        state.promotion = res;
    } catch (err) {
        if (seq !== state.promotionSeq) return;
        state.promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
    }
    recalcTotals();
}
