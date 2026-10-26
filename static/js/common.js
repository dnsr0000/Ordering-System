/*
 * 所有頁面共用的前端工具 (取代各頁重複的 fetch 改寫、escapeHtml、SSE、時鐘、閒置登出)。
 * 內容安全政策 (CSP) 不允許行內 <script> 與 onclick 等行內事件，頁面一律以 data-action 委派事件。
 */
(function (global) {
    'use strict';

    // ---------------------------------------------------------------- HTML 跳脫
    class SafeHtml {
        constructor(value) { this.value = value; }
        toString() { return this.value; }
    }

    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function render(value) {
        if (value instanceof SafeHtml) return value.value;
        if (Array.isArray(value)) return value.map(render).join('');
        if (value === null || value === undefined || value === false) return '';
        return escapeHtml(value);
    }

    /**
     * 樣板字串標籤：插入的值一律自動跳脫，巢狀的 html`` 與陣列會直接組合。
     *   el.innerHTML = html`<b>${user.name}</b>${items.map(i => html`<li>${i.name}</li>`)}`;
     */
    function html(strings, ...values) {
        let out = strings[0];
        values.forEach((v, i) => { out += render(v) + strings[i + 1]; });
        return new SafeHtml(out);
    }

    function setHtml(el, content) {
        el.innerHTML = render(content);
    }

    /**
     * 關閉 Bootstrap 視窗後再執行 fn (例如接著開下一個視窗)。視窗還在開啟動畫中時 hide() 會被 Bootstrap 略過，
     * 因此同時等「開啟完成」再關一次 (手指點太快時不會卡住)；關閉後移除這個等待，不影響下一次開啟。
     */
    function hideModalThen(el, fn) {
        if (!el.classList.contains('show')) {
            fn();
            return;
        }
        const inst = window.bootstrap.Modal.getOrCreateInstance(el);
        const onShown = () => inst.hide();
        el.addEventListener('shown.bs.modal', onShown, { once: true });
        el.addEventListener('hidden.bs.modal', () => {
            el.removeEventListener('shown.bs.modal', onShown);
            fn();
        }, { once: true });
        inst.hide();
    }

    // ---------------------------------------------------------------- API
    class ApiError extends Error {
        constructor(message, status, data) {
            super(message);
            this.status = status;
            this.data = data || {};
        }
    }

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    /**
     * 呼叫後端 API：自動帶 CSRF Token、JSON 編碼，非 2xx 時拋出 ApiError (message 為後端的提示訊息)。
     *   const data = await Kiosk.api('/api/orders/5/status', { json: { status: 'Completed' } });
     *   await Kiosk.api('/face_login', { body: formData });   // FormData 直接送出
     */
    async function api(url, options = {}) {
        const { json, body, method, headers = {}, ...rest } = options;
        const init = { method: method || (json !== undefined || body !== undefined ? 'POST' : 'GET'), headers: { ...headers }, ...rest };
        init.headers['X-CSRFToken'] = csrfToken();
        init.headers.Accept = 'application/json';
        if (json !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(json);
        } else if (body !== undefined) {
            init.body = body;
        }
        let response;
        try {
            response = await fetch(url, init);
        } catch (err) {
            throw new ApiError('連線失敗，請檢查網路後再試一次！', 0, {});
        }
        let data = {};
        try { data = await response.json(); } catch (err) { /* 非 JSON 回應 */ }
        if (!response.ok || data.success === false) {
            throw new ApiError(data.message || `操作失敗 (HTTP ${response.status})`, response.status, data);
        }
        return data;
    }

    // ---------------------------------------------------------------- 頁面資料
    function pageData(id = 'page-data') {
        const el = document.getElementById(id);
        if (!el) return {};
        try { return JSON.parse(el.textContent || '{}'); } catch (err) { return {}; }
    }

    // ---------------------------------------------------------------- 事件委派
    /**
     * 以 data-action 取代 onclick：<button data-action="complete" data-id="5">
     *   Kiosk.on(document, 'click', { complete: (el, ev) => completeOrder(el.dataset.id) });
     */
    function on(root, eventName, handlers) {
        (root || document).addEventListener(eventName, (ev) => {
            const el = ev.target.closest('[data-action]');
            if (!el || !(el.dataset.action in handlers)) return;
            handlers[el.dataset.action](el, ev);
        });
    }

    // 表單送出前確認：<form data-confirm="確定刪除？"> (取代 onsubmit="return confirm(...)"，訊息不會被當成程式碼)
    document.addEventListener('submit', (ev) => {
        const form = ev.target;
        if (form instanceof HTMLFormElement && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
            ev.preventDefault();
        }
    }, true);

    // ---------------------------------------------------------------- 即時通知 (SSE)
    /**
     * 訂單有變動時呼叫 callback (建立、狀態變更、取消)。
     * EventSource 斷線會自動重連；切回分頁時也會重新整理一次，避免背景期間漏掉更新。
     */
    function onOrdersChanged(callback, { minIntervalMs = 1000 } = {}) {
        // 短時間內多個事件合併成一次重新整理；尖峰時段連續有訂單時每個畫面最多每秒重新抓一次
        // (舊做法每來一個事件就重新計時，事件不斷時會一直不更新，畫面多時也會放大伺服器負載)
        let pending = null;
        let last = 0;
        const trigger = () => {
            if (pending) return;
            pending = setTimeout(() => {
                pending = null;
                last = Date.now();
                callback();
            }, Math.max(150, last + minIntervalMs - Date.now()));
        };
        const source = new EventSource('/api/orders_stream');
        source.onmessage = trigger;
        document.addEventListener('visibilitychange', () => { if (!document.hidden) trigger(); });
        return source;
    }

    // ---------------------------------------------------------------- 時鐘 / 閒置登出
    function startClock(el) {
        if (!el) return;
        const tick = () => { el.textContent = new Date().toTimeString().split(' ')[0]; };
        tick();
        setInterval(tick, 1000);
    }

    /** Kiosk 無人操作一段時間後自動登出 (預設 120 秒)；beforeLogout 可用來清除本機暫存 (例如購物車) */
    function idleLogout({ timeoutMs = 120000, url = '/logout', next = '/', beforeLogout = null } = {}) {
        let timer = null;
        const reset = () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                try { if (beforeLogout) beforeLogout(); } catch (err) { /* 不影響登出 */ }
                setA11y(false);   // 下一位顧客回到一般字體
                // 登出只接受 POST (避免其他網站以連結或圖片讓顧客被登出)
                fetch(url, { method: 'POST', headers: { 'X-CSRFToken': csrfToken() }, credentials: 'same-origin' })
                    .catch(() => {})
                    .finally(() => { window.location.href = next; });
            }, timeoutMs);
        };
        ['click', 'touchstart', 'keydown', 'mousemove', 'scroll'].forEach((e) => document.addEventListener(e, reset, { passive: true }));
        reset();
    }

    // ---------------------------------------------------------------- 大字 / 高對比模式
    // 按鈕 data-action="toggle-a11y"；只記在這個分頁 (sessionStorage)，點餐機閒置登出時還原
    const A11Y_KEY = 'kiosk-a11y';

    function setA11y(on) {
        document.documentElement.classList.toggle('a11y-large', on);
        document.querySelectorAll('[data-action="toggle-a11y"]').forEach((b) => b.setAttribute('aria-pressed', on ? 'true' : 'false'));
        try { sessionStorage.setItem(A11Y_KEY, on ? '1' : '0'); } catch (err) { /* 瀏覽器不允許儲存時只影響本頁 */ }
    }

    try { if (sessionStorage.getItem(A11Y_KEY) === '1') document.documentElement.classList.add('a11y-large'); } catch (err) { /* 略過 */ }
    document.addEventListener('DOMContentLoaded', () => setA11y(document.documentElement.classList.contains('a11y-large')));
    document.addEventListener('click', (ev) => {
        const btn = ev.target.closest && ev.target.closest('[data-action="toggle-a11y"]');
        if (btn) setA11y(!document.documentElement.classList.contains('a11y-large'));
    });

    // ---------------------------------------------------------------- 圖片備援
    // <img data-fallback="/static/img/placeholder.svg">：檔案遺失時改用預設圖 (取代 onerror 屬性)
    function useFallback(img) {
        if (img.dataset.fallback && img.getAttribute('src') !== img.dataset.fallback) img.src = img.dataset.fallback;
    }
    document.addEventListener('error', (ev) => {
        if (ev.target instanceof HTMLImageElement) useFallback(ev.target);
    }, true);
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('img[data-fallback]').forEach((img) => {
            if (img.complete && img.naturalWidth === 0) useFallback(img);
        });
    });

    // ---------------------------------------------------------------- 斷線提示
    // 網路中斷時在畫面底部顯示提示 (點餐、結帳需要連線)；恢復後自動隱藏
    function updateOnlineBanner() {
        let banner = document.getElementById('kiosk-offline-banner');
        if (navigator.onLine) {
            if (banner) banner.remove();
            return;
        }
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'kiosk-offline-banner';
            banner.className = 'offline-banner';
            banner.setAttribute('role', 'alert');
            banner.textContent = '⚠️ 網路已中斷：恢復連線前無法送出訂單或結帳';
            document.body.appendChild(banner);
        }
    }
    window.addEventListener('online', updateOnlineBanner);
    window.addEventListener('offline', updateOnlineBanner);
    document.addEventListener('DOMContentLoaded', updateOnlineBanner);

    // ---------------------------------------------------------------- 其他
    function newOrderId() {
        if (global.crypto && crypto.randomUUID) return crypto.randomUUID();
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = Math.random() * 16 | 0;
            return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }

    function money(value) {
        return `$${Math.round(Number(value) || 0).toLocaleString('zh-TW')}`;
    }

    global.Kiosk = { html, setHtml, hideModalThen, escapeHtml, api, ApiError, csrfToken, pageData, on, onOrdersChanged, startClock, idleLogout, newOrderId, money };
})(window);
