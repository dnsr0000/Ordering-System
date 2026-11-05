/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：桌邊呼叫服務、套餐 / 客製化 / 加購視窗、寄杯、菜單篩選與排序。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import { $, api, availableAddons, html, menuItems, modal, modifierOptions, pageData, state, ui } from './core.js';
import { addLine, makeLine, remainingAddonQuota } from './cart.js';

// 桌邊呼叫服務 (掃桌位 QR code 後，登入前後都可使用)
export async function callService(btn) {
    btn.disabled = true;
    try {
        const res = await api('/api/table/call', { json: { kind: btn.dataset.kind } });
        alert(`🔔 ${res.message}`);
    } catch (err) {
        alert(`❌ ${err.message}`);
    } finally {
        setTimeout(() => { btn.disabled = false; }, 3000);
    }
}
// ================================================================ 套餐 / 客製化
/** 下拉選項：值固定為中文 (送到廚房的客製化文字)，顯示文字依語言 (ui.*_values 以 | 分隔，數量不符時顯示中文) */
function optionsHtml(values, labels) {
    const shown = typeof labels === 'string' ? labels.split('|') : [];
    return values.map((v, i) => html`<option value="${v}">${shown.length === values.length ? shown[i] : v}</option>`);
}

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

export function renderComboCards(item) {
    $('comboModalTitle').textContent = `【${item.name}】${ui.enjoy_how || '享用方式'}`;
    const single = html`
        <div class="col-md-6">
            <div class="combo-card p-4 rounded-3 h-100 d-flex flex-column justify-content-between" data-action="pick-combo" data-combo-id="" role="button" tabindex="0">
                <div>
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="badge bg-secondary">${ui.single_main || '單點主餐'}</span>
                        <span class="text-success fw-bold fs-5">$${item.price}</span>
                    </div>
                    <h5 class="fw-bold text-white mb-1">${item.name}${ui.single_suffix || ' (單點)'}</h5>
                    <p class="text-white-50 small mb-0">${ui.single_desc || '僅單點本品項，不搭配任何附餐或飲品。'}</p>
                </div>
                <div class="mt-3 pt-2 border-top border-secondary text-end">
                    <span class="btn btn-outline-light btn-sm fw-bold px-3">${ui.choose_single || '選擇單點'} <i class="bi bi-chevron-right"></i></span>
                </div>
            </div>
        </div>`;
    const combos = item.combos.map((c) => {
        const sold = !c.is_available;
        return html`
        <div class="col-md-6">
            <div class="combo-card p-4 rounded-3 h-100 d-flex flex-column justify-content-between ${sold ? 'card-sold-out' : ''}"
                 ${sold ? '' : html`data-action="pick-combo" data-combo-id="${c.id}" role="button" tabindex="0"`}>
                <div>
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="badge ${sold ? 'bg-secondary' : 'bg-warning text-dark fw-bold'}">${sold ? (ui.combo_sold_out || '配餐售罄') : (ui.combo_upgrade || '升級超值套餐')}</span>
                        <div>
                            <span class="${sold ? 'text-white-50' : 'text-danger fw-bold fs-5'}">+$${c.price}</span>
                            <small class="text-white-50 ms-1">(${ui.combo_total || '總價 $'}${Number(item.price) + Number(c.price)})</small>
                        </div>
                    </div>
                    <h5 class="fw-bold ${sold ? 'text-white-50 text-decoration-line-through' : 'text-warning'} mb-1 combo-name">${c.name}</h5>
                    <p class="text-white-50 small mb-0">${c.description || ui.combo_desc_default || '含特選附餐與指定飲品組合'}</p>
                </div>
                <div class="mt-3 pt-2 border-top border-secondary text-end">
                    ${sold
                        ? html`<button class="btn btn-secondary btn-sm fw-bold" type="button" disabled><i class="bi bi-slash-circle"></i> ${ui.combo_side_unavailable || '配餐缺貨中'}</button>`
                        : html`<span class="btn btn-warning btn-sm fw-bold text-dark px-3">${ui.upgrade_combo || '升級套餐'} <i class="bi bi-chevron-right"></i></span>`}
                </div>
            </div>
        </div>`;
    });
    Kiosk.setHtml($('combo-cards-container'), [single, combos]);
}

export function pickCombo(comboId) {
    const item = state.pendingItem;
    if (!item) return;
    const combo = comboId ? (item.combos || []).find((c) => String(c.id) === String(comboId)) : null;
    // 等套餐視窗完全關閉後再開客製化視窗，避免兩個 Modal 的轉場互相干擾
    Kiosk.hideModalThen($('comboModal'), () => proceedToModifiers(item, combo, false));
}

