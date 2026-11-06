/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：購物車資料 (正規化、加入、加購配額、重算價格) 與購物車畫面。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js，加購配額 / 單價 / 合併規則在 static/js/shared/。
 */
import {
    $, REWARD_LABEL, REWARD_TAG, api, availableAddons, html, menuItems, modifierById, priceChannel, isGuest, setText, state, storeCart, ui,
} from './core.js';
import { calculateTotal, schedulePromotionPreview, setUserPoints } from './totals.js';
import * as addons from '../shared/addons.js';
import { comboChoiceInfo, modifiersTotal, unitPrice } from '../shared/pricing.js';
import { mergeLine } from '../shared/cartline.js';

// ================================================================ 購物車資料
/** 把舊版或紅利商城寫入的品項轉成目前格式；無法辨識的品項丟棄 */
export function normalizeLine(raw) {
    if (!raw || typeof raw !== 'object') return null;
    let id = Number(raw.menu_item_id);
    if (!id && raw.id !== undefined) {
        const legacy = String(raw.id);
        id = Number(legacy.startsWith('reward_') ? legacy.split('_')[1] : legacy);
    }
    if (!id) id = Number(raw.reward_item_id || raw.item_id);
    if (!id) return null;
    const isReward = Boolean(raw.is_reward) || String(raw.id || '').startsWith('reward_');
    const item = menuItems.get(id);
    return {
        menu_item_id: id,
        name: String(raw.name || (item ? item.name : '') || '').replace(REWARD_LABEL, '').trim(),
        price: Number(raw.price) || 0,
        quantity: Math.max(1, parseInt(raw.quantity, 10) || 1),
        customization: String(raw.customization || ''),
        size: String(raw.size || ''),
        modifier_ids: (raw.modifier_ids || []).map(Number).filter(Boolean),
        combo_option_id: Number(raw.combo_option_id) || null,
        combo_name: String(raw.combo_name || ''),
        combo_choices: (raw.combo_choices || []).map(Number).filter(Boolean),
        pass_id: Number(raw.pass_id) || 0,
        choice_labels: (raw.choice_labels || []).map(String),
        is_add_on: raw.is_add_on === true || raw.is_add_on === 'true',
        is_reward: isReward,
        points_cost: Number(raw.points_cost) || 0,
        can_addon: raw.can_addon !== false,
        // 菜單已顯示售完的品項直接標示，避免送出後才被擋下
        is_sold_out: Boolean(raw.is_sold_out) || (item ? !item.is_available : false),
    };
}

/** 購物車品項的顯示說明：[套餐: X]、客製化文字、加料(+N) (與後端產生的明細相同) */
export function lineDescription(line) {
    const parts = [];
    if (line.pass_id) parts.push(`[${ui.cart_pass_tag || '寄杯'}]`);
    if (line.combo_name) parts.push(`[${ui.cart_combo_tag || '套餐'}: ${line.combo_name}]`);
    parts.push(...(line.choice_labels || []));
    if (line.size) parts.push(line.size);
    if (line.customization) parts.push(line.customization);
    line.modifier_ids.forEach((id) => {
        const m = modifierById.get(id);
        if (m) parts.push(`${m.name}(+${m.price})`);
    });
    return parts.join('、');
}

export function lineCategory(line) {
    const item = menuItems.get(line.menu_item_id);
    return item ? item.category : '未分類';
}

export function saveCart() {
    state.checkoutOrderId = null;   // 內容變了：下一次結帳是新的訂單
    storeCart(state.cart);
}

export function makeLine(item, { combo = null, isAddon = false, customization = '', modifierIds = [], size = '', choiceIds = [],
                          passId = 0 } = {}) {
    const choice = comboChoiceInfo(combo, choiceIds);
    const modifierTotal = modifiersTotal(modifierIds, modifierById);
    let price;
    if (passId) price = modifierTotal;   // 寄杯兌換：免費 (只收加料)
    else if (isAddon) price = (Number(item.add_on_price) || 0) + modifierTotal;
    else price = unitPrice({ item, channel: priceChannel(), isMember: !isGuest, size, combo, choiceIds, modifierTotal });
    return {
        menu_item_id: Number(item.id),
        name: item.name,
        price,
        quantity: 1,
        customization,
        size: isAddon ? '' : size,
        modifier_ids: modifierIds,
        combo_option_id: combo ? combo.id : null,
        combo_name: combo ? combo.name : '',
        combo_choices: choice.ids,
        choice_labels: choice.labels,
        pass_id: passId || 0,
        is_add_on: isAddon,
        is_reward: false,
        points_cost: 0,
        can_addon: isAddon || passId ? false : (combo ? combo.can_addon !== false : true),
        is_sold_out: false,
    };
}

