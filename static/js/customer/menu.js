/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：桌邊呼叫服務、套餐 / 客製化 / 加購視窗、寄杯、菜單篩選與排序。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js，客製化視窗的 HTML / 讀回與套餐卡片版面在 static/js/shared/。
 */
import { $, api, availableAddons, channelPrice, html, isItemAvailable, menuItems, modal, modifierOptions, pageData, state, ui } from './core.js';
import { addLine, itemName, makeLine, remainingAddonQuota } from './cart.js';
import * as mods from '../shared/modifiers.js';
import { comboAvailable, comboCardHtml } from '../shared/combos.js';
import { passRemaining } from '../shared/cartline.js';

// 桌邊呼叫服務 (掃桌位 QR code 後，登入前後都可使用)
export async function callService(btn) {
    btn.disabled = true;
    try {
        const res = await api('/api/table/call', { json: { kind: btn.dataset.kind } });
        alert(`🔔 ${res.message_i18n || res.message}`);
    } catch (err) {
        alert(`❌ ${err.message}`);
    } finally {
        setTimeout(() => { btn.disabled = false; }, 3000);
    }
}
// ================================================================ 套餐 / 客製化
/** 配餐 / 任選選項目前可點：以菜單的即時供應狀態為準 (live.js 會更新)，不在菜單裡的品項依後端標示 */
const sideAvailable = (id, side) => isItemAvailable(id, side.is_available !== false);
/**
 * 套餐的 is_available 是頁面載入時依配餐庫存算出的靜態值 (live.js 不會更新)：配餐 / 任選選項都在菜單裡時
 * 改以即時的 sideAvailable 判斷 (載入時配餐售完、之後補貨也能選)；有不在菜單裡的配餐時仍參考後端標示
 */
const liveCombo = (c) => ([...(c.sides || []), ...(c.choices || []).flatMap((g) => g.options || [])]
    .every((s) => menuItems.has(Number(s.menu_item_id))) ? { ...c, is_available: true } : c);

export function startAdd(itemId) {
    const item = menuItems.get(Number(itemId));
    if (!item || !item.is_available) return;
    if (item.combos && item.combos.length) {
        state.pendingItem = item;
        renderComboCards(item);
        modal('comboModal').show();
    } else {
        proceedToModifiers(item, null, false);
    }
}

const COMBO_CARD = 'combo-card p-4 rounded-3 h-100 d-flex flex-column justify-content-between';
const COMBO_HEADER = 'd-flex justify-content-between align-items-center mb-2';
const COMBO_FOOTER = 'mt-3 pt-2 border-top border-secondary text-end';

/** 單點 / 升級套餐卡片 (版面在 shared/combos.js；配餐或任選全部售完的套餐不能選) */
export function renderComboCards(item) {
    $('comboModalTitle').textContent = `【${itemName(item.id, item.name)}】${ui.enjoy_how || '享用方式'}`;
    // 與加入購物車 (makeLine) 相同：依價格通路 (內用 / 外帶 / 線上) 與會員價顯示
    const base = channelPrice(item);
    const single = comboCardHtml({
        cardCls: COMBO_CARD,
        attrs: html`data-action="pick-combo" data-combo-id="" role="button" tabindex="0"`,
        headerCls: COMBO_HEADER,
        badge: html`<span class="badge bg-secondary">${ui.single_main || '單點主餐'}</span>`,
        price: html`<span class="text-success fw-bold fs-5">$${base}</span>`,
        title: html`<h5 class="fw-bold text-white mb-1">${itemName(item.id, item.name)}${ui.single_suffix || ' (單點)'}</h5>`,
        desc: html`<p class="text-white-50 small mb-0">${ui.single_desc || '僅單點本品項，不搭配任何附餐或飲品。'}</p>`,
        footerCls: COMBO_FOOTER,
        footer: html`<span class="btn btn-outline-light btn-sm fw-bold px-3">${ui.choose_single || '選擇單點'} <i class="bi bi-chevron-right"></i></span>`,
    });
    const combos = item.combos.map((c) => {
        const sold = !comboAvailable(liveCombo(c), sideAvailable);
        return comboCardHtml({
            cardCls: `${COMBO_CARD} ${sold ? 'card-sold-out' : ''}`,
            attrs: sold ? '' : html`data-action="pick-combo" data-combo-id="${c.id}" role="button" tabindex="0"`,
            headerCls: COMBO_HEADER,
            badge: html`<span class="badge ${sold ? 'bg-secondary' : 'bg-warning text-dark fw-bold'}">${sold ? (ui.combo_sold_out || '配餐售罄') : (ui.combo_upgrade || '升級超值套餐')}</span>`,
            price: html`<div>
                            <span class="${sold ? 'text-white-50' : 'text-danger fw-bold fs-5'}">+$${c.price}</span>
                            <small class="text-white-50 ms-1">(${ui.combo_total || '總價 $'}${base + Number(c.price)})</small>
                        </div>`,
            title: html`<h5 class="fw-bold ${sold ? 'text-white-50 text-decoration-line-through' : 'text-warning'} mb-1 combo-name">${c.name}</h5>`,
            desc: html`<p class="text-white-50 small mb-0">${c.description || ui.combo_desc_default || '含特選附餐與指定飲品組合'}</p>`,
            footerCls: COMBO_FOOTER,
            footer: sold
                ? html`<button class="btn btn-secondary btn-sm fw-bold" type="button" disabled><i class="bi bi-slash-circle"></i> ${ui.combo_side_unavailable || '配餐缺貨中'}</button>`
                : html`<span class="btn btn-warning btn-sm fw-bold text-dark px-3">${ui.upgrade_combo || '升級套餐'} <i class="bi bi-chevron-right"></i></span>`,
        });
    });
    Kiosk.setHtml($('combo-cards-container'), [single, combos]);
}

