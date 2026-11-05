/*
 * 語音點餐 (customer.js 的模組)：瀏覽器語音辨識 (Web Speech API) 或直接打字 → /api/voice/parse 解析成品項 →
 * 顧客在視窗裡確認數量後才加入購物車 (價格由 makeLine 依菜單計算，與手動點選完全相同)。
 * 沒有語音辨識 (Firefox、非 HTTPS) 時仍可用文字輸入。
 */
import { $, html, menuItems, modal, pageData, ui } from './core.js';
import { addLine, makeLine } from './cart.js';

const LANGS = { zh: 'zh-TW', en: 'en-US', ja: 'ja-JP' };
let recognition = null;
let parsed = [];
let heardFinal = '';

const t = (key, fallback) => ui[key] || fallback;
const status = (text, show = true) => {
    const el = $('voiceStatus');
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('d-none', !show);
};

function reset() {
    parsed = [];
    heardFinal = '';
    $('voiceText').value = '';
    ['voiceResult', 'voiceEmpty', 'voiceHeard', 'voiceAddBtn', 'voiceSourceNote', 'voiceUnmatched'].forEach((id) => $(id).classList.add('d-none'));
    status('', false);
}

function open() {
    reset();
    modal('voiceModal').show();
    setTimeout(() => $('voiceText').focus(), 300);
}

// ---------------------------------------------------------------- 語音辨識
function stop() {
    if (recognition) {
        try { recognition.stop(); } catch (err) { /* 已停止 */ }
    }
    recognition = null;
    $('voiceStartBtn').classList.remove('d-none');
    $('voiceStopBtn').classList.add('d-none');
}

function start() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) { status(t('voice_unsupported', '此瀏覽器不支援語音辨識，請直接輸入文字')); $('voiceText').focus(); return; }
    if (!window.isSecureContext) { status(t('voice_unsupported', '此瀏覽器不支援語音辨識，請直接輸入文字')); return; }
    stop();
    recognition = new Recognition();
    recognition.lang = LANGS[pageData.lang] || 'zh-TW';
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    heardFinal = '';
    recognition.onresult = (ev) => {
        let interim = '';
        for (let i = ev.resultIndex; i < ev.results.length; i += 1) {
            const text = ev.results[i][0].transcript;
            if (ev.results[i].isFinal) heardFinal += text;
            else interim += text;
        }
        $('voiceText').value = heardFinal || interim;
    };
    recognition.onerror = (ev) => {
        status(ev.error === 'not-allowed' ? t('voice_denied', '未允許使用麥克風，請直接輸入文字') : `${ev.error}`);
        stop();
    };
    recognition.onend = () => {
        stop();
        if ($('voiceText').value.trim()) parse();
        else status('', false);
    };
    $('voiceStartBtn').classList.add('d-none');
    $('voiceStopBtn').classList.remove('d-none');
    status(t('voice_listening', '聆聽中… 請說話'));
    try { recognition.start(); } catch (err) { status(String(err.message || err)); stop(); }
}

// ---------------------------------------------------------------- 解析與確認
function attrText(line) {
    const parts = [];
    if (line.size) parts.push(line.size);
    if (line.ice || line.sugar) parts.push([line.ice, line.sugar].filter(Boolean).join('/'));
    if (line.spicy) parts.push(line.spicy);
    if (line.modifier_names && line.modifier_names.length) parts.push(line.modifier_names.join('、'));
    return parts.join(' • ');
}

