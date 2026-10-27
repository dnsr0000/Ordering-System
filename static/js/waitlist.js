/* 候位進度頁：每 15 秒更新；輪到時震動提示 */
(function () {
    'use strict';
    const data = Kiosk.pageData();
    const stateEl = document.getElementById('waitState');
    let last = data.status;

    async function refresh() {
        if (['seated', 'cancelled'].includes(last)) return;
        try {
            const d = await Kiosk.api(`/waitlist/${data.token}?format=json`);
            if (d.status === 'waiting') stateEl.textContent = `前面還有 ${d.ahead} 組`;
            else if (d.status === 'called') stateEl.textContent = '輪到您了，請至櫃檯入座！';
            else if (d.status === 'seated') stateEl.textContent = '已入座，祝用餐愉快';
            else stateEl.textContent = '已過號 / 取消';
            if (d.status === 'called' && last !== 'called' && navigator.vibrate) navigator.vibrate([400, 200, 400]);
            last = d.status;
        } catch (err) { /* 暫時斷線 */ }
    }
    setInterval(refresh, 15000);
})();