export function pickCombo(comboId) {
    const item = state.pendingItem;
    if (!item) return;
    // 立即取走：視窗淡出期間再點一次 (觸控連點) 不會重複加入
    state.pendingItem = null;
    const combo = comboId ? (item.combos || []).find((c) => String(c.id) === String(comboId)) : null;
    // 等套餐視窗完全關閉後再開客製化視窗，避免兩個 Modal 的轉場互相干擾
    Kiosk.hideModalThen($('comboModal'), () => proceedToModifiers(item, combo, false));
}

/** 客製化視窗的外觀與文字 (shared/modifiers.js)；下拉的值固定中文，顯示文字依語言 */
export const MODIFIER_SCHEME = {
    idPrefix: '',
    sectionCls: 'p-3 mb-3 rounded border bg-dark shadow-sm',
    title: (text, colorCls) => html`<h6 class="fw-bold ${colorCls} mb-2 border-bottom border-secondary pb-1">${text}</h6>`,
    labels: {
        ice: ui.ice || '冰塊量', sugar: ui.sugar || '甜度', spicy: ui.spicy || '辣度調整', addons: ui.addons || '加料升級',
        noAddons: ui.no_addons || '目前沒有可選的加料項目', size: ui.size || '規格', pickOne: ui.pick_one || ' (任選一項)',
        soldOut: ui.sold_out_paren || ' (售完)', customize: ui.customize_suffix || ' 客製化',
    },
    values: { ice: ui.ice_values, sugar: ui.sugar_values, spicy: ui.spicy_values },
    addons: modifierOptions.filter((m) => m.category === 'addons'),
    isOptionAvailable: (o) => sideAvailable(o.menu_item_id, o),
};