export function modifierSectionHtml(key, title, group) {
    let body = '';
    if (group === 'ice_sugar') {
        body = html`
            <div class="row g-2">
                <div class="col-6">
                    <label class="form-label small text-white-50" for="mod-ice-${key}">${ui.ice || '冰塊量'}</label>
                    <select id="mod-ice-${key}" class="form-select form-select-sm" data-mod="ice">
                        ${optionsHtml(['正常冰', '少冰', '去冰', '熱飲'], ui.ice_values)}
                    </select>
                </div>
                <div class="col-6">
                    <label class="form-label small text-white-50" for="mod-sugar-${key}">${ui.sugar || '甜度'}</label>
                    <select id="mod-sugar-${key}" class="form-select form-select-sm" data-mod="sugar">
                        ${optionsHtml(['全糖', '半糖', '微糖', '無糖'], ui.sugar_values)}
                    </select>
                </div>
            </div>`;
    } else if (group === 'spicy') {
        body = html`
            <div>
                <label class="form-label small text-white-50" for="mod-spicy-${key}">${ui.spicy || '辣度調整'}</label>
                <select id="mod-spicy-${key}" class="form-select form-select-sm" data-mod="spicy">
                    ${optionsHtml(['不辣', '微辣', '中辣', '大辣'], ui.spicy_values)}
                </select>
            </div>`;
    } else if (group === 'addons') {
        body = html`
            <div>
                <label class="form-label small text-white-50 mb-1">${ui.addons || '加料升級'}</label>
                <div class="d-flex flex-wrap gap-3">
                    ${modifierOptions.filter((m) => m.category === 'addons').map((m) => html`
                        <div class="form-check">
                            <input class="form-check-input" type="checkbox" data-mod="addon" data-id="${m.id}" id="addon-${m.id}-${key}">
                            <label class="form-check-label small text-white" for="addon-${m.id}-${key}">${m.name} (+${m.price})</label>
                        </div>`)}
                </div>
            </div>`;
    }
    return html`
        <div class="mod-section p-3 mb-3 rounded border border-secondary bg-dark shadow-sm" data-key="${key}">
            <h6 class="fw-bold text-warning mb-2 border-bottom border-secondary pb-1">${title}</h6>
            ${body}
        </div>`;
}

/** 規格選擇 (單選)：第一個為預設 */
export function sizeSectionHtml(item) {
    const sign = (d) => (d > 0 ? ` (+$${d})` : (d < 0 ? ` (-$${-d})` : ''));
    return html`
        <div class="mod-section p-3 mb-3 rounded border border-warning bg-dark shadow-sm" data-key="size">
            <h6 class="fw-bold text-warning mb-2 border-bottom border-secondary pb-1">${ui.size || '規格'}</h6>
            <div class="d-flex flex-wrap gap-2" role="radiogroup">
                ${item.sizes.map((s, i) => html`
                    <input type="radio" class="btn-check" name="item-size" id="size-${i}" value="${s.name}" data-mod="size" ${i === 0 ? 'checked' : ''}>
                    <label class="btn btn-outline-warning btn-sm" for="size-${i}">${s.name}${sign(Number(s.delta) || 0)}</label>`)}
            </div>
        </div>`;
}

/** 有客製化選項或規格時開啟客製化視窗，否則直接加入購物車 */
export function proceedToModifiers(item, combo, isAddon, passId = 0) {
    const hasMain = item.modifiers && item.modifiers !== 'none';
    const hasSizes = !isAddon && !passId && (item.sizes || []).length > 0;
    const sides = combo ? (combo.sides || []).filter((s) => s.customizable) : [];
    const choices = combo ? (combo.choices || []) : [];
    if (!hasMain && !sides.length && !hasSizes && !choices.length) {
        addLine(makeLine(item, { combo, isAddon, passId }));
        return;
    }
    state.pendingSelection = { item, combo, isAddon, sides, choices, passId };
    let title;
    if (isAddon) title = `${ui.addon_customize || '加購客製化 - '}${item.name}`;
    else title = combo ? `${ui.customize_for || '客製化選擇 - '}${item.name} ${ui.combo_prefix || '[套餐: '}${combo.name}]` : `${ui.customize_for || '客製化選擇 - '}${item.name}`;
    $('modifierModalTitle').textContent = title;

    const isComboMode = Boolean(combo || sides.length);
    const mainTitle = isAddon ? `${ui.addon_prefix || '加購：'}${item.name}` : (isComboMode ? `${ui.main_prefix || '主餐：'}${item.name}` : item.name);
    Kiosk.setHtml($('modifierModalBody'), [
        hasSizes ? sizeSectionHtml(item) : '',
        hasMain ? modifierSectionHtml('main', mainTitle, item.modifiers) : '',
        sides.map((side, i) => modifierSectionHtml(`side${i}`, `${ui.side_prefix || '配餐：'}${side.name}`, side.modifiers)),
        choices.map((g, gi) => choiceSectionHtml(g, gi)),
    ]);
    syncChoiceMods();
    modal('modifierModal').show();
}

