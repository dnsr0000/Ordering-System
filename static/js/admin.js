/*
 * 後台共用腳本 (後台首頁 admin.html 與菜單頁 admin_menu.html)：首頁分頁 (由網址決定)、實時訂單看板、總覽數字與菜單庫存的即時更新。
 * 菜單 / 套餐 / 圖片裁切見 admin-menu.js；優惠券與兌換餐點見 admin-coupons.js；會員頁見 admin-members.js。
 * 本檔需最先載入，提供 window.AdminPage 給 admin-menu.js 共用。
 */
(function () {
    'use strict';
    const { html, $, setText, store } = Kiosk;
    const data = Kiosk.pageData();

    function autoExpand(el) {
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = `${el.scrollHeight}px`;
    }

    const showModal = (id) => Kiosk.modal(id).show();

    const AdminPage = window.AdminPage = {
        data,
        storage: store,
        autoExpand,
        showModal,
        // 菜單品項的編輯預設值 (id 字串 → 資料)，即時同步庫存時一併更新
        menuItems: new Map((data.menuItems || []).map((item) => [String(item.id), item])),
    };

    // ---------------------------------------------------------------- 頁籤 (只有後台首頁有)
    // 由網址決定分頁：/admin = 總覽、?tab=orders | rewards。切換分頁時同步更新網址與側欄標示，
    // 側欄連結、重新整理與動作後的導回都會停在網址指定的分頁 (不另外記憶上次的分頁，避免與側欄不一致)
    function syncTabUrl(key) {
        const params = new URLSearchParams(window.location.search);
        if (key === 'home') params.delete('tab');
        else params.set('tab', key);
        const query = params.toString();
        window.history.replaceState({}, document.title, window.location.pathname + (query ? `?${query}` : ''));
        document.querySelectorAll('a.admin-nav-link').forEach((link) => {
            const url = new URL(link.href, window.location.href);
            if (url.pathname !== window.location.pathname) return;
            const on = (url.searchParams.get('tab') || 'home') === key;
            link.classList.toggle('active', on);
            if (on) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        });
    }

    function initTabs() {
        if (!$('adminTab')) return;
        const target = new URLSearchParams(window.location.search).get('tab') || 'home';
        const btn = $(`${target}-tab`) || document.querySelector('#adminTab [data-tab-key]');
        if (btn) bootstrap.Tab.getOrCreateInstance(btn).show();
        document.querySelectorAll('#adminTab [data-tab-key]').forEach((tabBtn) => {
            tabBtn.addEventListener('shown.bs.tab', () => syncTabUrl(tabBtn.dataset.tabKey));
        });
    }

    // ---------------------------------------------------------------- 實時訂單看板
    // 狀態下拉選單：「⏳ 製作中」等文字由共用的狀態表組成
    const STATUS_OPTIONS = Object.entries(Kiosk.ORDER_STATUS).map(([value, st]) => [value, `${st.icon} ${st.text}`]);
    const grid = $('admin-orders-grid');

    function orderCard(o) {
        // 線上訂單在取餐分店收到前尚未編號 (分店編號後同步回來)：Kiosk.pickupNo 顯示「待分店確認」
        const showCash = o.payment_method === 'Cash' && Number(o.cash_received) > Number(o.total_price);
        return html`
        <div class="col-12 col-md-6 col-lg-4">
            <div class="card card-custom p-3 shadow-sm h-100 border-top border-4 ${(Kiosk.ORDER_STATUS[o.status] || {}).border || 'border-info'}">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <h5 class="fw-bold text-warning mb-0">取餐 ${Kiosk.pickupNo(o)} <small class="text-white-50 fs-6">(單號 #${o.id})</small></h5>
                    <div class="d-flex flex-wrap justify-content-end gap-1">
                        ${o.order_source === 'POS'
                            ? html`<span class="badge bg-warning text-dark"><i class="bi bi-pc-display-horizontal me-1"></i>POS (${o.cashier_username || '櫃檯'})</span>`
                            : (o.order_source === 'Online'
                                ? html`<span class="badge bg-success"><i class="bi bi-globe me-1"></i>線上點餐</span>`
                                : (o.order_source === 'Table'
                                    ? html`<span class="badge bg-light text-dark"><i class="bi bi-qr-code me-1"></i>桌邊掃碼</span>`
                                    : html`<span class="badge bg-info text-dark"><i class="bi bi-phone me-1"></i>自助機</span>`))}
                        ${o.pickup_node_name ? html`<span class="badge bg-light text-dark order-node-name"><i class="bi bi-shop me-1"></i>${o.pickup_node_name}</span>` : ''}
                        <span class="badge bg-primary">${o.order_type}</span>
                        <span class="badge bg-secondary">${o.payment_method}</span>
                        ${Kiosk.paymentBadge(o.payment_status)}
                    </div>
                </div>
                ${Kiosk.takeoutBlock(o)}
                <div class="text-white-50 small mb-2 d-flex justify-content-between">
                    <span><i class="bi bi-person me-1"></i>${o.user_name}</span>
                    <span><i class="bi bi-clock me-1"></i>${o.created_at}</span>
                </div>
                <hr class="border-secondary my-2">
                <div class="order-items-list mb-3 flex-grow-1">
                    ${(o.items || []).map((item) => html`
                    <div class="d-flex justify-content-between align-items-start mb-2">
                        <div>
                            <div class="text-white small fw-bold">${item.name} x ${Number(item.quantity) || 0}</div>
                            ${item.customization ? html`<small class="text-danger d-block item-custom">${item.customization}</small>` : ''}
                        </div>
                        <div class="text-white-50 small">${Kiosk.money(item.subtotal)}</div>
                    </div>`)}
                </div>
                <div class="d-flex justify-content-between align-items-center pt-2 border-top border-secondary mt-auto">
                    <div>
                        <small class="text-white-50 d-block order-meta">
                            折抵: ${Kiosk.money(o.discount_amount)}
                            ${showCash ? html` • 實收 ${Kiosk.money(o.cash_received)} / 找零 ${Kiosk.money(o.cash_change)}` : ''}
                        </small>
                        <span class="text-white fw-bold fs-5">${Kiosk.money(o.total_price)}</span>
                    </div>
                    <select class="form-select form-select-sm order-status-select" data-action="order-status" data-id="${o.id}" aria-label="訂單狀態">
                        ${STATUS_OPTIONS.map(([value, label]) => html`<option value="${value}" ${value === o.status ? html`selected` : ''}>${label}</option>`)}
                    </select>
                </div>
            </div>
        </div>`;
    }

    function renderOrders(orders) {
        if (!grid) return;
        // 店員正在操作狀態下拉選單時先不重繪，避免選單被關閉
        if (grid.contains(document.activeElement) && document.activeElement.tagName === 'SELECT') return;
        Kiosk.setHtml(grid, orders.length ? orders.map(orderCard) : html`
            <div class="col-12 text-center py-5">
                <i class="bi bi-inbox text-secondary display-3"></i>
                <p class="text-white-50 mt-2">目前沒有任何訂單紀錄</p>
            </div>`);
    }

    async function changeOrderStatus(select) {
        select.blur();
        try {
            await Kiosk.setOrderStatus(select.dataset.id, select.value);
        } catch (err) {
            window.alert(`❌ ${err.message}`);
        }
        syncLive();
    }

    // ---------------------------------------------------------------- 菜單庫存即時同步 (菜單頁的清單)
    function updateMenuStock(menuItems) {
        menuItems.forEach((it) => {
            const soldOut = it.is_sold_out || it.stock <= 0;
            const badge = $(`item-stock-badge-${it.id}`);
            if (badge) {
                badge.textContent = it.stock;
                badge.className = `badge fw-bold px-2 py-1 ${Kiosk.stockClass(it.stock, soldOut)}`;
            }
            const tagCell = $(`item-tags-${it.id}`);
            if (tagCell) {
                const existing = tagCell.querySelector('.sold-out-badge');
                if (soldOut && !existing) {
                    const span = document.createElement('span');
                    span.className = 'badge bg-secondary me-1 sold-out-badge';
                    span.textContent = '已售完';
                    tagCell.prepend(span);
                } else if (!soldOut && existing) {
                    existing.remove();
                }
            }
            // 編輯視窗開啟時要帶入最新庫存
            const formData = AdminPage.menuItems.get(String(it.id));
            if (formData) {
                formData.stock = it.stock;
                formData.is_sold_out = soldOut;
            }
        });
    }

    // ---------------------------------------------------------------- 總覽數字
    function renderTopItems(topItems) {
        const el = $('stat-top-items');
        if (!el || !Array.isArray(topItems)) return;
        const max = Math.max(0, ...topItems.map((t) => Number(t.quantity) || 0));
        Kiosk.setHtml(el, topItems.length ? topItems.map((t, idx) => {
            const pct = max ? Math.round((Number(t.quantity) || 0) / max * 1000) / 10 : 0;
            return html`
            <div class="list-group-item bg-transparent border-secondary text-white px-0 py-2">
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <div class="fw-bold">${idx + 1}. ${t.name}</div>
                        <small class="text-white-50">已售 ${Number(t.quantity) || 0} 份</small>
                    </div>
                    <span class="badge bg-warning text-dark">$${Number(t.revenue) || 0}</span>
                </div>
                <div class="top-item-bar mt-1" aria-hidden="true"><span style="width: ${pct}%"></span></div>
            </div>`;
        }) : html`<div class="text-white-50">目前尚無銷售資料。</div>`);
    }

    function updateAnalytics(stats) {
        if (!stats || !$('stat-today-orders')) return;
        setText('stat-today-orders', stats.today_orders);
        setText('stat-today-revenue', `$${stats.today_revenue}`);
        setText('stat-pending-count', stats.pending_count);
        const stale = Number(stats.stale_pending_count) || 0;
        setText('stat-stale-pending-n', stale);
        const staleEl = $('stat-stale-pending');
        if (staleEl) staleEl.hidden = stale <= 0;
        setText('stat-eta-minutes', `${stats.eta_minutes} 分鐘`);
        setText('stat-eta-basis', stats.eta_based_on_samples
            ? `依今日實際平均 ${stats.eta_per_order_minutes} 分鐘/單 (已排除極端值)`
            : `今日尚無出餐紀錄，暫以 ${stats.eta_per_order_minutes} 分鐘/單估算`);
        setText('stat-avg-order-value', Math.round(Number(stats.avg_order_value) || 0));
        setText('stat-avg-order-time', stats.avg_order_time ? stats.avg_order_time : '-');
        setText('stat-completed-today', stats.completed_today ?? 0);
        if (stats.ai_advice) setText('stat-ai-advice', stats.ai_advice);
        renderTopItems(stats.top_items);
    }

    // ---------------------------------------------------------------- 即時同步 (SSE + 備援輪詢)：需要「報表」頁面權限
    const rankingLimit = Math.max(1, Number(data.rankingLimit) || 3);
    let sessionExpired = false;
    let eventSource = null;

    async function syncLive() {
        if (sessionExpired) return;
        let res;
        try {
            res = await Kiosk.api(`/api/admin_live_orders?limit=${rankingLimit}`);
        } catch (err) {
            if (err.status === 401) {
                sessionExpired = true;
                if (eventSource) eventSource.close();
                window.alert('⚠️ 管理者登入已過期，請重新登入！');
                window.location.href = data.loginUrl || '/admin';
            }
            return; // 其他暫時性錯誤：下一次通知或輪詢時再同步
        }
        updateMenuStock(res.menu_items || []);
        updateAnalytics(res.analytics);
        renderOrders(res.orders || []);
    }

    function startLiveSync() {
        if (!data.liveSync || !(grid || $('menuItemTable') || $('stat-top-items'))) return;
        syncLive();
        // SSE 斷線期間 (或不支援 EventSource 時) 每 3 秒輪詢一次
        eventSource = Kiosk.onOrdersChanged(syncLive, { fallbackMs: 3000 });
        // AI 營運建議在背景產生，定期刷新一次避免需要手動重新整理
        setInterval(syncLive, 10000);
        // 按登出時先停止即時同步 (close 一併停止備援輪詢)：避免登出途中的同步請求拿到 401 而跳出「登入已過期」
        document.querySelectorAll('form[action$="/admin/logout"]').forEach((form) => {
            form.addEventListener('submit', () => {
                sessionExpired = true;
                eventSource.close();
            });
        });
    }

    // ---------------------------------------------------------------- 熱門排行筆數
    function submitRankingLimit() {
        const input = $('rankingLimitInput');
        let limit = Math.floor(Number(input && input.value));
        if (!limit || limit < 1) limit = 3;
        limit = Math.min(limit, 100);
        window.location.href = `${data.loginUrl || '/admin'}?limit=${limit}`;
    }

    // ---------------------------------------------------------------- 初始化
    initTabs();
    if (grid) Kiosk.on(grid, 'change', { 'order-status': (el) => changeOrderStatus(el) });
    Kiosk.on(document, 'click', { 'ranking-limit': submitRankingLimit });
    $('rankingLimitInput')?.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
            ev.preventDefault();
            submitRankingLimit();
        }
    });
    document.querySelectorAll('textarea.auto-expand-desc').forEach((ta) => ta.addEventListener('input', () => autoExpand(ta)));

    startLiveSync();
})();
