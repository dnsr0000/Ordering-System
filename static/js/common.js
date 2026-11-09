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
            throw new ApiError(t('net_failed', '連線失敗，請檢查網路後再試一次！'), 0, {});
        }
        let data = {};
        try { data = await response.json(); } catch (err) { /* 非 JSON 回應 */ }
        if (!response.ok || data.success === false) {
            // 後端訊息維持中文；顧客頁為英 / 日文時伺服器另附 message_i18n (app/services/i18n_pages.py)
            throw new ApiError(data.message_i18n || data.message || t('api_failed', '操作失敗') + ` (HTTP ${response.status})`,
                               response.status, data);
        }
        return data;
    }

    // ---------------------------------------------------------------- 顧客頁多語系
    let uiStrings = null;
    /** 介面文字：非中文的顧客頁由 base.html 嵌入 #ui-data；沒有時 (中文 / 員工頁) 回傳內建的中文 fallback */
    function t(key, fallback) {
        if (uiStrings === null) uiStrings = pageData('ui-data');
        const v = uiStrings[key];
        return v === undefined || v === null ? fallback : v;
    }

    // ---------------------------------------------------------------- 頁面資料
    function pageData(id = 'page-data') {
        const el = document.getElementById(id);
        if (!el) return {};
        try { return JSON.parse(el.textContent || '{}'); } catch (err) { return {}; }
    }

    // ---------------------------------------------------------------- 事件委派
    /**
     * 頁面的 ES module (customer.js / pos.js 與它們匯入的模組) 載入完成前，顧客就按了 data-action 按鈕時
     * (手機網路慢、剛開頁面就點)：先記下來，等 Kiosk.on 綁定該動作後補跑一次，點擊不會不見。
     * 綁定前的點擊只保留幾秒；綁定後走一般流程。
     */
    const earlyClicks = [];
    let clickBound = false;
    document.addEventListener('click', (ev) => {
        if (clickBound) return;
        const el = ev.target instanceof Element ? ev.target.closest('[data-action]') : null;
        if (el && earlyClicks.length < 20) earlyClicks.push({ el, ev });
    }, true);

    /**
     * 以 data-action 取代 onclick：<button data-action="complete" data-id="5">
     *   Kiosk.on(document, 'click', { complete: (el, ev) => completeOrder(el.dataset.id) });
     */
    function on(root, eventName, handlers) {
        const target = root || document;
        target.addEventListener(eventName, (ev) => {
            const el = ev.target.closest('[data-action]');
            if (!el || !(el.dataset.action in handlers)) return;
            handlers[el.dataset.action](el, ev);
        });
        if (eventName !== 'click') return;
        if (!clickBound) {
            clickBound = true;
            setTimeout(() => { earlyClicks.length = 0; }, 5000);
        }
        // 綁定前被按下的按鈕：等呼叫端的初始化 (載入購物車等) 做完再補跑
        const mine = earlyClicks.filter(({ el }) => el.dataset.action in handlers && target.contains(el));
        if (!mine.length) return;
        mine.forEach((entry) => earlyClicks.splice(earlyClicks.indexOf(entry), 1));
        setTimeout(() => mine.forEach(({ el, ev }) => { if (document.contains(el)) handlers[el.dataset.action](el, ev); }), 0);
    }

    // 表單送出前確認：<form data-confirm="確定刪除？"> (取代 onsubmit="return confirm(...)"，訊息不會被當成程式碼)
    //   <button data-confirm> 只在按這顆按鈕送出時確認 (優先於 form 上的 data-confirm)
    document.addEventListener('submit', (ev) => {
        const form = ev.target;
        if (!(form instanceof HTMLFormElement)) return;
        const msg = (ev.submitter && ev.submitter.dataset && ev.submitter.dataset.confirm) || form.dataset.confirm;
        if (msg && !window.confirm(msg)) ev.preventDefault();
    }, true);

    // ---------------------------------------------------------------- 即時更新的統一做法
    // 員工畫面與店內看板 (後台、POS、KDS、取餐、叫號、數位看板…)：訂單資料一律用 onOrdersChanged (SSE)，
    //   斷線期間自動改為輪詢 (預設 SSE_FALLBACK_MS)；不另外寫定時器重抓訂單。
    // 「依時間變化」、SSE 不會通知的資料 (供應時段、AI 建議、暫停接單自動恢復…) 與顧客頁 (我的訂單、出餐進度、候位、
    //   售完狀態)：一律用 poll()。顧客頁不用 SSE：每條 SSE 連線占用伺服器一條執行緒 (公開連線每個伺服器行程所有店家合計上限 25 條，超過時優先淘汰連線最多的那家店)，
    //   且 SSE 是整店的廣播，不適合給個別顧客。
    const SSE_FALLBACK_MS = 5000;
    const SSE_HIDDEN_CLOSE_MS = 15000;

    /**
     * 訂單有變動時呼叫 callback (建立、狀態變更、取消)。
     * EventSource 斷線會自動重連；切回分頁時也會重新整理一次，避免背景期間漏掉更新。
     * 分頁在背景一段時間後關閉 SSE、改為每 fallbackMs 輪詢，切回時重新連線 (回傳物件只提供 close / addEventListener)。
     * fallbackMs：斷線期間 (或瀏覽器不支援 EventSource、伺服器拒絕連線而不再重連時) 每隔這麼久輪詢一次，連線恢復後停止；
     *   預設 SSE_FALLBACK_MS，重新整理代價高的頁面 (例如整頁 reload) 可傳較長的值。
     * onStatus(ok)：連線 / 斷線時回呼 (畫面上的狀態點)。
     */
    function onOrdersChanged(callback, { minIntervalMs = 1000, fallbackMs = SSE_FALLBACK_MS, onStatus = null } = {}) {
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
        let fallbackTimer = null;
        const startFallback = () => { if (fallbackMs && !fallbackTimer) fallbackTimer = setInterval(callback, fallbackMs); };
        const stopFallback = () => { clearInterval(fallbackTimer); fallbackTimer = null; };
        if (!global.EventSource) {
            document.addEventListener('visibilitychange', () => { if (!document.hidden) trigger(); });
            if (onStatus) onStatus(false);
            startFallback();
            return { close: stopFallback, addEventListener() {} };
        }
        // 分頁在背景超過 SSE_HIDDEN_CLOSE_MS 就關閉 SSE、改為輪詢，切回時重新連線：門市主機是 HTTP/1.1，
        // 瀏覽器對同一主機最多 6 條連線 (所有分頁共用)，每個看板分頁常駐一條 SSE 會占滿連線，其他請求都會卡住
        let source = null;
        let hideTimer = null;
        let closed = false;
        const extra = [];
        const connect = () => {
            if (source || closed) return;
            source = new EventSource('/api/orders_stream');
            source.onmessage = trigger;
            source.addEventListener('open', () => { stopFallback(); if (onStatus) onStatus(true); });
            source.addEventListener('error', () => { startFallback(); if (onStatus) onStatus(false); });
            extra.forEach(([type, fn]) => source.addEventListener(type, fn));
        };
        const sleep = () => { if (source) { source.close(); source = null; } startFallback(); };
        const onVisibility = () => {
            clearTimeout(hideTimer);
            if (closed) return;
            if (document.hidden) {
                hideTimer = setTimeout(sleep, SSE_HIDDEN_CLOSE_MS);
                return;
            }
            connect();   // 連線成功 (open) 時停止背景期間的輪詢
            trigger();
        };
        document.addEventListener('visibilitychange', onVisibility);
        connect();
        if (document.hidden) hideTimer = setTimeout(sleep, SSE_HIDDEN_CLOSE_MS);
        return {
            close() {
                closed = true;
                clearTimeout(hideTimer);
                stopFallback();
                if (source) { source.close(); source = null; }
            },
            addEventListener(type, fn) {
                extra.push([type, fn]);
                if (source) source.addEventListener(type, fn);
            },
        };
    }

    /**
     * 定時輪詢 (顧客頁、與 SSE 不會通知的「依時間變化」資料)。回傳 { stop() }。
     * - 上一次還沒完成時不重疊執行；callback 的錯誤不會中斷輪詢 (由 callback 自行處理顯示)
     * - 頁面在背景時暫停、切回來立即補一次；keepInBackground: true 則背景照常 (出餐進度、候位：等著被通知)
     * - immediate: true 時開始就先執行一次
     */
    function poll(callback, intervalMs, { immediate = false, keepInBackground = false } = {}) {
        let running = false;
        let stopped = false;
        const run = async () => {
            if (stopped || running || (!keepInBackground && document.hidden)) return;
            running = true;
            try { await callback(); } catch (err) { /* 下一輪再試 */ } finally { running = false; }
        };
        const timer = setInterval(run, intervalMs);
        const onVisible = () => { if (!document.hidden) run(); };
        if (!keepInBackground) document.addEventListener('visibilitychange', onVisible);
        if (immediate) run();
        return {
            stop() {
                stopped = true;
                clearInterval(timer);
                document.removeEventListener('visibilitychange', onVisible);
            },
        };
    }

    /** 員工畫面的資料請求回 401 (登入逾時 / 被登出)：重新載入一次讓伺服器導到登入頁，畫面不會停在舊資料。回傳是否已處理 */
    let authLost = false;
    function handleAuthLost(err) {
        if (!err || err.status !== 401) return false;
        if (!authLost) {
            authLost = true;
            global.location.reload();
        }
        return true;
    }

    // ---------------------------------------------------------------- 時鐘 / 閒置登出
    function startClock(el) {
        if (!el) return;
        const tick = () => { el.textContent = new Date().toTimeString().split(' ')[0]; };
        tick();
        setInterval(tick, 1000);
    }
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-clock]').forEach(startClock);   // 後台頂欄時鐘
    });

    /**
     * Kiosk 無人操作一段時間後自動登出 (預設 120 秒)；beforeLogout 可用來清除本機暫存 (例如購物車)。
     * 登出請求沒有成功 (斷線、伺服器 5xx) 時不跳頁：顯示鎖定畫面並持續重試，成功後才回首頁
     * (顧客登入狀態存在 cookie 的 session，伺服器沒收到登出就不會清掉，跳回首頁會直接進到上一位會員的點餐頁)。
     */
    function idleLogout({ timeoutMs = 120000, url = '/logout', next = '/', beforeLogout = null, retryMs = 5000 } = {}) {
        let timer = null;
        let loggingOut = false;
        let lock = null;
        const showLock = () => {
            if (lock) return;
            lock = document.createElement('div');
            lock.id = 'kiosk-logout-lock';
            lock.className = 'device-lock';
            lock.setAttribute('role', 'alert');
            const text = document.createElement('div');
            text.className = 'device-lock-text';
            text.textContent = t('logout_retrying', '正在登出上一位顧客，網路恢復後會自動完成，請稍候');
            lock.appendChild(text);
            document.body.appendChild(lock);
        };
        const attempt = () => {
            const timeout = global.AbortSignal && AbortSignal.timeout ? { signal: AbortSignal.timeout(15000) } : {};
            // 登出只接受 POST (避免其他網站以連結或圖片讓顧客被登出)
            fetch(url, { method: 'POST', headers: { 'X-CSRFToken': csrfToken() }, credentials: 'same-origin', ...timeout })
                // 4xx (例如 CSRF 無效：頁面所屬的 session 已不存在) 重試也不會成功，視同完成；5xx / 429 / 連不上才重試
                .then((resp) => resp.status < 500 && resp.status !== 429, () => false)
                .then((done) => {
                    if (done) {
                        window.location.href = next;
                        return;
                    }
                    showLock();
                    setTimeout(attempt, retryMs);
                });
        };
        const reset = () => {
            if (loggingOut) return;
            clearTimeout(timer);
            timer = setTimeout(() => {
                loggingOut = true;
                try { if (beforeLogout) beforeLogout(); } catch (err) { /* 不影響登出 */ }
                setA11y(false);   // 下一位顧客回到一般字體
                attempt();
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

    // ---------------------------------------------------------------- 淺色 / 深色 (顧客頁，)
    // 按鈕 data-action="toggle-theme"；記在 localStorage (顧客自己的手機會記住)；<html data-bs-theme="light|dark">
    const THEME_KEY = 'kiosk-theme';

    function setTheme(mode) {
        document.documentElement.setAttribute('data-bs-theme', mode);
        document.querySelectorAll('[data-action="toggle-theme"]').forEach((b) => {
            b.setAttribute('aria-pressed', mode === 'light' ? 'true' : 'false');
            const icon = b.querySelector('i');
            if (icon) icon.className = mode === 'light' ? 'bi bi-moon-stars' : 'bi bi-sun';
        });
        try { localStorage.setItem(THEME_KEY, mode); } catch (err) { /* 隱私模式 */ }
    }

    try {
        const saved = localStorage.getItem(THEME_KEY);
        if ((saved === 'light' || saved === 'dark') && document.querySelector('[data-action="toggle-theme"], [data-theme-switch]')) {
            document.documentElement.setAttribute('data-bs-theme', saved);
        }
    } catch (err) { /* 略過 */ }
    document.addEventListener('DOMContentLoaded', () => {
        if (document.querySelector('[data-action="toggle-theme"]')) setTheme(document.documentElement.getAttribute('data-bs-theme') === 'light' ? 'light' : 'dark');
    });
    document.addEventListener('click', (ev) => {
        const btn = ev.target.closest && ev.target.closest('[data-action="toggle-theme"]');
        if (btn) setTheme(document.documentElement.getAttribute('data-bs-theme') === 'light' ? 'dark' : 'light');
    });

    // ---------------------------------------------------------------- PWA 安裝提示
    // 瀏覽器允許安裝時顯示 #pwaInstallBtn (顧客頁)；按下後呼叫瀏覽器的安裝對話框
    let installEvent = null;
    window.addEventListener('beforeinstallprompt', (ev) => {
        ev.preventDefault();
        installEvent = ev;
        document.querySelectorAll('[data-action="pwa-install"]').forEach((b) => b.classList.remove('d-none'));
    });
    window.addEventListener('appinstalled', () => {
        installEvent = null;
        document.querySelectorAll('[data-action="pwa-install"]').forEach((b) => b.classList.add('d-none'));
    });
    document.addEventListener('click', async (ev) => {
        const btn = ev.target.closest && ev.target.closest('[data-action="pwa-install"]');
        if (!btn || !installEvent) return;
        installEvent.prompt();
        try { await installEvent.userChoice; } catch (err) { /* 使用者取消 */ }
        installEvent = null;
        btn.classList.add('d-none');
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
    // 網路中斷時在畫面底部顯示提示 (點餐、結帳需要連線；門市點餐機的現金單會先暫存，見 kiosk-offline.js)；恢復後自動隱藏。
    // 提示條的高度記在 --offline-banner-h，讓左下角的「暫存單」按鈕移到提示條上方 (theme.css)
    function updateOnlineBanner() {
        let banner = document.getElementById('kiosk-offline-banner');
        if (navigator.onLine) {
            if (banner) banner.remove();
            document.documentElement.style.removeProperty('--offline-banner-h');
            return;
        }
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'kiosk-offline-banner';
            banner.className = 'offline-banner';
            banner.setAttribute('role', 'alert');
            banner.textContent = global.KioskOffline
                ? t('offline_banner_queue', '⚠️ 網路已中斷：現金訂單會先暫存，恢復連線後自動送出')
                : t('offline_banner', '⚠️ 網路已中斷：恢復連線前無法送出訂單或結帳');
            document.body.appendChild(banner);
        }
        document.documentElement.style.setProperty('--offline-banner-h', `${banner.offsetHeight}px`);
    }
    window.addEventListener('online', updateOnlineBanner);
    window.addEventListener('offline', updateOnlineBanner);
    window.addEventListener('resize', () => { if (!navigator.onLine) updateOnlineBanner(); });
    document.addEventListener('DOMContentLoaded', updateOnlineBanner);

    // ---------------------------------------------------------------- DOM 小工具
    const $ = (id) => document.getElementById(id);
    const modal = (id) => global.bootstrap.Modal.getOrCreateInstance(typeof id === 'string' ? $(id) : id);

    function setText(id, value) {
        const el = typeof id === 'string' ? $(id) : id;
        if (el) el.textContent = value;
    }

    /** 金額列：值 > 0 才顯示該列，並填入四捨五入後的數字 */
    function toggleRow(rowId, valueId, value) {
        const row = $(rowId);
        if (!row) return;
        row.classList.toggle('d-none', !(value > 0));
        setText(valueId, Math.round(value));
    }

    // ---------------------------------------------------------------- localStorage (隱私模式或空間不足時不會拋錯)
    // 顧客購物車依店家分開 (同一網域以 /shop/<slug> 切換店家時不會帶到別家的品項)；預設店家沿用舊鍵 (升級前的購物車不受影響)
    const shopMeta = document.querySelector('meta[name="shop"]');
    const shopSlug = shopMeta ? shopMeta.getAttribute('content') || '' : '';
    const CART_KEY = shopSlug && shopSlug !== 'default' ? `kiosk_cart_${shopSlug}` : 'kiosk_cart';
    const store = {
        get(key, fallback = null) {
            try {
                const raw = localStorage.getItem(key);
                return raw === null ? fallback : JSON.parse(raw);
            } catch (err) { return fallback; }
        },
        set(key, value) {
            try { localStorage.setItem(key, JSON.stringify(value)); } catch (err) { /* 略過 */ }
        },
        remove(key) {
            try { localStorage.removeItem(key); } catch (err) { /* 略過 */ }
        },
    };

    // ---------------------------------------------------------------- 訂單顯示
    const pad3 = (v) => String(v).padStart(3, '0');
    /** 取餐號：數字 → #001；傳訂單物件時線上訂單尚未被分店編號 (pickup_pending) 顯示「待分店確認」 */
    function pickupNo(order) {
        if (order && typeof order === 'object') {
            if (order.pickup_pending) return t('pickup_pending', '待分店確認');
            return `#${pad3(order.display_pickup_number ?? order.pickup_number ?? order.id)}`;
        }
        return `#${pad3(order)}`;
    }

    /** 訂單狀態：Bootstrap 色系、圖示、中文 (顧客頁以 ui 字串覆寫文字) */
    const ORDER_STATUS = {
        Pending: { cls: 'bg-warning text-dark', border: 'border-warning', icon: '⏳', text: '製作中' },
        Completed: { cls: 'bg-success', border: 'border-success', icon: '✅', text: '已出餐' },
        PickedUp: { cls: 'bg-secondary', border: 'border-secondary', icon: '📦', text: '已取餐' },
        Cancelled: { cls: 'bg-danger', border: 'border-danger', icon: '❌', text: '已取消' },
    };
    /** 付款狀態 (線上付款與桌邊結帳)：refund_required = 訂單取消後才收到付款，請到綠界廠商後台退款 */
    const PAYMENT_STATUS = {
        pending: { cls: 'bg-warning text-dark', text: '待付款' },
        paid: { cls: 'bg-success', text: '已線上付款' },
        expired: { cls: 'bg-secondary', text: '付款逾時' },
        refund_required: { cls: 'bg-danger', text: '需退款' },
        refunded: { cls: 'bg-secondary', text: '已退款' },
        unpaid: { cls: 'bg-warning text-dark', text: '未結帳' },
        settled: { cls: 'bg-success', text: '已結帳' },
    };
    function statusBadge(status, text) {
        const st = ORDER_STATUS[status] || { cls: 'bg-info text-dark', icon: '', text: status };
        return html`<span class="badge ${st.cls}">${st.icon ? `${st.icon} ` : ''}${text || st.text}</span>`;
    }
    function paymentBadge(status, text) {
        const st = PAYMENT_STATUS[status];
        return st ? html`<span class="badge order-payment ${st.cls}">${text || st.text}</span>` : '';
    }

    /** 庫存徽章的色系：售完紅、≤ LOW_STOCK 黃，其餘依 style (success 後台 / dark POS 卡片)；門檻由後端 menu_service.LOW_STOCK 經 <meta> 提供 */
    const lowStockMeta = document.querySelector('meta[name="low-stock"]');
    const LOW_STOCK = Number(lowStockMeta && lowStockMeta.getAttribute('content')) || 5;
    function stockClass(stock, soldOut, style = 'success') {
        if (soldOut || Number(stock) <= 0) return 'bg-danger';
        if (Number(stock) <= LOW_STOCK) return 'bg-warning text-dark';
        return style === 'dark' ? 'bg-dark border border-secondary text-white-50' : 'bg-success';
    }

    /**
     * 訂單的餐具與備註區塊 (後台 / 廚房訂單卡共用)：備註不分單型都顯示 (內用、外送平台單也有整單備註)；
     * 餐具只有外帶與外送到府 (顧客有選) 才顯示，外送平台單沒有提供餐具需求
     */
    function takeoutBlock(o, noteClass = '') {
        const cutlery = o.order_type === '外帶' || o.order_type === '外送到府';
        if (!cutlery && !o.note) return '';
        return html`
        <div class="mb-2">
            ${cutlery ? html`<span class="badge ${o.need_cutlery ? 'bg-success' : 'bg-secondary'}">${o.need_cutlery ? '要餐具' : '不用餐具'}</span>` : ''}
            ${o.note ? html`<div class="small text-warning mt-1 ${noteClass}">備註：${o.note}</div>` : ''}
        </div>`;
    }

    /** 員工端變更訂單狀態 (後台 / 廚房 / 取餐 / POS 共用的 API) */
    function setOrderStatus(orderId, status, extra = {}) {
        return api(`/api/orders/${encodeURIComponent(orderId)}/status`, { json: { status, ...extra } });
    }

    /** 以 If-None-Match 輪詢 JSON：內容未變 (304) 回傳 null；state 物件保存 etag */
    async function pollJson(url, state = {}) {
        const headers = state.etag ? { 'If-None-Match': state.etag } : {};
        const resp = await fetch(url, { headers, cache: 'no-store' });
        if (resp.status === 304) return null;
        if (!resp.ok) throw new ApiError(`HTTP ${resp.status}`, resp.status, {});
        state.etag = resp.headers.get('ETag') || '';
        return resp.json();
    }

    /** 短提示音 (桌邊呼叫、叫號)；瀏覽器不允許自動播放時略過 */
    function beep(frequency = 880, durationMs = 250) {
        try {
            const ctx = new (global.AudioContext || global.webkitAudioContext)();
            const osc = ctx.createOscillator();
            osc.frequency.value = frequency;
            osc.connect(ctx.destination);
            osc.start();
            setTimeout(() => { osc.stop(); ctx.close(); }, durationMs);
        } catch (err) { /* 略過 */ }
    }

    // ---------------------------------------------------------------- 其他
    function newOrderId() {
        if (global.crypto && crypto.randomUUID) return crypto.randomUUID();
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = Math.random() * 16 | 0;
            return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }

    /** 金額：$1,234；負數顯示為 -$1,234 */
    function money(value) {
        const n = Math.round(Number(value) || 0);
        return `${n < 0 ? '-' : ''}$${Math.abs(n).toLocaleString('zh-TW')}`;
    }

    global.Kiosk = {
        html, setHtml, hideModalThen, escapeHtml, api, ApiError, csrfToken, pageData, t, on, onOrdersChanged, poll, handleAuthLost,
        startClock, idleLogout,
        newOrderId, money, $, modal, setText, toggleRow, store, CART_KEY, pad3, pickupNo, ORDER_STATUS, PAYMENT_STATUS, statusBadge,
        paymentBadge, LOW_STOCK, stockClass, takeoutBlock, setOrderStatus, pollJson, beep,
    };
})(window);
