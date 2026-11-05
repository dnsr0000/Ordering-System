/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：單價估算 (僅供畫面預覽，實際金額一律由後端重算)。
 * 與後端相同的規則：通路價 (prices[channel]，沒有則 price) → 會員價取較低者 (MenuItem.price_for(member=True))
 * → 規格加減價 (sizes[].delta，沒指定時用第一個規格) 不得低於 0 → 套餐加價 → 任選加價 → 加料。
 * 純邏輯、不碰 DOM。
 */

/** 通路價：channel = dine_in / takeout / online / delivery；isMember 為真且有會員價時取較低者 */
export function channelPrice(item, channel, isMember) {
    const base = Number((item.prices && item.prices[channel]) || item.price) || 0;
    if (isMember && item.member_prices && item.member_prices[channel] !== undefined) {
        return Math.min(base, Number(item.member_prices[channel]) || base);
    }
    return base;
}

/** 實際採用的規格：名稱不存在時用第一個規格；品項沒有規格時為空字串 */
export function pickSize(item, size) {
    const sizes = (item && item.sizes) || [];
    if (!sizes.length) return '';
    return (sizes.find((s) => s.name === size) || sizes[0]).name;
}

/** 規格加減價 (與後端相同：沒有指定時用第一個規格) */
export function sizeDelta(item, size) {
    const sizes = (item && item.sizes) || [];
    if (!sizes.length) return 0;
    const chosen = sizes.find((s) => s.name === size) || sizes[0];
    return Number(chosen.delta) || 0;
}

/** 套餐任選：選中的選項 → { ids, extra, labels } (labels 例如「飲料:紅茶(+10)」) */
export function comboChoiceInfo(combo, ids) {
    const out = { ids: [], extra: 0, labels: [] };
    const chosen = (ids || []).map(Number);
    ((combo && combo.choices) || []).forEach((g) => {
        const opt = (g.options || []).find((o) => chosen.includes(Number(o.id)));
        if (!opt) return;
        const extra = Number(opt.extra_price) || 0;
        out.ids.push(Number(opt.id));
        out.extra += extra;
        out.labels.push(`${g.group}:${opt.name}${extra ? `(+${extra})` : ''}`);
    });
    return out;
}

/** 加料合計：modifierById 為 Map (id → { price })；不存在的 ID 不計 */
export function modifiersTotal(ids, modifierById) {
    return (ids || []).reduce((sum, id) => sum + (modifierById.has(id) ? Number(modifierById.get(id).price) || 0 : 0), 0);
}

/** 一般品項單價：max(0, 通路價 + 規格) + 套餐加價 + 任選加價 + 加料合計 */
export function unitPrice({ item, channel, isMember = false, size = '', combo = null, choiceIds = [], modifierTotal = 0 }) {
    return Math.max(0, channelPrice(item, channel, isMember) + sizeDelta(item, size))
        + (combo ? Number(combo.price) || 0 : 0) + comboChoiceInfo(combo, choiceIds).extra + (Number(modifierTotal) || 0);
}
