/*
 * 會員頁 (templates/admin_members.html)：會員資料編輯視窗、會員持券管理、名冊排序。
 */
(function () {
    'use strict';
    const { html } = Kiosk;
    const data = Kiosk.pageData();
    const $ = (id) => document.getElementById(id);
    const showModal = (id) => bootstrap.Modal.getOrCreateInstance($(id)).show();

    // ---------------------------------------------------------------- 名冊排序：換選項就重新查詢
    document.querySelectorAll('#memberSearchForm [data-autosubmit]').forEach((sel) => {
        sel.addEventListener('change', () => sel.form.requestSubmit());
    });

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
        if (reloadOnClose) window.location.reload();   // 名冊上的票券張數要重新算
    });

    Kiosk.on(document, 'click', {
        'edit-user': (el) => openEditUser(el),
        'user-coupons': (el) => openUserCoupons(el),
    });
    Kiosk.on(couponsBody, 'click', { 'toggle-user-coupon': (el) => toggleUserCoupon(el) });
})();
