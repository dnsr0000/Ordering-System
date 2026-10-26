/*
 * 即時看板 (/admin/live)：以 page-data 先畫出，之後每 30 秒與訂單變動 (SSE) 時重新抓取 /api/admin/live。
 */
(function () {
    'use strict';

    const { html, setHtml, api } = Kiosk;
    const $ = (id) => document.getElementById(id);
    const money = (v) => `$${Math.round(Number(v) || 0).toLocaleString()}`;

    function render(d) {
        $('liveAt').textContent = d.at;
        $('liveRevenue').textContent = money(d.revenue);
        $('liveOrders').textContent = d.orders;
        $('liveAvg').textContent = money(d.avg_ticket);
        $('livePending').textContent = d.pending;
        $('liveCancelled').textContent = d.cancelled;
        const vs = $('liveVs');
        if (d.vs_yesterday === null || d.vs_yesterday === undefined) {
            vs.textContent = `昨日同時段 ${money(d.yesterday_same_time)}`;
            vs.className = 'small text-white-50';
        } else {
            vs.textContent = `較昨日同時段 ${d.vs_yesterday >= 0 ? '+' : ''}${d.vs_yesterday}%`;
            vs.className = `small ${d.vs_yesterday >= 0 ? 'text-success' : 'text-danger'}`;
        }
        setHtml($('liveStores').querySelector('tbody'), (d.stores || []).length ? d.stores.map((s) => html`
            <tr class="live-store-row">
                <td class="fw-bold">${s.name}</td>
                <td>${s.connected ? html`<span class="badge bg-success">連線中</span>` : html`<span class="badge bg-danger">離線 (${s.last_seen})</span>`}</td>
                <td class="text-warning">${money(s.revenue)}</td><td>${s.orders}</td><td>${money(s.avg_ticket)}</td>
                <td>${s.pending}</td><td>${s.online}</td>
            </tr>`) : html`<tr><td colspan="7" class="text-center text-white-50">今天尚無訂單</td></tr>`);
        setHtml($('liveTop'), (d.top_items || []).map((i) => html`<li>${i.name} <span class="text-white-50">x${i.qty}</span></li>`));
        const max = Math.max(1, d.hour_max || 1);
        setHtml($('liveHourly'), (d.hourly || []).map((v, h) => (v || (h >= 7 && h <= 22)) ? html`
            <div class="bar-row"><span>${String(h).padStart(2, '0')}:00</span>
                <div class="bar-track"><div class="bar-fill" data-pct="${Math.round(v * 100 / max)}"></div></div>
                <span>${money(v)}</span></div>` : ''));
        document.querySelectorAll('#liveHourly .bar-fill').forEach((el) => { el.style.width = `${el.dataset.pct}%`; });
        setHtml($('liveRecent').querySelector('tbody'), (d.recent || []).map((o) => html`
            <tr><td>${o.time}</td><td>#${o.id}</td><td>${o.store}</td><td>${money(o.total)}</td><td>${o.status}</td></tr>`));
    }

    async function refresh() {
        try {
            render(await api('/api/admin/live'));
        } catch (err) { /* 暫時斷線：下一次再試 */ }
    }

    render(Kiosk.pageData());
    setInterval(refresh, 30000);
    try { Kiosk.onOrdersChanged(refresh); } catch (err) { /* 不支援 SSE 時只用定時更新 */ }
})();
