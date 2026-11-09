/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：1~3. 分類與搜尋、點選商品 / 紅利兌換 / 套餐、客製化選項與規格。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js，客製化視窗的 HTML / 讀回、套餐卡片版面與單價在 static/js/shared/。
 */
import {
    $, afterHidden, cartRewardPoints, channel, html, isItemAvailable, itemMap, modalOf, modifierById, modifierOptions, num,
    REWARD_PREFIX, setHtml, state,
} from './core.js';
import { addLine, renderCart } from './cart.js';
import * as mods from '../shared/modifiers.js';
import { comboAvailable, comboCardHtml } from '../shared/combos.js';
import { comboChoiceInfo, modifiersTotal, pickSize, unitPrice } from '../shared/pricing.js';

// ======================================================================
// 1. 分類與搜尋過濾
// ======================================================================
export function selectCategory(btn) {
    state.category = btn.dataset.cat;
    document.querySelectorAll('#posCategoryBar .cat-pill').forEach((b) => b.classList.toggle('active', b === btn));
    filterMenu();
}

export function filterMenu() {
    const q = ($('posSearchInput').value || '').trim().toLowerCase();
    const cat = state.category;
    document.querySelectorAll('.pos-menu-col').forEach((col) => {
        const name = (col.dataset.name || '').toLowerCase();
        const colCat = col.dataset.category || '';
        const matchSearch = !q || name.includes(q) || colCat.toLowerCase().includes(q);
        let matchCat = true;
        if (cat === '__popular__') matchCat = col.dataset.popular === '1';
        else if (cat === '__discount__') matchCat = col.dataset.discount === '1';
        else if (cat === '__reward__') matchCat = col.dataset.reward === '1';
        else if (cat !== 'all') matchCat = colCat === cat;
        col.classList.toggle('d-none', !(matchSearch && matchCat));
    });
}

export function clearSearch() {
    $('posSearchInput').value = '';
    filterMenu();
}

/**
 * 條碼 / 商品代碼 (菜單進階設定的「外送平台 / 條碼代碼」)：掃描槍打字後送 Enter，代碼完全相符時直接加入
 * (有套餐 / 客製化 / 規格的商品一樣走選擇視窗)。回傳是否有對應的商品。
 */
export function scanCode(raw) {
    const code = String(raw || '').trim().toLowerCase();
    if (!code) return false;
    const item = [...itemMap.values()].find((i) => (i.external_code || '').trim().toLowerCase() === code);
    if (!item) return false;
    if (!isItemAvailable(item.id)) {
        alert(`⚠️ 【${item.name}】已售完或目前不供應`);
        return true;
    }
    // 掃碼一律是一般販售：停在「🎁 紅利兌換」分頁時也不可變成點數兌換
    const card = document.getElementById(`pos-card-${item.id}`);
    if (card) pickItem(card, { allowReward: false });
    else sellItem(item);
    return true;
}

/** 搜尋框按 Enter：先當條碼找；找不到且只剩一個符合的商品時直接加入 */
export function searchEnter() {
    const input = $('posSearchInput');
    const q = (input.value || '').trim();
    if (!q) return;
    if (scanCode(q)) {
        clearSearch();
        return;
    }
    const visible = [...document.querySelectorAll('.pos-menu-col:not(.d-none) [data-action="pickItem"]')];
    if (visible.length === 1) {
        pickItem(visible[0], { allowReward: false });
        clearSearch();
    }
}

// ======================================================================
// 2. 點選商品 / 紅利兌換 / 套餐
// ======================================================================
/**
 * 不在供應時段 (頁面載入時的 in_service) 的品項結帳時會被後端拒絕，先提醒收銀；
 * 頁面開著跨過供應時段時這個值可能已過時，所以只確認、不硬擋
 */
function confirmInService(item) {
    if (item.in_service !== false) return true;
    return window.confirm(`⚠️ 【${item.name}】目前不在供應時段${item.service_hint ? ` (${item.service_hint})` : ''}，結帳時會被拒絕。\n仍要加入購物車嗎？`);
}

/** 一般販售加入 (有套餐時先選單點 / 套餐) */
function sellItem(item) {
    if (!confirmInService(item)) return;
    if ((item.combos || []).length > 0) {
        openComboModal(item);
    } else {
        startConfigure({ item, combo: null, mode: 'normal' });
    }
}

/** 點選品項卡片；allowReward = false (掃碼 / 搜尋 Enter) 時不看目前分頁，一律一般販售 */
export function pickItem(card, { allowReward = true } = {}) {
    if (card.dataset.soldout === '1') return;
    const item = itemMap.get(Number(card.dataset.itemId));
    if (!item) return;

    // 在「🎁 紅利兌換」分頁點擊紅利商品，直接走紅利兌換
    if (allowReward && state.category === '__reward__' && item.is_reward) {
        redeemItem(item);
        return;
    }
    sellItem(item);
}

