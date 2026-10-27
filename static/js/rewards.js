/*
 * 紅利回饋商城：以點數兌換優惠券 / 餐點、退回尚未使用的兌換商品。
 * 兌換的餐點會加入點餐頁的購物車 (localStorage 'kiosk_cart')；退回時一併從購物車移除。
 */
(function () {
    'use strict';

    const { html } = Kiosk;
    const CART_KEY = 'kiosk_cart';
    const IDLE_TIMEOUT_MS = 10 * 60 * 1000;
    const pageData = Kiosk.pageData();
    const modifierOptions = pageData.modifierOptions || [];
    let currentPoints = Number(pageData.points) || 0;
    let selected = null; // 目前彈窗中的兌換目標

    const pointsEl = document.getElementById('currentPoints');
    const modalEl = document.getElementById('redeemModal');
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    const modalName = document.getElementById('modalItemName');
    const modalPoints = document.getElementById('modalItemPoints');
    const modalAlert = document.getElementById('modalAlertText');
    const modBox = document.getElementById('modalModifierBox');
    const modContent = document.getElementById('modalModifierContent');
    const confirmBtn = document.getElementById('confirmRedeemBtn');

    // ---------------------------------------------------------------- 購物車 (與點餐頁共用)
    function readCart() {
        try {
            const cart = JSON.parse(localStorage.getItem(CART_KEY) || '[]');
            return Array.isArray(cart) ? cart : [];
        } catch (err) {
            return [];
        }
    }

    function writeCart(cart) {
        try { localStorage.setItem(CART_KEY, JSON.stringify(cart)); } catch (err) { /* 無痕模式等無法寫入 */ }
    }

    /** 退回兌換後，從購物車移除一份對應的兌換品 (數量 > 1 時減一) */
    function removeRewardFromCart(itemId) {
        const cart = readCart();
        for (let i = cart.length - 1; i >= 0; i -= 1) {
            const line = cart[i];
            if (line && line.is_reward && Number(line.menu_item_id) === itemId) {
                if ((Number(line.quantity) || 1) > 1) line.quantity -= 1; else cart.splice(i, 1);
                writeCart(cart);
                return;
            }
        }
    }

    // ---------------------------------------------------------------- 畫面
    function setPoints(points) {
        currentPoints = Number(points) || 0;
        pointsEl.textContent = currentPoints;
        updateRedeemButtons();
    }

    function updateRedeemButtons() {
        document.querySelectorAll('.reward-item').forEach((card) => {
            const btn = card.querySelector('button.btn-redeem');
            if (!btn) return;
            const enough = currentPoints >= Number(card.dataset.points);
            btn.disabled = !enough;
            // 已持有同一種券時顯示「再兌換一張」(數量由伺服器帶在 data-held)
            const again = Number(btn.dataset.held) > 0;
            btn.textContent = enough ? (again ? '再兌換一張' : '立即兌換') : '點數不足';
        });
    }

    function selectField(id, label, options) {
        return html`
            <div class="mb-2">
                <label class="form-label small fw-bold text-secondary mb-1" for="${id}">${label}</label>
                <select id="${id}" class="form-select form-select-sm">
                    ${options.map((o) => html`<option value="${o}">${o}</option>`)}
                </select>
            </div>`;
    }

    function modifierForm(type) {
        if (type === 'ice_sugar') {
            return html`${selectField('reward-mod-ice', '冰塊量', ['正常冰', '少冰', '去冰', '熱飲'])}
                ${selectField('reward-mod-sugar', '甜度', ['全糖', '半糖', '微糖', '無糖'])}`;
        }
        if (type === 'spicy') {
            return selectField('reward-mod-spicy', '辣度調整', ['不辣', '微辣', '中辣', '大辣']);
        }
        if (type === 'addons') {
            const addons = modifierOptions.filter((m) => m.category === 'addons');
            if (!addons.length) return '';
            return html`
                <label class="form-label small fw-bold text-secondary mb-1">加料選項</label>
                <div class="d-flex flex-wrap gap-3">
                    ${addons.map((m) => html`
                        <div class="form-check">
                            <input class="form-check-input reward-mod-addon" type="checkbox" value="${m.id}" id="rewAddon-${m.id}">
                            <label class="form-check-label small text-dark" for="rewAddon-${m.id}">${m.name} (+${m.price})</label>
                        </div>`)}
                </div>`;
        }
        return '';
    }

    function openModal(name, points) {
        modalName.textContent = name;
        modalPoints.textContent = points;
        modal.show();
    }

    function promptRedeemItem(btn) {
        const { id, name, points, modifiers } = btn.dataset;
        selected = { type: 'item', id: Number(id), name };
        const form = modifierForm(modifiers || 'none');
        Kiosk.setHtml(modContent, form);
        modBox.classList.toggle('d-none', !form);
        modalAlert.textContent = '兌換成功後，此品項（$0 元）將直接加入您的購物車中！';
        openModal(name, points);
    }

    function promptRedeemCoupon(btn) {
        const { id, title, code, points, discount } = btn.dataset;
        const held = Number(btn.dataset.held) || 0;
        selected = { type: 'coupon', id: Number(id), name: title };
        Kiosk.setHtml(modContent, '');
        modBox.classList.add('d-none');
        Kiosk.setHtml(modalAlert, html`
            ${discount ? html`<div class="fw-bold text-success fs-5 mb-1">${discount}</div>` : ''}
            ${held ? html`<div class="text-warning fw-bold mb-1">您目前還有 ${held} 張未使用，兌換後共 ${held + 1} 張。</div>` : ''}
            兌換成功後，系統將提供專屬促銷代碼【${code}】供結帳折抵使用！`);
        openModal(title, points);
    }

    /** 收集彈窗中的客製化選項 (冰塊/甜度/辣度為文字，加料以 modifier_ids 由後端計價) */
    function collectCustomization() {
        const value = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
        const parts = [];
        const ice = value('reward-mod-ice');
        const sugar = value('reward-mod-sugar');
        if (ice && sugar) parts.push(`${ice} / ${sugar}`);
        const spicy = value('reward-mod-spicy');
        if (spicy) parts.push(spicy);
        const modifierIds = [...modContent.querySelectorAll('.reward-mod-addon:checked')].map((cb) => Number(cb.value));
        return { customization: parts.join('、'), modifier_ids: modifierIds };
    }

    // ---------------------------------------------------------------- 兌換 / 退回
    async function executeRedeem() {
        if (!selected) return;
        const payload = selected.type === 'coupon'
            ? { coupon_id: selected.id }
            : { item_id: selected.id, ...collectCustomization() };

        const idle = confirmBtn.innerHTML;
        confirmBtn.disabled = true;
        Kiosk.setHtml(confirmBtn, html`<span class="spinner-border spinner-border-sm me-2"></span>兌換中...`);
        try {
            const data = await Kiosk.api('/api/redeem_reward', { json: payload });
            setPoints(data.remaining_points);
            modal.hide();
            if (data.is_coupon) {
                alert(data.message);
            } else {
                const cart = readCart();
                cart.push(data.redeemed_item);
                writeCart(cart);
                alert(`${data.message}\n已直接加入您的點餐清單！`);
            }
            window.location.reload();
        } catch (err) {
            alert(err.message);
        } finally {
            confirmBtn.disabled = false;
            confirmBtn.innerHTML = idle;
        }
    }

    async function refundItem(btn) {
        const itemId = Number(btn.dataset.id);
        if (!window.confirm(`確定要退回 1 份【${btn.dataset.name}】並取回點數嗎？`)) return;
        btn.disabled = true;
        try {
            const data = await Kiosk.api('/api/refund_reward_item', { json: { item_id: itemId, quantity: 1 } });
            removeRewardFromCart(itemId);
            setPoints(data.current_points);
            alert(`🎉 ${data.message}`);
            window.location.reload();
        } catch (err) {
            alert(`❌ 退點失敗：${err.message}`);
            btn.disabled = false;
        }
    }

    // Kiosk 閒置 10 分鐘：清空購物車並登出，避免下一位顧客看到上一位的資料
    Kiosk.idleLogout({
        timeoutMs: IDLE_TIMEOUT_MS,
        beforeLogout: () => localStorage.removeItem(CART_KEY),
    });

    modalEl.addEventListener('hidden.bs.modal', () => { selected = null; });

    // ---------------------------------------------------------------- 會員動態條碼
    let qrTimer = null;

    async function loadMemberQr(rotate = false) {
        const msg = document.getElementById('memberQrMsg');
        msg.textContent = '';
        try {
            const data = rotate
                ? await Kiosk.api('/api/member_qr', { json: { action: 'rotate' } })
                : await Kiosk.api('/api/member_qr');
            const img = document.getElementById('memberQrImg');
            img.src = data.qr;
            img.dataset.token = data.token;
            clearTimeout(qrTimer);
            qrTimer = setTimeout(() => loadMemberQr(), Math.max(5, data.expires_in) * 1000);
        } catch (err) {
            msg.textContent = err.message;
        }
    }

    const qrModalEl = document.getElementById('memberQrModal');
    if (qrModalEl) qrModalEl.addEventListener('hidden.bs.modal', () => clearTimeout(qrTimer));

    Kiosk.on(document, 'click', {
        'show-member-qr': () => {
            bootstrap.Modal.getOrCreateInstance(qrModalEl).show();
            loadMemberQr();
        },
        'rotate-member-qr': () => loadMemberQr(true),
        'redeem-item': (el) => promptRedeemItem(el),
        'redeem-coupon': (el) => promptRedeemCoupon(el),
        'confirm-redeem': () => executeRedeem(),
        'refund-item': (el) => refundItem(el),
    });

    // ---------------------------------------------------------------- 行銷訊息開關
    const optIn = document.getElementById('marketingOptIn');
    if (optIn) {
        optIn.addEventListener('change', async () => {
            const msg = document.getElementById('marketingMsg');
            try {
                const data = await Kiosk.api('/api/member/marketing', { json: { opt_in: optIn.checked } });
                msg.textContent = data.message;
            } catch (err) {
                optIn.checked = !optIn.checked;
                msg.textContent = err.message;
            }
        });
    }

    updateRedeemButtons();
})();
