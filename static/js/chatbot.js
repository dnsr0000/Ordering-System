/*
 * 顧客端 AI 客服對話窗 (點餐頁、出餐進度頁)：右下角浮動按鈕 → 對話面板。
 * 問題送到 /api/chat；回答附來源：retrieval / faq (店家資料) 直接顯示，llm 標示「AI 回答，請以櫃檯為準」。
 * 設定由 <div id="chatbot" data-...> 帶入；對話只保留在這個頁面 (不存伺服器)。
 */
(function () {
    'use strict';

    const root = document.getElementById('chatbot');
    if (!root) return;
    const { html } = Kiosk;
    const cfg = root.dataset;
    const history = [];
    let open = false;
    let busy = false;

    Kiosk.setHtml(root, html`
        <button type="button" class="chatbot-fab" data-action="chat-toggle" aria-expanded="false" aria-controls="chatbotPanel" title="${cfg.title}">
            <i class="bi bi-chat-dots-fill"></i><span class="chatbot-fab-label">${cfg.title}</span>
        </button>
        <section class="chatbot-panel d-none" id="chatbotPanel" role="dialog" aria-label="${cfg.title}">
            <header class="chatbot-head">
                <div><i class="bi bi-robot me-1"></i><strong>${cfg.botName}</strong><div class="chatbot-sub">${cfg.subtitle}</div></div>
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

    function bubble(role, text, source) {
        const note = role === 'bot' && source === 'llm' ? html`<div class="chatbot-note"><i class="bi bi-stars me-1"></i>${cfg.aiNote}</div>` : '';
        const el = document.createElement('div');
        el.className = `chatbot-msg chatbot-${role}`;
        Kiosk.setHtml(el, html`<div class="chatbot-text">${text}</div>${note}`);
        messages.appendChild(el);
        messages.scrollTop = messages.scrollHeight;
        return el;
    }

    function renderSuggestions(list) {
        Kiosk.setHtml(suggestBox, (list || []).map((q) => html`<button type="button" class="chatbot-chip" data-action="chat-ask">${q}</button>`));
    }

    async function ask(text) {
        text = (text || '').trim();
        if (!text || busy) return;
        busy = true;
        input.value = '';
        bubble('user', text);
        history.push({ role: 'user', text });
        const typing = bubble('bot', '…');
        try {
            const res = await Kiosk.api('/api/chat', { json: { message: text, history: history.slice(-6), order_uuid: cfg.orderUuid || '' } });
            typing.remove();
            bubble('bot', res.answer, res.source);
            history.push({ role: 'bot', text: res.answer });
            renderSuggestions(res.suggestions);
        } catch (err) {
            typing.remove();
            bubble('bot', err.message || cfg.failed);
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
            input.focus();
        }
    }

    Kiosk.on(document, 'click', {
        'chat-toggle': () => toggle(),
        'chat-ask': (btn) => ask(btn.textContent),
    });
    document.getElementById('chatbotForm').addEventListener('submit', (ev) => { ev.preventDefault(); ask(input.value); });
})();
