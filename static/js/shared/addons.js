/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：條件加購配額。
 * 每一份合格正價餐點最多搭配一份加購，且加購品必須符合該餐點的觸發條件 (分類 / 指定品項 / 任一主餐)；
 * 以二分圖配對判斷整車加購是否都配得到餐點 (與後端 _validate_addon_quota / _addon_qualifies 同一套規則)。
 * 純邏輯、不碰 DOM：購物車陣列與查詢函式由頁面注入
 *   categoryOf(line)  → 該購物車品項的分類名稱
 *   addonDef(id)      → 可加購的品項定義 (兩頁一律用 filterAddons 過濾過的清單：can_be_add_on 且 is_available)
 */
export const NON_MAIN_CATEGORIES = ['飲料', '點心', '副餐', '配餐'];

const normalizeCategory = (value) => String(value || '').trim().toLowerCase();
const isMainCategory = (category) => {
    const cat = normalizeCategory(category);
    if (!cat) return true;
    return !NON_MAIN_CATEGORIES.map(normalizeCategory).includes(cat);
};

/** 可加購的品項 (加購價 add_on_price；售完或關閉加購的品項不列入) */
export function filterAddons(items) {
    return [...items].filter((i) => i.can_be_add_on && i.is_available);
}

/** 這一列正價餐點是否符合 addon 的觸發條件 */
export function qualifiesForAddon(line, addon, categoryOf) {
    if (!line || line.is_add_on || line.is_reward || line.can_addon === false) return false;
    const cat = String(categoryOf(line) || '未分類').trim();
    const trigger = addon.addon_trigger_type || 'any';
    const target = (addon.addon_trigger_target || '').trim().toLowerCase();
    if (trigger === 'category') return normalizeCategory(cat) === (target || normalizeCategory(addon.category));
    // 後台存餐點名稱，舊資料可能存餐點 ID；兩種都接受
    if (trigger === 'item') return String(line.menu_item_id) === target || String(line.name || '').trim().toLowerCase() === target;
    return isMainCategory(cat);
}

/** 可搭配加購的正價餐點，一份一個元素 (數量 3 就出現 3 次) */
export function mainUnits(cart) {
    const units = [];
    cart.forEach((line) => {
        if (line.is_add_on || line.is_reward || line.can_addon === false) return;
        for (let k = 0; k < line.quantity; k++) units.push(line);
    });
    return units;
}

/** 二分圖配對：addonDefs 中每一份加購是否都能配到一份合格的正價餐點 */
export function addonsFeasible(cart, addonDefs, categoryOf) {
    const mains = mainUnits(cart);
    if (addonDefs.length > mains.length) return false;
    const owner = new Array(mains.length).fill(-1);
    const assign = (a, seen) => {
        for (let m = 0; m < mains.length; m++) {
            if (seen[m] || !qualifiesForAddon(mains[m], addonDefs[a], categoryOf)) continue;
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

/** 購物車裡已有的加購，一份一個定義 */
export function cartAddonDefs(cart, addonDef) {
    const defs = [];
    cart.filter((l) => l.is_add_on).forEach((l) => {
        const def = addonDef(l.menu_item_id);
        if (def) for (let k = 0; k < l.quantity; k++) defs.push(def);
    });
    return defs;
}

/** 在不破壞既有加購配對的前提下，該加購品還能再加幾份 */
export function remainingAddonQuota(cart, addon, { categoryOf, addonDef }) {
    const current = cartAddonDefs(cart, addonDef);
    let n = 0;
    while (addonsFeasible(cart, current.concat(new Array(n + 1).fill(addon)), categoryOf)) n++;
    return n;
}

/**
 * 依購物車順序逐份保留加購品，無法配對到合格餐點 (或已不可加購) 的部分自動減量。
 * 直接修改各列的 quantity；回傳 { lines: 數量 > 0 的品項, changed: 是否有調整 }
 */
export function enforceAddonQuota(cart, { categoryOf, addonDef }) {
    let changed = false;
    const kept = [];
    cart.forEach((line) => {
        if (!line.is_add_on) return;
        const def = addonDef(line.menu_item_id);
        let allowed = 0;
        if (def) {
            while (allowed < line.quantity && addonsFeasible(cart, kept.concat([def]), categoryOf)) {
                kept.push(def);
                allowed++;
            }
        }
        if (allowed !== line.quantity) {
            line.quantity = allowed;
            changed = true;
        }
    });
    return { lines: cart.filter((l) => l.quantity > 0), changed };
}
