/*
 * 櫃檯 POS 收銀頁 (templates/pos.html)。
 * - 菜單、加料、紅利設定與公開優惠券由 page-data 提供 (menu_service.storefront_payload)。
 * - 結帳送出結構化品項 {menu_item_id, quantity, modifier_ids, combo_option_id, is_add_on, is_reward, customization}，
 *   金額一律以後端計算為準，前端只做預覽。
 * - 所有動態內容以 Kiosk.html (自動跳脫) 或 textContent 寫入 DOM。
 */
(function () {
    'use strict';

    const { html, setHtml, api } = Kiosk;
    const $ = (id) => document.getElementById(id);
    const modalOf = (id) => bootstrap.Modal.getOrCreateInstance($(id));

    const pageData = Kiosk.pageData();
    const itemMap = new Map((pageData.items || []).map((it) => [Number(it.id), it]));
    const modifierOptions = pageData.modifiers || [];
    const rewardConfig = pageData.reward || {};
    const publicCoupons = pageData.publicCoupons || [];
    const HOLD_STORAGE_KEY = `pos_held_orders_v2_tenant_${pageData.tenantId || 1}`;
    const NON_MAIN_CATEGORIES = ['飲料', '點心', '副餐', '配餐'];
    const REWARD_PREFIX = '🎁 [點數兌換] ';
    const SUBMIT_LABEL = html`<i class="bi bi-cash-stack me-1"></i> 收銀結帳 (F2)`;

    const state = {
        cart: [],              // 購物車品項 (見 makeLine)
        member: null,          // { id, name, phone, points, has_pin, coupons }
        category: 'all',
        promoCode: '',         // 已驗證成功的優惠碼
        promoDiscount: 0,
        promoSeq: 0,           // 優惠券驗證請求序號：只採用最新一次請求的結果
        comboItem: null,       // 正在選擇套餐的主餐
        pending: null,         // 正在客製化的品項 { item, combo, mode: normal | reward | addon }
        finalTotal: 0,
        checkout: null,        // { id: client_order_id, sig: 結帳內容 }：內容不變的重送沿用同一個 id，確保只建立一張單
        submitting: false,
        liveOrders: [],
        unpaidTables: [],      // 桌邊掃碼點餐尚未結帳的桌位
        liveFilter: 'all',
    };

    // ======================================================================
    // 小工具
    // ======================================================================
    const num = (v) => Number(v) || 0;
    const sumBy = (list, fn) => list.reduce((s, x) => s + fn(x), 0);
    const hasMods = (mods) => Boolean(mods) && mods !== 'none';
    const isItemAvailable = (id) => {
        const it = itemMap.get(Number(id));
        return Boolean(it && it.is_available);
    };
    const comboAvailable = (combo) => combo.is_available !== false && (combo.sides || []).every((s) => isItemAvailable(s.menu_item_id));
    const cartSubtotal = () => sumBy(state.cart, (l) => l.price * l.quantity);
    const cartRewardPoints = () => sumBy(state.cart.filter((l) => l.is_reward), (l) => l.points_cost * l.quantity);
    const displayName = (line) => (line.is_reward ? REWARD_PREFIX : '') + line.name;
    const pad3 = (v) => String(v).padStart(3, '0');

    /** 一個 Modal 關閉動畫結束後再執行 (避免兩個 Modal 交疊造成背景遮罩殘留) */
    function afterHidden(id, fn) {
        const el = $(id);
        if (!el.classList.contains('show')) {
            fn();
            return;
        }
        el.addEventListener('hidden.bs.modal', fn, { once: true });
        modalOf(id).hide();
    }

    function storageGet(key, fallback) {
        try {
            return JSON.parse(localStorage.getItem(key) || 'null') || fallback;
        } catch (err) {
            return fallback;
        }
    }

    function storageSet(key, value) {
        try {
            localStorage.setItem(key, JSON.stringify(value));
        } catch (err) { /* 無痕模式或空間不足：掛單只保留在畫面上 */ }
    }

    // ======================================================================
    // 1. 分類與搜尋過濾
    // ======================================================================
    function selectCategory(btn) {
        state.category = btn.dataset.cat;
        document.querySelectorAll('#posCategoryBar .cat-pill').forEach((b) => b.classList.toggle('active', b === btn));
        filterMenu();
    }

    function filterMenu() {
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

    function clearSearch() {
        $('posSearchInput').value = '';
        filterMenu();
    }

    // ======================================================================
    // 2. 點選商品 / 紅利兌換 / 套餐
    // ======================================================================
    function pickItem(card) {
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

    function redeemItem(item) {
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

    function openComboModal(item) {
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

    function chooseCombo(el) {
        if (el.dataset.soldout === '1' || !state.comboItem) return;
        const item = state.comboItem;
        const comboId = Number(el.dataset.comboId);
        const combo = comboId ? item.combos.find((c) => Number(c.id) === comboId) : null;
        afterHidden('posComboModal', () => startConfigure({ item, combo, mode: 'normal' }));
    }

    // ======================================================================
    // 3. 客製化選項
    // ======================================================================
    const ICE_OPTIONS = ['正常冰', '少冰', '去冰', '熱飲'];
    const SUGAR_OPTIONS = ['全糖', '半糖', '微糖', '無糖'];
    const SPICY_OPTIONS = ['不辣', '微辣', '中辣', '大辣'];

    function selectHtml(id, cls, label, options) {
        return html`
            <label class="form-label small text-white-50 mb-1" for="${id}">${label}</label>
            <select id="${id}" class="form-select form-select-sm ${cls}">
                ${options.map((o) => html`<option value="${o}">${o}</option>`)}
            </select>`;
    }

    function modifierGroupHtml(key, title, kind) {
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
    function collectGroup(key) {
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

    function startConfigure(ctx) {
        const mainMods = ctx.item.modifiers;
        const sides = ctx.combo ? (ctx.combo.sides || []).filter((s) => s.customizable && hasMods(s.modifiers)) : [];
        if (!hasMods(mainMods) && sides.length === 0) {
            addLine(makeLine(ctx, [], 0, []));
            return;
        }
        state.pending = { ...ctx, sides };
        const titleName = ctx.mode === 'reward' ? REWARD_PREFIX + ctx.item.name : ctx.item.name;
        $('posModifierModalTitle').textContent = ctx.mode === 'addon' ? `加購客製化 - ${titleName}` : `客製化 - ${titleName}`;
        const groups = [];
        if (hasMods(mainMods)) groups.push(modifierGroupHtml('main', ctx.mode === 'addon' ? `加購：${ctx.item.name}` : titleName, mainMods));
        sides.forEach((s, idx) => groups.push(modifierGroupHtml(`side${idx}`, `配餐：${s.name}`, s.modifiers)));
        setHtml($('posModifierModalBody'), groups);
        modalOf('posModifierModal').show();
    }

    function confirmModifier() {
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
        state.pending = null;
        addLine(makeLine(ctx, parts, extra, ids));
        modalOf('posModifierModal').hide();
    }

    /** 建立購物車品項 (價格與後端 order_service._parse_lines 相同規則，僅供預覽) */
    function makeLine({ item, combo, mode }, parts, extra, modifierIds) {
        const line = {
            menu_item_id: Number(item.id),
            name: item.name,
            category: item.category,
            quantity: 1,
            modifier_ids: modifierIds,
            combo_option_id: combo ? Number(combo.id) : null,
            is_add_on: mode === 'addon',
            is_reward: mode === 'reward',
            points_cost: mode === 'reward' ? num(item.redeem_points) : 0,
            can_addon: mode === 'normal' && (!combo || combo.can_addon !== false),
        };
        if (mode === 'reward') {
            line.price = extra;
            line.customization = parts.join('、') || '紅利免費兌換';
        } else if (mode === 'addon') {
            line.price = num(item.add_on_price) + extra;
            line.customization = parts.join('、');
        } else {
            line.price = num(item.price) + (combo ? num(combo.price) : 0) + extra;
            line.customization = (combo ? [`[套餐: ${combo.name}]`, ...parts] : parts).join('、');
        }
        return line;
    }

    // ======================================================================
    // 4. 購物車與條件加購 (每一份合格正價餐點最多搭配一份加購，與後端 _validate_addon_quota 相同)
    // ======================================================================
    function lineQualifiesForAddon(line, addon) {
        if (!line || line.is_add_on || line.is_reward || line.can_addon === false) return false;
        const cat = (line.category || '未分類').trim();
        const trigger = addon.addon_trigger_type || 'any';
        const target = (addon.addon_trigger_target || '').trim().toLowerCase();
        if (trigger === 'category') return cat.toLowerCase() === (target || (addon.category || '').trim().toLowerCase());
        // 後台存餐點名稱，舊資料可能存餐點 ID；兩種都接受
        if (trigger === 'item') return String(line.menu_item_id) === target || line.name.trim().toLowerCase() === target;
        return !NON_MAIN_CATEGORIES.includes(cat);
    }

    function mainUnits() {
        const units = [];
        state.cart.forEach((l) => {
            if (l.is_add_on || l.is_reward || l.can_addon === false) return;
            for (let k = 0; k < l.quantity; k++) units.push(l);
        });
        return units;
    }

    /** 二分圖配對：addonDefs 中每一份加購是否都能配到一份合格的正價餐點 */
    function addonsFeasible(addonDefs) {
        const mains = mainUnits();
        if (addonDefs.length > mains.length) return false;
        const owner = new Array(mains.length).fill(-1);
        const tryAssign = (a, seen) => {
            for (let m = 0; m < mains.length; m++) {
                if (seen[m] || !lineQualifiesForAddon(mains[m], addonDefs[a])) continue;
                seen[m] = true;
                if (owner[m] === -1 || tryAssign(owner[m], seen)) {
                    owner[m] = a;
                    return true;
                }
            }
            return false;
        };
        for (let a = 0; a < addonDefs.length; a++) {
            if (!tryAssign(a, new Array(mains.length).fill(false))) return false;
        }
        return true;
    }

    function availableAddons() {
        return [...itemMap.values()].filter((it) => it.can_be_add_on && it.is_available);
    }

    function cartAddonDefs() {
        const defs = [];
        state.cart.filter((l) => l.is_add_on).forEach((l) => {
            const def = itemMap.get(l.menu_item_id);
            if (def) for (let k = 0; k < l.quantity; k++) defs.push(def);
        });
        return defs;
    }

    /** 此加購品目前還能再加幾份 (不破壞既有加購配對) */
    function remainingAddonQuota(addon) {
        const current = cartAddonDefs();
        let n = 0;
        while (addonsFeasible(current.concat(new Array(n + 1).fill(addon)))) n++;
        return n;
    }

    function triggerAddon(btn) {
        const item = itemMap.get(Number(btn.dataset.id));
        if (!item || !item.can_be_add_on || remainingAddonQuota(item) <= 0) {
            alert('⚠️ 加購名額已達上限！');
            return;
        }
        startConfigure({ item, combo: null, mode: 'addon' });
    }

    const lineKey = (l) => JSON.stringify([l.menu_item_id, l.combo_option_id, l.customization, l.is_add_on, l.is_reward, l.modifier_ids]);

    function addLine(line) {
        const existing = state.cart.find((l) => lineKey(l) === lineKey(line));
        if (existing) existing.quantity += 1;
        else state.cart.push(line);
        renderCart();
    }

    function changeQty(idx, delta) {
        const line = state.cart[idx];
        if (!line) return;
        if (delta > 0 && line.is_reward) {
            if (!state.member || (state.member.points - cartRewardPoints()) < line.points_cost) {
                alert('⚠️ 會員剩餘紅利點數不足，無法再增加兌換數量！');
                return;
            }
        }
        if (delta > 0 && line.is_add_on) {
            const def = itemMap.get(line.menu_item_id);
            if (!def || remainingAddonQuota(def) <= 0) {
                alert('⚠️ 加購名額已達上限！');
                return;
            }
        }
        line.quantity += delta;
        if (line.quantity <= 0) state.cart.splice(idx, 1);
        renderCart();
    }

    function clearCart() {
        if (state.cart.length === 0) return;
        state.cart = [];
        resetPromo();
        renderCart();
    }

    /** 依購物車順序逐份保留加購，配不到合格餐點的加購自動減量 */
    function enforceAddonQuota() {
        const kept = [];
        state.cart.forEach((line) => {
            if (!line.is_add_on) return;
            const def = itemMap.get(line.menu_item_id);
            let allowed = 0;
            if (def) {
                for (let k = 0; k < line.quantity; k++) {
                    if (!addonsFeasible(kept.concat([def]))) break;
                    kept.push(def);
                    allowed++;
                }
            }
            line.quantity = allowed;
        });
        state.cart = state.cart.filter((l) => l.quantity > 0);
    }

    function renderCart({ revalidate = true } = {}) {
        enforceAddonQuota();
        const listEl = $('posCartList');
        const addonBox = $('posAddonRecommendBox');
        $('posCartCount').textContent = sumBy(state.cart, (l) => l.quantity);

        if (state.cart.length === 0) {
            setHtml(listEl, html`<div class="text-center text-white-50 py-4 small">尚未點選任何餐點</div>`);
            addonBox.textContent = '';
            recalcTotals();
            return;
        }

        setHtml(listEl, state.cart.map((line, idx) => html`
            <div class="pos-cart-row d-flex justify-content-between align-items-center py-1" data-cart-idx="${idx}">
                <div class="pe-2">
                    <div class="fw-bold text-white small">
                        <span class="pos-cart-name">${displayName(line)}</span>
                        ${line.is_add_on ? html`<span class="badge bg-info text-dark ms-1 pos-xxs">加購</span>` : ''}
                        ${line.is_reward ? html`<span class="badge bg-warning text-dark ms-1 pos-xxs">-${line.points_cost}點</span>` : ''}
                    </div>
                    ${line.customization ? html`<div class="text-info pos-cart-custom">${line.customization}</div>` : ''}
                    <div class="text-white-50 pos-xs">$${line.price} x ${line.quantity} = <strong class="text-warning">$${line.price * line.quantity}</strong></div>
                </div>
                <div class="btn-group btn-group-sm flex-shrink-0">
                    <button type="button" class="btn btn-outline-secondary text-white px-2 py-0" data-action="cartQty" data-idx="${idx}" data-delta="-1" aria-label="減少">-</button>
                    <button type="button" class="btn btn-outline-secondary text-white px-2 py-0" data-action="cartQty" data-idx="${idx}" data-delta="1" aria-label="增加">+</button>
                    <button type="button" class="btn btn-outline-danger px-2 py-0" data-action="cartQty" data-idx="${idx}" data-delta="-999" aria-label="移除"><i class="bi bi-x"></i></button>
                </div>
            </div>`));

        const eligible = availableAddons()
            .map((a) => ({ addon: a, quota: remainingAddonQuota(a) }))
            .filter((x) => x.quota > 0);
        if (eligible.length > 0) {
            setHtml(addonBox, html`
                <div class="p-2 rounded bg-dark border border-secondary mb-2">
                    <div class="small fw-bold text-info mb-1 pos-cart-custom"><i class="bi bi-stars me-1"></i>可加購優惠品</div>
                    <div class="d-flex gap-1 overflow-x-auto pb-1">
                        ${eligible.map(({ addon, quota }) => html`
                            <button type="button" class="btn btn-sm btn-outline-info py-0 px-2 flex-shrink-0 text-start pos-cart-custom"
                                    data-action="addon" data-id="${addon.id}">
                                + ${addon.name} <strong class="text-warning">$${num(addon.add_on_price)}</strong> <small>(餘${quota})</small>
                            </button>`)}
                    </div>
                </div>`);
        } else {
            addonBox.textContent = '';
        }

        recalcTotals();
        if (revalidate) revalidatePromo();
    }

    // ======================================================================
    // 5. 會員查詢 / 多筆候選 / PIN 設定
    // ======================================================================
    async function lookupMember() {
        const q = ($('posMemberQuery').value || '').trim();
        if (!q) return;
        try {
            const res = await api('/api/pos/member_lookup', { json: { query: q } });
            if (res.multiple && Array.isArray(res.candidates)) {
                setHtml($('posMemberSelectModalBody'), res.candidates.map((c) => html`
                    <div class="p-2 rounded bg-dark border border-secondary mb-2 d-flex justify-content-between align-items-center pos-member-candidate">
                        <div>
                            <div class="fw-bold text-white">${c.name} <span class="text-warning ms-1">${c.phone}</span></div>
                            <small class="text-white-50">可用紅利：${num(c.points)} 點 • 可用券：${(c.coupons || []).length} 張
                                ${c.has_pin ? '' : html`• <span class="text-danger">尚未設PIN</span>`}</small>
                        </div>
                        <button type="button" class="btn btn-sm btn-warning fw-bold text-dark" data-action="selectMember" data-id="${c.id}">
                            選擇此會員
                        </button>
                    </div>`));
                modalOf('posMemberSelectModal').show();
                return;
            }
            bindMember(res.member);
        } catch (err) {
            alert(err.message);
        }
    }

    async function selectMember(id) {
        try {
            const res = await api('/api/pos/member_lookup', { json: { member_id: Number(id) } });
            modalOf('posMemberSelectModal').hide();
            bindMember(res.member);
        } catch (err) {
            alert(err.message);
        }
    }

    function bindMember(member) {
        if (!member) return;
        state.member = { ...member, points: num(member.points) };
        const badge = $('posMemberStatusBadge');
        badge.className = 'badge bg-success pos-xxs';
        badge.textContent = '已綁定會員';
        $('posMemberSearchBox').classList.add('d-none');
        const box = $('posMemberBoundBox');
        box.classList.remove('d-none');
        box.classList.add('d-flex');
        $('posMemberName').textContent = member.name;
        $('posMemberPhone').textContent = `(${member.phone})`;
        const ptsRow = $('posPointsRedeemRow');
        if (ptsRow) {
            ptsRow.classList.remove('d-none');
            ptsRow.classList.add('d-flex');
        }
        populateCouponSelect();
        recalcTotals();
    }

    function clearMember() {
        state.member = null;
        state.cart = state.cart.filter((l) => !l.is_reward);   // 紅利兌換品必須綁定會員
        $('posMemberQuery').value = '';
        const badge = $('posMemberStatusBadge');
        badge.className = 'badge bg-secondary pos-xxs';
        badge.textContent = '未綁定 (一般訪客)';
        $('posMemberSearchBox').classList.remove('d-none');
        const box = $('posMemberBoundBox');
        box.classList.add('d-none');
        box.classList.remove('d-flex');
        const ptsRow = $('posPointsRedeemRow');
        if (ptsRow) {
            ptsRow.classList.add('d-none');
            ptsRow.classList.remove('d-flex');
        }
        const toggle = $('posUsePointsToggle');
        if (toggle) toggle.checked = false;
        const ptsInput = $('posPointsInput');
        if (ptsInput) {
            ptsInput.value = 0;
            ptsInput.disabled = true;
        }
        populateCouponSelect();
        renderCart();
    }

    function openPinModal() {
        if (!state.member) return;
        $('posPinMemberLabel').textContent = `${state.member.name} (${state.member.phone})`;
        $('posPinInput').value = '';
        $('posPinMsg').textContent = '';
        modalOf('posPinModal').show();
    }

    async function submitPin(ev) {
        ev.preventDefault();
        if (!state.member) return;
        const pin = ($('posPinInput').value || '').trim();
        const msg = $('posPinMsg');
        if (!/^\d{4,6}$/.test(pin)) {
            msg.textContent = '⚠️ PIN 碼格式不正確，請輸入 4 至 6 位純數字！';
            return;
        }
        const btn = $('posPinSubmitBtn');
        btn.disabled = true;
        try {
            const res = await api('/api/pos/set_member_pin', { json: { user_id: state.member.id, pin } });
            state.member.has_pin = true;
            modalOf('posPinModal').hide();
            alert(res.message || '設定成功');
        } catch (err) {
            msg.textContent = err.message;
        } finally {
            btn.disabled = false;
        }
    }

    // ======================================================================
    // 6. 優惠券 / 點數折抵 / 金額計算
    // ======================================================================
    const minSpendText = (c) => (num(c.min_spend) > 0 ? ` (滿$${Math.round(num(c.min_spend))})` : '');

    function populateCouponSelect() {
        const sel = $('posMemberCouponSelect');
        const memberCoupons = (state.member && state.member.coupons) || [];
        setHtml(sel, [
            html`<option value="">-- 選擇優惠券 --</option>`,
            memberCoupons.map((c) => html`<option value="${c.code}">${c.title}${minSpendText(c)}</option>`),
            publicCoupons.map((c) => html`<option value="${c.code}">[公開] ${c.label ? `[${c.label}] ` : ''}${c.title}${minSpendText(c)}</option>`),
        ]);
        sel.value = state.promoCode && [...sel.options].some((o) => o.value === state.promoCode) ? state.promoCode : '';
    }

    function setPromoMsg(ok, text) {
        const el = $('posPromoMsg');
        el.className = `small mb-1 pos-xs ${ok ? 'text-success' : 'text-danger'}`;
        setHtml(el, text ? html`<i class="bi ${ok ? 'bi-check-circle' : 'bi-x-circle'} me-1"></i>${text}` : '');
    }

    /** 清掉優惠券狀態，並讓進行中的驗證請求失效 */
    function resetPromo() {
        state.promoSeq++;
        state.promoCode = '';
        state.promoDiscount = 0;
        $('posPromoInput').value = '';
        $('posPromoMsg').textContent = '';
        $('posMemberCouponSelect').value = '';
    }

    function applyPromo() {
        const code = ($('posPromoInput').value || '').trim();
        if (!code) {
            state.promoSeq++;
            state.promoCode = '';
            state.promoDiscount = 0;
            $('posPromoMsg').textContent = '';
            recalcTotals();
            return;
        }
        runPromoCheck(code);
    }

    /** 購物車變動後重新驗證已套用的優惠券 (門檻與百分比折扣都會隨小計改變) */
    function revalidatePromo() {
        if (state.promoCode && state.cart.length) runPromoCheck(state.promoCode);
    }

    async function runPromoCheck(code) {
        const seq = ++state.promoSeq;
        try {
            const res = await api('/api/pos/verify_promo', {
                json: { promo_code: code, subtotal: cartSubtotal(), user_id: state.member ? state.member.id : 0 },
            });
            if (seq !== state.promoSeq) return;   // 已有更新的請求或已被重置，丟棄過期結果
            state.promoCode = res.valid ? code : '';
            state.promoDiscount = res.valid ? num(res.discount) : 0;
            setPromoMsg(res.valid, res.message);
            recalcTotals();
        } catch (err) {
            // 網路異常時保留原狀；結帳時後端仍會重新驗證優惠券
            if (seq === state.promoSeq) setPromoMsg(false, err.message);
        }
    }

    function maxPointsCash(remaining) {
        let maxCash = Math.floor(remaining);
        if (num(rewardConfig.maxDiscount) > 0) maxCash = Math.min(maxCash, num(rewardConfig.maxDiscount));
        return maxCash;
    }

    function togglePoints() {
        const toggle = $('posUsePointsToggle');
        const input = $('posPointsInput');
        if (!toggle || !input) return;
        if (toggle.checked && state.member) {
            input.disabled = false;
            const ppd = Math.max(1, num(rewardConfig.pointsPerDollar) || 1);
            const maxCash = maxPointsCash(Math.max(0, cartSubtotal() - state.promoDiscount));
            const availPts = Math.max(0, state.member.points - cartRewardPoints());
            const maxPts = Math.min(availPts, maxCash * ppd);
            input.value = Math.floor(maxPts / ppd) * ppd;
        } else {
            input.disabled = true;
            input.value = 0;
        }
        recalcTotals();
    }

    function recalcTotals() {
        const subtotal = cartSubtotal();
        const afterPromo = Math.max(0, subtotal - state.promoDiscount);
        let pointsDiscount = 0;
        const toggle = $('posUsePointsToggle');
        const input = $('posPointsInput');
        if (state.member && rewardConfig.isEnabled && toggle && toggle.checked && input) {
            const ppd = Math.max(1, num(rewardConfig.pointsPerDollar) || 1);
            const maxCash = maxPointsCash(afterPromo);
            const availPts = Math.max(0, state.member.points - cartRewardPoints());
            const val = Math.min(Math.max(0, num(input.value)), Math.min(availPts, maxCash * ppd));
            input.value = val;
            pointsDiscount = Math.min(Math.floor(val / ppd), maxCash);
        }
        if (state.member) $('posMemberPoints').textContent = Math.max(0, state.member.points - cartRewardPoints());

        // 與後端相同：應付金額四捨五入到整數元
        state.finalTotal = Math.floor(Math.max(0, afterPromo - pointsDiscount) + 0.5);
        $('posSubtotal').textContent = Math.round(subtotal);
        $('posPromoDiscount').textContent = Math.round(state.promoDiscount);
        $('posPointsDiscount').textContent = Math.round(pointsDiscount);
        $('posFinalTotal').textContent = state.finalTotal;
        updateCashChange();
    }

    // ======================================================================
    // 7. 用餐方式 / 付款方式 / 現金找零
    // ======================================================================
    function onOrderTypeChange() {
        $('posCutleryBox').classList.toggle('d-none', !$('posTypeTakeout').checked);
    }

    function setTable(value) {
        $('posTableInput').value = value;
        if (value === '外帶客') {
            $('posTypeTakeout').checked = true;
            onOrderTypeChange();
        }
    }

    function onPaymentChange() {
        const isCash = $('posPaymentMethod').value === 'Cash';
        $('posCashInputCol').classList.toggle('pos-invisible', !isCash);
        $('posQuickCashRow').classList.toggle('d-none', !isCash);
    }

    function quickCash(mode) {
        const input = $('posCashReceived');
        if (mode === 'exact') input.value = state.finalTotal;
        else if (mode === '+100') input.value = num(input.value) + 100;
        else input.value = num(mode);
        updateCashChange();
    }

    function updateCashChange() {
        const received = num($('posCashReceived').value);
        const badge = $('posChangeDisplay');
        const base = 'input-group-text bg-dark border-secondary fw-bold';
        if (received <= 0) {
            badge.className = `${base} text-white-50`;
            badge.textContent = '找零 $0';
        } else if (received < state.finalTotal) {
            badge.className = `${base} text-danger`;
            badge.textContent = `差 $${state.finalTotal - received}`;
        } else {
            badge.className = `${base} text-success`;
            badge.textContent = `找零 $${received - state.finalTotal}`;
        }
    }

    // ======================================================================
    // 8. 結帳送出與收據
    // ======================================================================
    function payloadLine(l) {
        return {
            menu_item_id: l.menu_item_id,
            quantity: l.quantity,
            modifier_ids: l.modifier_ids,
            combo_option_id: l.combo_option_id,
            is_add_on: l.is_add_on,
            is_reward: l.is_reward,
            customization: l.customization,
        };
    }

    function setSubmitting(on) {
        state.submitting = on;
        const btn = $('posSubmitBtn');
        btn.disabled = on;
        setHtml(btn, on ? html`<span class="spinner-border spinner-border-sm me-2"></span>結帳處理中...` : SUBMIT_LABEL);
    }

    function resetTerminal() {
        state.cart = [];
        resetPromo();          // 先重置優惠券，下一位顧客才不會繼承上一單的折扣
        clearMember();         // 內含 renderCart()
        $('posTableInput').value = '';
        $('posOrderNote').value = '';
        $('posCashReceived').value = '';
        updateCashChange();
    }

    async function submitOrder() {
        if (state.submitting) return;
        if (state.cart.length === 0) {
            alert('⚠️ 收銀點餐清單為空，請先點選餐點！');
            return;
        }
        const finalTotal = state.finalTotal;
        const payMethod = $('posPaymentMethod').value;
        const received = num($('posCashReceived').value);
        if (payMethod === 'Cash' && received > 0 && received < finalTotal) {
            alert(`⚠️ 實收現金 ($${received}) 小於應收總額 ($${finalTotal})！`);
            return;
        }
        const orderType = $('posTypeTakeout').checked ? '外帶' : '內用';
        const toggle = $('posUsePointsToggle');
        const usePoints = state.member && toggle && toggle.checked ? num($('posPointsInput').value) : 0;
        const payload = {
            items: state.cart.map(payloadLine),
            user_id: state.member ? state.member.id : 0,
            table_number: ($('posTableInput').value || '').trim(),
            order_type: orderType,
            payment_method: payMethod,
            need_cutlery: orderType === '外帶' ? $('posNeedCutlery').checked : false,
            note: ($('posOrderNote').value || '').trim(),
            promo_code: state.promoCode,
            use_points: usePoints,
            cash_received: received > 0 ? received : finalTotal,
        };
        // 同一份結帳內容重送 (例如連線逾時後再按一次) 沿用同一個 client_order_id，內容改變才換新的
        const sig = JSON.stringify(payload);
        if (!state.checkout || state.checkout.sig !== sig) state.checkout = { id: Kiosk.newOrderId(), sig };

        setSubmitting(true);
        try {
            const res = await api('/api/pos/checkout', { json: { ...payload, client_order_id: state.checkout.id } });
            state.checkout = null;
            showReceipt(res, res.replayed ? '此筆訂單先前已結帳 • 收據預覽' : '結帳成功 • 收據預覽');
            resetTerminal();
        } catch (err) {
            alert(`❌ ${err.message}`);
        } finally {
            setSubmitting(false);
            syncLiveState();
        }
    }

    function showReceipt(d, title) {
        $('rcpt-title').textContent = title;
        $('rcpt-pickup-no').textContent = `#${pad3(d.pickup_number || d.order_id)}`;
        $('rcpt-order-id').textContent = `#${d.order_id}`;
        $('rcpt-customer').textContent = d.table_number || d.user_name || '櫃檯POS';
        $('rcpt-order-type').textContent = d.order_type + (d.order_type === '外帶' ? (d.need_cutlery ? ' (附餐具)' : ' (免餐具)') : '');
        $('rcpt-cashier').textContent = d.cashier || 'POS';
        $('rcpt-time').textContent = d.created_at || new Date().toLocaleString();

        const noteBox = $('rcpt-note-box');
        noteBox.classList.toggle('d-none', !d.note);
        noteBox.textContent = d.note ? `備註：${d.note}` : '';

        setHtml($('rcpt-items-tbody'), (d.items || []).map((it) => html`
            <tr>
                <td>
                    <div class="fw-bold">${it.name || it.item_name}</div>
                    ${it.customization ? html`<div class="rcpt-custom">${it.customization}</div>` : ''}
                </td>
                <td class="text-center">x${num(it.quantity)}</td>
                <td class="text-end">$${num(it.subtotal) || num(it.price) * num(it.quantity)}</td>
            </tr>`));

        $('rcpt-subtotal').textContent = Math.round(num(d.subtotal) || num(d.total_price));
        $('rcpt-discount').textContent = Math.round(num(d.discount_amount));
        $('rcpt-total').textContent = Math.round(num(d.total_price));
        $('rcpt-pay-method').textContent = d.payment_method || 'Cash';
        $('rcpt-received').textContent = Math.round(d.cash_received !== undefined && d.cash_received !== null ? num(d.cash_received) : num(d.total_price));
        $('rcpt-change').textContent = Math.round(num(d.cash_change));

        const ptsBox = $('rcpt-points-box');
        const showPts = num(d.points_earned) > 0 || num(d.remaining_points) > 0;
        ptsBox.classList.toggle('d-none', !showPts);
        $('rcpt-earned-pts').textContent = `+${num(d.points_earned)} 點`;
        $('rcpt-remain-pts').textContent = `${num(d.remaining_points)} 點`;

        modalOf('posReceiptModal').show();
    }

    // ======================================================================
    // 9. 掛單 / 取單
    // ======================================================================
    const getHeld = () => {
        const list = storageGet(HOLD_STORAGE_KEY, []);
        return Array.isArray(list) ? list : [];
    };

    function saveHeld(list) {
        storageSet(HOLD_STORAGE_KEY, list);
        $('holdCountBadge').textContent = list.length;
    }

    function holdOrder() {
        if (state.cart.length === 0) {
            alert('⚠️ 目前沒有餐點可掛單！');
            return;
        }
        const list = getHeld();
        const table = ($('posTableInput').value || '').trim();
        list.push({
            id: Date.now(),
            label: table || (state.member ? state.member.name : `掛單 #${list.length + 1}`),
            time: new Date().toTimeString().split(' ')[0],
            cart: state.cart,
            member: state.member,
            table,
            orderType: $('posTypeTakeout').checked ? '外帶' : '內用',
            note: ($('posOrderNote').value || '').trim(),
            promoCode: state.promoCode,   // 取回時一律重新驗證，不沿用舊折抵金額
        });
        saveHeld(list);
        state.cart = [];
        resetPromo();
        clearMember();
        $('posTableInput').value = '';
        $('posOrderNote').value = '';
    }

    function openHoldModal() {
        const list = getHeld();
        setHtml($('posHoldModalBody'), list.length === 0
            ? html`<div class="text-center text-white-50 py-4">目前沒有任何暫存掛單</div>`
            : list.map((h, idx) => html`
                <div class="p-3 rounded bg-dark border border-secondary mb-2 d-flex justify-content-between align-items-center">
                    <div>
                        <div class="fw-bold text-warning">${h.label} <small class="text-white-50 ms-2">${h.time}</small></div>
                        <div class="small text-white-50">${(h.cart || []).map((l) => `${displayName(l)} x${num(l.quantity)}`).join('、')}</div>
                    </div>
                    <div class="d-flex gap-2">
                        <button type="button" class="btn btn-sm btn-warning fw-bold text-dark" data-action="recallHold" data-idx="${idx}">取回結帳</button>
                        <button type="button" class="btn btn-sm btn-outline-danger" data-action="removeHold" data-idx="${idx}" aria-label="刪除掛單"><i class="bi bi-trash"></i></button>
                    </div>
                </div>`));
        modalOf('posHoldModal').show();
    }

    function recallHold(idx) {
        const list = getHeld();
        const target = list[idx];
        if (!target) return;
        // 購物車還有餐點時拒絕取單，避免畫面上的餐點被覆蓋遺失
        if (state.cart.length > 0) {
            alert('⚠️ 目前購物車尚有餐點，請先結帳、掛單或清空後再取回掛單！');
            return;
        }
        resetPromo();
        state.cart = Array.isArray(target.cart) ? target.cart : [];
        if (target.member) bindMember(target.member);
        else clearMember();
        $('posTableInput').value = target.table || '';
        $('posOrderNote').value = target.note || '';
        $(target.orderType === '外帶' ? 'posTypeTakeout' : 'posTypeDineIn').checked = true;
        onOrderTypeChange();
        if (target.promoCode) {   // 先填入代碼，由 renderCart() 依目前小計與會員重新驗證
            state.promoCode = target.promoCode;
            $('posPromoInput').value = target.promoCode;
        }
        list.splice(idx, 1);
        saveHeld(list);
        renderCart();
        modalOf('posHoldModal').hide();
    }

    function removeHold(idx) {
        const list = getHeld();
        list.splice(idx, 1);
        saveHeld(list);
        openHoldModal();
    }

    // ======================================================================
    // 10. 即時庫存與當日訂單 (SSE 通知後重新抓取)
    // ======================================================================
    const STATUS_TEXT = { Pending: '製作中', Completed: '已出餐', PickedUp: '已取餐', Cancelled: '已取消' };
    const STATUS_BADGES = {
        Pending: html`<span class="badge bg-warning text-dark">⏳ 製作中</span>`,
        Completed: html`<span class="badge bg-success">✅ 待取餐</span>`,
        PickedUp: html`<span class="badge bg-secondary">📦 已取餐</span>`,
        Cancelled: html`<span class="badge bg-danger">❌ 已取消</span>`,
    };

    function applyStock(menuItems) {
        menuItems.forEach((m) => {
            const it = itemMap.get(Number(m.id));
            const soldOut = Boolean(m.is_sold_out) || num(m.stock) <= 0;
            if (it) {
                it.stock = m.stock;
                it.is_available = !soldOut;
            }
            const card = $(`pos-card-${m.id}`);
            if (card) {
                card.dataset.soldout = soldOut ? '1' : '0';
                card.classList.toggle('pos-item-soldout', soldOut);
            }
            const badge = $(`pos-stock-badge-${m.id}`);
            if (badge) {
                badge.textContent = soldOut ? '已售完' : `庫存 ${m.stock}`;
                badge.className = `badge pos-xxs ${soldOut ? 'bg-danger' : (num(m.stock) <= 5 ? 'bg-warning text-dark' : 'bg-dark border border-secondary text-white-50')}`;
            }
        });
    }

    async function syncLiveState() {
        let res;
        try {
            res = await api('/api/pos/today_orders');
        } catch (err) {
            return;   // 同步失敗略過，下一次通知會再抓
        }
        if (Array.isArray(res.menu_items)) {
            applyStock(res.menu_items);
            if (state.cart.length) renderCart({ revalidate: false });   // 加購品售完時重新整理推薦列
        }
        if (Array.isArray(res.orders)) {
            state.liveOrders = res.orders;
            $('topTodayOrderCount').textContent = res.orders.filter((o) => o.status !== 'Cancelled').length;
            renderLiveOrders();
        }
        if (Array.isArray(res.unpaid_tables)) {
            state.unpaidTables = res.unpaid_tables;
            if ($('topUnpaidTableCount')) $('topUnpaidTableCount').textContent = res.unpaid_tables.length;
            renderTables();
        }
    }

    // ---------------------------------------------------------------- 桌位結帳 (桌邊掃碼點餐)
    const TABLE_PAYMENTS = [['Cash', '現金'], ['Credit Card', '信用卡'], ['Line Pay', 'Line Pay'], ['街口支付', '街口支付'], ['Apple Pay', 'Apple Pay']];

    function openTables() {
        syncLiveState();
        modalOf('posTablesModal').show();
    }

    function renderTables() {
        const grid = $('posTablesGrid');
        if (!grid) return;
        const tables = state.unpaidTables || [];
        if (tables.length === 0) {
            setHtml(grid, html`<div class="col-12 text-center text-white-50 py-5">目前沒有未結帳的桌位</div>`);
            return;
        }
        setHtml(grid, tables.map((t) => html`
            <div class="col-md-6 col-lg-4">
                <div class="p-3 rounded bg-dark border border-info h-100 d-flex flex-column justify-content-between pos-table-card" data-table-id="${t.table_id}">
                    <div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <h5 class="fw-bold text-info mb-0">${t.name} 桌</h5>
                            <span class="badge bg-warning text-dark">${t.orders.length} 張未結帳</span>
                        </div>
                        <div class="border-top border-bottom border-secondary py-2 mb-2 pos-live-items">
                            ${t.orders.map((o) => html`
                                <div class="small text-white-50 mt-1">取餐 #${pad3(o.pickup_number || o.id)} • ${o.created_at} • ${STATUS_TEXT[o.status] || o.status}</div>
                                ${o.items.map((it) => html`
                                    <div class="d-flex justify-content-between small">
                                        <span class="text-white">${it.name} x${num(it.quantity)}</span>
                                        <span class="text-white-50">$${Math.round(num(it.price) * num(it.quantity))}</span>
                                    </div>`)}`)}
                        </div>
                    </div>
                    <div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span class="text-white-50">應收</span>
                            <span class="fw-bold text-warning fs-4">$${num(t.total)}</span>
                        </div>
                        <div class="row g-1 mb-2">
                            <div class="col-6">
                                <select class="form-select form-select-sm table-pay-method" aria-label="付款方式">
                                    ${TABLE_PAYMENTS.map(([v, label]) => html`<option value="${v}">${label}</option>`)}
                                </select>
                            </div>
                            <div class="col-6">
                                <input type="number" class="form-control form-control-sm table-cash" min="0" placeholder="實收 (空白=剛好)" aria-label="實收現金">
                            </div>
                        </div>
                        <div class="small text-info mb-2 table-change"></div>
                        <button type="button" class="btn btn-info fw-bold text-dark w-100" data-action="settleTable" data-id="${t.table_id}">
                            <i class="bi bi-cash-coin me-1"></i>結帳
                        </button>
                    </div>
                </div>
            </div>`));
    }

    function tableCardChanged(ev) {
        const card = ev.target.closest('.pos-table-card');
        if (!card) return;
        const t = (state.unpaidTables || []).find((x) => String(x.table_id) === card.dataset.tableId);
        const isCash = card.querySelector('.table-pay-method').value === 'Cash';
        const cashInput = card.querySelector('.table-cash');
        cashInput.disabled = !isCash;
        const received = num(cashInput.value);
        card.querySelector('.table-change').textContent = isCash && received
            ? (received >= num(t && t.total) ? `找零 $${received - num(t && t.total)}` : '實收金額不足') : '';
    }

    async function settleTable(btn) {
        const card = btn.closest('.pos-table-card');
        const method = card.querySelector('.table-pay-method').value;
        const cash = method === 'Cash' ? num(card.querySelector('.table-cash').value) : 0;
        btn.disabled = true;
        try {
            const res = await api(`/api/pos/tables/${encodeURIComponent(btn.dataset.id)}/settle`,
                { json: { payment_method: method, cash_received: cash } });
            alert(`✅ ${res.table} 桌結帳完成：應收 $${res.total}${res.change ? `，實收 $${res.cash_received}，找零 $${res.change}` : ''}`);
        } catch (err) {
            alert(`❌ ${err.message}`);
            btn.disabled = false;
        }
        syncLiveState();
    }

    function openLiveOrders() {
        syncLiveState();
        modalOf('posLiveOrdersModal').show();
    }

    function filterLiveOrders(btn) {
        state.liveFilter = btn.dataset.status;
        document.querySelectorAll('#posLiveFilterGroup .btn').forEach((b) => b.classList.toggle('active', b === btn));
        renderLiveOrders();
    }

    function renderLiveOrders() {
        const grid = $('posLiveOrdersGrid');
        const list = state.liveFilter === 'all' ? state.liveOrders : state.liveOrders.filter((o) => o.status === state.liveFilter);
        if (list.length === 0) {
            setHtml(grid, html`<div class="col-12 text-center text-white-50 py-5">目前沒有符合篩選條件的當日訂單</div>`);
            return;
        }
        setHtml(grid, list.map((o) => html`
            <div class="col-md-6 col-lg-4">
                <div class="p-3 rounded bg-dark border border-secondary h-100 d-flex flex-column justify-content-between pos-live-order" data-order-id="${o.id}">
                    <div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <h5 class="fw-bold text-warning mb-0">${o.pickup_pending ? '待分店確認' : `#${pad3(o.pickup_number || o.id)}`} <small class="text-white-50 fs-6">(單號 #${o.id})</small></h5>
                            <span>
                                ${o.payment_status === 'unpaid' ? html`<span class="badge bg-warning text-dark me-1">未結帳</span>` : ''}
                                ${STATUS_BADGES[o.status] || STATUS_BADGES.Pending}
                            </span>
                        </div>
                        <div class="small text-white-50 mb-2 d-flex justify-content-between">
                            <span><i class="bi bi-person me-1"></i>${o.user_name} • ${o.order_type}${o.pickup_node_name ? ` • ${o.pickup_node_name}` : ''}</span>
                            <span>${o.created_at}</span>
                        </div>
                        <div class="border-top border-bottom border-secondary py-2 mb-2 pos-live-items">
                            ${(o.items || []).map((it) => html`
                                <div class="d-flex justify-content-between small">
                                    <span class="text-white">${it.name || it.item_name} x${num(it.quantity)}</span>
                                    <span class="text-white-50">$${Math.round(num(it.price) * num(it.quantity))}</span>
                                </div>
                                ${it.customization ? html`<div class="text-danger pos-cart-custom">${it.customization}</div>` : ''}`)}
                        </div>
                    </div>
                    <div class="d-flex justify-content-between align-items-center pt-1">
                        <div>
                            <span class="fw-bold text-white fs-5">$${Math.round(num(o.total_price))}</span>
                            <small class="text-white-50 ms-1">(${o.payment_method})</small>
                        </div>
                        <div class="d-flex gap-1">
                            <button type="button" class="btn btn-sm btn-outline-light" data-action="reprint" data-id="${o.id}" title="補印收據" aria-label="補印收據">
                                <i class="bi bi-printer"></i>
                            </button>
                            ${o.status === 'Pending' ? html`
                                <button type="button" class="btn btn-sm btn-success fw-bold" data-action="orderStatus" data-id="${o.id}" data-status="Completed">出餐</button>
                                <button type="button" class="btn btn-sm btn-outline-danger" data-action="orderStatus" data-id="${o.id}" data-status="Cancelled">取消</button>` : ''}
                            ${o.status === 'Completed' ? html`
                                <button type="button" class="btn btn-sm btn-info fw-bold text-dark" data-action="orderStatus" data-id="${o.id}" data-status="PickedUp">核銷取餐</button>
                                <button type="button" class="btn btn-sm btn-outline-danger" data-action="orderStatus" data-id="${o.id}" data-status="Cancelled">取消</button>` : ''}
                        </div>
                    </div>
                </div>
            </div>`));
    }

    async function updateOrderStatus(orderId, status) {
        if (status === 'Cancelled' && !confirm(`確定要取消訂單 #${orderId} 並全額回滾庫存與紅利點數嗎？`)) return;
        try {
            await api(`/api/orders/${encodeURIComponent(orderId)}/status`, { json: { status } });
        } catch (err) {
            alert(`❌ ${err.message}`);
        }
        syncLiveState();
    }

    function reprint(orderId) {
        const o = state.liveOrders.find((x) => Number(x.id) === Number(orderId));
        if (!o) return;
        showReceipt({
            order_id: o.id,
            pickup_number: o.pickup_number || o.id,
            table_number: o.user_name,
            order_type: o.order_type,
            need_cutlery: o.need_cutlery,
            note: o.note,
            created_at: o.created_at,
            cashier: o.cashier_username || '補印收據',
            subtotal: num(o.total_price) + num(o.discount_amount),
            discount_amount: o.discount_amount,
            total_price: o.total_price,
            payment_method: o.payment_method,
            cash_received: o.cash_received,
            cash_change: o.cash_change,
            items: o.items,
        }, '補印收據 • 收據預覽');
    }

    function startLiveSync() {
        const dot = $('sse-status-dot');
        const setDot = (ok) => {
            dot.className = ok ? 'text-success' : 'text-warning';
            dot.textContent = ok ? '● 即時連線中' : '● 輪詢同步中';
        };
        if (!window.EventSource) {
            setDot(false);
            setInterval(syncLiveState, 5000);
            return;
        }
        const source = Kiosk.onOrdersChanged(syncLiveState);
        source.addEventListener('open', () => setDot(true));
        source.addEventListener('error', () => setDot(false));
    }

    // ======================================================================
    // 11. 事件綁定
    // ======================================================================
    Kiosk.on(document, 'click', {
        category: (el) => selectCategory(el),
        clearSearch: () => clearSearch(),
        pickItem: (el) => pickItem(el),
        redeemItem: (el) => redeemItem(itemMap.get(Number(el.dataset.itemId))),
        chooseCombo: (el) => chooseCombo(el),
        confirmModifier: () => confirmModifier(),
        addon: (el) => triggerAddon(el),
        cartQty: (el) => changeQty(Number(el.dataset.idx), Number(el.dataset.delta)),
        clearCart: () => clearCart(),
        lookupMember: () => lookupMember(),
        selectMember: (el) => selectMember(el.dataset.id),
        clearMember: () => clearMember(),
        openPinModal: () => openPinModal(),
        applyPromo: () => applyPromo(),
        setTable: (el) => setTable(el.dataset.value),
        quickCash: (el) => quickCash(el.dataset.mode),
        holdOrder: () => holdOrder(),
        submitOrder: () => submitOrder(),
        openHold: () => openHoldModal(),
        recallHold: (el) => recallHold(Number(el.dataset.idx)),
        removeHold: (el) => removeHold(Number(el.dataset.idx)),
        openLiveOrders: () => openLiveOrders(),
        openTables: () => openTables(),
        settleTable: (el) => settleTable(el),
        liveFilter: (el) => filterLiveOrders(el),
        orderStatus: (el) => updateOrderStatus(el.dataset.id, el.dataset.status),
        reprint: (el) => reprint(el.dataset.id),
        printReceipt: () => window.print(),
    });

    $('posSearchInput').addEventListener('input', filterMenu);
    if ($('posTablesGrid')) {
        $('posTablesGrid').addEventListener('input', tableCardChanged);
        $('posTablesGrid').addEventListener('change', tableCardChanged);
    }
    $('posMemberQuery').addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
            ev.preventDefault();
            lookupMember();
        }
    });
    $('posPromoInput').addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
            ev.preventDefault();
            applyPromo();
        }
    });
    $('posMemberCouponSelect').addEventListener('change', (ev) => {
        $('posPromoInput').value = ev.target.value || '';
        applyPromo();
    });
    document.querySelectorAll('input[name="posOrderType"]').forEach((r) => r.addEventListener('change', onOrderTypeChange));
    $('posPaymentMethod').addEventListener('change', onPaymentChange);
    $('posCashReceived').addEventListener('input', updateCashChange);
    const pointsToggle = $('posUsePointsToggle');
    if (pointsToggle) pointsToggle.addEventListener('change', togglePoints);
    const pointsInput = $('posPointsInput');
    if (pointsInput) pointsInput.addEventListener('input', recalcTotals);
    $('posPinForm').addEventListener('submit', submitPin);
    $('posPinModal').addEventListener('shown.bs.modal', () => $('posPinInput').focus());
    // 收據顯示後聚焦「關閉」按鈕，按 Enter 即可關閉並接續下一筆
    $('posReceiptModal').addEventListener('shown.bs.modal', () => $('rcpt-close-btn').focus());

    document.addEventListener('keydown', (ev) => {
        if (ev.key === 'F2') {
            ev.preventDefault();
            submitOrder();
        } else if (ev.key === 'F4') {
            ev.preventDefault();
            holdOrder();
        } else if (ev.key === 'Escape' && document.activeElement === $('posSearchInput')) {
            clearSearch();
        }
    });

    // ======================================================================
    // 啟動
    // ======================================================================
    Kiosk.startClock($('pos-clock'));
    populateCouponSelect();
    saveHeld(getHeld());
    renderCart();
    syncLiveState();
    startLiveSync();
})();