function render() {
    const list = $('voiceLines');
    const usable = parsed.filter((l) => l.available && l.quantity > 0);
    $('voiceResult').classList.toggle('d-none', parsed.length === 0);
    $('voiceEmpty').classList.toggle('d-none', parsed.length > 0);
    $('voiceAddBtn').classList.toggle('d-none', usable.length === 0);
    Kiosk.setHtml(list, parsed.map((line, idx) => html`
        <li class="list-group-item bg-dark text-white border-secondary d-flex justify-content-between align-items-center gap-2 ${line.available ? '' : 'opacity-50'}" data-voice-idx="${idx}">
            <div class="min-w-0">
                <div class="fw-bold">${line.name}${line.available ? '' : html` <span class="badge bg-secondary">${t('voice_sold_out', '目前無法供應')}</span>`}</div>
                ${attrText(line) ? html`<div class="small text-white-50">${attrText(line)}</div>` : ''}
            </div>
            <div class="d-flex align-items-center gap-1 flex-shrink-0">
                <button type="button" class="btn btn-sm btn-outline-light" data-action="voice-qty" data-idx="${idx}" data-delta="-1" aria-label="-">−</button>
                <span class="px-2 fw-bold" aria-live="polite">${line.quantity}</span>
                <button type="button" class="btn btn-sm btn-outline-light" data-action="voice-qty" data-idx="${idx}" data-delta="1" aria-label="+" ${line.available ? '' : 'disabled'}>+</button>
            </div>
        </li>`));
}

async function parse() {
    const text = $('voiceText').value.trim();
    if (!text) { $('voiceText').focus(); return; }
    status(t('voice_parsing', '解析中…'));
    $('voiceParseBtn').disabled = true;
    try {
        const res = await Kiosk.api('/api/voice/parse', { json: { text } });
        parsed = (res.lines || []).map((l) => ({ ...l, quantity: Math.max(1, Number(l.quantity) || 1) }));
        $('voiceHeard').textContent = `${t('voice_heard', '聽到：')}${res.text || text}`;
        $('voiceHeard').classList.remove('d-none');
        $('voiceSourceNote').classList.toggle('d-none', !(res.source === 'gemini' || res.source === 'local'));
        const unmatched = res.unmatched || [];
        $('voiceUnmatched').textContent = unmatched.length ? `${t('voice_unmatched', '沒聽懂：')}${unmatched.join('、')}` : '';
        $('voiceUnmatched').classList.toggle('d-none', unmatched.length === 0);
        render();
        status('', false);
    } catch (err) {
        status(err.message);
    } finally {
        $('voiceParseBtn').disabled = false;
    }
}

function changeQty(idx, delta) {
    const line = parsed[idx];
    if (!line) return;
    line.quantity = Math.max(0, Math.min(20, line.quantity + delta));
    if (line.quantity === 0) parsed.splice(idx, 1);
    render();
}

function addAll() {
    let added = 0;
    parsed.forEach((line) => {
        const item = menuItems.get(Number(line.menu_item_id));
        if (!item || !line.available || line.quantity <= 0) return;
        const texts = [];
        if (item.modifiers === 'ice_sugar') texts.push(`${line.ice || '正常冰'}/${line.sugar || '全糖'}`);
        if (item.modifiers === 'spicy' && line.spicy) texts.push(line.spicy);
        const size = line.size || ((item.sizes || [])[0] ? item.sizes[0].name : '');
        for (let i = 0; i < line.quantity; i += 1) {
            addLine(makeLine(item, { customization: texts.join('、'), modifierIds: line.modifier_ids || [], size }));
            added += 1;
        }
    });
    modal('voiceModal').hide();
    if (added) {
        const toast = document.createElement('div');
        toast.className = 'alert alert-success position-fixed bottom-0 start-50 translate-middle-x mb-4 shadow voice-toast';
        toast.setAttribute('role', 'status');
        toast.textContent = t('voice_added', '已加入 {n} 項').replace('{n}', String(added));
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 2500);
    }
}

export const voiceActions = {
    'voice-open': () => open(),
    'voice-start': () => start(),
    'voice-stop': () => stop(),
    'voice-parse': () => parse(),
    'voice-add': () => addAll(),
    'voice-qty': (btn) => changeQty(Number(btn.dataset.idx), Number(btn.dataset.delta)),
};

document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.target && ev.target.id === 'voiceText') { ev.preventDefault(); parse(); }
});
