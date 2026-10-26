/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：1~3. 分類與搜尋、點選商品 / 紅利兌換 / 套餐、客製化選項與規格。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import {
    $, afterHidden, cartRewardPoints, channelPrice, comboAvailable, hasMods, html, isItemAvailable, itemMap, modalOf,
    modifierOptions, num, REWARD_PREFIX, setHtml, state,
} from './core.js';
import { addLine, renderCart } from './cart.js';

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

// ======================================================================
// 2. 點選商品 / 紅利兌換 / 套餐
// ======================================================================
export function pickItem(card) {
    if (card.dataset.soldout === '1') return;
    const item = itemMap.get(Number(card.dataset.itemId));
    if (!item) return;

    // 在「🎁 紅利兌換」分頁點擊紅利商品，直接走紅利兌換
    if (state.category === '__reward__' && item.is_reward) {
        redeemItem(item);
        return;
    }
    if ((item.combos || []).length > 0) {
        openComboModal(item);
    } else {
        startConfigure({ item, combo: null, mode: 'normal' });
    }
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
    startConfigure({ item, combo: null, mode: 'reward' });
}

export function openComboModal(item) {
    state.comboItem = item;
    $('posComboModalTitle').textContent = `【${item.name}】選擇單點或套餐`;
    const single = html`
        <div class="col-md-6">
            <div class="pos-combo-card p-3 rounded-3 h-100 d-flex flex-column justify-content-between"
                 data-action="chooseCombo" data-combo-id="0" data-soldout="0">
                <div>
                    <div class="d-flex justify-content-between mb-2">
                        <span class="badge bg-secondary">單點主餐</span>
                        <span class="fw-bold text-warning fs-5">$${num(item.price)}</span>
                    </div>
                    <h6 class="fw-bold text-white">${item.name} (單點)</h6>
                    <small class="text-white-50">僅單點主餐，不附配餐。</small>
                </div>
                <div class="text-end mt-3">
                    <span class="btn btn-sm btn-outline-light fw-bold">選擇單點 <i class="bi bi-chevron-right"></i></span>
                </div>
            </div>
        </div>`;
    const combos = item.combos.map((c) => {
        const sold = !comboAvailable(c);
        return html`
        <div class="col-md-6">
            <div class="pos-combo-card is-combo p-3 rounded-3 h-100 d-flex flex-column justify-content-between ${sold ? 'pos-item-soldout' : ''}"
                 data-action="chooseCombo" data-combo-id="${c.id}" data-soldout="${sold ? 1 : 0}">
                <div>
                    <div class="d-flex justify-content-between mb-2">
                        <span class="badge ${sold ? 'bg-danger' : 'bg-warning text-dark fw-bold'}">${sold ? '配餐售罄' : '升級套餐'}</span>
                        <span class="fw-bold text-danger fs-5">+$${num(c.price)} <small class="text-white-50 fs-6">($${num(item.price) + num(c.price)})</small></span>
                    </div>
                    <h6 class="fw-bold text-warning">${c.name}</h6>
                    <small class="text-white-50">${c.description || ''}</small>
                </div>
                <div class="text-end mt-3">
                    <span class="btn btn-sm ${sold ? 'btn-secondary' : 'btn-warning text-dark'} fw-bold">${sold ? '配餐缺貨' : '升級此套餐'}</span>
                </div>
            </div>
        </div>`;
    });
    setHtml($('posComboCardsContainer'), [single, combos]);
    modalOf('posComboModal').show();
}

export function chooseCombo(el) {
    if (el.dataset.soldout === '1' || !state.comboItem) return;
    const item = state.comboItem;
    const comboId = Number(el.dataset.comboId);
    const combo = comboId ? item.combos.find((c) => Number(c.id) === comboId) : null;
    afterHidden('posComboModal', () => startConfigure({ item, combo, mode: 'normal' }));
}