/** 加入購物車 (同一品項同樣客製化時合併數量) */
export function addLine(line) {
    mergeLine(state.cart, line);
    saveCart();
    updateCartUI();
}

// ================================================================ 加購配額 (shared/addons.js；規則與後端一致)
/** 配額計算用的查表 (可加購且有供應的品項 id → 定義)；每次計算只建一次，不在迴圈裡重複過濾整份菜單 */
function addonRules() {
    const defs = new Map(availableAddons().map((a) => [Number(a.id), a]));
    return { categoryOf: lineCategory, addonDef: (id) => defs.get(Number(id)) };
}

/** 在不破壞既有加購配對的前提下，該加購品還能再加幾份 */
export function remainingAddonQuota(addon) {
    return addons.remainingAddonQuota(state.cart, addon, addonRules());
}

/** 依購物車順序逐份保留加購品，無法配對到合格餐點的部分自動減量；有調整時回傳 true */
export function enforceAddonQuota() {
    const result = addons.enforceAddonQuota(state.cart, addonRules());
    state.cart = result.lines;
    return result.changed;
}

/** 內用 / 外帶切換 (或菜單改價) 時，一般品項改用該通路的價格；加購品與寄杯只重算加料 */
export function repriceCart() {
    state.cart.forEach((l) => {
        if (l.is_reward) return;
        const modifierTotal = modifiersTotal(l.modifier_ids, modifierById);
        if (l.pass_id) {
            l.price = modifierTotal;
            return;
        }
        const item = menuItems.get(l.menu_item_id);
        if (!item) return;
        if (l.is_add_on) {
            l.price = (Number(item.add_on_price) || 0) + modifierTotal;
            return;
        }
        const combo = l.combo_option_id ? (item.combos || []).find((c) => Number(c.id) === Number(l.combo_option_id)) : null;
        l.price = unitPrice({ item, channel: priceChannel(), isMember: !isGuest, size: l.size, combo, choiceIds: l.combo_choices || [], modifierTotal });
    });
    saveCart();
    updateCartUI();
}

// ================================================================ 購物車畫面
export function cartLineHtml(line, idx) {
    const soldOut = line.is_sold_out;
    const desc = lineDescription(line);
    const action = (soldOut || line.is_reward)
        ? html`<button class="btn btn-sm btn-outline-danger fw-bold" type="button" data-action="remove" data-index="${idx}"><i class="bi bi-trash"></i> ${ui.remove || '移除'}</button>`
        : html`<div class="btn-group btn-group-sm">
                <button class="btn btn-outline-secondary text-white" type="button" data-action="qty" data-index="${idx}" data-delta="-1" aria-label="${ui.decrease || '減少'}">-</button>
                <button class="btn btn-outline-secondary text-white" type="button" data-action="qty" data-index="${idx}" data-delta="1" aria-label="${ui.increase || '增加'}">+</button>
            </div>`;
    return html`
        <div class="cart-line d-flex justify-content-between align-items-center mb-2 border-bottom border-secondary pb-2 ${soldOut ? 'bg-danger bg-opacity-10 p-2 rounded' : ''}">
            <div>
                <div class="fw-bold ${soldOut ? 'text-decoration-line-through text-danger' : 'text-warning'}">
                    ${line.is_reward ? `${REWARD_TAG} ` : ''}<span class="cart-line-name">${line.name}</span>
                    ${line.is_add_on ? html`<span class="badge bg-info text-dark ms-1">${ui.addon_badge || '加購'}</span>` : ''}
                    ${soldOut ? html`<span class="badge bg-danger ms-1">${ui.sold_out || '已售完'}</span>` : ''}
                </div>
                ${desc ? html`<small class="d-block ${soldOut ? 'text-decoration-line-through text-white-50' : 'text-info'}">${desc}</small>` : ''}
                <small class="text-white-50 ${soldOut ? 'text-decoration-line-through' : ''}">${Kiosk.money(line.price)} x ${line.quantity}</small>
            </div>
            <div class="d-flex align-items-center">${action}</div>
        </div>`;
}

