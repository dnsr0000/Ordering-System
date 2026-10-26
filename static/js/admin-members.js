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
        $('edit-coupon-member-only').checked = d.memberOnly === '1';
        $('edit-coupon-valid-from').value = d.validFrom || '';
        $('edit-coupon-valid-until').value = d.validUntil || '';
        $('edit-coupon-valid-days').value = d.validDays || 0;
        $('edit-coupon-login-required').checked = d.loginRequired === '1';
        $('edit-coupon-uses-per-member').value = d.usesPerMember || 0;
        $('edit-coupon-max-discount').value = d.maxDiscount || 0;
        syncMemberOnly('edit-coupon');
        showModal('editCouponModal');
    }

    // 會員專屬券不在紅利商城兌換：兌換點數欄位停用並歸零
    function syncMemberOnly(prefix) {
        const only = $(`${prefix}-member-only`);
        if (!only) return;
        [`${prefix}-reward-points`, `${prefix}-reward-discount`].forEach((id) => {
            const el = $(id);
            if (!el) return;
            el.disabled = only.checked;
            if (only.checked) el.value = 0;
        });
    }
    ['coupon', 'edit-coupon'].forEach((prefix) => {
        $(`${prefix}-member-only`)?.addEventListener('change', () => syncMemberOnly(prefix));
    });

    // ---------------------------------------------------------------- 批量發放
    function syncIssueTarget() {
        const target = $('issue-target').value;
        $('issue-tier-row').classList.toggle('d-none', target !== 'tier');
        $('issue-phones-row').classList.toggle('d-none', target !== 'phones');
        $('issue-phones').required = target === 'phones';
    }

    function openIssueCoupon(btn) {
        const d = btn.dataset;
        $('issueCouponForm').action = `/admin/coupons/${encodeURIComponent(d.id)}/issue`;
        $('issue-coupon-title').textContent = d.title || '';
        $('issue-coupon-code').textContent = d.code || '';
        const limit = Number(d.limit) || 0;
        $('issue-coupon-limit').textContent = limit <= 0 ? '無上限' : `${limit} 張`;
        $('issue-coupon-period').textContent = d.period || '不限';
        syncIssueTarget();
        showModal('issueCouponModal');
    }
    $('issue-target')?.addEventListener('change', syncIssueTarget);

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
    const userCoupons = {};   // 開啟票券視窗時才向伺服器載入
    const couponsBody = $('userCouponsModalBody');
    let activeUser = null;          // { id, name }
    let reloadOnClose = false;

    const historyDays = data.couponHistoryDays || 7;
    const STATUS_BADGE = {
        available: html`<span class="badge bg-success">未使用</span>`,
        upcoming: html`<span class="badge bg-info text-dark">活動未開始</span>`,
        used: html`<span class="badge bg-dark text-white-50 border border-secondary">已使用</span>`,
        expired: html`<span class="badge bg-danger">已過期</span>`,
    };

    function couponRow(c) {
        const unused = c.status === 'available' || c.status === 'upcoming';
        let when = '';
        if (c.status === 'used') when = `使用於 ${c.used_at || '-'}`;
        else if (c.status === 'expired') when = `${c.expires_at || '-'} 到期`;
        else when = c.expires_at ? `${c.expires_at} 到期` : '無到期日';
        // 已過期的券不提供切換 (要再給會員請重新發放)
        const button = c.status === 'expired' ? '' : html`
            <button type="button" class="btn btn-sm ${unused ? 'btn-outline-secondary' : 'btn-outline-success'}"
                    data-action="toggle-user-coupon" data-id="${c.id}">${unused ? '設為已使用' : '設為未使用'}</button>`;
        return html`
            <tr>
                <td>
                    <div class="fw-bold text-white">${c.title} <span class="small text-warning">${c.label}</span></div>
                    <small class="text-white-50">代碼 ${c.code}｜${c.source}｜領取 ${c.created_at}</small>
                </td>
                <td>${STATUS_BADGE[c.status] || ''}<div class="small text-white-50 mt-1">${when}</div></td>
                <td class="text-end">${button}</td>
            </tr>`;
    }

    function couponTable(list, empty) {
        if (!list.length) return html`<div class="text-center text-white-50 small py-3">${empty}</div>`;
        return html`
            <div class="table-responsive">
                <table class="table table-dark-custom align-middle mb-0">
                    <tbody>${list.map(couponRow)}</tbody>
                </table>
            </div>`;
    }

    function renderUserCoupons() {
        const coupons = userCoupons[activeUser.id] || [];
        const unused = coupons.filter((c) => c.status === 'available' || c.status === 'upcoming');
        const history = coupons.filter((c) => c.status === 'used' || c.status === 'expired');
        Kiosk.setHtml(couponsBody, html`
            <h6 class="fw-bold text-success mb-2"><i class="bi bi-ticket-perforated me-1"></i>未使用 (${unused.length})</h6>
            ${couponTable(unused, '目前沒有可使用的優惠券。')}
            <h6 class="fw-bold text-white-50 mt-4 mb-2"><i class="bi bi-clock-history me-1"></i>已使用 / 已過期 (近 ${historyDays} 天，${history.length})</h6>
            ${couponTable(history, `近 ${historyDays} 天沒有已使用或過期的優惠券。`)}
            <div class="small text-white-50 mt-2">超過 ${historyDays} 天的已使用 / 已過期紀錄不顯示 (資料仍保留，每人上限與報表照常計算)。</div>`);
    }

    async function openUserCoupons(btn) {
        activeUser = { id: btn.dataset.userId, name: btn.dataset.userName || '' };
        reloadOnClose = false;
        $('userCouponsModalTitle').textContent = `${activeUser.name} 的專屬票券管理`;
        Kiosk.setHtml(couponsBody, html`<div class="text-center text-white-50 py-3"><span class="spinner-border spinner-border-sm me-2"></span>載入中...</div>`);
        showModal('userCouponsModal');
        try {
            const res = await Kiosk.api(`/admin/users/${encodeURIComponent(activeUser.id)}/coupons`);
            userCoupons[activeUser.id] = res.coupons || [];
            renderUserCoupons();
        } catch (err) {
            Kiosk.setHtml(couponsBody, html`<div class="text-danger text-center py-3">❌ ${err.message}</div>`);
        }
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
        'issue-coupon': (el) => openIssueCoupon(el),
    });
    Kiosk.on(couponsBody, 'click', { 'toggle-user-coupon': (el) => toggleUserCoupon(el) });
})();
