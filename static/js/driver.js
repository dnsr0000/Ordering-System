/*
 * 外送員手機頁 (/driver/<token>)：按「開始回報位置」後用 Geolocation watchPosition 取得位置，
 * 每隔設定秒數 (或移動超過 25 公尺) 回報一次到 /api/driver/<token>/position；伺服器回 409 (配送已結束) 時停止。
 * Geolocation 只在 HTTPS (或 localhost) 下可用。
 */
(function () {
    'use strict';

    const data = Kiosk.pageData();
    const startBtn = document.getElementById('gpsStartBtn');
    const stopBtn = document.getElementById('gpsStopBtn');
    const msg = document.getElementById('gpsMsg');
    const statusEl = document.getElementById('driverStatus');
    const intervalMs = Math.max(5, Number(data.interval) || 15) * 1000;
    const MIN_MOVE_M = 25;

    let watchId = null;
    let timer = null;
    let latest = null;       // 最新取得的座標
    let lastSent = null;     // 上次送出的座標
    let lastSentAt = 0;
    let sent = 0;

    const say = (text, cls = 'text-white-50') => { msg.textContent = text; msg.className = `small mt-2 ${cls}`; };

    function distanceM(a, b) {
        const r = 6371000;
        const p1 = (a.lat * Math.PI) / 180;
        const p2 = (b.lat * Math.PI) / 180;
        const dp = ((b.lat - a.lat) * Math.PI) / 180;
        const dl = ((b.lng - a.lng) * Math.PI) / 180;
        const x = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
        return 2 * r * Math.asin(Math.sqrt(x));
    }

    async function send(force = false) {
        if (!latest) return;
        const moved = !lastSent || distanceM(lastSent, latest) >= MIN_MOVE_M;
        const due = Date.now() - lastSentAt >= intervalMs;
        if (!force && !moved && !due) return;
        try {
            // age：這個定位是幾秒前取得的 (GPS 中斷時定時重送的是舊位置，伺服器據此記錄實際時間)
            const age = latest.fixAt ? Math.max(0, Math.round((Date.now() - latest.fixAt) / 1000)) : 0;
            const res = await Kiosk.api(`/api/driver/${data.token}/position`,
                { json: { lat: latest.lat, lng: latest.lng, accuracy: latest.accuracy, age } });
            lastSent = latest;
            lastSentAt = Date.now();
            sent += 1;
            say(`已回報 ${sent} 次，最後 ${res.at}${latest.accuracy ? `（精度 ±${Math.round(latest.accuracy)} 公尺）` : ''}`, 'text-success');
            if (statusEl && res.delivery_status) statusEl.textContent = { assigned: '已指派外送員', out: '配送中' }[res.delivery_status] || statusEl.textContent;
            if (!res.active) finish('配送已結束，已停止回報。辛苦了！');
        } catch (err) {
            if (err.status === 409 || err.status === 404) finish(err.message || '配送已結束，已停止回報');
            else say(`回報失敗：${err.message}（會再重試）`, 'text-danger');
        }
    }

    function onPosition(pos) {
        latest = { lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy || 0, fixAt: pos.timestamp || Date.now() };
        send();
    }

    function onError(err) {
        const reasons = { 1: '未允許定位，請在瀏覽器設定允許位置存取後重試', 2: '目前無法取得位置 (請確認已開啟 GPS)', 3: '取得位置逾時，持續嘗試中…' };
        say(reasons[err.code] || err.message, err.code === 1 ? 'text-danger' : 'text-warning');
        if (err.code === 1) stop();
    }

    function start() {
        if (!navigator.geolocation) { say('此瀏覽器不支援定位', 'text-danger'); return; }
        if (!window.isSecureContext) { say('定位需要 HTTPS 網址，請聯絡店家', 'text-danger'); return; }
        watchId = navigator.geolocation.watchPosition(onPosition, onError, { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
        timer = setInterval(() => send(true), intervalMs);
        startBtn.classList.add('d-none');
        stopBtn.classList.remove('d-none');
        say('定位中…請允許位置存取');
        try { localStorage.setItem(`driver-gps-${data.token}`, '1'); } catch (e) { /* 私密模式 */ }
    }

    function stop() {
        if (watchId !== null) navigator.geolocation.clearWatch(watchId);
        if (timer) clearInterval(timer);
        watchId = timer = null;
        startBtn.classList.remove('d-none');
        stopBtn.classList.add('d-none');
        try { localStorage.removeItem(`driver-gps-${data.token}`); } catch (e) { /* ignore */ }
    }

    function finish(text) {
        stop();
        startBtn.disabled = true;
        startBtn.textContent = '配送已結束';
        say(text, 'text-info');
    }

    Kiosk.on(document, 'click', { start: () => start(), stop: () => { stop(); say('已停止回報'); } });
    // 重新整理後自動繼續 (watchPosition 需要使用者手勢才會跳出授權；已授權過的瀏覽器會直接繼續)
    try {
        if (data.active && localStorage.getItem(`driver-gps-${data.token}`) === '1') start();
    } catch (e) { /* ignore */ }
})();