export function redeemItem(item) {
    if (!item || !isItemAvailable(item.id)) return;
    if (!state.member) {
        alert('⚠️ 請先於右側「會員查詢 / 綁定」輸入顧客手機號碼或姓名綁定會員，才能使用紅利兌換！');
        $('posMemberQuery').focus();
        return;
    }
    const need = num(item.redeem_points);
    const available = state.member.points - cartRewardPoints();
    if (available < need) {
        alert(`⚠️ 會員可用紅利點數不足！\n目前剩餘可用：${available} 點，此商品需要：${need} 點。`);
        return;
    }
    if (!confirmInService(item)) return;
    startConfigure({ item, combo: null, mode: 'reward' });
}

const COMBO_CARD = 'pos-combo-card p-3 rounded-3 h-100 d-flex flex-column justify-content-between';
const COMBO_HEADER = 'd-flex justify-content-between mb-2';
const COMBO_FOOTER = 'text-end mt-3';

/** 單點 / 升級套餐卡片 (版面在 shared/combos.js；配餐或任選全部售完的套餐不能選) */
export function openComboModal(item) {
    state.comboItem = item;
    $('posComboModalTitle').textContent = `【${item.name}】選擇單點或套餐`;
    const single = comboCardHtml({
        cardCls: COMBO_CARD,
        attrs: html`data-action="chooseCombo" data-combo-id="0" data-soldout="0"`,
        headerCls: COMBO_HEADER,
        badge: html`<span class="badge bg-secondary">單點主餐</span>`,
        price: html`<span class="fw-bold text-warning fs-5">$${num(item.price)}</span>`,
        title: html`<h6 class="fw-bold text-white">${item.name} (單點)</h6>`,
        desc: html`<small class="text-white-50">僅單點主餐，不附配餐。</small>`,
        footerCls: COMBO_FOOTER,
        footer: html`<span class="btn btn-sm btn-outline-light fw-bold">選擇單點 <i class="bi bi-chevron-right"></i></span>`,
    });
    const combos = item.combos.map((c) => {
        const sold = !comboAvailable(c, isItemAvailable);
        return comboCardHtml({
            cardCls: `${COMBO_CARD} is-combo ${sold ? 'pos-item-soldout' : ''}`,
            attrs: html`data-action="chooseCombo" data-combo-id="${c.id}" data-soldout="${sold ? 1 : 0}"`,
            headerCls: COMBO_HEADER,
            badge: html`<span class="badge ${sold ? 'bg-danger' : 'bg-warning text-dark fw-bold'}">${sold ? '配餐售罄' : '升級套餐'}</span>`,
            price: html`<span class="fw-bold text-danger fs-5">+$${num(c.price)} <small class="text-white-50 fs-6">($${num(item.price) + num(c.price)})</small></span>`,
            title: html`<h6 class="fw-bold text-warning">${c.name}</h6>`,
            desc: html`<small class="text-white-50">${c.description || ''}</small>`,
            footerCls: COMBO_FOOTER,
            footer: html`<span class="btn btn-sm ${sold ? 'btn-secondary' : 'btn-warning text-dark'} fw-bold">${sold ? '配餐缺貨' : '升級此套餐'}</span>`,
        });
    });
    setHtml($('posComboCardsContainer'), [single, combos]);
    modalOf('posComboModal').show();
}

export function chooseCombo(el) {
    if (el.dataset.soldout === '1' || !state.comboItem) return;
    const item = state.comboItem;
    state.comboItem = null;   // 視窗淡出期間再點一下不可重複加入
    const comboId = Number(el.dataset.comboId);
    const combo = comboId ? item.combos.find((c) => Number(c.id) === comboId) : null;
    afterHidden('posComboModal', () => startConfigure({ item, combo, mode: 'normal' }));
}

// ======================================================================
// 3. 客製化選項 (HTML 與讀回在 shared/modifiers.js；這裡只決定外觀與文字)
// ======================================================================
export const MODIFIER_SCHEME = {
    idPrefix: 'pos-',
    sectionCls: 'p-3 mb-2 rounded bg-dark border',
    title: (text, colorCls) => html`<div class="fw-bold ${colorCls} small mb-2 border-bottom border-secondary pb-1">${text}</div>`,
    labels: {
        ice: '冰塊', sugar: '甜度', spicy: '辣度', addons: '', noAddons: '目前沒有可選的加料項目', size: '規格',
        pickOne: ' (任選一項)', soldOut: ' (售完)', customize: ' 客製化',
    },
    values: {},
    addons: modifierOptions.filter((m) => m.category === 'addons'),
    isOptionAvailable: (o) => isItemAvailable(o.menu_item_id),
};

/** 加料寫進客製化文字：「起司(+15)」；不認得的加料略過 */
const describeAddon = (id) => {
    const opt = modifierById.get(Number(id));
    return opt ? `${opt.name}(+${num(opt.price)})` : null;
};

/** 規格 (單選)：第一個為預設；紅利兌換、加購品與寄杯不適用 */
export function itemSizes(ctx) {
    return ctx.mode === 'normal' ? (ctx.item.sizes || []) : [];
}

