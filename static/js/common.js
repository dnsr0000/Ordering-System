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
    function onOrdersChanged(callback) {
        let pending = null;
        const trigger = () => {           // 短時間內多個事件合併成一次重新整理
            clearTimeout(pending);
            pending = setTimeout(callback, 150);
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
                // 登出只接受 POST (避免其他網站以連結或圖片讓顧客被登出)
                fetch(url, { method: 'POST', headers: { 'X-CSRFToken': csrfToken() }, credentials: 'same-origin' })
                    .catch(() => {})
                    .finally(() => { window.location.href = next; });
            }, timeoutMs);
        };
        ['click', 'touchstart', 'keydown', 'mousemove', 'scroll'].forEach((e) => document.addEventListener(e, reset, { passive: true }));
        reset();
    }

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

    global.Kiosk = { html, setHtml, escapeHtml, api, ApiError, csrfToken, pageData, on, onOrdersChanged, startClock, idleLogout, newOrderId, money };
})(window);
