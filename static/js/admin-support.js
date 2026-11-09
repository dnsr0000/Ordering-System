/*
 * 後台「客服對話」(templates/admin_support.html)：左側清單、右側對話與回覆。
 * 清單與對話內容以 Kiosk.poll 定時更新 (客服訊息不經 SSE)；有新的待回覆對話時發出提示音。
 */
(function () {
    'use strict';
    const { html, $ } = Kiosk;
    const LIST_MS = 5000;
    const CHAT_MS = 3000;
    const SENDER_CLASS = { customer: 'support-customer', bot: 'support-bot', staff: 'support-staff', system: 'support-system' };
    // 轉接前與 AI 的對話是顧客的瀏覽器帶過來的紀錄 (可能被竄改)，只供參考，不代表店家實際的承諾
    const SENDER_LABEL = { customer: '顧客', bot: 'AI 客服 (轉接前紀錄，由顧客端帶入，僅供參考)', staff: '' };
    const STATUS_CLASS = { waiting: 'bg-danger', open: 'bg-success', closed: 'bg-secondary' };

    let currentId = 0;
    let lastId = 0;
    let generation = 0;   // 每次 openChat 遞增；之前送出的讀取回應一律丟棄
    let knownWaiting = null;
    const listEl = $('supportList');
    const box = $('supportMessages');
    const input = $('supportInput');

    function renderList(chats, waiting) {
        const badge = $('supportWaiting');
        badge.textContent = waiting ? `${waiting} 則待回覆` : '';
        badge.classList.toggle('d-none', !waiting);
        if (knownWaiting !== null && waiting > knownWaiting) Kiosk.beep(988, 300);
        knownWaiting = waiting;
        Kiosk.setHtml(listEl, chats.length ? chats.map((c) => html`
            <button type="button" class="support-item ${c.id === currentId ? 'active' : ''}" data-action="support-open" data-id="${c.id}">
                <div class="d-flex justify-content-between align-items-center gap-2">
                    <strong class="text-truncate">${c.customer}</strong>
                    <span class="d-flex gap-1 align-items-center">
                        ${c.unread ? html`<span class="badge bg-danger">${c.unread}</span>` : ''}
                        <span class="badge ${STATUS_CLASS[c.status] || 'bg-secondary'}">${c.status_label}</span>
                    </span>
                </div>
                <div class="support-item-last text-white-50">${c.last_at} ${c.last_text}</div>
            </button>`) : html`<div class="text-white-50 small p-2">目前沒有客服對話</div>`);
    }

    async function loadList() {
        try {
            const res = await Kiosk.api(`/api/admin/support/chats${$('supportShowClosed').checked ? '?closed=1' : ''}`);
            renderList(res.chats || [], res.waiting || 0);
        } catch (err) {
            Kiosk.handleAuthLost(err);
        }
    }

    function appendMessages(list) {
        const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        (list || []).forEach((m) => {
            if (m.id <= lastId) return;
            lastId = m.id;
            const el = document.createElement('div');
            el.className = `support-msg ${SENDER_CLASS[m.sender] || 'support-system'}`;
            const who = m.sender === 'staff' ? m.staff_name : (SENDER_LABEL[m.sender] || '');
            Kiosk.setHtml(el, m.sender === 'system' ? html`${m.time} ${m.text}` : html`
                <div class="support-text">${m.text}</div><div class="support-meta">${who} ${m.time}</div>`);
            box.appendChild(el);
        });
        if (nearBottom || list.length) box.scrollTop = box.scrollHeight;
    }

    function renderHeader(chat) {
        $('supportCustomer').textContent = chat.customer;
        const status = $('supportStatus');
        status.textContent = chat.status_label;
        status.className = `badge ms-1 ${STATUS_CLASS[chat.status] || 'bg-secondary'}`;
        $('supportMeta').textContent = `${chat.page === 'track' ? '出餐進度頁' : '點餐頁'}轉接 · ${chat.created_at}${chat.order_id ? ` · 訂單 #${chat.order_id}` : ''}`;
        const closed = chat.status === 'closed';
        $('supportCloseBtn').classList.toggle('d-none', closed);
        input.disabled = closed;
        $('supportSend').disabled = closed;
        input.placeholder = closed ? '此對話已結束' : '輸入回覆，Enter 送出 (Shift+Enter 換行)';
    }

    async function loadChat() {
        if (!currentId) return;
        const id = currentId;
        const gen = generation;
        const after = lastId;
        try {
            const res = await Kiosk.api(`/api/admin/support/chats/${id}?after=${after}`);
            // 讀取期間已切換 / 重新點開對話，或其他讀取已先推進 lastId：丟棄，避免跳過較舊的訊息
            if (id !== currentId || gen !== generation || after !== lastId) return;
            renderHeader(res.chat);
            appendMessages(res.messages);
        } catch (err) {
            Kiosk.handleAuthLost(err);
        }
    }

    function openChat(id) {
        currentId = Number(id);
        lastId = 0;
        generation += 1;
        box.textContent = '';
        $('supportEmpty').classList.add('d-none');
        $('supportConversation').classList.remove('d-none');
        listEl.querySelectorAll('.support-item').forEach((el) => el.classList.toggle('active', Number(el.dataset.id) === currentId));
        loadChat().then(() => { loadList(); input.focus(); });
    }

    async function sendReply() {
        const text = input.value.trim();
        if (!text || !currentId) return;
        $('supportSend').disabled = true;
        try {
            await Kiosk.api(`/api/admin/support/chats/${currentId}/reply`, { json: { text } });
            input.value = '';
            // 依序向伺服器取回 (含剛送出的回覆與顧客可能同時傳來的訊息)，不跳過 id 較小的訊息
            await loadChat();
            loadList();
        } catch (err) {
            window.alert(`❌ ${err.message}`);
        } finally {
            $('supportSend').disabled = false;
            input.focus();
        }
    }

    async function closeChat() {
        if (!currentId || !window.confirm('確定結束這個客服對話？顧客會看到「真人客服已結束」。')) return;
        try {
            const res = await Kiosk.api(`/api/admin/support/chats/${currentId}/close`, { method: 'POST' });
            renderHeader(res.chat);
            loadChat();
            loadList();
        } catch (err) {
            window.alert(`❌ ${err.message}`);
        }
    }

    Kiosk.on(document, 'click', {
        'support-open': (el) => openChat(el.dataset.id),
        'support-close': () => closeChat(),
    });
    $('supportForm').addEventListener('submit', (ev) => { ev.preventDefault(); sendReply(); });
    input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
            ev.preventDefault();
            sendReply();
        }
    });
    $('supportShowClosed').addEventListener('change', loadList);

    Kiosk.poll(loadList, LIST_MS, { immediate: true });
    Kiosk.poll(loadChat, CHAT_MS);
})();