export function startConfigure(ctx) {
    const mainMods = ctx.item.modifiers;
    const sizes = itemSizes(ctx);
    const sides = ctx.combo ? (ctx.combo.sides || []).filter((s) => s.customizable && mods.hasMods(s.modifiers)) : [];
    const choices = ctx.combo ? (ctx.combo.choices || []) : [];
    if (!mods.hasMods(mainMods) && sides.length === 0 && !sizes.length && !choices.length) {
        addLine(makeLine(ctx, [], 0, []));
        return;
    }
    state.pending = { ...ctx, sides, choices };
    const titleName = ctx.mode === 'reward' ? REWARD_PREFIX + ctx.item.name : ctx.item.name;
    $('posModifierModalTitle').textContent = ctx.mode === 'addon' ? `加購客製化 - ${titleName}` : `客製化 - ${titleName}`;
    const groups = [];
    if (sizes.length) groups.push(mods.sizeSectionHtml(MODIFIER_SCHEME, sizes));
    if (mods.hasMods(mainMods)) groups.push(mods.modifierSectionHtml(MODIFIER_SCHEME, 'main', ctx.mode === 'addon' ? `加購：${ctx.item.name}` : titleName, mainMods));
    sides.forEach((s, idx) => groups.push(mods.modifierSectionHtml(MODIFIER_SCHEME, `side${idx}`, `配餐：${s.name}`, s.modifiers)));
    choices.forEach((g, gi) => groups.push(mods.choiceSectionHtml(MODIFIER_SCHEME, g, gi)));
    setHtml($('posModifierModalBody'), groups);
    syncChoiceMods();
    modalOf('posModifierModal').show();
}

export function syncChoiceMods() {
    mods.syncChoiceMods($('posModifierModalBody'));
}

export function confirmModifier() {
    const ctx = state.pending;
    if (!ctx) return;
    const r = mods.readModifierModal($('posModifierModalBody'), {
        hasMain: mods.hasMods(ctx.item.modifiers), sides: ctx.sides, choices: ctx.choices || [], describeAddon,
    });
    if (r.missing) {
        alert(`⚠️ 請在「${r.missing.group}」中選擇一項！`);
        return;
    }
    state.pending = null;
    addLine(makeLine(ctx, r.texts, modifiersTotal(r.modifierIds, modifierById), r.modifierIds, r.size, r.choiceIds));
    modalOf('posModifierModal').hide();
}

/** 建立購物車品項 (價格與後端 order_service._parse_lines 相同規則，僅供預覽；extra = 加料合計) */
export function makeLine({ item, combo, mode, passId }, parts, extra, modifierIds, size = '', choiceIds = []) {
    const choice = comboChoiceInfo(combo, choiceIds);
    const sizeName = mode === 'normal' ? pickSize(item, size) : '';
    const line = {
        size: sizeName,
        menu_item_id: Number(item.id),
        name: item.name,
        category: item.category,
        quantity: 1,
        modifier_ids: modifierIds,
        combo_option_id: combo ? Number(combo.id) : null,
        combo_choices: choice.ids,
        is_add_on: mode === 'addon',
        is_reward: mode === 'reward',
        points_cost: mode === 'reward' ? num(item.redeem_points) : 0,
        can_addon: mode === 'normal' && (!combo || combo.can_addon !== false),
        pass_id: mode === 'pass' ? Number(passId) : 0,
    };
    if (mode === 'pass') {   // 寄杯兌換：免費 (只收加料)；「[寄杯]」標籤由後端加上 (購物車以徽章顯示)，這裡不重複寫入
        line.price = extra;
        line.customization = parts.join('、');
        return line;
    }
    if (mode === 'reward') {
        line.price = extra;
        line.customization = parts.join('、') || '紅利免費兌換';
    } else if (mode === 'addon') {
        line.price = num(item.add_on_price) + extra;
        line.customization = parts.join('、');
    } else {
        line.extra_price = (combo ? num(combo.price) : 0) + choice.extra + extra;
        line.price = unitPrice({ item, channel: channel(), isMember: memberPricing(), size: sizeName, modifierTotal: line.extra_price });
        const named = sizeName ? [sizeName, ...parts] : parts;
        line.customization = (combo ? [`[套餐: ${combo.name}]`, ...choice.labels, ...named] : named).join('、');
    }
    return line;
}

/** 是否套用會員價：外送平台單不綁會員 (後端以非會員計價)，外送單登錄模式一律用一般價 */
const memberPricing = () => Boolean(state.member) && !state.deliveryMode;

/** 切換內用 / 外帶 / 外送單或綁定會員時，一般品項改用該通路的價格 (加購、寄杯、紅利與改過價的品項不變) */
export function repriceCart() {
    state.cart.forEach((l) => {
        if (l.is_reward || l.is_add_on || l.pass_id || l.override_price !== undefined) return;
        const item = itemMap.get(l.menu_item_id);
        if (item) l.price = unitPrice({ item, channel: channel(), isMember: memberPricing(), size: l.size, modifierTotal: l.extra_price });
    });
    renderCart();
}
