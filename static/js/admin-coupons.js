/*
 * 後台首頁「回饋商城與優惠券」分頁：優惠券 (新增 / 編輯 / 折扣提示 / 批量發放) 與兌換餐點的上架 / 改點數。
 */
(function () {
    'use strict';
    const { html } = Kiosk;
    const $ = (id) => document.getElementById(id);
    const showModal = (id) => bootstrap.Modal.getOrCreateInstance($(id)).show();

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

    // ---------------------------------------------------------------- 兌換餐點：上架 (空白表單) 或修改點數 (帶入該餐點)
    function openRewardModal(btn) {
        const select = $('add-reward-item');
        const editing = Boolean(btn && btn.dataset.id);
        select.disabled = false;
        select.value = editing ? btn.dataset.id : '';
        $('add-reward-points').value = editing ? btn.dataset.points : '';
        $('add-reward-discount').value = editing ? btn.dataset.discount : 0;
        $('add-reward-submit').textContent = editing ? '儲存點數' : '確認上架商城';
        showModal('addRewardModal');
    }

    Kiosk.on(document, 'click', {
        'edit-coupon': (el) => openEditCoupon(el),
        'issue-coupon': (el) => openIssueCoupon(el),
        'add-reward': () => openRewardModal(null),
        'edit-reward': (el) => openRewardModal(el),
    });
})();