// ======================================================================
// 3. 客製化選項
// ======================================================================
export const ICE_OPTIONS = ['正常冰', '少冰', '去冰', '熱飲'];
export const SUGAR_OPTIONS = ['全糖', '半糖', '微糖', '無糖'];
export const SPICY_OPTIONS = ['不辣', '微辣', '中辣', '大辣'];

export function selectHtml(id, cls, label, options) {
    return html`
        <label class="form-label small text-white-50 mb-1" for="${id}">${label}</label>
        <select id="${id}" class="form-select form-select-sm ${cls}">
            ${options.map((o) => html`<option value="${o}">${o}</option>`)}
        </select>`;
}

export function modifierGroupHtml(key, title, kind) {
    let body = '';
    if (kind === 'ice_sugar') {
        body = html`<div class="row g-2">
            <div class="col-6">${selectHtml(`pos-mod-${key}-ice`, 'mod-ice', '冰塊', ICE_OPTIONS)}</div>
            <div class="col-6">${selectHtml(`pos-mod-${key}-sugar`, 'mod-sugar', '甜度', SUGAR_OPTIONS)}</div>
        </div>`;
    } else if (kind === 'spicy') {
        body = html`<div>${selectHtml(`pos-mod-${key}-spicy`, 'mod-spicy', '辣度', SPICY_OPTIONS)}</div>`;
    } else if (kind === 'addons') {
        const addons = modifierOptions.filter((m) => m.category === 'addons');
        body = addons.length ? html`<div class="d-flex flex-wrap gap-3">
            ${addons.map((m) => html`
                <div class="form-check">
                    <input class="form-check-input mod-addon" type="checkbox" id="pos-mod-${key}-addon-${m.id}" data-id="${m.id}">
                    <label class="form-check-label small text-white" for="pos-mod-${key}-addon-${m.id}">${m.name} (+$${num(m.price)})</label>
                </div>`)}
        </div>` : html`<div class="small text-white-50">目前沒有可選的加料項目</div>`;
    }
    return html`<div class="p-3 mb-2 rounded bg-dark border border-secondary" data-group="${key}" data-kind="${kind}">
        <div class="fw-bold text-warning small mb-2 border-bottom border-secondary pb-1">${title}</div>
        ${body}
    </div>`;
}

/** 讀取一組客製化選項，回傳 { parts: 顯示文字, extra: 加價, ids: 加料 ID } */
export function collectGroup(key) {
    const group = document.querySelector(`#posModifierModalBody [data-group="${key}"]`);
    const result = { parts: [], extra: 0, ids: [] };
    if (!group) return result;
    const val = (cls) => (group.querySelector(`.${cls}`) || {}).value;
    switch (group.dataset.kind) {
        case 'ice_sugar':
            if (val('mod-ice') && val('mod-sugar')) result.parts.push(`${val('mod-ice')}/${val('mod-sugar')}`);
            break;
        case 'spicy':
            if (val('mod-spicy')) result.parts.push(val('mod-spicy'));
            break;
        case 'addons':
            group.querySelectorAll('.mod-addon:checked').forEach((cb) => {
                const opt = modifierOptions.find((m) => Number(m.id) === Number(cb.dataset.id));
                if (!opt) return;
                result.ids.push(Number(opt.id));
                result.extra += num(opt.price);
                result.parts.push(`${opt.name}(+${num(opt.price)})`);
            });
            break;
        default:
            break;
    }
    return result;
}

/** 規格 (單選)：第一個為預設；紅利兌換與加購品不適用 */
export function itemSizes(ctx) {
    return ctx.mode === 'normal' ? (ctx.item.sizes || []) : [];
}

