/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：客製化視窗 (冰塊 / 甜度、辣度、加料、規格、套餐任選) 的 HTML 與讀回。
 * 兩頁用同一套標記：
 *   區塊  <div class="mod-section …" data-key="main|side0|choice0|choice0-<optId>|size" data-group=(同 data-key) data-kind="…">
 *   下拉  <select class="mod-ice|mod-sugar|mod-spicy" data-mod="ice|sugar|spicy">   加料 <input class="mod-addon" data-mod="addon" data-id>
 *   規格  <input class="mod-size" data-mod="size">   任選 <input class="mod-choice" data-mod="choice"> (群組 data-choice-group)
 * 外觀 (class、標題標籤、文字) 由頁面以 scheme 傳入：
 *   { idPrefix: '' | 'pos-', sectionCls, title(text, colorCls) → Kiosk.html, labels: { ice, sugar, spicy, addons, noAddons, size,
 *     pickOne, soldOut, customize }, values: { ice, sugar, spicy } (顯示文字 'a|b|c'，送出的值固定中文), addons: [加料選項],
 *     isOptionAvailable(option) }
 */
const { html } = Kiosk;

export const ICE_OPTIONS = ['正常冰', '少冰', '去冰', '熱飲'];
export const SUGAR_OPTIONS = ['全糖', '半糖', '微糖', '無糖'];
export const SPICY_OPTIONS = ['不辣', '微辣', '中辣', '大辣'];

/** 品項 / 配餐有客製化群組 (ice_sugar / spicy / addons)；'none' 或空值視為沒有 */
export const hasMods = (mods) => Boolean(mods) && mods !== 'none';

/** 規格加減價的顯示： (+$10) / (-$5) / 空字串 */
export const priceSign = (d) => (d > 0 ? ` (+$${d})` : (d < 0 ? ` (-$${-d})` : ''));

/** 下拉選項：值固定為中文 (送到廚房的客製化文字)，顯示文字依語言 (以 | 分隔，數量不符時顯示中文) */
function optionsHtml(values, shown) {
    const labels = typeof shown === 'string' ? shown.split('|') : [];
    return values.map((v, i) => html`<option value="${v}">${labels.length === values.length ? labels[i] : v}</option>`);
}

export function selectHtml(id, name, label, values, shown) {
    return html`
        <label class="form-label small text-white-50 mb-1" for="${id}">${label}</label>
        <select id="${id}" class="form-select form-select-sm mod-${name}" data-mod="${name}">${optionsHtml(values, shown)}</select>`;
}

/** 一個客製化區塊：kind = ice_sugar / spicy / addons */
export function modifierSectionHtml(scheme, key, title, kind) {
    const id = (suffix) => `${scheme.idPrefix}mod-${key}-${suffix}`;
    const L = scheme.labels;
    const values = scheme.values || {};
    let body = '';
    if (kind === 'ice_sugar') {
        body = html`
            <div class="row g-2">
                <div class="col-6">${selectHtml(id('ice'), 'ice', L.ice, ICE_OPTIONS, values.ice)}</div>
                <div class="col-6">${selectHtml(id('sugar'), 'sugar', L.sugar, SUGAR_OPTIONS, values.sugar)}</div>
            </div>`;
    } else if (kind === 'spicy') {
        body = html`<div>${selectHtml(id('spicy'), 'spicy', L.spicy, SPICY_OPTIONS, values.spicy)}</div>`;
    } else if (kind === 'addons') {
        const addons = scheme.addons || [];
        body = html`
            <div>
                ${L.addons ? html`<label class="form-label small text-white-50 mb-1">${L.addons}</label>` : ''}
                ${addons.length ? html`
                    <div class="d-flex flex-wrap gap-3">
                        ${addons.map((m) => html`
                            <div class="form-check">
                                <input class="form-check-input mod-addon" type="checkbox" data-mod="addon" data-id="${m.id}" id="${id(`addon-${m.id}`)}">
                                <label class="form-check-label small text-white" for="${id(`addon-${m.id}`)}">${m.name} (+$${Number(m.price) || 0})</label>
                            </div>`)}
                    </div>` : html`<div class="small text-white-50">${L.noAddons}</div>`}
            </div>`;
    }
    return html`
        <div class="mod-section ${scheme.sectionCls} border-secondary" data-key="${key}" data-group="${key}" data-kind="${kind}">
            ${scheme.title(title, 'text-warning')}
            ${body}
        </div>`;
}

/** 規格選擇 (單選)：第一個為預設 */
export function sizeSectionHtml(scheme, sizes) {
    const p = scheme.idPrefix;
    return html`
        <div class="mod-section ${scheme.sectionCls} border-warning" data-key="size" data-group="size" data-kind="size">
            ${scheme.title(scheme.labels.size, 'text-warning')}
            <div class="d-flex flex-wrap gap-2" role="radiogroup">
                ${sizes.map((s, i) => html`
                    <input type="radio" class="btn-check mod-size" name="${p}item-size" id="${p}size-${i}" value="${s.name}" data-mod="size"
                           data-delta="${Number(s.delta) || 0}" ${i === 0 ? 'checked' : ''}>
                    <label class="btn btn-outline-warning btn-sm" for="${p}size-${i}">${s.name}${priceSign(Number(s.delta) || 0)}</label>`)}
            </div>
        </div>`;
}

