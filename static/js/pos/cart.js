/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：4. 購物車與條件加購、促銷活動預覽。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js，加購配額與合併規則在 static/js/shared/ (與顧客點餐頁相同)。
 */
import { $, api, cartRewardPoints, channel, displayName, html, itemMap, num, posCfg, setHtml, state, sumBy } from './core.js';
import { startConfigure } from './menu.js';
import { recalcTotals, resetPromo, revalidatePromo } from './totals.js';
import * as addons from '../shared/addons.js';
import { mergeLine } from '../shared/cartline.js';
import { createPreviewScheduler, payloadLine } from '../shared/orderpayload.js';

// ======================================================================
// 4. 購物車與條件加購 (每一份合格正價餐點最多搭配一份加購，與後端 _validate_addon_quota 相同；shared/addons.js)
// ======================================================================
export function availableAddons() {
    return addons.filterAddons(itemMap.values());
}

// 加購品的定義一律從「可加購且有供應」的清單找 (售完或關閉加購的品項視為不存在，數量會被自動調整)；
// 查表每次配額計算只建一次，不在迴圈裡重複過濾整份菜單
const addonDef = (id) => availableAddons().find((a) => Number(a.id) === Number(id));
function addonRules() {
    const defs = new Map(availableAddons().map((a) => [Number(a.id), a]));
    return { categoryOf: (line) => line.category || '未分類', addonDef: (id) => defs.get(Number(id)) };
}

/** 此加購品目前還能再加幾份 (不破壞既有加購配對) */
export function remainingAddonQuota(addon) {
    return addons.remainingAddonQuota(state.cart, addon, addonRules());
}

export function triggerAddon(btn) {
    const item = itemMap.get(Number(btn.dataset.id));
    if (!item || !item.can_be_add_on || remainingAddonQuota(item) <= 0) {
        alert('⚠️ 加購名額已達上限！');
        return;
    }
    startConfigure({ item, combo: null, mode: 'addon' });
}

/** 加入購物車 (同一品項同樣客製化 / 加料時合併數量，加料順序不影響) */
export function addLine(line) {
    mergeLine(state.cart, line);
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
        const def = addonDef(line.menu_item_id);
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
    state.orderDiscount = null;   // 整單折扣跟著這張單走，不可帶到下一位顧客
    resetPromo();
    renderCart();
}

/** 依購物車順序逐份保留加購，配不到合格餐點的加購自動減量 */
export function enforceAddonQuota() {
    state.cart = addons.enforceAddonQuota(state.cart, addonRules()).lines;
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

// ---------------------------------------------------------------- 促銷活動預覽 (結帳時以後端計算為準；shared/orderpayload.js 的排程)
const preview = createPreviewScheduler(runPromotionPreview, 250);

export function schedulePromotionPreview() {
    preview.schedule();
}

export async function runPromotionPreview(isCurrent = () => true) {
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
        if (!isCurrent()) return;
        state.promotion = res;
    } catch (err) {
        if (!isCurrent()) return;
        state.promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
    }
    recalcTotals();
}