export function addonRecommendHtml() {
    const eligible = availableAddons()
        .map((a) => ({ addon: a, quota: remainingAddonQuota(a) }))
        .filter((x) => x.quota > 0);
    if (!eligible.length) return '';
    return html`
        <div class="mb-2 p-2 bg-secondary bg-opacity-10 rounded border border-secondary">
            <div class="text-warning small fw-bold mb-2"><i class="bi bi-stars"></i> ${ui.addon_offer || '專屬加購優惠'}</div>
            <div class="d-flex gap-2 overflow-auto pb-1 addon-strip">
                ${eligible.map(({ addon, quota }) => html`
                    <div class="card bg-dark border-secondary p-2 d-inline-block shadow-sm flex-shrink-0 addon-card">
                        <div class="small fw-bold text-white text-truncate" title="${addon.name}">${addon.name}</div>
                        <div class="text-info cart-tiny">${(ui.addon_quota || '尚可加購 {n} 份').replace('{n}', quota)}</div>
                        <div class="mt-1">
                            <del class="text-white-50 cart-tiny">$${addon.price}</del>
                            <span class="text-danger fw-bold small">$${addon.add_on_price}</span>
                        </div>
                        <button class="btn btn-sm btn-outline-info w-100 py-0 mt-1" type="button" data-action="addon" data-id="${addon.id}">${ui.addon_badge || '加購'}</button>
                    </div>`)}
            </div>
        </div>`;
}

export function updateCartUI() {
    if (enforceAddonQuota()) {
        saveCart();
        setTimeout(() => alert(`⚠️ ${ui.addon_adjusted || '購物車餐點異動導致加購品名額不足，已自動調整數量！'}`), 100);
    }
    setText('cart-count', state.cart.reduce((sum, l) => sum + l.quantity, 0));
    const container = $('cart-items');
    if (!state.cart.length) {
        Kiosk.setHtml(container, html`<div class="text-center text-white-50 py-4">${ui.cart_empty || '購物車目前空空如也'}</div>`);
        $('addon-recommend-container').innerHTML = '';
    } else {
        Kiosk.setHtml(container, state.cart.map(cartLineHtml));
        Kiosk.setHtml($('addon-recommend-container'), addonRecommendHtml());
    }
    calculateTotal();
    schedulePromotionPreview();
}

export function changeQty(index, delta) {
    const line = state.cart[index];
    if (!line) return;
    if (delta > 0 && line.is_reward) {
        alert(`⚠️ ${ui.reward_qty || '紅利兌換餐點無法直接增加數量，如需多份請前往回饋商城再次兌換！'}`);
        return;
    }
    if (delta > 0 && line.is_add_on) {
        const def = addonRules().addonDef(line.menu_item_id);
        if (!def || remainingAddonQuota(def) <= 0) {
            alert(`⚠️ ${ui.addon_max || '加購品數量已達上限！'}`);
            return;
        }
    }
    if (delta < 0 && line.quantity + delta <= 0) {
        removeLine(index);
        return;
    }
    line.quantity += delta;
    saveCart();
    updateCartUI();
}

export async function removeLine(index) {
    const line = state.cart[index];
    if (!line) return;
    if (line.is_reward) {
        if (!confirm((ui.reward_remove_confirm || '確定要移除兌換餐點【{n}】嗎？移除後將返還扣除的紅利點數。').replace('{n}', line.name))) return;
        try {
            const data = await api('/api/refund_reward_item', { json: { item_id: line.menu_item_id, quantity: line.quantity } });
            setUserPoints(data.current_points);
            alert(`🎉 ${data.message_i18n || data.message}`);
        } catch (err) {
            alert(`❌ ${ui.rw_return_failed || '退點失敗：'}${err.message}`);
            return;
        }
        // 退點期間購物車可能已變動：以同一個物件找回位置再移除
        const pos = state.cart.indexOf(line);
        if (pos >= 0) state.cart.splice(pos, 1);
    } else {
        state.cart.splice(index, 1);
    }
    saveCart();
    updateCartUI();
}