/** 套餐任選群組：單選 (售完的選項不能選)，選項可客製時顯示該選項的客製化 */
export function choiceSectionHtml(g, gi) {
    const first = g.options.find((o) => o.is_available !== false);
    return html`
        <div class="mod-section p-3 mb-3 rounded border border-info bg-dark shadow-sm" data-key="choice${gi}" data-choice-group="${gi}">
            <h6 class="fw-bold text-info mb-2 border-bottom border-secondary pb-1">${g.group}${ui.pick_one || ' (任選一項)'}</h6>
            <div class="d-flex flex-wrap gap-2" role="radiogroup">
                ${g.options.map((o) => html`
                    <input type="radio" class="btn-check" name="combo-choice-${gi}" id="choice-${gi}-${o.id}" value="${o.id}"
                           data-mod="choice" ${o.is_available === false ? 'disabled' : ''} ${first && o.id === first.id ? 'checked' : ''}>
                    <label class="btn btn-outline-info btn-sm" for="choice-${gi}-${o.id}">${o.name}${Number(o.extra_price) ? ` (+$${o.extra_price})` : ''}${o.is_available === false ? (ui.sold_out_paren || ' (售完)') : ''}</label>`)}
            </div>
            ${g.options.filter((o) => o.customizable && o.modifiers && o.modifiers !== 'none').map((o) => html`
                <div class="choice-mods mt-2 d-none" data-opt="${o.id}">
                    ${modifierSectionHtml(`choice${gi}-${o.id}`, `${o.name}${ui.customize_suffix || ' 客製化'}`, o.modifiers)}
                </div>`)}
        </div>`;
}

export function syncChoiceMods() {
    document.querySelectorAll('#modifierModalBody [data-choice-group]').forEach((sec) => {
        const checked = sec.querySelector('[data-mod="choice"]:checked');
        sec.querySelectorAll('.choice-mods').forEach((box) => {
            box.classList.toggle('d-none', !checked || box.dataset.opt !== checked.value);
        });
    });
}

/** 讀取一個客製化區塊：回傳 { text: 冰塊/甜度或辣度文字, modifierIds: 勾選的加料 } */
export function readSection(section) {
    if (!section) return { text: '', modifierIds: [] };
    const val = (name) => {
        const el = section.querySelector(`[data-mod="${name}"]`);
        return el ? el.value : '';
    };
    const parts = [];
    if (val('ice') && val('sugar')) parts.push(`${val('ice')}/${val('sugar')}`);
    if (val('spicy')) parts.push(val('spicy'));
    const modifierIds = [...section.querySelectorAll('[data-mod="addon"]:checked')].map((cb) => Number(cb.dataset.id));
    return { text: parts.join('、'), modifierIds };
}

export function confirmModifiers() {
    const sel = state.pendingSelection;
    if (!sel) return;
    const body = $('modifierModalBody');
    const texts = [];
    const modifierIds = [];

    const main = readSection(body.querySelector('.mod-section[data-key="main"]'));
    if (main.text) texts.push(main.text);
    modifierIds.push(...main.modifierIds);

    sel.sides.forEach((side, i) => {
        const s = readSection(body.querySelector(`.mod-section[data-key="side${i}"]`));
        if (s.text) texts.push(`${side.name}(${s.text})`);
        modifierIds.push(...s.modifierIds);
    });

    const choiceIds = [];
    for (const [gi, g] of (sel.choices || []).entries()) {
        const checked = body.querySelector(`[data-choice-group="${gi}"] [data-mod="choice"]:checked`);
        if (!checked) {
            alert(`⚠️ ${(ui.pick_one_in || '請在「{g}」中選擇一項！').replace('{g}', g.group)}`);
            return;
        }
        choiceIds.push(Number(checked.value));
        const opt = g.options.find((o) => String(o.id) === checked.value);
        const box = body.querySelector(`[data-choice-group="${gi}"] .choice-mods[data-opt="${checked.value}"] .mod-section`);
        const c = readSection(box);
        if (c.text) texts.push(`${opt.name}(${c.text})`);
        modifierIds.push(...c.modifierIds);
    }
    const sizeInput = body.querySelector('[data-mod="size"]:checked');
    addLine(makeLine(sel.item, { combo: sel.combo, isAddon: sel.isAddon, customization: texts.join('、'), modifierIds,
        size: sizeInput ? sizeInput.value : '', choiceIds, passId: sel.passId || 0 }));
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
    const used = state.cart.filter((l) => Number(l.pass_id) === Number(p.id)).reduce((s, l) => s + l.quantity, 0);
    if (used >= Number(p.remaining)) {
        alert(`⚠️ ${(ui.pass_all_added || '寄杯「{n}」剩 {r} 次，已全部加入購物車').replace('{n}', p.name).replace('{r}', p.remaining)}`);
        return;
    }
    proceedToModifiers(item, null, false, p.id);
}

export function triggerAddon(itemId) {
    const addon = availableAddons.find((a) => Number(a.id) === Number(itemId));
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
    try {
        const saved = JSON.parse(localStorage.getItem(DIET_KEY) || 'null');
        if (saved && typeof saved === 'object') return { veg: !!saved.veg, mild: !!saved.mild, allergens: (saved.allergens || []).filter((x) => typeof x === 'string') };
    } catch (err) { /* 隱私模式 */ }
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
    try { localStorage.setItem(DIET_KEY, JSON.stringify(f)); } catch (err) { /* 隱私模式 */ }
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
    try { localStorage.removeItem(DIET_KEY); } catch (err) { /* 隱私模式 */ }
    filterMenu();
    applySort();
}