/** 有客製化選項或規格時開啟客製化視窗，否則直接加入購物車 */
export function proceedToModifiers(item, combo, isAddon, passId = 0) {
    const hasMain = mods.hasMods(item.modifiers);
    const hasSizes = !isAddon && !passId && (item.sizes || []).length > 0;
    // 配餐：可客製且本身有客製化群組才顯示 (與 POS 相同)
    const sides = combo ? (combo.sides || []).filter((s) => s.customizable && mods.hasMods(s.modifiers)) : [];
    const choices = combo ? (combo.choices || []) : [];
    if (!hasMain && !sides.length && !hasSizes && !choices.length) {
        addLine(makeLine(item, { combo, isAddon, passId }));
        return;
    }
    state.pendingSelection = { item, combo, isAddon, sides, choices, passId };
    const name = itemName(item.id, item.name);   // 顯示用品名 (依語言)；makeLine 送後端的仍是中文原名
    let title;
    if (isAddon) title = `${ui.addon_customize || '加購客製化 - '}${name}`;
    else title = combo ? `${ui.customize_for || '客製化選擇 - '}${name} ${ui.combo_prefix || '[套餐: '}${combo.name}]` : `${ui.customize_for || '客製化選擇 - '}${name}`;
    $('modifierModalTitle').textContent = title;

    const isComboMode = Boolean(combo || sides.length);
    const mainTitle = isAddon ? `${ui.addon_prefix || '加購：'}${name}` : (isComboMode ? `${ui.main_prefix || '主餐：'}${name}` : name);
    Kiosk.setHtml($('modifierModalBody'), [
        hasSizes ? mods.sizeSectionHtml(MODIFIER_SCHEME, item.sizes) : '',
        hasMain ? mods.modifierSectionHtml(MODIFIER_SCHEME, 'main', mainTitle, item.modifiers) : '',
        sides.map((side, i) => mods.modifierSectionHtml(MODIFIER_SCHEME, `side${i}`, `${ui.side_prefix || '配餐：'}${side.name}`, side.modifiers)),
        choices.map((g, gi) => mods.choiceSectionHtml(MODIFIER_SCHEME, g, gi)),
    ]);
    syncChoiceMods();
    modal('modifierModal').show();
}

export function syncChoiceMods() {
    mods.syncChoiceMods($('modifierModalBody'));
}

export function confirmModifiers() {
    const sel = state.pendingSelection;
    if (!sel) return;
    const r = mods.readModifierModal($('modifierModalBody'), { hasMain: mods.hasMods(sel.item.modifiers), sides: sel.sides, choices: sel.choices || [] });
    if (r.missing) {
        alert(`⚠️ ${(ui.pick_one_in || '請在「{g}」中選擇一項！').replace('{g}', r.missing.group)}`);
        return;
    }
    // 立即清掉 (不等 hidden.bs.modal)：視窗淡出期間再按一次「確認加入」不會重複加入
    state.pendingSelection = null;
    addLine(makeLine(sel.item, { combo: sel.combo, isAddon: sel.isAddon, customization: r.texts.join('、'), modifierIds: r.modifierIds,
        size: r.size, choiceIds: r.choiceIds, passId: sel.passId || 0 }));
    modal('modifierModal').hide();
}

/** 寄杯：兌換一份 (不能超過剩餘次數) */
export function usePass(passId) {
    const p = (pageData.passes || []).find((x) => Number(x.id) === Number(passId));
    const item = p ? menuItems.get(Number(p.menu_item_id)) : null;
    if (!p || !item) {
        alert(`⚠️ ${ui.pass_item_missing || '找不到可兌換的餐點'}`);
        return;
    }
    // 與 startAdd 相同：售完 / 不在供應時段 / 原料不足的餐點不能兌換 (否則要到送出後才被後端擋下)
    if (!item.is_available) {
        alert(`⚠️ ${item.name} - ${ui.sold_out || '已售完'}`);
        return;
    }
    if (passRemaining(state.cart, p) <= 0) {
        alert(`⚠️ ${(ui.pass_all_added || '寄杯「{n}」剩 {r} 次，已全部加入購物車').replace('{n}', p.name).replace('{r}', p.remaining)}`);
        return;
    }
    proceedToModifiers(item, null, false, p.id);
}

export function triggerAddon(itemId) {
    const addon = availableAddons().find((a) => Number(a.id) === Number(itemId));
    if (!addon) {
        alert(`⚠️ ${ui.addon_missing || '查無此加購品設定！'}`);
        return;
    }
    if (remainingAddonQuota(addon) <= 0) {
        alert(`⚠️ ${ui.addon_quota_used || '加購名額已用完！每購買一份合格餐點限加購一份優惠品，加購品無法再次折抵。'}`);
        return;
    }
    proceedToModifiers(addon, null, true);
}

// ================================================================ 菜單篩選與排序
export const PRICE_SORT_LABELS = {
    null: html`<i class="bi bi-arrow-down-up"></i> ${ui.price_sort || '價格 ⇅'}`,
    asc: html`<i class="bi bi-sort-numeric-down"></i> ${ui.price_asc || '價格 ↑ (低到高)'}`,
    desc: html`<i class="bi bi-sort-numeric-up-alt"></i> ${ui.price_desc || '價格 ↓ (高到低)'}`,
};

