/*
 * POS 客顯螢幕 (/pos/display，放在面向顧客的第二螢幕)：
 * 由同一台電腦的 POS 頁以 BroadcastChannel('pos-customer-display') 傳來購物車、應付金額、收款與找零。
 */
(function () {
    'use strict';

    const { html, setHtml } = Kiosk;
    const $ = (id) => document.getElementById(id);
    const money = (v) => `$${Math.round(Number(v) || 0).toLocaleString()}`;
    const PAID_HOLD_MS = 10000;
    let paidUntil = 0;

    function show(which) {
        ['cdIdle', 'cdCart', 'cdPaid'].forEach((id) => $(id).classList.toggle('d-none', id !== which));
        $('customerDisplay').dataset.state = { cdIdle: 'idle', cdCart: 'cart', cdPaid: 'paid' }[which];
    }

    function renderCart(msg) {
        const lines = msg.lines || [];
        if (!lines.length) { show('cdIdle'); return; }
        setHtml($('cdLines'), lines.map((l) => html`
            <tr><td><div class="fw-bold">${l.name}</div>${l.detail ? html`<small class="text-white-50">${l.detail}</small>` : ''}</td>
                <td class="text-center">${l.quantity}</td><td class="text-end">${money(l.subtotal)}</td></tr>`));
        const t = msg.totals || {};
        const discount = (t.promotion || 0) + (t.coupon || 0) + (t.tierDiscount || 0) + (t.manual || 0) + (t.pointsDiscount || 0);
        $('cdSubtotal').textContent = money(t.subtotal);
        $('cdDiscountRow').classList.toggle('d-none', !(discount > 0));
        $('cdDiscount').textContent = `-${money(discount)}`;
        $('cdServiceRow').classList.toggle('d-none', !(t.service > 0));
        $('cdService').textContent = money(t.service);
        $('cdTotal').textContent = money(msg.total);
        const cash = Number(msg.cash) || 0;
        $('cdCashRow').classList.toggle('d-none', !(cash > 0));
        $('cdChangeRow').classList.toggle('d-none', !(cash > 0));
        $('cdCash').textContent = money(cash);
        $('cdChange').textContent = money(Math.max(0, cash - (Number(msg.total) || 0)));
        const member = $('cdMember');
        member.classList.toggle('d-none', !msg.member);
        if (msg.member) member.textContent = `會員 ${msg.member.name}・點數 ${msg.member.points}`;
        show('cdCart');
    }

    function onMessage(msg) {
        if (!msg || typeof msg !== 'object') return;
        if (msg.kind === 'paid') {
            paidUntil = Date.now() + PAID_HOLD_MS;
            $('cdPickup').textContent = `#${String(msg.pickup || '').padStart(3, '0')}`;
            const change = Number(msg.change) || 0;
            $('cdPaidChange').classList.toggle('d-none', !(change > 0));
            $('cdPaidChange').textContent = `找您 ${money(change)}`;
            show('cdPaid');
            return;
        }
        if (msg.kind === 'cart') {
            // 收款完成畫面停留幾秒；期間 POS 清空購物車不覆蓋，開始點下一位客人的餐才切回
            if (Date.now() < paidUntil && !(msg.lines || []).length) return;
            paidUntil = 0;
            renderCart(msg);
        }
    }

    function tick() {
        $('cdClock').textContent = new Date().toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit' });
        if (paidUntil && Date.now() > paidUntil && $('customerDisplay').dataset.state === 'paid') {
            paidUntil = 0;
            show('cdIdle');
        }
    }

    if ('BroadcastChannel' in window) {
        const channel = new BroadcastChannel('pos-customer-display');
        channel.onmessage = (ev) => onMessage(ev.data);
        channel.postMessage({ kind: 'hello' });   // 請 POS 重送目前的購物車
    } else {
        $('cdWelcome').textContent = '此瀏覽器不支援客顯同步，請改用 Chrome / Edge';
    }
    tick();
    setInterval(tick, 1000);
})();
