/**
 * 已註冊的裝置 (點餐機 / POS / 廚房看板…)：每分鐘回報心跳；後台遠端鎖定時蓋上鎖定畫面，要求重新整理時重新載入。
 */
(() => {
    const body = document.body;
    if (!body.dataset.device || !window.Kiosk) return;
    let overlay = null;

    function showLock(message) {
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.className = 'device-lock';
            overlay.id = 'deviceLock';
            overlay.setAttribute('role', 'alertdialog');
            overlay.innerHTML = '<div><div class="device-lock-icon">🔒</div><div class="device-lock-text"></div></div>';
            document.body.appendChild(overlay);
        }
        overlay.querySelector('.device-lock-text').textContent = message;
    }

    function hideLock() {
        if (overlay) {
            overlay.remove();
            overlay = null;
        }
    }

    async function beat() {
        let delay = 60000;
        try {
            const res = await window.Kiosk.api('/api/device/heartbeat', {
                json: { version: body.dataset.appVersion || '', path: window.location.pathname } });
            if (!res.registered) return;
            if (res.reload) {
                window.location.reload();
                return;
            }
            if (res.locked) {
                showLock(res.message);
                delay = 15000;
            } else {
                hideLock();
            }
        } catch (err) {
            delay = 60000;   // 網路中斷：下一分鐘再試
        }
        setTimeout(beat, delay);
    }

    setTimeout(beat, 1500);
})();