export function renderSortButtons() {
    document.querySelectorAll('.sort-btn').forEach((b) => b.classList.toggle('active', b.dataset.mode === state.featureSort));
    const priceBtn = $('priceSortBtn');
    priceBtn.classList.toggle('active', state.priceOrder !== null);
    Kiosk.setHtml(priceBtn, PRICE_SORT_LABELS[state.priceOrder]);
}

export function applySort() {
    const container = $('menuList');
    const rows = [...container.querySelectorAll('.menu-row')];
    // 依原始順序作為最後的比較基準，取消排序時可回到原本的排列
    rows.sort((a, b) => {
        if (state.featureSort) {
            const diff = Number(b.dataset[state.featureSort] || 0) - Number(a.dataset[state.featureSort] || 0);
            if (diff) return diff;
        }
        if (state.priceOrder) {
            const diff = Number(a.dataset.price) - Number(b.dataset.price);
            if (diff) return state.priceOrder === 'asc' ? diff : -diff;
        }
        return Number(a.dataset.order) - Number(b.dataset.order);
    });
    rows.forEach((r) => container.appendChild(r));
    renderSortButtons();
}

export function toggleFeatureSort(mode) {
    state.featureSort = state.featureSort === mode ? null : mode;
    applySort();
}

export function cyclePriceSort() {
    state.priceOrder = state.priceOrder === null ? 'asc' : (state.priceOrder === 'asc' ? 'desc' : null);
    applySort();
}

// ---------------------------------------------------------------- 飲食篩選 (素食 / 不辣 / 不含某過敏原)，記在 localStorage
const DIET_KEY = 'kiosk_diet_filter';

function loadDietFilter() {
    const saved = Kiosk.store.get(DIET_KEY, null);
    if (saved && typeof saved === 'object') return { veg: !!saved.veg, mild: !!saved.mild, allergens: (saved.allergens || []).filter((x) => typeof x === 'string') };
    return { veg: false, mild: false, allergens: [] };
}

export function dietFilter() {
    if (!state.dietFilter) state.dietFilter = loadDietFilter();
    return state.dietFilter;
}

function renderDietChips() {
    const f = dietFilter();
    document.querySelectorAll('#dietFilterBar .diet-chip').forEach((chip) => {
        const on = chip.dataset.kind === 'allergen' ? f.allergens.includes(chip.dataset.code) : !!f[chip.dataset.kind];
        chip.classList.toggle('active', on);
        chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
}

export function toggleDietFilter(chip) {
    const f = dietFilter();
    if (chip.dataset.kind === 'allergen') {
        const code = chip.dataset.code;
        f.allergens = f.allergens.includes(code) ? f.allergens.filter((x) => x !== code) : [...f.allergens, code];
    } else {
        f[chip.dataset.kind] = !f[chip.dataset.kind];
    }
    Kiosk.store.set(DIET_KEY, f);
    renderDietChips();
    filterMenu();
}

/** 這一列是否符合飲食篩選 (data-allergens / data-diet / data-spicy 由樣板帶出) */
export function passesDietFilter(row) {
    const f = dietFilter();
    if (f.veg && !row.dataset.diet) return false;
    if (f.mild && Number(row.dataset.spicy || 0) > 0) return false;
    if (f.allergens.length) {
        const has = (row.dataset.allergens || '').split(',').filter(Boolean);
        if (has.some((a) => f.allergens.includes(a))) return false;
    }
    return true;
}

export function filterMenu() {
    const q = $('searchInput').value.trim().toLowerCase();
    const max = Number($('maxPrice').value) || Number.POSITIVE_INFINITY;
    const cat = $('categoryFilter').value;
    if ($('dietFilterBar')) renderDietChips();
    document.querySelectorAll('#menuList .menu-row').forEach((row) => {
        const text = row.textContent.toLowerCase();
        const visible = text.includes(q) && Number(row.dataset.price || 0) <= max
            && (cat === 'all' || row.dataset.category === cat) && passesDietFilter(row);
        row.classList.toggle('d-none', !visible);
    });
}

export function resetFilters() {
    $('searchInput').value = '';
    $('maxPrice').value = '';
    $('categoryFilter').value = 'all';
    state.featureSort = null;
    state.priceOrder = null;
    state.dietFilter = { veg: false, mild: false, allergens: [] };
    Kiosk.store.remove(DIET_KEY);
    filterMenu();
    applySort();
}
