/*
 * 點餐機斷線暫存 (第四輪)：送出訂單時連不到門市主機 (網路中斷、主機重新啟動) →
 * 現金 (櫃檯付款) 的訪客訂單先存在這台點餐機的瀏覽器 (localStorage)，畫面給顧客一個暫存單號，
 * 連線恢復後自動補送到 /api/kiosk/offline_order (以 client_order_id 去重，不會重複建單)。
 * 補送結果 (取餐號 / 失敗原因) 顯示在左下角的「暫存單」按鈕，店員可點開查看。
 * 使用點數、優惠券、紅利兌換、寄杯、電子支付、外送到府的訂單不暫存 (需要即時確認)。
 */
(function (global) {
    'use strict';

    const KEY = 'kiosk_offline_queue_v1';
    const RETRY_MS = 15000;
    const KEEP_DONE_MS = 12 * 3600 * 1000;
    const script = document.currentScript;
    const shop = script ? (script.dataset.shop || '') : '';
    const onOfflinePage = Boolean(document.getElementById('offline-page'));
    let flushing = false;

    function load() {
        try {
            const data = JSON.parse(localStorage.getItem(KEY) || '{}');
            return { pending: data.pending || [], done: data.done || [], failed: data.failed || [] };
        } catch (err) {
            return { pending: [], done: [], failed: [] };
        }
    }

    function save(q) {
        const cutoff = Date.now() - KEEP_DONE_MS;
        q.done = q.done.filter((e) => (e.sent_ms || 0) > cutoff).slice(-20);
        q.failed = q.failed.slice(-20);
        try { localStorage.setItem(KEY, JSON.stringify(q)); } catch (err) { /* 儲存空間已滿：仍顯示在畫面上 */ }
        render(q);
    }

    function localIso(d) {
        const p = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    /** 連線層級的失敗 (伺服器沒回應 / 閘道錯誤) 才暫存；其他錯誤 (售完、驗證) 照原本方式處理 */
    function shouldQueue(err) {
        return Boolean(shop) && err && (err.status === 0 || [502, 503, 504].includes(err.status));
    }

    /** 不能暫存的原因 (空字串 = 可以) */
    function blockReason(payload) {
        if (payload.payment_method !== 'Cash') return '只有「櫃檯付款 (現金)」的訂單可以暫存';
        if ((payload.use_points || 0) > 0 || payload.promo_code) return '使用點數或優惠券的訂單需要連線確認';
        if ((payload.items || []).some((i) => i.is_reward || i.pass_id)) return '紅利兌換與寄杯需要連線確認';
        if (payload.delivery || payload.pickup_at) return '外送與預約取餐需要連線確認';
        return '';
    }

    function enqueue(payload, total, summary) {
        const q = load();
        const id = payload.client_order_id;
        const existing = q.pending.find((e) => e.id === id);
        if (existing) return existing;
        const entry = {
            id, shop, code: String(id).replace(/[^0-9a-f]/gi, '').slice(-4).toUpperCase(),
            queued_at: localIso(new Date()), total: Number(total) || 0, summary: String(summary || '').slice(0, 200),
            order: { ...payload, use_points: 0, promo_code: '', expected_total: null },
        };
        q.pending.push(entry);
        save(q);
        setTimeout(flush, RETRY_MS);
        return entry;
    }

    async function flush() {
        if (flushing || onOfflinePage || !navigator.onLine || !load().pending.length) return;
        flushing = true;
        try {
            for (;;) {
                const entry = load().pending[0];
                if (!entry) break;
                let data = null;
                let failure = null;
                try {
                    const timeout = global.AbortSignal && AbortSignal.timeout ? { signal: AbortSignal.timeout(20000) } : {};
                    data = await Kiosk.api('/api/kiosk/offline_order', {
                        json: { shop: entry.shop, code: entry.code, queued_at: entry.queued_at, order: entry.order },
                        ...timeout,
                    });
                } catch (err) {
                    const retry = err.status === 0 || err.status >= 500 || err.status === 429
                        || (err.status === 403 && /CSRF/.test(err.message || ''));
                    if (retry) break;   // 仍連不上：下次再試
                    failure = err.message || `HTTP ${err.status}`;
                }
                // 等待回應期間可能又有新的暫存單 (其他顧客結帳)：重新讀取後只移動這一筆，不可用舊的副本覆蓋
                const q = load();
                q.pending = q.pending.filter((e) => e.id !== entry.id);
                if (failure) {
                    q.failed.push({ ...entry, error: failure });
                } else {
                    q.done.push({ ...entry, order: null, sent_ms: Date.now(), pickup_number: data.pickup_number,
                                  server_total: data.total_price });
                }
                save(q);
            }
        } finally {
            flushing = false;
            render();
        }
    }

    // ------------------------------------------------------------------ 左下角狀態與清單 (店員查看)
    let badge = null;
    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function render(q) {
        q = q || load();
        const total = q.pending.length + q.done.length + q.failed.length;
        if (!badge) {
            badge = el('button', 'btn btn-sm shadow position-fixed d-none', '');
            badge.id = 'offlineQueueBadge';
            badge.type = 'button';
            badge.style.cssText = 'left: 12px; bottom: 12px; z-index: 1080';
            badge.addEventListener('click', openList);
            document.body.append(badge);
        }
        badge.classList.toggle('d-none', !total);
        badge.className = badge.className.replace(/btn-(warning|danger|success)/g, '').trim();
        badge.classList.add(q.failed.length ? 'btn-danger' : (q.pending.length ? 'btn-warning' : 'btn-success'));
        badge.textContent = q.pending.length ? `暫存單 ${q.pending.length} 筆待補送`
            : (q.failed.length ? `暫存單 ${q.failed.length} 筆補送失敗` : `暫存單已補送 ${q.done.length} 筆`);
    }

    function openList() {
        const q = load();
        let modalEl = document.getElementById('offlineQueueModal');
        if (modalEl) modalEl.remove();
        modalEl = el('div', 'modal fade');
        modalEl.id = 'offlineQueueModal';
        modalEl.tabIndex = -1;
        const dialog = el('div', 'modal-dialog modal-dialog-scrollable');
        const content = el('div', 'modal-content bg-dark text-white');
        const header = el('div', 'modal-header');
        header.append(el('h5', 'modal-title', '斷線暫存單'));
        const close = el('button', 'btn-close btn-close-white');
        close.type = 'button';
        close.dataset.bsDismiss = 'modal';
        close.setAttribute('aria-label', '關閉');
        header.append(close);
        const body = el('div', 'modal-body small');
        const rows = [
            ...q.pending.map((e) => [e, 'text-warning', '待補送']),
            ...q.failed.map((e) => [e, 'text-danger', `補送失敗：${e.error}`]),
            ...q.done.map((e) => [e, 'text-success', `已送出，取餐號 #${String(e.pickup_number).padStart(3, '0')}`]),
        ];
        rows.forEach(([e, cls, label]) => {
            const row = el('div', 'border-bottom border-secondary py-2 offline-entry');
            row.dataset.code = e.code;
            row.append(el('div', 'fw-bold', `暫存單號 ${e.code}・${e.queued_at.replace('T', ' ').slice(5, 16)}・$${e.total}`));
            if (e.summary) row.append(el('div', 'text-white-50', e.summary));
            row.append(el('div', cls, label));
            body.append(row);
        });
        if (!rows.length) body.append(el('div', 'text-white-50', '沒有暫存單'));
        const footer = el('div', 'modal-footer');
        const clear = el('button', 'btn btn-outline-light btn-sm', '清除已完成 / 失敗的紀錄');
        clear.type = 'button';
        clear.id = 'offlineQueueClear';
        clear.addEventListener('click', () => {
            const cur = load();
            cur.done = [];
            cur.failed = [];
            save(cur);
            bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        });
        const retry = el('button', 'btn btn-warning btn-sm text-dark', '立即補送');
        retry.type = 'button';
        retry.addEventListener('click', () => { flush(); bootstrap.Modal.getOrCreateInstance(modalEl).hide(); });
        footer.append(clear, retry);
        content.append(header, body, footer);
        dialog.append(content);
        modalEl.append(dialog);
        document.body.append(modalEl);
        bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }

    /** 給顧客的暫存收據 */
    function showReceipt(entry) {
        let modalEl = document.getElementById('offlineReceiptModal');
        if (modalEl) modalEl.remove();
        modalEl = el('div', 'modal fade');
        modalEl.id = 'offlineReceiptModal';
        modalEl.tabIndex = -1;
        const dialog = el('div', 'modal-dialog modal-dialog-centered');
        const content = el('div', 'modal-content bg-dark text-white text-center p-4');
        content.append(el('i', 'bi bi-wifi-off display-4 text-warning'));
        content.append(el('h5', 'fw-bold mt-2', '網路暫時中斷，訂單已暫存'));
        content.append(el('div', 'text-white-50 small', '暫存單號'));
        const code = el('div', 'display-5 fw-bold text-warning', entry.code);
        code.id = 'offlineReceiptCode';
        content.append(code);
        content.append(el('div', 'mt-2', `金額約 $${entry.total}，請至櫃檯出示暫存單號付款`));
        content.append(el('div', 'small text-white-50 mt-1', '連線恢復後會自動送到廚房並編取餐號 (店員可查看)'));
        const ok = el('button', 'btn btn-warning fw-bold text-dark mt-3', '我知道了');
        ok.type = 'button';
        ok.dataset.bsDismiss = 'modal';
        content.append(ok);
        dialog.append(content);
        modalEl.append(dialog);
        document.body.append(modalEl);
        bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }

    global.KioskOffline = { shouldQueue, blockReason, enqueue, flush, showReceipt, load };

    render();
    if (onOfflinePage) {
        // 離線頁是快取的頁面 (CSRF Token 可能過期)：恢復連線後回到點餐首頁，由新的頁面補送
        global.addEventListener('online', () => { if (load().pending.length) global.location.href = '/'; });
    } else {
        global.addEventListener('online', () => setTimeout(flush, 1000));
        setInterval(flush, RETRY_MS);
        setTimeout(flush, 2000);
    }
})(window);