export function sizeGroupHtml(sizes) {
    const sign = (d) => (d > 0 ? ` (+$${d})` : (d < 0 ? ` (-$${-d})` : ''));
    return html`<div class="p-3 mb-2 rounded bg-dark border border-warning" data-group="size" data-kind="size">
        <div class="fw-bold text-warning small mb-2 border-bottom border-secondary pb-1">規格</div>
        <div class="d-flex flex-wrap gap-2" role="radiogroup">
            ${sizes.map((s, i) => html`
                <input type="radio" class="btn-check pos-size" name="pos-size" id="pos-size-${i}" value="${s.name}" data-delta="${num(s.delta)}" ${i === 0 ? 'checked' : ''}>
                <label class="btn btn-outline-warning btn-sm" for="pos-size-${i}">${s.name}${sign(num(s.delta))}</label>`)}
        </div>
    </div>`;
}

export function startConfigure(ctx) {
    const mainMods = ctx.item.modifiers;
    const sizes = ctx.mode === 'pass' ? [] : itemSizes(ctx);
    const sides = ctx.combo ? (ctx.combo.sides || []).filter((s) => s.customizable && hasMods(s.modifiers)) : [];
    const choices = ctx.combo ? (ctx.combo.choices || []) : [];
    if (!hasMods(mainMods) && sides.length === 0 && !sizes.length && !choices.length) {
        addLine(makeLine(ctx, [], 0, []));
        return;
    }
    state.pending = { ...ctx, sides, choices };
    const titleName = ctx.mode === 'reward' ? REWARD_PREFIX + ctx.item.name : ctx.item.name;
    $('posModifierModalTitle').textContent = ctx.mode === 'addon' ? `加購客製化 - ${titleName}` : `客製化 - ${titleName}`;
    const groups = [];
    if (sizes.length) groups.push(sizeGroupHtml(sizes));
    if (hasMods(mainMods)) groups.push(modifierGroupHtml('main', ctx.mode === 'addon' ? `加購：${ctx.item.name}` : titleName, mainMods));
    sides.forEach((s, idx) => groups.push(modifierGroupHtml(`side${idx}`, `配餐：${s.name}`, s.modifiers)));
    choices.forEach((g, gi) => groups.push(choiceGroupHtml(g, gi)));
    setHtml($('posModifierModalBody'), groups);
    syncChoiceMods();
    modalOf('posModifierModal').show();
}

/** 套餐任選群組：單選 (售完的選項不能選)，選項可客製時顯示該選項的客製化 */
export function choiceGroupHtml(g, gi) {
    const first = g.options.find((o) => isItemAvailable(o.menu_item_id));
    return html`<div class="p-3 mb-2 rounded bg-dark border border-info" data-choice-group="${gi}">
        <div class="fw-bold text-info small mb-2 border-bottom border-secondary pb-1">${g.group} (任選一項)</div>
        <div class="d-flex flex-wrap gap-2" role="radiogroup">
            ${g.options.map((o) => html`
                <input type="radio" class="btn-check pos-choice" name="pos-choice-${gi}" id="pos-choice-${gi}-${o.id}" value="${o.id}"
                       ${isItemAvailable(o.menu_item_id) ? '' : 'disabled'} ${first && o.id === first.id ? 'checked' : ''}>
                <label class="btn btn-outline-info btn-sm" for="pos-choice-${gi}-${o.id}">${o.name}${num(o.extra_price) ? ` (+$${num(o.extra_price)})` : ''}</label>`)}
        </div>
        ${g.options.filter((o) => o.customizable && hasMods(o.modifiers)).map((o) => html`
            <div class="pos-choice-mods mt-2 d-none" data-opt="${o.id}">
                ${modifierGroupHtml(`choice${gi}-${o.id}`, `${o.name} 客製化`, o.modifiers)}
            </div>`)}
    </div>`;
}

export function syncChoiceMods() {
    document.querySelectorAll('#posModifierModalBody [data-choice-group]').forEach((sec) => {
        const checked = sec.querySelector('.pos-choice:checked');
        sec.querySelectorAll('.pos-choice-mods').forEach((box) => {
            box.classList.toggle('d-none', !checked || box.dataset.opt !== checked.value);
        });
    });
}