/** 套餐任選群組：單選 (售完的選項不能選)，選項可客製時顯示該選項的客製化區塊 */
export function choiceSectionHtml(scheme, g, gi) {
    const p = scheme.idPrefix;
    const L = scheme.labels;
    const options = g.options || [];
    const avail = (o) => scheme.isOptionAvailable(o);
    const first = options.find(avail);
    return html`
        <div class="mod-section ${scheme.sectionCls} border-info" data-key="choice${gi}" data-group="choice${gi}" data-kind="choice" data-choice-group="${gi}">
            ${scheme.title(`${g.group}${L.pickOne}`, 'text-info')}
            <div class="d-flex flex-wrap gap-2" role="radiogroup">
                ${options.map((o) => html`
                    <input type="radio" class="btn-check mod-choice" name="${p}combo-choice-${gi}" id="${p}choice-${gi}-${o.id}" value="${o.id}"
                           data-mod="choice" ${avail(o) ? '' : 'disabled'} ${first && o.id === first.id ? 'checked' : ''}>
                    <label class="btn btn-outline-info btn-sm" for="${p}choice-${gi}-${o.id}">${o.name}${Number(o.extra_price) ? ` (+$${Number(o.extra_price)})` : ''}${avail(o) ? '' : L.soldOut}</label>`)}
            </div>
            ${options.filter((o) => o.customizable && hasMods(o.modifiers)).map((o) => html`
                <div class="choice-mods mt-2 d-none" data-opt="${o.id}">
                    ${modifierSectionHtml(scheme, `choice${gi}-${o.id}`, `${o.name}${L.customize}`, o.modifiers)}
                </div>`)}
        </div>`;
}

/** 只顯示目前選中的任選選項的客製化區塊 (root 為視窗內容元素) */
export function syncChoiceMods(root) {
    root.querySelectorAll('[data-choice-group]').forEach((sec) => {
        const checked = sec.querySelector('[data-mod="choice"]:checked');
        sec.querySelectorAll('.choice-mods').forEach((box) => {
            box.classList.toggle('d-none', !checked || box.dataset.opt !== checked.value);
        });
    });
}

/**
 * 讀取一個客製化區塊 → { parts: [冰塊/甜度、辣度 (、加料文字)], modifierIds: 勾選的加料 }
 * describeAddon(id) 回傳加料的顯示文字 (POS 把加料寫進客製化文字)，回傳 null 表示不認得、略過；不傳時只收 ID
 */
export function readSection(section, describeAddon = null) {
    if (!section) return { parts: [], modifierIds: [] };
    const val = (name) => {
        const el = section.querySelector(`[data-mod="${name}"]`);
        return el ? el.value : '';
    };
    const parts = [];
    if (val('ice') && val('sugar')) parts.push(`${val('ice')}/${val('sugar')}`);
    if (val('spicy')) parts.push(val('spicy'));
    const modifierIds = [];
    section.querySelectorAll('[data-mod="addon"]:checked').forEach((cb) => {
        const id = Number(cb.dataset.id);
        const text = describeAddon ? describeAddon(id) : '';
        if (text === null || text === undefined) return;
        modifierIds.push(id);
        if (text) parts.push(text);
    });
    return { parts, modifierIds };
}

/**
 * 讀回整個客製化視窗：主餐、配餐 (sides)、任選群組 (choices) 與規格。
 * 回傳 { texts: 客製化文字片段 (配餐 / 任選為「名稱(文字)」), modifierIds, choiceIds, size, missing: 沒選的任選群組或 null }
 */
export function readModifierModal(root, { hasMain = true, sides = [], choices = [], describeAddon = null } = {}) {
    const texts = [];
    const modifierIds = [];
    const take = (section, wrap) => {
        const r = readSection(section, describeAddon);
        modifierIds.push(...r.modifierIds);
        if (r.parts.length) texts.push(wrap ? `${wrap}(${r.parts.join('、')})` : r.parts.join('、'));
    };
    if (hasMain) take(root.querySelector('[data-key="main"]'), '');
    sides.forEach((side, i) => take(root.querySelector(`[data-key="side${i}"]`), side.name));
    const choiceIds = [];
    for (const [gi, g] of choices.entries()) {
        const checked = root.querySelector(`[data-choice-group="${gi}"] [data-mod="choice"]:checked`);
        if (!checked) return { missing: g, texts, modifierIds, choiceIds, size: '' };
        choiceIds.push(Number(checked.value));
        const opt = (g.options || []).find((o) => String(o.id) === checked.value);
        take(root.querySelector(`[data-key="choice${gi}-${checked.value}"]`), opt ? opt.name : '');
    }
    const sizeInput = root.querySelector('[data-mod="size"]:checked');
    return { missing: null, texts, modifierIds, choiceIds, size: sizeInput ? sizeInput.value : '' };
}
