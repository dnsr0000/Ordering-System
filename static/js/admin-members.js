/*
 * 店家管理後台：優惠券 (新增/編輯/折扣提示)、會員資料編輯與會員持券管理。
 * 依賴 admin.js 提供的 window.AdminPage。
 */
(function () {
    'use strict';
    const { html } = Kiosk;
    const { data, showModal } = window.AdminPage;
    const $ = (id) => document.getElementById(id);

    // ---------------------------------------------------------------- 優惠券折扣輸入
    /** 與後端 parse_discount_value 相同的折數換算：0.85 / 8.5 / 85 都代表 85 折 (應付百分比) */
    function toPercent(val) {
        if (val <= 1) return Math.round(val * 100);
        if (val <= 10) return Math.round(val * 10);
        return Math.round(val);
    }

    /** 與後端 Coupon.discount_label 相同的顯示：90 → 9 折、85 → 85 折、5 → 0.5 折 */
    function percentLabel(pct) {
        if (pct % 10 === 0) return `${pct / 10} 折`;
        return `${pct > 10 ? pct : pct / 10} 折`;
    }

    function updateDiscountHint(prefix) {
        const type = $(`${prefix}-disc-type`);
        const input = $(`${prefix}-disc-val`);
        const hint = $(`${prefix}-disc-hint`);
        if (!type || !input || !hint) return;
        const raw = input.value.trim();
        const val = Number(raw);
        if (!raw || Number.isNaN(val)) {
            hint.style.display = 'none';
            return;
        }
        let cls = 'text-success';
        let content;
        if (val <= 0) {
            cls = 'text-danger';
            content = html`<i class="bi bi-exclamation-circle me-1"></i> 請輸入大於 0 的數值`;
        } else if (type.value === 'percent') {
            const pct = toPercent(val);
            if (pct >= 100) {
                cls = 'text-danger';
                content = html`<i class="bi bi-x-circle me-1"></i> 折扣不可大於等於全額 (無折扣)`;
            } else {
                cls = 'text-info';
                content = html`<i class="bi bi-magic me-1"></i> 系統將辨識為：<strong>${percentLabel(pct)}</strong> (結帳金額 × ${pct}%)`;
            }
        } else {
            content = html`<i class="bi bi-cash me-1"></i> 結帳整單現折：<strong>$${Math.round(val)} 元</strong>`;
        }
        hint.className = `small mt-1 fw-bold ${cls}`;
        Kiosk.setHtml(hint, content);
        hint.style.display = 'block';
    }

    function setDiscountMode(prefix, type) {
        const label = $(`${prefix}-disc-val-label`);
        const input = $(`${prefix}-disc-val`);
        if (!label || !input) return;
        if (type === 'percent') {
            label.textContent = '折扣折數 (支援 0.85、8.5 或 85)';
            input.placeholder = '例如 85 (代表 85 折)';
            input.max = '99';
            input.step = '0.01';
        } else {
            label.textContent = '折抵金額 (NT$)';
            input.placeholder = '50';
            input.removeAttribute('max');
            input.step = '1';
        }
        updateDiscountHint(prefix);
    }

    document.querySelectorAll('.coupon-disc-type').forEach((sel) => {
        sel.addEventListener('change', () => setDiscountMode(sel.dataset.prefix, sel.value));
    });
    document.querySelectorAll('.coupon-disc-val').forEach((input) => {
        input.addEventListener('input', () => {
            const prefix = input.dataset.prefix;
            const type = $(`${prefix}-disc-type`);
            const val = parseFloat(input.value);
            // 輸入 0.x 時自動切換為折數模式
            if (val > 0 && val < 1 && type.value === 'fixed') {
                type.value = 'percent';
                setDiscountMode(prefix, 'percent');
            } else {
                updateDiscountHint(prefix);
            }
        });
    });
    document.querySelectorAll('.per-user-limit').forEach((input) => {
        input.addEventListener('focus', () => { if (input.value === '無上限') input.select(); });
    });

    function openEditCoupon(btn) {
        const d = btn.dataset;
        $('editCouponForm').action = `/admin/edit_coupon/${encodeURIComponent(d.id)}`;
        $('edit-coupon-title').value = d.title || '';
        $('edit-coupon-code').value = d.code || '';
        const type = d.discountType === 'percent' ? 'percent' : 'fixed';
        $('edit-coupon-disc-type').value = type;
        $('edit-coupon-disc-val').value = d.discountValue || 0;
        setDiscountMode('edit-coupon', type);
        $('edit-coupon-min-spend').value = Math.round(Number(d.minSpend) || 0);
        $('edit-coupon-reward-points').value = d.rewardPoints || 0;
        $('edit-coupon-reward-discount').value = d.rewardDiscountPoints || 0;
        const limit = Number(d.perUserLimit) || 0;
        $('edit-coupon-per-user-limit').value = limit <= 0 ? '無上限' : limit;
        showModal('editCouponModal');
    }

    // ---------------------------------------------------------------- 會員資料
    function openEditUser(btn) {
        const d = btn.dataset;
        $('editUserForm').action = `/admin/edit_user/${encodeURIComponent(d.id)}`;
        $('edit-user-name').value = d.name || '';
        $('edit-user-phone').value = d.phone || '';
        $('edit-user-points').value = d.points || 0;
        $('edit-user-loaded-points').value = d.points || 0;
        // 每次開啟都清空 PIN 欄位，避免瀏覽器自動帶入已儲存的密碼而在不知情下重設會員 PIN
        $('edit-user-pin').value = '';
        showModal('editUserModal');
    }

    // ---------------------------------------------------------------- 會員持券管理
    const userCoupons = data.userCoupons || {};
    const couponsBody = $('userCouponsModalBody');
    let activeUser = null;          // { id, name }
    let reloadOnClose = false;

    function renderUserCoupons() {
        const coupons = userCoupons[activeUser.id] || [];
        Kiosk.setHtml(couponsBody, coupons.length === 0
            ? html`<div class="text-center text-white-50 py-3">此會員目前尚無任何專屬優惠券。</div>`
            : html`
            <div class="table-responsive">
                <table class="table table-dark-custom align-middle mb-0">
                    <thead>
                        <tr class="text-white-50 small">
                            <th>票券名稱 / 代碼</th>
                            <th>目前狀態</th>
                            <th class="text-end">切換使用權限</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${coupons.map((c) => html`
                        <tr>
                            <td>
                                <div class="fw-bold text-white">${c.title}</div>
                                <small class="text-warning">代碼: ${c.code}</small>
                            </td>
                            <td>${c.is_used
                                ? html`<span class="badge bg-dark text-white-50 border border-secondary">已核銷 (Used)</span>`
                                : html`<span class="badge bg-success text-white">未使用 (Available)</span>`}</td>
                            <td class="text-end">
                                <button type="button" class="btn btn-sm ${c.is_used ? 'btn-outline-success' : 'btn-outline-secondary'}"
                                        data-action="toggle-user-coupon" data-id="${c.id}">
                                    ${c.is_used ? '設為可用' : '設為已核銷'}
                                </button>
                            </td>
                        </tr>`)}
                    </tbody>
                </table>
            </div>`);
    }

    function openUserCoupons(btn) {
        activeUser = { id: btn.dataset.userId, name: btn.dataset.userName || '' };
        reloadOnClose = false;
        $('userCouponsModalTitle').textContent = `${activeUser.name} 的專屬票券管理`;
        renderUserCoupons();
        showModal('userCouponsModal');
    }

    async function toggleUserCoupon(btn) {
        btn.disabled = true;
        try {
            const res = await Kiosk.api(`/admin/toggle_user_coupon/${encodeURIComponent(btn.dataset.id)}`, { method: 'POST' });
            userCoupons[activeUser.id] = res.coupons || [];
            reloadOnClose = true;
            renderUserCoupons();
        } catch (err) {
            btn.disabled = false;
            window.alert(`❌ ${err.message}`);
        }
    }

    $('userCouponsModal')?.addEventListener('hidden.bs.modal', () => {
        if (reloadOnClose) {
            window.AdminPage.rememberTab('users');
            window.location.reload();
        }
    });

    Kiosk.on(document, 'click', {
        'edit-coupon': (el) => openEditCoupon(el),
        'edit-user': (el) => openEditUser(el),
        'user-coupons': (el) => openUserCoupons(el),
    });
    Kiosk.on(couponsBody, 'click', { 'toggle-user-coupon': (el) => toggleUserCoupon(el) });
})();