export function confirmModifier() {
    const ctx = state.pending;
    if (!ctx) return;
    const parts = [];
    let extra = 0;
    const ids = [];
    if (hasMods(ctx.item.modifiers)) {
        const main = collectGroup('main');
        parts.push(...main.parts);
        extra += main.extra;
        ids.push(...main.ids);
    }
    ctx.sides.forEach((side, idx) => {
        const g = collectGroup(`side${idx}`);
        extra += g.extra;
        ids.push(...g.ids);
        if (g.parts.length) parts.push(`${side.name}(${g.parts.join('、')})`);
    });
    const choiceIds = [];
    for (const [gi, g] of (ctx.choices || []).entries()) {
        const checked = document.querySelector(`#posModifierModalBody [data-choice-group="${gi}"] .pos-choice:checked`);
        if (!checked) {
            alert(`⚠️ 請在「${g.group}」中選擇一項！`);
            return;
        }
        choiceIds.push(Number(checked.value));
        const opt = g.options.find((o) => String(o.id) === checked.value);
        const c = collectGroup(`choice${gi}-${checked.value}`);
        extra += c.extra;
        ids.push(...c.ids);
        if (c.parts.length) parts.push(`${opt.name}(${c.parts.join('、')})`);
    }
    const sizeInput = document.querySelector('#posModifierModalBody .pos-size:checked');
    state.pending = null;
    addLine(makeLine(ctx, parts, extra, ids, sizeInput ? sizeInput.value : '', choiceIds));
    modalOf('posModifierModal').hide();
}

/** 建立購物車品項 (價格與後端 order_service._parse_lines 相同規則，僅供預覽) */
export function sizeDelta(item, size) {
    const sizes = item.sizes || [];
    if (!sizes.length) return 0;
    return num((sizes.find((s) => s.name === size) || sizes[0]).delta);
}

/** 套餐任選：選中的選項 → { ids, extra, labels } */
export function comboChoiceInfo(combo, ids) {
    const out = { ids: [], extra: 0, labels: [] };
    ((combo && combo.choices) || []).forEach((g) => {
        const opt = g.options.find((o) => ids.includes(Number(o.id)));
        if (!opt) return;
        out.ids.push(Number(opt.id));
        out.extra += num(opt.extra_price);
        out.labels.push(`${g.group}:${opt.name}${num(opt.extra_price) ? `(+${num(opt.extra_price)})` : ''}`);
    });
    return out;
}

export function makeLine({ item, combo, mode, passId }, parts, extra, modifierIds, size = '', choiceIds = []) {
    const choice = comboChoiceInfo(combo, choiceIds);
    const sizes = mode === 'normal' ? (item.sizes || []) : [];
    const sizeName = sizes.length ? (sizes.find((s) => s.name === size) || sizes[0]).name : '';
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
    if (mode === 'pass') {   // 寄杯兌換：免費 (只收加料)
        line.price = extra;
        line.customization = ['[寄杯]', ...parts].join('、');
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
        line.price = Math.max(0, channelPrice(item) + sizeDelta(item, sizeName)) + line.extra_price;
        const named = sizeName ? [sizeName, ...parts] : parts;
        line.customization = (combo ? [`[套餐: ${combo.name}]`, ...choice.labels, ...named] : named).join('、');
    }
    return line;
}

/** 切換內用 / 外帶 / 外送單時，一般品項改用該通路的價格 (改過價的品項不變) */
export function repriceCart() {
    state.cart.forEach((l) => {
        if (l.is_reward || l.is_add_on || l.pass_id || l.override_price !== undefined) return;
        const item = itemMap.get(l.menu_item_id);
        if (item) l.price = Math.max(0, channelPrice(item) + sizeDelta(item, l.size)) + num(l.extra_price);
    });
    renderCart();
}
