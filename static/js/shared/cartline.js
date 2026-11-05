/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：購物車品項的識別鍵、合併加入、伺服器價格套用、寄杯剩餘次數。
 * 純邏輯、不碰 DOM。購物車品項格式：
 *   { menu_item_id, name, price, quantity, customization, size, modifier_ids[], combo_option_id, combo_choices[],
 *     pass_id, is_add_on, is_reward, ... }
 */

/** 同一品項 + 同樣的套餐 / 規格 / 客製化 / 加料 (順序無關) / 任選 / 寄杯 才視為同一列 */
export function lineKey(line) {
    return [
        line.menu_item_id, line.combo_option_id || 0, line.is_add_on ? 1 : 0, line.is_reward ? 1 : 0, line.size || '',
        line.customization || '', [...(line.modifier_ids || [])].map(Number).sort((a, b) => a - b).join(','),
        (line.combo_choices || []).join(','), line.pass_id || 0,
    ].join('|');
}

/** 加入購物車：已有同一列時把 line.quantity 加上去，否則新增一列；回傳最後在購物車裡的那一列 */
export function mergeLine(cart, line) {
    const key = lineKey(line);
    const existing = cart.find((l) => lineKey(l) === key);
    if (existing) {
        existing.quantity += line.quantity;
        return existing;
    }
    cart.push(line);
    return line;
}

/** 伺服器回報的各列單價 (line_prices，與購物車同順序) 套回購物車；skip(line) 為真的列不改。回傳是否有套用 */
export function applyLinePrices(cart, prices, { skip = () => false } = {}) {
    const list = Array.isArray(prices) ? prices : [];
    if (list.length !== cart.length) return false;
    cart.forEach((l, i) => {
        if (!skip(l)) l.price = Number(list[i]) || 0;
    });
    return true;
}

/** 寄杯還能兌換幾份：剩餘次數 − 購物車裡已加入的份數 */
export function passRemaining(cart, pass) {
    const used = cart.filter((l) => Number(l.pass_id) === Number(pass.id)).reduce((s, l) => s + (Number(l.quantity) || 0), 0);
    return (Number(pass.remaining) || 0) - used;
}
