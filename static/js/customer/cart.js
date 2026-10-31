/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：購物車資料 (正規化、加入、加購配額、重算價格) 與購物車畫面。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import {
    $, NON_MAIN_CATEGORIES, REWARD_LABEL, api, availableAddons, channelPrice, html, menuItems, modifierById,
    setText, state, storeCart,
} from './core.js';
import { calculateTotal, schedulePromotionPreview, setUserPoints } from './totals.js';

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

export function lineKey(line) {
    return [line.menu_item_id, line.combo_option_id || 0, line.is_add_on ? 1 : 0, line.size || '', line.customization,
        [...line.modifier_ids].sort((a, b) => a - b).join(','), (line.combo_choices || []).join(','), line.pass_id || 0].join('|');
}

/** 購物車品項的顯示說明：[套餐: X]、客製化文字、加料(+N) (與後端產生的明細相同) */
export function lineDescription(line) {
    const parts = [];
    if (line.pass_id) parts.push('[寄杯]');
    if (line.combo_name) parts.push(`[套餐: ${line.combo_name}]`);
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

export function modifiersTotal(ids) {
    return ids.reduce((sum, id) => sum + (modifierById.has(id) ? Number(modifierById.get(id).price) || 0 : 0), 0);
}

/** 規格加減價 (與後端相同：沒有指定時用第一個規格) */
export function sizeDelta(item, size) {
    const sizes = (item && item.sizes) || [];
    if (!sizes.length) return 0;
    const chosen = sizes.find((s) => s.name === size) || sizes[0];
    return Number(chosen.delta) || 0;
}

/** 套餐任選：選中的選項 → { ids, extra, labels } */
export function comboChoiceInfo(combo, ids) {
    const out = { ids: [], extra: 0, labels: [] };
    (combo && combo.choices ? combo.choices : []).forEach((g) => {
        const opt = g.options.find((o) => ids.includes(Number(o.id)));
        if (!opt) return;
        out.ids.push(Number(opt.id));
        out.extra += Number(opt.extra_price) || 0;
        out.labels.push(`${g.group}:${opt.name}${Number(opt.extra_price) ? `(+${opt.extra_price})` : ''}`);
    });
    return out;
}

export function makeLine(item, { combo = null, isAddon = false, customization = '', modifierIds = [], size = '', choiceIds = [],
                          passId = 0 } = {}) {
    const choice = comboChoiceInfo(combo, choiceIds);
    let base = isAddon ? Number(item.add_on_price) || 0
        : Math.max(0, channelPrice(item) + sizeDelta(item, size)) + (combo ? Number(combo.price) || 0 : 0) + choice.extra;
    if (passId) base = 0;   // 寄杯兌換：免費 (只收加料)
    return {
        menu_item_id: Number(item.id),
        name: item.name,
        price: base + modifiersTotal(modifierIds),
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

export function addLine(line) {
    const key = lineKey(line);
    const existing = state.cart.find((l) => !l.is_reward && lineKey(l) === key);
    if (existing) existing.quantity += 1;
    else state.cart.push(line);
    saveCart();
    updateCartUI();
}

// ================================================================ 加購配額
// 每一份合格正價餐點最多搭配一份加購，且加購品必須符合該餐點的觸發條件 (二分圖配對，與後端一致)
export function qualifiesForAddon(line, addon) {
    if (!line || line.is_add_on || line.is_reward || line.can_addon === false) return false;
    const cat = lineCategory(line).trim();
    const trigger = addon.addon_trigger_type || 'any';
    const target = (addon.addon_trigger_target || '').trim().toLowerCase();
    if (trigger === 'category') {
        return cat.toLowerCase() === (target || (addon.category || '').trim().toLowerCase());
    }
    if (trigger === 'item') {
        return String(line.menu_item_id) === target || line.name.trim().toLowerCase() === target;
    }
    return !NON_MAIN_CATEGORIES.includes(cat);
}

export function mainUnits() {
    const units = [];
    state.cart.forEach((line) => {
        if (line.is_add_on || line.is_reward || line.can_addon === false) return;
        for (let k = 0; k < line.quantity; k++) units.push(line);
    });
    return units;
}

export function addonsFeasible(addonDefs) {
    const mains = mainUnits();
    if (addonDefs.length > mains.length) return false;
    const owner = new Array(mains.length).fill(-1);
    const assign = (a, seen) => {
        for (let m = 0; m < mains.length; m++) {
            if (seen[m] || !qualifiesForAddon(mains[m], addonDefs[a])) continue;
            seen[m] = true;
            if (owner[m] === -1 || assign(owner[m], seen)) {
                owner[m] = a;
                return true;
            }
        }
        return false;
    };
    for (let a = 0; a < addonDefs.length; a++) {
        if (!assign(a, new Array(mains.length).fill(false))) return false;
    }
    return true;
}

export function cartAddonDefs() {
    const defs = [];
    state.cart.filter((l) => l.is_add_on).forEach((l) => {
        const def = availableAddons.find((a) => Number(a.id) === l.menu_item_id);
        if (def) for (let k = 0; k < l.quantity; k++) defs.push(def);
    });
    return defs;
}

/** 在不破壞既有加購配對的前提下，該加購品還能再加幾份 */
export function remainingAddonQuota(addon) {
    const current = cartAddonDefs();
    let n = 0;
    while (addonsFeasible(current.concat(new Array(n + 1).fill(addon)))) n++;
    return n;
}

/** 依購物車順序逐份保留加購品，無法配對到合格餐點的部分自動減量；有調整時回傳 true */
export function enforceAddonQuota() {
    let changed = false;
    const kept = [];
    state.cart.forEach((line) => {
        if (!line.is_add_on) return;
        const def = availableAddons.find((a) => Number(a.id) === line.menu_item_id);
        let allowed = 0;
        if (def) {
            while (allowed < line.quantity && addonsFeasible(kept.concat([def]))) {
                kept.push(def);
                allowed++;
            }
        }
        if (allowed !== line.quantity) {
            line.quantity = allowed;
            changed = true;
        }
    });
    state.cart = state.cart.filter((l) => l.quantity > 0);
    return changed;
}

/** 內用 / 外帶切換時，一般品項改用該通路的價格 */
export function repriceCart() {
    state.cart.forEach((l) => {
        if (l.is_reward) return;
        if (l.pass_id) {
            l.price = modifiersTotal(l.modifier_ids);
            return;
        }
        const item = menuItems.get(l.menu_item_id);
        if (!item) return;
        if (l.is_add_on) {
            l.price = (Number(item.add_on_price) || 0) + modifiersTotal(l.modifier_ids);
            return;
        }
        const combo = l.combo_option_id ? (item.combos || []).find((c) => Number(c.id) === Number(l.combo_option_id)) : null;
        l.price = Math.max(0, channelPrice(item) + sizeDelta(item, l.size)) + (combo ? Number(combo.price) || 0 : 0)
            + comboChoiceInfo(combo, l.combo_choices || []).extra + modifiersTotal(l.modifier_ids);
    });
    saveCart();
    updateCartUI();
}

// ================================================================ 購物車畫面
export function cartLineHtml(line, idx) {
    const soldOut = line.is_sold_out;
    const desc = lineDescription(line);
    const action = (soldOut || line.is_reward)
        ? html`<button class="btn btn-sm btn-outline-danger fw-bold" type="button" data-action="remove" data-index="${idx}"><i class="bi bi-trash"></i> 移除</button>`
        : html`<div class="btn-group btn-group-sm">
                <button class="btn btn-outline-secondary text-white" type="button" data-action="qty" data-index="${idx}" data-delta="-1" aria-label="減少">-</button>
                <button class="btn btn-outline-secondary text-white" type="button" data-action="qty" data-index="${idx}" data-delta="1" aria-label="增加">+</button>
            </div>`;
    return html`
        <div class="cart-line d-flex justify-content-between align-items-center mb-2 border-bottom border-secondary pb-2 ${soldOut ? 'bg-danger bg-opacity-10 p-2 rounded' : ''}">
            <div>
                <div class="fw-bold ${soldOut ? 'text-decoration-line-through text-danger' : 'text-warning'}">
                    ${line.is_reward ? `${REWARD_LABEL} ` : ''}<span class="cart-line-name">${line.name}</span>
                    ${line.is_add_on ? html`<span class="badge bg-info text-dark ms-1">加購</span>` : ''}
                    ${soldOut ? html`<span class="badge bg-danger ms-1">已售完</span>` : ''}
                </div>
                ${desc ? html`<small class="d-block ${soldOut ? 'text-decoration-line-through text-white-50' : 'text-info'}">${desc}</small>` : ''}
                <small class="text-white-50 ${soldOut ? 'text-decoration-line-through' : ''}">$${Math.round(line.price)} x ${line.quantity}</small>
            </div>
            <div class="d-flex align-items-center">${action}</div>
        </div>`;
}

export function addonRecommendHtml() {
    const eligible = availableAddons
        .map((a) => ({ addon: a, quota: remainingAddonQuota(a) }))
        .filter((x) => x.quota > 0);
    if (!eligible.length) return '';
    return html`
        <div class="mb-2 p-2 bg-secondary bg-opacity-10 rounded border border-secondary">
            <div class="text-warning small fw-bold mb-2"><i class="bi bi-stars"></i> 專屬加購優惠</div>
            <div class="d-flex gap-2 overflow-auto pb-1 addon-strip">
                ${eligible.map(({ addon, quota }) => html`
                    <div class="card bg-dark border-secondary p-2 d-inline-block shadow-sm flex-shrink-0 addon-card">
                        <div class="small fw-bold text-white text-truncate" title="${addon.name}">${addon.name}</div>
                        <div class="text-info cart-tiny">尚可加購 ${quota} 份</div>
                        <div class="mt-1">
                            <del class="text-white-50 cart-tiny">$${addon.price}</del>
                            <span class="text-danger fw-bold small">$${addon.add_on_price}</span>
                        </div>
                        <button class="btn btn-sm btn-outline-info w-100 py-0 mt-1" type="button" data-action="addon" data-id="${addon.id}">加購</button>
                    </div>`)}
            </div>
        </div>`;
}

export function updateCartUI() {
    if (enforceAddonQuota()) {
        saveCart();
        setTimeout(() => alert('⚠️ 購物車餐點異動導致加購品名額不足，已自動調整數量！'), 100);
    }
    setText('cart-count', state.cart.reduce((sum, l) => sum + l.quantity, 0));
    const container = $('cart-items');
    if (!state.cart.length) {
        Kiosk.setHtml(container, html`<div class="text-center text-white-50 py-4">購物車目前空空如也</div>`);
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
        alert('⚠️ 紅利兌換餐點無法直接增加數量，如需多份請前往回饋商城再次兌換！');
        return;
    }
    if (delta > 0 && line.is_add_on) {
        const def = availableAddons.find((a) => Number(a.id) === line.menu_item_id);
        if (!def || remainingAddonQuota(def) <= 0) {
            alert('⚠️ 加購品數量已達上限！');
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
        if (!confirm(`確定要移除兌換餐點【${line.name}】嗎？移除後將返還扣除的紅利點數。`)) return;
        try {
            const data = await api('/api/refund_reward_item', { json: { item_id: line.menu_item_id, quantity: line.quantity } });
            setUserPoints(data.current_points);
            alert(`🎉 ${data.message}`);
        } catch (err) {
            alert(`❌ 退點失敗：${err.message}`);
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
