/*
 * 顧客自助點餐 (Kiosk) 頁面：菜單篩選排序、套餐/客製化/加購、購物車、優惠券、紅利折抵、結帳、我的訂單、閒置登出。
 * 菜單資料由後端以 JSON 嵌入 (page-data)，畫面上的使用者資料一律經 Kiosk.html 跳脫後才插入 DOM。
 *
 * 購物車存在 localStorage 'kiosk_cart' (紅利商城兌換的商品也會寫進來)，每一列格式：
 *   { menu_item_id, name, price(單價估算), quantity, customization, modifier_ids: [], combo_option_id,
 *     combo_name, is_add_on, is_reward, points_cost, can_addon, is_sold_out }
 * 金額僅供畫面預估，實際金額一律由後端依 menu_item_id / modifier_ids / combo_option_id 重新計算。
 */
(function () {
    'use strict';

    const { html, api } = Kiosk;
    const CART_KEY = 'kiosk_cart';
    const IDLE_TIMEOUT_MS = 10 * 60 * 1000;   // 顧客閒置 10 分鐘自動登出
    const NON_MAIN_CATEGORIES = ['飲料', '點心', '副餐', '配餐'];
    const REWARD_LABEL = '[點數兌換]';

    const pageData = Kiosk.pageData();

    // ---------------------------------------------------------------- localStorage (隱私模式可能無法使用)
    function loadCart() {
        try { return JSON.parse(localStorage.getItem(CART_KEY) || '[]'); } catch (err) { return []; }
    }
    function storeCart(lines) {
        try { localStorage.setItem(CART_KEY, JSON.stringify(lines)); } catch (err) { /* 忽略 */ }
    }
    function clearStoredCart() {
        try { localStorage.removeItem(CART_KEY); } catch (err) { /* 忽略 */ }
    }

    // 未登入的歡迎頁：清掉上一位顧客留下的購物車 (登出、閒置登出都會回到這裡)
    if (!pageData.loggedIn) {
        clearStoredCart();
        return;
    }

    const $ = (id) => document.getElementById(id);
    const modal = (id) => bootstrap.Modal.getOrCreateInstance($(id));

    const menu = pageData.menu || {};
    const rewardConfig = menu.reward || {};
    const isGuest = Boolean(pageData.isGuest);
    // 線上點餐 (線上端)：必須先選取餐分店；取餐號由分店接單後才產生
    const isOnline = Boolean(pageData.online);
    const menuItems = new Map((menu.items || []).map((i) => [Number(i.id), i]));
    const modifierOptions = menu.modifiers || [];
    const modifierById = new Map(modifierOptions.map((m) => [Number(m.id), m]));
    // 可加購的品項 (加購價 add_on_price；觸發條件與後端 _addon_qualifies 同一套規則)
    const availableAddons = (menu.items || []).filter((i) => i.can_be_add_on && i.is_available);

    let userPoints = Number(pageData.userPoints) || 0;
    let cart = loadCart().map(normalizeLine).filter(Boolean);
    let appliedPromoDiscount = 0;
    let activePromoCode = '';
    let pendingItem = null;        // 目前選擇套餐中的餐點
    let pendingSelection = null;   // 目前客製化中的品項 { item, combo, isAddon, sides }
    let checkoutOrderId = null;    // 本次結帳的 client_order_id (重送沿用，購物車變動後重新產生)
    let submitting = false;
    let myOrdersTimer = null;

    // ================================================================ 購物車資料
    /** 把舊版或紅利商城寫入的品項轉成目前格式；無法辨識的品項丟棄 */
    function normalizeLine(raw) {
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
            modifier_ids: (raw.modifier_ids || []).map(Number).filter(Boolean),
            combo_option_id: Number(raw.combo_option_id) || null,
            combo_name: String(raw.combo_name || ''),
            is_add_on: raw.is_add_on === true || raw.is_add_on === 'true',
            is_reward: isReward,
            points_cost: Number(raw.points_cost) || 0,
            can_addon: raw.can_addon !== false,
            // 菜單已顯示售完的品項直接標示，避免送出後才被擋下
            is_sold_out: Boolean(raw.is_sold_out) || (item ? !item.is_available : false),
        };
    }

    function lineKey(line) {
        return [line.menu_item_id, line.combo_option_id || 0, line.is_add_on ? 1 : 0, line.customization,
            [...line.modifier_ids].sort((a, b) => a - b).join(',')].join('|');
    }

    /** 購物車品項的顯示說明：[套餐: X]、客製化文字、加料(+N) (與後端產生的明細相同) */
    function lineDescription(line) {
        const parts = [];
        if (line.combo_name) parts.push(`[套餐: ${line.combo_name}]`);
        if (line.customization) parts.push(line.customization);
        line.modifier_ids.forEach((id) => {
            const m = modifierById.get(id);
            if (m) parts.push(`${m.name}(+${m.price})`);
        });
        return parts.join('、');
    }

    function lineCategory(line) {
        const item = menuItems.get(line.menu_item_id);
        return item ? item.category : '未分類';
    }

    function saveCart() {
        checkoutOrderId = null;   // 內容變了：下一次結帳是新的訂單
        storeCart(cart);
    }

    function modifiersTotal(ids) {
        return ids.reduce((sum, id) => sum + (modifierById.has(id) ? Number(modifierById.get(id).price) || 0 : 0), 0);
    }

    function makeLine(item, { combo = null, isAddon = false, customization = '', modifierIds = [] } = {}) {
        const base = isAddon ? Number(item.add_on_price) || 0 : Number(item.price) + (combo ? Number(combo.price) || 0 : 0);
        return {
            menu_item_id: Number(item.id),
            name: item.name,
            price: base + modifiersTotal(modifierIds),
            quantity: 1,
            customization,
            modifier_ids: modifierIds,
            combo_option_id: combo ? combo.id : null,
            combo_name: combo ? combo.name : '',
            is_add_on: isAddon,
            is_reward: false,
            points_cost: 0,
            can_addon: isAddon ? false : (combo ? combo.can_addon !== false : true),
            is_sold_out: false,
        };
    }

    function addLine(line) {
        const key = lineKey(line);
        const existing = cart.find((l) => !l.is_reward && lineKey(l) === key);
        if (existing) existing.quantity += 1;
        else cart.push(line);
        saveCart();
        updateCartUI();
    }

    // ================================================================ 加購配額
    // 每一份合格正價餐點最多搭配一份加購，且加購品必須符合該餐點的觸發條件 (二分圖配對，與後端一致)
    function qualifiesForAddon(line, addon) {
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

    function mainUnits() {
        const units = [];
        cart.forEach((line) => {
            if (line.is_add_on || line.is_reward || line.can_addon === false) return;
            for (let k = 0; k < line.quantity; k++) units.push(line);
        });
        return units;
    }

    function addonsFeasible(addonDefs) {
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

    function cartAddonDefs() {
        const defs = [];
        cart.filter((l) => l.is_add_on).forEach((l) => {
            const def = availableAddons.find((a) => Number(a.id) === l.menu_item_id);
            if (def) for (let k = 0; k < l.quantity; k++) defs.push(def);
        });
        return defs;
    }

    /** 在不破壞既有加購配對的前提下，該加購品還能再加幾份 */
    function remainingAddonQuota(addon) {
        const current = cartAddonDefs();
        let n = 0;
        while (addonsFeasible(current.concat(new Array(n + 1).fill(addon)))) n++;
        return n;
    }

    /** 依購物車順序逐份保留加購品，無法配對到合格餐點的部分自動減量；有調整時回傳 true */
    function enforceAddonQuota() {
        let changed = false;
        const kept = [];
        cart.forEach((line) => {
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
        cart = cart.filter((l) => l.quantity > 0);
        return changed;
    }

    // ================================================================ 金額計算 (與後端 process_checkout 相同的進位規則)
    function computeTotals() {
        const subtotal = cart.reduce((sum, l) => sum + l.price * l.quantity, 0);
        const promo = Math.min(appliedPromoDiscount, subtotal);
        const remaining = Math.max(0, subtotal - promo);
        let pointsUsed = 0;
        let pointsDiscount = 0;
        const toggle = $('usePointsToggle');
        if (!isGuest && rewardConfig.isEnabled && toggle && toggle.checked) {
            const input = $('pointsToUse');
            const ppd = Math.max(1, Number(rewardConfig.pointsPerDollar) || 1);
            let maxCash = Math.floor(remaining);
            if (rewardConfig.maxDiscount > 0) maxCash = Math.min(maxCash, rewardConfig.maxDiscount);
            const maxPoints = Math.min(userPoints, maxCash * ppd);
            let val = Math.floor(Number(input.value) || 0);
            val = Math.min(Math.max(val, 0), maxPoints);
            if (String(val) !== input.value) input.value = val;
            pointsDiscount = Math.min(Math.floor(val / ppd), maxCash);
            pointsUsed = pointsDiscount * ppd;
        }
        const final = Math.floor(Math.max(0, remaining - pointsDiscount) + 0.5);
        const earned = isGuest ? 0 : Math.floor(final / Math.max(1, Number(rewardConfig.spendPerPoint) || 100));
        return { subtotal, promo, pointsUsed, pointsDiscount, final, earned };
    }

    function setText(id, value) {
        const el = $(id);
        if (el) el.textContent = value;
    }

    function calculateTotal() {
        const t = computeTotals();
        setText('cart-subtotal', Math.round(t.subtotal));
        setText('cart-promo-discount', Math.round(t.promo));
        setText('cart-points-discount', Math.round(t.pointsDiscount));
        setText('cart-final', t.final);
        setText('cart-earned-points', t.earned);
        return t;
    }

    function setUserPoints(points) {
        userPoints = Number(points) || 0;
        setText('nav-user-points', userPoints);
        setText('cart-user-points', userPoints);
    }

    // ================================================================ 購物車畫面
    function cartLineHtml(line, idx) {
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

    function addonRecommendHtml() {
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

    function updateCartUI() {
        if (enforceAddonQuota()) {
            saveCart();
            setTimeout(() => alert('⚠️ 購物車餐點異動導致加購品名額不足，已自動調整數量！'), 100);
        }
        setText('cart-count', cart.reduce((sum, l) => sum + l.quantity, 0));
        const container = $('cart-items');
        if (!cart.length) {
            Kiosk.setHtml(container, html`<div class="text-center text-white-50 py-4">購物車目前空空如也</div>`);
            $('addon-recommend-container').innerHTML = '';
        } else {
            Kiosk.setHtml(container, cart.map(cartLineHtml));
            Kiosk.setHtml($('addon-recommend-container'), addonRecommendHtml());
        }
        calculateTotal();
    }

    function changeQty(index, delta) {
        const line = cart[index];
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

    async function removeLine(index) {
        const line = cart[index];
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
            const pos = cart.indexOf(line);
            if (pos >= 0) cart.splice(pos, 1);
        } else {
            cart.splice(index, 1);
        }
        saveCart();
        updateCartUI();
    }

    // ================================================================ 套餐 / 客製化
    function startAdd(itemId) {
        const item = menuItems.get(Number(itemId));
        if (!item || !item.is_available) return;
        if (item.combos && item.combos.length) {
            pendingItem = item;
            renderComboCards(item);
            modal('comboModal').show();
        } else {
            proceedToModifiers(item, null, false);
        }
    }

    function renderComboCards(item) {
        $('comboModalTitle').textContent = `【${item.name}】享用方式`;
        const single = html`
            <div class="col-md-6">
                <div class="combo-card p-4 rounded-3 h-100 d-flex flex-column justify-content-between" data-action="pick-combo" data-combo-id="" role="button" tabindex="0">
                    <div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span class="badge bg-secondary">單點主餐</span>
                            <span class="text-success fw-bold fs-5">$${item.price}</span>
                        </div>
                        <h5 class="fw-bold text-white mb-1">${item.name} (單點)</h5>
                        <p class="text-white-50 small mb-0">僅單點本品項，不搭配任何附餐或飲品。</p>
                    </div>
                    <div class="mt-3 pt-2 border-top border-secondary text-end">
                        <span class="btn btn-outline-light btn-sm fw-bold px-3">選擇單點 <i class="bi bi-chevron-right"></i></span>
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
                            <span class="badge ${sold ? 'bg-secondary' : 'bg-warning text-dark fw-bold'}">${sold ? '配餐售罄' : '升級超值套餐'}</span>
                            <div>
                                <span class="${sold ? 'text-white-50' : 'text-danger fw-bold fs-5'}">+$${c.price}</span>
                                <small class="text-white-50 ms-1">(總價 $${Number(item.price) + Number(c.price)})</small>
                            </div>
                        </div>
                        <h5 class="fw-bold ${sold ? 'text-white-50 text-decoration-line-through' : 'text-warning'} mb-1 combo-name">${c.name}</h5>
                        <p class="text-white-50 small mb-0">${c.description || '含特選附餐與指定飲品組合'}</p>
                    </div>
                    <div class="mt-3 pt-2 border-top border-secondary text-end">
                        ${sold
                            ? html`<button class="btn btn-secondary btn-sm fw-bold" type="button" disabled><i class="bi bi-slash-circle"></i> 配餐缺貨中</button>`
                            : html`<span class="btn btn-warning btn-sm fw-bold text-dark px-3">升級套餐 <i class="bi bi-chevron-right"></i></span>`}
                    </div>
                </div>
            </div>`;
        });
        Kiosk.setHtml($('combo-cards-container'), [single, combos]);
    }

    function pickCombo(comboId) {
        const item = pendingItem;
        if (!item) return;
        const combo = comboId ? (item.combos || []).find((c) => String(c.id) === String(comboId)) : null;
        // 等套餐視窗完全關閉後再開客製化視窗，避免兩個 Modal 的轉場互相干擾
        $('comboModal').addEventListener('hidden.bs.modal', () => proceedToModifiers(item, combo, false), { once: true });
        modal('comboModal').hide();
    }

    function modifierSectionHtml(key, title, group) {
        let body = '';
        if (group === 'ice_sugar') {
            body = html`
                <div class="row g-2">
                    <div class="col-6">
                        <label class="form-label small text-white-50" for="mod-ice-${key}">冰塊量</label>
                        <select id="mod-ice-${key}" class="form-select form-select-sm" data-mod="ice">
                            <option value="正常冰">正常冰</option><option value="少冰">少冰</option>
                            <option value="去冰">去冰</option><option value="熱飲">熱飲</option>
                        </select>
                    </div>
                    <div class="col-6">
                        <label class="form-label small text-white-50" for="mod-sugar-${key}">甜度</label>
                        <select id="mod-sugar-${key}" class="form-select form-select-sm" data-mod="sugar">
                            <option value="全糖">全糖</option><option value="半糖">半糖</option>
                            <option value="微糖">微糖</option><option value="無糖">無糖</option>
                        </select>
                    </div>
                </div>`;
        } else if (group === 'spicy') {
            body = html`
                <div>
                    <label class="form-label small text-white-50" for="mod-spicy-${key}">辣度調整</label>
                    <select id="mod-spicy-${key}" class="form-select form-select-sm" data-mod="spicy">
                        <option value="不辣">不辣</option><option value="微辣">微辣</option>
                        <option value="中辣">中辣</option><option value="大辣">大辣</option>
                    </select>
                </div>`;
        } else if (group === 'addons') {
            body = html`
                <div>
                    <label class="form-label small text-white-50 mb-1">加料升級</label>
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

    /** 有客製化選項時開啟客製化視窗，否則直接加入購物車 */
    function proceedToModifiers(item, combo, isAddon) {
        const hasMain = item.modifiers && item.modifiers !== 'none';
        const sides = combo ? (combo.sides || []).filter((s) => s.customizable) : [];
        if (!hasMain && !sides.length) {
            addLine(makeLine(item, { combo, isAddon }));
            return;
        }
        pendingSelection = { item, combo, isAddon, sides };
        let title;
        if (isAddon) title = `加購客製化 - ${item.name}`;
        else title = combo ? `客製化選擇 - ${item.name} [套餐: ${combo.name}]` : `客製化選擇 - ${item.name}`;
        $('modifierModalTitle').textContent = title;

        const isComboMode = Boolean(combo || sides.length);
        const mainTitle = isAddon ? `加購：${item.name}` : (isComboMode ? `主餐：${item.name}` : item.name);
        Kiosk.setHtml($('modifierModalBody'), [
            hasMain ? modifierSectionHtml('main', mainTitle, item.modifiers) : '',
            sides.map((side, i) => modifierSectionHtml(`side${i}`, `配餐：${side.name}`, side.modifiers)),
        ]);
        modal('modifierModal').show();
    }

    /** 讀取一個客製化區塊：回傳 { text: 冰塊/甜度或辣度文字, modifierIds: 勾選的加料 } */
    function readSection(section) {
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

    function confirmModifiers() {
        const sel = pendingSelection;
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

        addLine(makeLine(sel.item, { combo: sel.combo, isAddon: sel.isAddon, customization: texts.join('、'), modifierIds }));
        modal('modifierModal').hide();
    }

    function triggerAddon(itemId) {
        const addon = availableAddons.find((a) => Number(a.id) === Number(itemId));
        if (!addon) {
            alert('⚠️ 查無此加購品設定！');
            return;
        }
        if (remainingAddonQuota(addon) <= 0) {
            alert('⚠️ 加購名額已用完！每購買一份合格餐點限加購一份優惠品，加購品無法再次折抵。');
            return;
        }
        proceedToModifiers(addon, null, true);
    }

    // ================================================================ 優惠券 / 點數
    function renderCoupons(coupons) {
        const select = $('userCouponSelect');
        const badge = $('user-coupon-badge');
        if (!select) return;
        const list = coupons || [];
        if (!list.length) {
            Kiosk.setHtml(select, html`<option value="" disabled selected>-- 尚無可用優惠券 --</option>`);
            if (badge) Kiosk.setHtml(badge, html`<a href="/rewards" class="text-warning text-decoration-none"><i class="bi bi-gift"></i> 去商城兌換</a>`);
            return;
        }
        const current = activePromoCode;
        Kiosk.setHtml(select, [
            html`<option value="">-- 選擇我的優惠券 --</option>`,
            list.map((c) => html`<option value="${c.code}" ${c.code === current ? 'selected' : ''}>${c.title} (代碼: ${c.code})${c.min_spend > 0 ? ` - 滿$${Math.round(c.min_spend)}可用` : ''}</option>`),
        ]);
        if (badge) Kiosk.setHtml(badge, html`<span class="badge bg-warning text-dark py-0">${list.length} 張可用</span>`);
    }

    async function syncLatestUserCoupons() {
        if (isGuest) return;
        try {
            const data = await api('/api/user_available_coupons');
            setUserPoints(data.points);
            renderCoupons(data.coupons);
            calculateTotal();
        } catch (err) {
            /* 背景同步失敗不打擾顧客，下次開啟購物車會再試 */
        }
    }

    function showPromoMessage(ok, message) {
        const el = $('promo-msg');
        el.className = `small mt-1 ${ok ? 'text-success' : 'text-danger'}`;
        Kiosk.setHtml(el, html`<i class="bi ${ok ? 'bi-check-circle' : 'bi-x-circle'}"></i> ${message}`);
    }

    async function verifyPromo(code, { silent = false } = {}) {
        const subtotal = cart.reduce((sum, l) => sum + l.price * l.quantity, 0);
        try {
            const res = await api('/api/verify_promo', { json: { promo_code: code, subtotal: Math.round(subtotal) } });
            activePromoCode = res.valid ? code : '';
            appliedPromoDiscount = res.valid ? Number(res.discount) || 0 : 0;
            if (!silent || !res.valid) showPromoMessage(res.valid, res.message);
        } catch (err) {
            activePromoCode = '';
            appliedPromoDiscount = 0;
            showPromoMessage(false, err.message);
        }
        calculateTotal();
    }

    function applyPromoCode() {
        const code = $('promoInput').value.trim();
        if (!code) {
            activePromoCode = '';
            appliedPromoDiscount = 0;
            $('promo-msg').textContent = '';
            calculateTotal();
            return;
        }
        verifyPromo(code);
    }

    function togglePoints() {
        const toggle = $('usePointsToggle');
        const input = $('pointsToUse');
        $('points-input-box').classList.toggle('d-none', !toggle.checked);
        // 開啟時預設帶入可使用的最大點數 (computeTotals 會再依上限修正)
        input.value = toggle.checked ? userPoints : 0;
        calculateTotal();
    }

    // ================================================================ 結帳
    function takeoutInfo() {
        const orderType = $('orderType').value;
        const takeout = orderType === '外帶';
        return {
            orderType,
            needCutlery: takeout ? $('needCutlery').checked : false,
            note: takeout ? ($('takeoutNote').value || '').trim() : '',
        };
    }

    async function openPreview() {
        if (!cart.length) {
            alert('購物車為空');
            return;
        }
        if (isOnline && !pageData.pickupNode) {
            alert('請先在頁面上方選擇取餐分店，再進行結帳！');
            return;
        }
        const soldOut = cart.find((l) => l.is_sold_out);
        if (soldOut) {
            alert(`⚠️ 購物車中有已售完餐點【${soldOut.name}】，請先點擊「移除」後再結帳！`);
            return;
        }
        // 購物車可能在套用優惠碼後變動 (百分比折扣、低消門檻)：送出前重新驗證一次
        if (activePromoCode) await verifyPromo(activePromoCode, { silent: true });

        const t = calculateTotal();
        const info = takeoutInfo();
        Kiosk.setHtml($('preview-items'), html`
            <ul class="list-group">
                ${cart.map((l) => {
                    const desc = lineDescription(l);
                    return html`
                    <li class="list-group-item bg-dark text-white border-secondary d-flex justify-content-between align-items-center">
                        <div>
                            <div>${l.is_reward ? `${REWARD_LABEL} ` : ''}${l.name} x${l.quantity}</div>
                            ${desc ? html`<small class="text-info">${desc}</small>` : ''}
                        </div>
                        <span class="fw-bold">$${Math.round(l.price * l.quantity)}</span>
                    </li>`;
                })}
            </ul>
            <div class="mt-3 p-3 rounded bg-secondary bg-opacity-10 border border-secondary">
                <div class="small text-white-50">${pageData.table ? '桌邊點餐' : '外帶設定'}</div>
                <div class="fw-bold text-white">${pageData.table ? `${pageData.table} 桌 • 內用 • 用餐後至櫃檯結帳`
                    : (info.orderType === '外帶' ? (info.needCutlery ? '需要餐具' : '不需要餐具') : '內用')}</div>
                ${info.note ? html`<div class="small text-warning mt-2">備註：${info.note}</div>` : ''}
            </div>`);
        setText('preview-subtotal', Math.round(t.subtotal));
        setText('preview-promo', Math.round(t.promo));
        setText('preview-points', Math.round(t.pointsDiscount));
        setText('preview-final', t.final);
        setText('preview-earned', t.earned);
        modal('confirmModal').show();
    }

    function checkoutPayload() {
        const t = computeTotals();
        const info = takeoutInfo();
        if (!checkoutOrderId) checkoutOrderId = Kiosk.newOrderId();
        return {
            client_order_id: checkoutOrderId,
            items: cart.map((l) => ({
                menu_item_id: l.menu_item_id,
                quantity: l.quantity,
                modifier_ids: l.modifier_ids,
                combo_option_id: l.combo_option_id,
                is_add_on: l.is_add_on,
                is_reward: l.is_reward,
                customization: l.customization,
            })),
            payment_method: $('paymentMethod').value,
            order_type: info.orderType,
            need_cutlery: info.needCutlery,
            note: info.note,
            promo_code: activePromoCode,
            use_points: t.pointsUsed,
        };
    }

    function handleCheckoutError(err) {
        const data = err.data || {};
        if (data.sold_out && Array.isArray(data.sold_out_items)) {
            cart.forEach((l) => { if (data.sold_out_items.includes(l.name)) l.is_sold_out = true; });
            saveCart();
            modal('confirmModal').hide();
            alert(`⚠️ 抱歉！以下餐點剛好售罄：\n【${data.sold_out_items.join('、')}】\n已為您更新菜單並在購物車中劃掉標記，請移除後再結帳！`);
            window.location.reload();
            return;
        }
        if (data.insufficient_stock) {
            modal('confirmModal').hide();
            alert(`⚠️ 點餐數量超過目前剩餘庫存：\n\n${err.message}\n\n請至購物車調整購買數量後再重新結帳！`);
            bootstrap.Offcanvas.getOrCreateInstance($('cart-sidebar')).show();
            return;
        }
        alert(err.message || '結帳失敗，請稍後再試！');
    }

    async function confirmCheckout() {
        if (!cart.length || submitting) return;
        submitting = true;
        const btn = $('confirmCheckoutBtn');
        btn.disabled = true;
        try {
            const data = await api('/submit_order', { json: checkoutPayload() });
            if (data.payment_url) {  // 線上點餐：先付款，付款完成後訂單才送到分店
                cart = [];
                saveCart();
                window.location.href = data.payment_url;
                return;
            }
            modal('confirmModal').hide();
            const sidebar = bootstrap.Offcanvas.getInstance($('cart-sidebar'));
            if (sidebar) sidebar.hide();
            showReceipt(data);
        } catch (err) {
            handleCheckoutError(err);
        } finally {
            submitting = false;
            btn.disabled = false;
        }
    }

    function showReceipt(data) {
        if (!isGuest) setUserPoints(data.remaining_points);
        setText('receipt-pickup', data.pickup_pending ? '待分店確認' : `#${String(data.pickup_number).padStart(3, '0')}`);
        setText('receipt-order-id', `#${data.order_id}`);
        setText('receipt-node', data.pickup_node_name || '');
        $('receipt-node-row').classList.toggle('d-none', !data.pickup_node_name);
        $('receipt-pending-hint').classList.toggle('d-none', !data.pickup_pending);
        setText('receipt-user', data.user_name);
        setText('receipt-type', data.order_type === '外帶'
            ? `${data.order_type} • ${data.need_cutlery ? '要餐具' : '不用餐具'}` : data.order_type);
        setText('receipt-payment', data.payment_method);
        setText('receipt-note', data.note || '');
        $('receipt-note-row').classList.toggle('d-none', !data.note);
        setText('receipt-subtotal', Math.round(data.subtotal));
        setText('receipt-discount', Math.round(data.discount_amount));
        setText('receipt-total', Math.round(data.total_price));
        setText('receipt-earned-points', `+${data.points_earned} 點`);
        setText('receipt-current-points', `${data.remaining_points} 點`);
        Kiosk.setHtml($('receipt-items-body'), (data.items || []).map((item) => html`
            <tr>
                <td>
                    <div>${item.name}</div>
                    ${item.customization ? html`<small class="text-danger fw-bold"><i class="bi bi-tag-fill"></i> ${item.customization}</small>` : ''}
                </td>
                <td>x ${item.quantity}</td>
                <td>$${item.price}</td>
                <td>$${item.subtotal}</td>
            </tr>`));
        modal('receiptModal').show();
    }

    function finishOrder() {
        cart = [];
        saveCart();
        window.location.reload();
    }

    // ================================================================ 我的訂單
    const STATUS_BADGES = { 製作中: 'bg-warning text-dark', 已出餐: 'bg-success', 已取餐: 'bg-secondary', 已取消: 'bg-danger' };

    const PAYMENT_BADGES = {
        pending: ['bg-warning text-dark', '待付款'],
        paid: ['bg-success', '已線上付款'],
        expired: ['bg-secondary', '付款逾時'],
        refund_required: ['bg-danger', '退款處理中'],
    };

    function orderCardHtml(o) {
        const ptsUsed = o.points_used || 0;
        return html`
            <div class="card mb-3 border-secondary shadow-sm text-dark">
                <div class="card-header bg-light d-flex justify-content-between align-items-center">
                    <div>
                        <span class="fw-bold text-success fs-5">訂單 #${o.user_order_no}</span>
                        <span class="badge ${STATUS_BADGES[o.status] || 'bg-danger'}">${o.status}</span>
                        <span class="badge bg-primary ms-1">${o.order_type}</span>
                        <span class="badge bg-warning text-dark fw-bold ms-1 order-pickup">${o.pickup_pending ? '取餐號：待分店確認' : `取餐 #${String(o.pickup_number || o.order_id).padStart(3, '0')}`}</span>
                        ${o.pickup_node_name ? html`<span class="badge bg-dark ms-1 order-node"><i class="bi bi-shop me-1"></i>${o.pickup_node_name}</span>` : ''}
                        ${PAYMENT_BADGES[o.payment_status] ? html`<span class="badge ms-1 order-payment ${PAYMENT_BADGES[o.payment_status][0]}">${PAYMENT_BADGES[o.payment_status][1]}</span>` : ''}
                        ${o.payment_url ? html`<a class="btn btn-sm btn-warning fw-bold ms-2 order-pay" href="${o.payment_url}">前往付款</a>` : ''}
                    </div>
                    <small class="text-muted">${o.created_at}</small>
                </div>
                <div class="card-body p-3">
                    <table class="table table-sm mb-2 align-middle">
                        <thead><tr class="text-muted"><th>品項</th><th>數量</th><th>單價</th><th>小計</th></tr></thead>
                        <tbody>
                            ${(o.items || []).map((item) => html`
                                <tr>
                                    <td><div>${item.name}</div>${item.customization ? html`<small class="text-danger fw-bold">${item.customization}</small>` : ''}</td>
                                    <td>x ${item.quantity}</td>
                                    <td>$${Math.round(item.price)}</td>
                                    <td>$${Math.round(item.subtotal)}</td>
                                </tr>`)}
                        </tbody>
                    </table>
                    <div class="d-flex justify-content-between align-items-center pt-2 border-top">
                        <small class="text-muted">
                            總折扣折抵: -$${Math.round(o.discount_amount || 0)} ${ptsUsed > 0 ? `(扣除 ${ptsUsed} 點)` : ''} | 獲得紅利: +${o.points_earned} 點
                        </small>
                        <div class="fw-bold text-danger fs-6">實付：$${Math.round(o.total_price)}</div>
                    </div>
                </div>
            </div>`;
    }

    async function fetchMyOrders() {
        const body = $('history-modal-body');
        try {
            const orders = await api('/my_orders');
            Kiosk.setHtml(body, Array.isArray(orders) && orders.length
                ? orders.map(orderCardHtml)
                : html`<div class="text-center text-muted py-4">目前尚無任何訂單紀錄！</div>`);
        } catch (err) {
            Kiosk.setHtml(body, html`<div class="text-center text-danger py-4">${err.message}</div>`);
        }
    }

    function showMyOrders() {
        fetchMyOrders();
        modal('historyModal').show();
    }

    // ================================================================ 菜單篩選與排序
    let featureSort = null;
    let priceOrder = null;
    const PRICE_SORT_LABELS = {
        null: html`<i class="bi bi-arrow-down-up"></i> 價格 ⇅`,
        asc: html`<i class="bi bi-sort-numeric-down"></i> 價格 ↑ (低到高)`,
        desc: html`<i class="bi bi-sort-numeric-up-alt"></i> 價格 ↓ (高到低)`,
    };

    function renderSortButtons() {
        document.querySelectorAll('.sort-btn').forEach((b) => b.classList.toggle('active', b.dataset.mode === featureSort));
        const priceBtn = $('priceSortBtn');
        priceBtn.classList.toggle('active', priceOrder !== null);
        Kiosk.setHtml(priceBtn, PRICE_SORT_LABELS[priceOrder]);
    }

    function applySort() {
        const container = $('menuList');
        const rows = [...container.querySelectorAll('.menu-row')];
        // 依原始順序作為最後的比較基準，取消排序時可回到原本的排列
        rows.sort((a, b) => {
            if (featureSort) {
                const diff = Number(b.dataset[featureSort] || 0) - Number(a.dataset[featureSort] || 0);
                if (diff) return diff;
            }
            if (priceOrder) {
                const diff = Number(a.dataset.price) - Number(b.dataset.price);
                if (diff) return priceOrder === 'asc' ? diff : -diff;
            }
            return Number(a.dataset.order) - Number(b.dataset.order);
        });
        rows.forEach((r) => container.appendChild(r));
        renderSortButtons();
    }

    function toggleFeatureSort(mode) {
        featureSort = featureSort === mode ? null : mode;
        applySort();
    }

    function cyclePriceSort() {
        priceOrder = priceOrder === null ? 'asc' : (priceOrder === 'asc' ? 'desc' : null);
        applySort();
    }

    function filterMenu() {
        const q = $('searchInput').value.trim().toLowerCase();
        const max = Number($('maxPrice').value) || Number.POSITIVE_INFINITY;
        const cat = $('categoryFilter').value;
        document.querySelectorAll('#menuList .menu-row').forEach((row) => {
            const text = row.textContent.toLowerCase();
            const visible = text.includes(q) && Number(row.dataset.price || 0) <= max
                && (cat === 'all' || row.dataset.category === cat);
            row.classList.toggle('d-none', !visible);
        });
    }

    function resetFilters() {
        $('searchInput').value = '';
        $('maxPrice').value = '';
        $('categoryFilter').value = 'all';
        featureSort = null;
        priceOrder = null;
        filterMenu();
        applySort();
    }

    // ================================================================ 事件綁定
    Kiosk.on(document, 'click', {
        'add': (el) => startAdd(el.dataset.id),
        'pick-combo': (el) => pickCombo(el.dataset.comboId),
        'confirm-modifiers': () => confirmModifiers(),
        'addon': (el) => triggerAddon(el.dataset.id),
        'qty': (el) => changeQty(Number(el.dataset.index), Number(el.dataset.delta)),
        'remove': (el) => removeLine(Number(el.dataset.index)),
        'apply-promo': () => applyPromoCode(),
        'preview': () => openPreview(),
        'checkout': () => confirmCheckout(),
        'finish': () => finishOrder(),
        'my-orders': () => showMyOrders(),
        'logout': () => clearStoredCart(),
        'sort': (el) => toggleFeatureSort(el.dataset.mode),
        'price-sort': () => cyclePriceSort(),
        'reset-filters': () => resetFilters(),
    });
    Kiosk.on(document, 'input', {
        'filter': () => filterMenu(),
        'recalc': () => calculateTotal(),
    });
    Kiosk.on(document, 'change', {
        'filter': () => filterMenu(),
        'pick-coupon': (el) => {
            $('promoInput').value = el.value;
            applyPromoCode();
        },
        'toggle-points': () => togglePoints(),
        'order-type': (el) => $('takeoutExtraBox').classList.toggle('d-none', el.value !== '外帶'),
    });
    // 卡片以鍵盤操作 (Enter / 空白鍵) 等同點擊
    document.addEventListener('keydown', (ev) => {
        if ((ev.key === 'Enter' || ev.key === ' ') && ev.target instanceof Element && ev.target.matches('[role="button"][data-action]')) {
            ev.preventDefault();
            ev.target.click();
        }
    });

    $('cart-sidebar').addEventListener('show.bs.offcanvas', syncLatestUserCoupons);
    $('modifierModal').addEventListener('hidden.bs.modal', () => { pendingSelection = null; });
    $('historyModal').addEventListener('shown.bs.modal', () => {
        if (!myOrdersTimer) myOrdersTimer = setInterval(fetchMyOrders, 3000);
    });
    $('historyModal').addEventListener('hidden.bs.modal', () => {
        clearInterval(myOrdersTimer);
        myOrdersTimer = null;
    });
    // ================================================================ 初始化
    document.querySelectorAll('#menuList .menu-row').forEach((row, i) => { row.dataset.order = i; });
    renderCoupons(pageData.coupons);
    storeCart(cart);   // 寫回正規化後的格式
    updateCartUI();
    // 點餐機無人操作自動登出；桌邊掃碼點餐是顧客自己的手機，不自動登出
    if (!pageData.table) Kiosk.idleLogout({ timeoutMs: IDLE_TIMEOUT_MS });
})();
