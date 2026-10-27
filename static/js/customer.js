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
    const pro = pageData.pro || {};
    const ui = pageData.ui || {};

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

    // 桌邊呼叫服務 (掃桌位 QR code 後，登入前後都可使用)
    async function callService(btn) {
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
    Kiosk.on(document, 'click', { 'call-service': (el) => callService(el) });

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
    let promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
    let promotionSeq = 0;
    let promotionTimer = null;

    /** 價格通路：線上點餐 / 外帶 / 內用 (與後端 order_service._price_channel 相同) */
    function priceChannel() {
        if (isOnline) return 'online';
        const sel = $('orderType');
        return sel && sel.value === '外帶' ? 'takeout' : 'dine_in';
    }
    function channelPrice(item) {
        return Number((item.prices && item.prices[priceChannel()]) || item.price) || 0;
    }

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
            size: String(raw.size || ''),
            modifier_ids: (raw.modifier_ids || []).map(Number).filter(Boolean),
            combo_option_id: Number(raw.combo_option_id) || null,
            combo_name: String(raw.combo_name || ''),
            combo_choices: (raw.combo_choices || []).map(Number).filter(Boolean),
            pass_id: Number(raw.pass_id) || 0,
            choice_labels: (raw.choice_labels || []).map(String),
            is_add_on: raw.is_add_on === true || raw.is_add_on === 'true',
            is_reward: isReward,
            points_cost: Number(raw.points_cost) || 0,
            can_addon: raw.can_addon !== false,
            // 菜單已顯示售完的品項直接標示，避免送出後才被擋下
            is_sold_out: Boolean(raw.is_sold_out) || (item ? !item.is_available : false),
        };
    }

    function lineKey(line) {
        return [line.menu_item_id, line.combo_option_id || 0, line.is_add_on ? 1 : 0, line.size || '', line.customization,
            [...line.modifier_ids].sort((a, b) => a - b).join(','), (line.combo_choices || []).join(','), line.pass_id || 0].join('|');
    }

    /** 購物車品項的顯示說明：[套餐: X]、客製化文字、加料(+N) (與後端產生的明細相同) */
    function lineDescription(line) {
        const parts = [];
        if (line.pass_id) parts.push('[寄杯]');
        if (line.combo_name) parts.push(`[套餐: ${line.combo_name}]`);
        parts.push(...(line.choice_labels || []));
        if (line.size) parts.push(line.size);
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

    /** 規格加減價 (與後端相同：沒有指定時用第一個規格) */
    function sizeDelta(item, size) {
        const sizes = (item && item.sizes) || [];
        if (!sizes.length) return 0;
        const chosen = sizes.find((s) => s.name === size) || sizes[0];
        return Number(chosen.delta) || 0;
    }

    /** 套餐任選：選中的選項 → { ids, extra, labels } */
    function comboChoiceInfo(combo, ids) {
        const out = { ids: [], extra: 0, labels: [] };
        (combo && combo.choices ? combo.choices : []).forEach((g) => {
            const opt = g.options.find((o) => ids.includes(Number(o.id)));
            if (!opt) return;
            out.ids.push(Number(opt.id));
            out.extra += Number(opt.extra_price) || 0;
            out.labels.push(`${g.group}:${opt.name}${Number(opt.extra_price) ? `(+${opt.extra_price})` : ''}`);
        });
        return out;
    }

    function makeLine(item, { combo = null, isAddon = false, customization = '', modifierIds = [], size = '', choiceIds = [],
                              passId = 0 } = {}) {
        const choice = comboChoiceInfo(combo, choiceIds);
        let base = isAddon ? Number(item.add_on_price) || 0
            : Math.max(0, channelPrice(item) + sizeDelta(item, size)) + (combo ? Number(combo.price) || 0 : 0) + choice.extra;
        if (passId) base = 0;   // 寄杯兌換：免費 (只收加料)
        return {
            menu_item_id: Number(item.id),
            name: item.name,
            price: base + modifiersTotal(modifierIds),
            quantity: 1,
            customization,
            size: isAddon ? '' : size,
            modifier_ids: modifierIds,
            combo_option_id: combo ? combo.id : null,
            combo_name: combo ? combo.name : '',
            combo_choices: choice.ids,
            choice_labels: choice.labels,
            pass_id: passId || 0,
            is_add_on: isAddon,
            is_reward: false,
            points_cost: 0,
            can_addon: isAddon || passId ? false : (combo ? combo.can_addon !== false : true),
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

    /** 內用 / 外帶切換時，一般品項改用該通路的價格 */
    function repriceCart() {
        cart.forEach((l) => {
            if (l.is_reward) return;
            if (l.pass_id) {
                l.price = modifiersTotal(l.modifier_ids);
                return;
            }
            const item = menuItems.get(l.menu_item_id);
            if (!item) return;
            if (l.is_add_on) {
                l.price = (Number(item.add_on_price) || 0) + modifiersTotal(l.modifier_ids);
                return;
            }
            const combo = l.combo_option_id ? (item.combos || []).find((c) => Number(c.id) === Number(l.combo_option_id)) : null;
            l.price = Math.max(0, channelPrice(item) + sizeDelta(item, l.size)) + (combo ? Number(combo.price) || 0 : 0)
                + comboChoiceInfo(combo, l.combo_choices || []).extra + modifiersTotal(l.modifier_ids);
        });
        saveCart();
        updateCartUI();
    }

    // ================================================================ 促銷活動預覽 (實際以後端結帳計算為準)
    function schedulePromotionPreview() {
        clearTimeout(promotionTimer);
        promotionTimer = setTimeout(runPromotionPreview, 300);
    }

    async function runPromotionPreview() {
        const seq = ++promotionSeq;
        if (!cart.length) {
            promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
            calculateTotal();
            return;
        }
        try {
            const res = await api('/api/promotions/preview', {
                json: { items: cart.map(itemPayload), channel: pageData.table ? 'table' : 'kiosk', order_type: $('orderType').value },
            });
            if (seq === promotionSeq) promotion = res;
        } catch (err) {
            if (seq === promotionSeq) promotion = { discount: 0, names: [], gifts: [], blocks_coupon: false };
        }
        calculateTotal();
    }

    function itemPayload(l) {
        return {
            menu_item_id: l.menu_item_id,
            quantity: l.quantity,
            modifier_ids: l.modifier_ids,
            combo_option_id: l.combo_option_id,
            is_add_on: l.is_add_on,
            is_reward: l.is_reward,
            customization: l.customization,
            size: l.size || '',
        };
    }

    // ================================================================ 金額計算 (與後端 process_checkout 相同的進位規則)
    function computeTotals() {
        const subtotal = cart.reduce((sum, l) => sum + l.price * l.quantity, 0);
        const promotionDiscount = Math.min(Number(promotion.discount) || 0, subtotal);
        const afterPromotion = Math.max(0, subtotal - promotionDiscount);
        const promo = promotion.blocks_coupon ? 0 : Math.min(appliedPromoDiscount, afterPromotion);
        let remaining = Math.max(0, afterPromotion - promo);
        const tierPct = Number(pro.tierDiscount) || 0;
        const tier = tierPct > 0 ? remaining * Math.min(100, tierPct) / 100 : 0;
        remaining = Math.max(0, remaining - tier);
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
        const afterDiscounts = Math.max(0, remaining - pointsDiscount);
        const servicePct = Number(pro.serviceChargePercent) || 0;
        const dineIn = pageData.table || $('orderType').value === '內用';
        const service = servicePct > 0 && pro.serviceChargeDineIn && dineIn ? Math.floor(afterDiscounts * servicePct / 100 + 0.5) : 0;
        const delivery = deliveryFee(subtotal);
        const final = Math.floor(afterDiscounts + 0.5) + service + delivery;
        const earned = isGuest ? 0 : Math.floor((final - delivery) / Math.max(1, Number(rewardConfig.spendPerPoint) || 100));
        return { subtotal, promo, pointsUsed, pointsDiscount, final, earned, promotionDiscount, tier, service, delivery };
    }

    /** 外送到府的外送費 (與後端相同：依配送區域，商品小計達免運門檻免收) */
    function deliveryFee(subtotal) {
        if (!pro.homeDelivery || !$('orderType') || $('orderType').value !== '外送到府') return 0;
        const opt = $('deliveryZone') && $('deliveryZone').selectedOptions[0];
        if (!opt) return 0;
        const freeOver = Number(pro.homeDelivery.freeOver) || 0;
        return freeOver && subtotal >= freeOver ? 0 : Number(opt.dataset.fee) || 0;
    }

    function toggleRow(rowId, valueId, value) {
        const row = $(rowId);
        if (!row) return;
        row.classList.toggle('d-none', !(value > 0));
        setText(valueId, Math.round(value));
    }

    function setText(id, value) {
        const el = $(id);
        if (el) el.textContent = value;
    }

    function calculateTotal() {
        const t = computeTotals();
        toggleRow('cart-promotion-row', 'cart-promotion', t.promotionDiscount);
        setText('cart-promotion-names', (promotion.names || []).join('、'));
        toggleRow('cart-tier-row', 'cart-tier', t.tier);
        toggleRow('cart-service-row', 'cart-service', t.service);
        toggleRow('cart-delivery-row', 'cart-delivery', t.delivery);
        const gifts = $('cart-gifts');
        if (gifts) Kiosk.setHtml(gifts, (promotion.gifts || []).map((g) => html`<div><i class="bi bi-gift me-1"></i>${ui.gift || '贈品'}：${g.name} x${g.quantity}</div>`));
        setText('cart-subtotal', Math.round(t.subtotal));
        setText('cart-promo-discount', Math.round(t.promo));
        setText('cart-points-discount', Math.round(t.pointsDiscount));
        setText('cart-final', t.final);
        setText('cart-earned-points', t.earned);
        toggleCalories('cart-calories-row', 'cart-calories');
        return t;
    }

    /** 購物車熱量合計 (有標示熱量的餐點才計入；後台關閉熱量顯示時隱藏) */
    function cartCalories() {
        return cart.reduce((sum, l) => sum + (Number((menuItems.get(Number(l.menu_item_id)) || {}).calories) || 0) * l.quantity, 0);
    }

    function toggleCalories(rowId, valueId) {
        const row = $(rowId);
        if (!row) return;
        const kcal = pro.showCalories ? cartCalories() : 0;
        row.classList.toggle('d-none', !(kcal > 0));
        setText(valueId, Math.round(kcal));
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
        schedulePromotionPreview();
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
        Kiosk.hideModalThen($('comboModal'), () => proceedToModifiers(item, combo, false));
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

    /** 規格選擇 (單選)：第一個為預設 */
    function sizeSectionHtml(item) {
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
    function proceedToModifiers(item, combo, isAddon, passId = 0) {
        const hasMain = item.modifiers && item.modifiers !== 'none';
        const hasSizes = !isAddon && !passId && (item.sizes || []).length > 0;
        const sides = combo ? (combo.sides || []).filter((s) => s.customizable) : [];
        const choices = combo ? (combo.choices || []) : [];
        if (!hasMain && !sides.length && !hasSizes && !choices.length) {
            addLine(makeLine(item, { combo, isAddon, passId }));
            return;
        }
        pendingSelection = { item, combo, isAddon, sides, choices, passId };
        let title;
        if (isAddon) title = `加購客製化 - ${item.name}`;
        else title = combo ? `客製化選擇 - ${item.name} [套餐: ${combo.name}]` : `客製化選擇 - ${item.name}`;
        $('modifierModalTitle').textContent = title;

        const isComboMode = Boolean(combo || sides.length);
        const mainTitle = isAddon ? `加購：${item.name}` : (isComboMode ? `主餐：${item.name}` : item.name);
        Kiosk.setHtml($('modifierModalBody'), [
            hasSizes ? sizeSectionHtml(item) : '',
            hasMain ? modifierSectionHtml('main', mainTitle, item.modifiers) : '',
            sides.map((side, i) => modifierSectionHtml(`side${i}`, `配餐：${side.name}`, side.modifiers)),
            choices.map((g, gi) => choiceSectionHtml(g, gi)),
        ]);
        syncChoiceMods();
        modal('modifierModal').show();
    }

    /** 套餐任選群組：單選 (售完的選項不能選)，選項可客製時顯示該選項的客製化 */
    function choiceSectionHtml(g, gi) {
        const first = g.options.find((o) => o.is_available !== false);
        return html`
            <div class="mod-section p-3 mb-3 rounded border border-info bg-dark shadow-sm" data-key="choice${gi}" data-choice-group="${gi}">
                <h6 class="fw-bold text-info mb-2 border-bottom border-secondary pb-1">${g.group} (任選一項)</h6>
                <div class="d-flex flex-wrap gap-2" role="radiogroup">
                    ${g.options.map((o) => html`
                        <input type="radio" class="btn-check" name="combo-choice-${gi}" id="choice-${gi}-${o.id}" value="${o.id}"
                               data-mod="choice" ${o.is_available === false ? 'disabled' : ''} ${first && o.id === first.id ? 'checked' : ''}>
                        <label class="btn btn-outline-info btn-sm" for="choice-${gi}-${o.id}">${o.name}${Number(o.extra_price) ? ` (+$${o.extra_price})` : ''}${o.is_available === false ? ' (售完)' : ''}</label>`)}
                </div>
                ${g.options.filter((o) => o.customizable && o.modifiers && o.modifiers !== 'none').map((o) => html`
                    <div class="choice-mods mt-2 d-none" data-opt="${o.id}">
                        ${modifierSectionHtml(`choice${gi}-${o.id}`, `${o.name} 客製化`, o.modifiers)}
                    </div>`)}
            </div>`;
    }

    function syncChoiceMods() {
        document.querySelectorAll('#modifierModalBody [data-choice-group]').forEach((sec) => {
            const checked = sec.querySelector('[data-mod="choice"]:checked');
            sec.querySelectorAll('.choice-mods').forEach((box) => {
                box.classList.toggle('d-none', !checked || box.dataset.opt !== checked.value);
            });
        });
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

        const choiceIds = [];
        for (const [gi, g] of (sel.choices || []).entries()) {
            const checked = body.querySelector(`[data-choice-group="${gi}"] [data-mod="choice"]:checked`);
            if (!checked) {
                alert(`⚠️ 請在「${g.group}」中選擇一項！`);
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
    function usePass(passId) {
        const p = (pageData.passes || []).find((x) => Number(x.id) === Number(passId));
        const item = p ? menuItems.get(Number(p.menu_item_id)) : null;
        if (!p || !item) {
            alert('⚠️ 找不到可兌換的餐點');
            return;
        }
        const used = cart.filter((l) => Number(l.pass_id) === Number(p.id)).reduce((s, l) => s + l.quantity, 0);
        if (used >= Number(p.remaining)) {
            alert(`⚠️ 寄杯「${p.name}」剩 ${p.remaining} 次，已全部加入購物車`);
            return;
        }
        proceedToModifiers(item, null, false, p.id);
    }

    async function openTableBill() {
        try {
            const res = await api('/api/table/bill');
            Kiosk.setHtml($('tableBillBody'), res.orders.length ? html`
                ${res.orders.map((o) => html`<div class="border-bottom py-1"><div class="fw-bold small">取餐 #${o.pickup_number} • $${o.total_price}</div>
                    ${o.items.map((i) => html`<div class="small text-secondary">${i.name} x${i.quantity}</div>`)}</div>`)}
                <div class="fs-5 fw-bold text-end mt-2">合計 <span id="tableBillTotal">$${res.total}</span></div>` : html`<div class="text-secondary">目前沒有需要結帳的訂單</div>`);
            $('tablePayBtn').disabled = !res.orders.length || !res.online_pay;
            modal('tableBillModal').show();
        } catch (err) {
            alert(`❌ ${err.message}`);
        }
    }

    async function payTableBill() {
        $('tablePayBtn').disabled = true;
        try {
            const res = await api('/api/table/pay', { method: 'POST', json: {} });
            window.location.href = res.payment_url;
        } catch (err) {
            alert(`❌ ${err.message}`);
            $('tablePayBtn').disabled = false;
        }
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
        const takeout = orderType === '外帶' || orderType === '外送到府';
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
        if ($('paymentMethod').value === '儲值金') {
            const t0 = computeTotals();
            if ((Number(pro.walletBalance) || 0) < t0.final) {
                alert(`⚠️ 儲值金餘額 $${Number(pro.walletBalance) || 0} 不足 (應付 $${t0.final})，請改用其他付款方式或至櫃檯加值！`);
                return;
            }
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
        toggleRow('preview-promotion-row', 'preview-promotion', t.promotionDiscount);
        toggleRow('preview-tier-row', 'preview-tier', t.tier);
        toggleRow('preview-service-row', 'preview-service', t.service);
        toggleRow('preview-delivery-row', 'preview-delivery', t.delivery);
        setText('preview-subtotal', Math.round(t.subtotal));
        setText('preview-promo', Math.round(t.promo));
        setText('preview-points', Math.round(t.pointsDiscount));
        setText('preview-final', checkoutPayload().expected_total);   // 伺服器回報過新金額時顯示新金額
        setText('preview-earned', t.earned);
        toggleCalories('preview-calories-row', 'preview-calories');
        const pickupSel = $('pickupTime');
        const pickupLabel = pickupSel && pickupSel.value ? pickupSel.options[pickupSel.selectedIndex].text : '';
        $('preview-pickup-row').classList.toggle('d-none', !pickupLabel);
        setText('preview-pickup', pickupLabel);
        modal('confirmModal').show();
    }

    /** 上一次送出時伺服器回報的金額 (購物車內容沒變、顧客已看過新金額時，再送出以它為準) */
    let acceptedTotal = null;
    function payloadKey(payload) {
        return JSON.stringify({ ...payload, client_order_id: '', expected_total: 0 });
    }

    function checkoutPayload() {
        const t = computeTotals();
        const info = takeoutInfo();
        if (!checkoutOrderId) checkoutOrderId = Kiosk.newOrderId();
        const payload = {
            client_order_id: checkoutOrderId,
            items: cart.map((l) => ({
                menu_item_id: l.menu_item_id,
                quantity: l.quantity,
                modifier_ids: l.modifier_ids,
                combo_option_id: l.combo_option_id,
                combo_choices: l.combo_choices || [],
                pass_id: l.pass_id || 0,
                is_add_on: l.is_add_on,
                is_reward: l.is_reward,
                customization: l.customization,
                size: l.size || '',
            })),
            payment_method: $('paymentMethod').value,
            order_type: info.orderType,
            need_cutlery: info.needCutlery,
            note: info.note,
            promo_code: activePromoCode,
            use_points: t.pointsUsed,
            invoice: invoicePayload(),
            pickup_at: $('pickupTime') ? $('pickupTime').value : '',
            delivery: info.orderType === '外送到府' ? {
                address: ($('deliveryAddress').value || '').trim(),
                phone: ($('deliveryPhone').value || '').trim(),
                zone: $('deliveryZone').value,
            } : null,
        };
        const key = payloadKey(payload);
        payload.expected_total = acceptedTotal && acceptedTotal.key === key ? acceptedTotal.total : t.final;
        return payload;
    }

    /** 伺服器回報金額與畫面不同 (剛改價等)：以伺服器的單價更新購物車；仍有差異時記住伺服器金額 (顧客確認後再送出) */
    function applyServerPrices(data) {
        const prices = data.line_prices || [];
        if (prices.length === cart.length) cart.forEach((l, i) => { l.price = Number(prices[i]) || 0; });
        saveCart();
        updateCartUI();
        const again = checkoutPayload();
        const total = Number(data.server_total) || 0;
        acceptedTotal = computeTotals().final === total ? null : { key: payloadKey(again), total };
    }

    const INVOICE_PLACEHOLDERS = { mobile: '手機條碼 /ABC1234', company: '統一編號 8 碼', donate: '愛心碼' };
    function invoicePayload() {
        const sel = $('invoiceType');
        if (!sel || !sel.value) return null;
        const value = ($('invoiceValue').value || '').trim();
        if (sel.value === 'company') return { type: 'company', buyer_id: value };
        if (sel.value === 'donate') return { type: 'donate', love_code: value };
        return { type: sel.value, carrier: value };
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
        if (data.price_changed) {
            modal('confirmModal').hide();
            applyServerPrices(data);
            alert(`⚠️ ${err.message}`);
            bootstrap.Offcanvas.getOrCreateInstance($('cart-sidebar')).show();
            return;
        }
        if (data.coupon_unavailable || data.points_changed) {
            // 優惠券或點數剛在其他裝置 / 門市 / 線上用掉：重新載入最新的點數與可用券，讓顧客確認金額後再送出
            modal('confirmModal').hide();
            alert(`⚠️ ${err.message}`);
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
            if (window.KioskOffline && window.KioskOffline.shouldQueue(err)) {
                queueOffline(err);
                return;
            }
            handleCheckoutError(err);
        } finally {
            submitting = false;
            btn.disabled = false;
        }
    }

    /** 連不到門市主機：可暫存的訂單 (現金、未用點數 / 券) 存在點餐機，連線恢復後自動補送 */
    function queueOffline(err) {
        const payload = checkoutPayload();
        const reason = KioskOffline.blockReason(payload);
        if (reason) {
            alert(`${err.message}\n\n（${reason}，請稍後再試或至櫃檯點餐）`);
            return;
        }
        const summary = cart.map((l) => `${l.name}×${l.quantity}`).join('、');
        const entry = KioskOffline.enqueue(payload, computeTotals().final, summary);
        cart = [];
        checkoutOrderId = null;
        saveCart();
        updateCartUI();
        modal('confirmModal').hide();
        const sidebar = bootstrap.Offcanvas.getInstance($('cart-sidebar'));
        if (sidebar) sidebar.hide();
        KioskOffline.showReceipt(entry);
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
        setText('receipt-delivery', data.delivery_address ? `${data.delivery_address} (外送費 $${data.delivery_fee || 0})` : '');
        $('receipt-delivery-row').classList.toggle('d-none', !data.delivery_address);
        setText('receipt-pickup-at', data.pickup_at || '');
        $('receipt-pickup-at-row').classList.toggle('d-none', !data.pickup_at);
        const svcRow = $('receipt-service-row');
        if (svcRow) {
            svcRow.classList.toggle('d-none', !(Number(data.service_charge) > 0));
            setText('receipt-service', Number(data.service_charge) || 0);
        }
        const invRow = $('receipt-invoice-row');
        if (invRow) {
            invRow.classList.toggle('d-none', !data.invoice);
            if (data.invoice) setText('receipt-invoice', data.invoice.invoice_no || `${data.invoice.carrier} (開立中)`);
        }
        const qrBox = $('receipt-qr-box');
        if (qrBox) {
            qrBox.classList.toggle('d-none', !data.track_qr);
            if (data.track_qr) $('receipt-qr').src = data.track_qr;
        }
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
        refunded: ['bg-secondary', '已退款'],
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
                        ${o.pickup_at ? html`<span class="badge bg-info text-dark ms-1 order-pickup-at"><i class="bi bi-clock me-1"></i>預約 ${o.pickup_at}</span>` : ''}
                        ${o.delivery_label ? html`<span class="badge bg-warning text-dark ms-1 order-delivery"><i class="bi bi-scooter me-1"></i>${o.delivery_label}</span>` : ''}
                        ${o.pickup_node_name ? html`<span class="badge bg-dark ms-1 order-node"><i class="bi bi-shop me-1"></i>${o.pickup_node_name}</span>` : ''}
                        ${PAYMENT_BADGES[o.payment_status] ? html`<span class="badge ms-1 order-payment ${PAYMENT_BADGES[o.payment_status][0]}">${PAYMENT_BADGES[o.payment_status][1]}</span>` : ''}
                        ${o.payment_url ? html`<a class="btn btn-sm btn-warning fw-bold ms-2 order-pay" href="${o.payment_url}">${o.payment_note ? '查看繳費資訊' : '前往付款'}</a>` : ''}
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
        'use-pass': (el) => usePass(el.dataset.passId),
        'table-bill': () => openTableBill(),
        'table-pay': () => payTableBill(),
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
        'order-type': (el) => {
            $('takeoutExtraBox').classList.toggle('d-none', el.value !== '外帶' && el.value !== '外送到府');
            if ($('homeDeliveryBox')) $('homeDeliveryBox').classList.toggle('d-none', el.value !== '外送到府');
            repriceCart();
        },
        'recalc-delivery': () => calculateTotal(),
    });
    if ($('invoiceType')) {
        $('invoiceType').addEventListener('change', () => {
            const type = $('invoiceType').value;
            $('invoiceValue').classList.toggle('d-none', !INVOICE_PLACEHOLDERS[type]);
            $('invoiceValue').placeholder = INVOICE_PLACEHOLDERS[type] || '';
        });
    }
    // 卡片以鍵盤操作 (Enter / 空白鍵) 等同點擊
    document.addEventListener('keydown', (ev) => {
        if ((ev.key === 'Enter' || ev.key === ' ') && ev.target instanceof Element && ev.target.matches('[role="button"][data-action]')) {
            ev.preventDefault();
            ev.target.click();
        }
    });

    $('cart-sidebar').addEventListener('show.bs.offcanvas', syncLatestUserCoupons);
    $('modifierModal').addEventListener('hidden.bs.modal', () => { pendingSelection = null; });
    $('modifierModalBody').addEventListener('change', (ev) => {
        if (ev.target.matches('[data-mod="choice"]')) syncChoiceMods();
    });
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
    repriceCart();     // 購物車留在瀏覽器的是加入當時的價格：以目前菜單重算 (店家可能已改價)
    // 點餐機無人操作自動登出；桌邊掃碼點餐是顧客自己的手機，不自動登出
    if (!pageData.table) Kiosk.idleLogout({ timeoutMs: IDLE_TIMEOUT_MS });
})();
