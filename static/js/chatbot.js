/*
 * 顧客端客服對話窗 (點餐頁、出餐進度頁)：右下角浮動按鈕 → 對話面板。
 * - AI 模式：問題送到 /api/chat；回答附來源，llm 標示「AI 回答，請以櫃檯為準」；
 *   答不出來 / 與本店無關時附「你是否要問」與「轉接真人客服」。
 * - 真人模式：按「轉接真人客服」→ /api/chat/handoff 建立對話 (附上與 AI 的對話)，之後訊息送給店員，
 *   以 Kiosk.poll 取得店員回覆 (顧客頁不用 SSE)；對話憑證存在 sessionStorage，重新整理後可接續。
 * 設定由 <div id="chatbot" data-...> 帶入。
 */
(function () {
    'use strict';

    const root = document.getElementById('chatbot');
    if (!root) return;
    const { html, t } = Kiosk;
    const cfg = root.dataset;
    const HANDOFF_KEY = `chatHandoff:${cfg.orderUuid || window.location.pathname}`;
    const POLL_MS = 4000;
    const history = [];
    let open = false;
    let busy = false;
    const human = { token: '', lastId: 0, poller: null };

    Kiosk.setHtml(root, html`
        <button type="button" class="chatbot-fab" data-action="chat-toggle" aria-expanded="false" aria-controls="chatbotPanel" title="${cfg.title}">
            <i class="bi bi-chat-dots-fill"></i><span class="chatbot-fab-label">${cfg.title}</span>
        </button>
        <section class="chatbot-panel d-none" id="chatbotPanel" role="dialog" aria-label="${cfg.title}">
            <header class="chatbot-head">
                <div class="chatbot-head-title">
                    <i class="bi bi-robot me-1" id="chatbotIcon"></i><strong id="chatbotName">${cfg.botName}</strong>
                    <div class="chatbot-sub" id="chatbotSub">${cfg.subtitle}</div>
                </div>
                ${cfg.handoff ? html`
                <button type="button" class="btn btn-sm btn-outline-light chatbot-head-btn" data-action="chat-handoff" id="chatbotHandoffBtn">
                    <i class="bi bi-person-headset me-1"></i>${t('chat_handoff', '轉接真人客服')}</button>
                <button type="button" class="btn btn-sm btn-outline-warning chatbot-head-btn d-none" data-action="chat-handoff-end" id="chatbotEndBtn">
                    ${t('chat_handoff_end', '結束')}</button>` : ''}
                <button type="button" class="btn-close btn-close-white" data-action="chat-toggle" aria-label="Close"></button>
            </header>
            <div class="chatbot-messages" id="chatbotMessages" aria-live="polite"></div>
            <div class="chatbot-suggest" id="chatbotSuggest"></div>
            <form class="chatbot-form" id="chatbotForm" autocomplete="off">
                <input type="text" class="form-control" id="chatbotInput" maxlength="300" placeholder="${cfg.placeholder}" aria-label="${cfg.placeholder}">
                <button type="submit" class="btn btn-warning fw-bold text-dark" id="chatbotSend" aria-label="${cfg.send}"><i class="bi bi-send-fill"></i></button>
            </form>
        </section>`);

    const panel = document.getElementById('chatbotPanel');
    const messages = document.getElementById('chatbotMessages');
    const suggestBox = document.getElementById('chatbotSuggest');
    const input = document.getElementById('chatbotInput');

    function scrollDown() { messages.scrollTop = messages.scrollHeight; }

    /** role: user / bot / staff / notice；extras：AI 標示、「你是否要問」、轉接按鈕 */
    function bubble(role, text, { source = '', didYouMean = [], handoff = false, who = '' } = {}) {
        const el = document.createElement('div');
        el.className = `chatbot-msg chatbot-${role}`;
        const note = role === 'bot' && source === 'llm'
            ? html`<div class="chatbot-note"><i class="bi bi-stars me-1"></i>${cfg.aiNote}</div>` : '';
        const dym = didYouMean.length ? html`
            <div class="chatbot-dym"><div class="chatbot-dym-title">${t('chat_did_you_mean', '你是否要問：')}</div>
                ${didYouMean.map((q) => html`<button type="button" class="chatbot-chip" data-action="chat-ask">${q}</button>`)}</div>` : '';
        const transfer = handoff && cfg.handoff && !human.token ? html`
            <button type="button" class="chatbot-chip chatbot-chip-handoff" data-action="chat-handoff">
                <i class="bi bi-person-headset me-1"></i>${t('chat_handoff', '轉接真人客服')}</button>` : '';
        Kiosk.setHtml(el, html`${who ? html`<div class="chatbot-who">${who}</div>` : ''}<div class="chatbot-text">${text}</div>${note}${dym}${transfer}`);
        messages.appendChild(el);
        scrollDown();
        return el;
    }

    function renderSuggestions(list) {
        Kiosk.setHtml(suggestBox, (list || []).map((q) => html`<button type="button" class="chatbot-chip" data-action="chat-ask">${q}</button>`));
    }

    // ---------------------------------------------------------------- AI 模式
    async function askBot(text) {
        bubble('user', text);
        history.push({ role: 'user', text });
        const typing = bubble('bot', '…');
        try {
            const res = await Kiosk.api('/api/chat', { json: { message: text, history: history.slice(-6), order_uuid: cfg.orderUuid || '' } });
            typing.remove();
            bubble('bot', res.answer, { source: res.source, didYouMean: res.did_you_mean || [], handoff: res.handoff });
            history.push({ role: 'bot', text: res.answer });
            renderSuggestions(res.suggestions);
        } catch (err) {
            typing.remove();
            bubble('bot', err.message || cfg.failed, { handoff: true });
        }
    }

    // ---------------------------------------------------------------- 真人模式
    function saveToken(token) {
        try {
            if (token) sessionStorage.setItem(HANDOFF_KEY, token);
            else sessionStorage.removeItem(HANDOFF_KEY);
        } catch (err) { /* 無痕模式等不能存取時略過 (重新整理後無法接續) */ }
    }

    function setHumanMode(on) {
        const btn = document.getElementById('chatbotHandoffBtn');
        const end = document.getElementById('chatbotEndBtn');
        if (btn) btn.classList.toggle('d-none', on);
        if (end) end.classList.toggle('d-none', !on);
        document.getElementById('chatbotIcon').className = `bi ${on ? 'bi-person-headset' : 'bi-robot'} me-1`;
        document.getElementById('chatbotName').textContent = on ? t('chat_handoff_mode', '真人客服對話中') : cfg.botName;
        suggestBox.classList.toggle('d-none', on);
        // 已在真人客服對話中：舊回答裡的「轉接真人客服」按鈕先藏起來
        messages.querySelectorAll('.chatbot-chip-handoff').forEach((el) => el.classList.toggle('d-none', on));
    }

    /** 店員的新回覆 (顧客自己的訊息送出時已顯示)；includeOwn：重新整理後接續時把整段對話補回來 */
    function showStaffMessages(list, includeOwn = false) {
        (list || []).forEach((m) => {
            human.lastId = Math.max(human.lastId, m.id);
            if (m.sender === 'staff') bubble('staff', m.text, { who: t('chat_staff', '客服人員') });
            else if (includeOwn && m.sender === 'customer') bubble('user', m.text);
            else if (includeOwn && m.sender === 'bot') bubble('bot', m.text);
        });
    }

    function endHuman(notice) {
        if (human.poller) human.poller.stop();
        human.poller = null;
        human.token = '';
        human.lastId = 0;
        saveToken('');
        setHumanMode(false);
        if (notice) bubble('notice', t('chat_handoff_closed', '真人客服已結束，有其他問題可以繼續問我。'));
    }

    async function pollHuman(includeOwn = false) {
        if (!human.token) return;
        try {
            const res = await Kiosk.api(`/api/chat/handoff/${encodeURIComponent(human.token)}?after=${human.lastId}`);
            showStaffMessages(res.messages, includeOwn);
            if (res.status === 'closed') endHuman(true);
        } catch (err) {
            if (err.status === 404) endHuman(false);   // 對話不存在 (資料被清除) 或店家關閉了真人客服
        }
    }

    function startPolling() {
        if (human.poller) human.poller.stop();
        human.poller = Kiosk.poll(() => pollHuman(), POLL_MS);
    }

    async function startHandoff() {
        if (human.token || busy) return;
        busy = true;
        try {
            const res = await Kiosk.api('/api/chat/handoff', { json: { history: history.slice(-12), order_uuid: cfg.orderUuid || '' } });
            human.token = res.token;
            (res.messages || []).forEach((m) => { human.lastId = Math.max(human.lastId, m.id); });   // 與 AI 的對話已在畫面上
            saveToken(res.token);
            setHumanMode(true);
            bubble('notice', t('chat_handoff_started', '已為您轉接真人客服，店員回覆後會顯示在這裡，請稍候。'));
            startPolling();
        } catch (err) {
            bubble('bot', err.message || cfg.failed);
        } finally {
            busy = false;
            input.focus();
        }
    }

    async function sendHuman(text) {
        bubble('user', text);
        try {
            // 不在這裡移動 lastId：店員可能剛好在這之前回覆 (id 較小)，留給下一次輪詢取得
            await Kiosk.api(`/api/chat/handoff/${encodeURIComponent(human.token)}/message`, { json: { text } });
        } catch (err) {
            bubble('bot', err.message || cfg.failed);
            if (err.status === 409 || err.status === 404) endHuman(false);
        }
    }

    async function endHandoff() {
        const token = human.token;
        endHuman(true);
        try { await Kiosk.api(`/api/chat/handoff/${encodeURIComponent(token)}/close`, { method: 'POST' }); } catch (err) { /* 已結束 */ }
    }

    async function resumeHandoff() {
        let token = '';
        try { token = sessionStorage.getItem(HANDOFF_KEY) || ''; } catch (err) { /* 略過 */ }
        if (!token || !cfg.handoff) return;
        human.token = token;
        setHumanMode(true);
        await pollHuman(true);   // 重新整理前的對話與離開期間店員的回覆
        if (human.token) startPolling();
    }

    // ---------------------------------------------------------------- 共用
    async function ask(text) {
        text = (text || '').trim();
        if (!text || busy) return;
        busy = true;
        input.value = '';
        try {
            if (human.token) await sendHuman(text);
            else await askBot(text);
        } finally {
            busy = false;
            input.focus();
        }
    }

    function toggle() {
        open = !open;
        panel.classList.toggle('d-none', !open);
        root.querySelector('.chatbot-fab').setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) {
            if (!messages.children.length) {
                bubble('bot', cfg.welcome);
                renderSuggestions((cfg.suggestions || '').split('|').filter(Boolean));
            }
            scrollDown();
            input.focus();
        }
    }

    Kiosk.on(document, 'click', {
        'chat-toggle': () => toggle(),
        'chat-ask': (btn) => ask(btn.textContent),
        'chat-handoff': () => startHandoff(),
        'chat-handoff-end': () => endHandoff(),
    });
    document.getElementById('chatbotForm').addEventListener('submit', (ev) => { ev.preventDefault(); ask(input.value); });
    resumeHandoff();
})();
