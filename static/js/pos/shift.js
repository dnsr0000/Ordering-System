/*
 * POS 交班 / 零用金、會員儲值、禮物卡售出與查詢 (ES module；對話框在 templates/_pos_pro_modals.html)。
 */
import { $, api, html, modalOf, num, posCfg, setHtml, state } from './core.js';
import { bindMember } from './member.js';
import { askCardLast4, withApproval } from './pro.js';

// ======================================================================
// 儲值 / 禮物卡售出的刷卡欄位 (prefix：posTopup / posGift → {prefix}Method、{prefix}Auth、{prefix}Last4)
// ======================================================================
const cardInputs = (prefix) => [$(`${prefix}Auth`), $(`${prefix}Last4`)].filter(Boolean);

function syncCardFields(prefix) {
    const isCard = $(`${prefix}Method`).value === 'Credit Card';
    cardInputs(prefix).forEach((el) => el.classList.toggle('d-none', !isCard));
}

/** 視窗重開：付款方式回到現金、清空授權碼 / 末四碼，避免下一筆沿用上一筆的刷卡資料 */
function resetCardFields(prefix) {
    $(`${prefix}Method`).value = 'Cash';
    cardInputs(prefix).forEach((el) => { el.value = ''; });
    syncCardFields(prefix);
}

/** 卡號末四碼：視窗有末四碼欄位就讀欄位；沒有欄位且刷卡機設定必填時另外詢問。取消回傳 null */
function cardLast4(prefix, amount) {
    if ($(`${prefix}Method`).value !== 'Credit Card') return '';
    const input = $(`${prefix}Last4`);
    if (input) return input.value.trim();
    return posCfg.cardRequireLast4 ? askCardLast4(amount) : '';
}

// ======================================================================
// 交班 / 零用金
// ======================================================================
let shiftReport = null;
let denominations = [2000, 1000, 500, 200, 100, 50, 20, 10, 5, 1];   // 與 shift_service.DENOMINATIONS 一致；開窗時以伺服器回傳為準

function setShiftBadge(open) {
    const badge = $('posShiftBadge');
    if (!badge) return;
    badge.className = `badge ${open ? 'bg-success' : 'bg-secondary'}`;
    badge.textContent = open ? '開班中' : '未開班';
}

function renderShift() {
    const open = Boolean(shiftReport);
    setShiftBadge(open);
    $('posShiftClosed').classList.toggle('d-none', open);
    $('posShiftOpen').classList.toggle('d-none', !open);
    $('posShiftResult').classList.add('d-none');
    if (!open) return;
    const r = shiftReport;
    const money = (v) => `${num(v) < 0 ? '-' : ''}$${Math.abs(num(v)).toLocaleString()}`;
    const tiles = [['開班', `${r.opened_at} ${r.opened_by}`], ['零用金', money(r.opening_cash)], ['現金收款', money(r.cash_sales + r.cash_topups)],
        ['現金退款', money(r.cash_refunds)], ['零用金異動', money(r.cash_movements)], ['應有現金', money(r.expected_cash)],
        ['訂單數', r.order_count], ['營業額', money(r.net_sales)]];
    setHtml($('posShiftSummary'), tiles.map(([label, value]) => html`
        <div class="col-6 col-md-3"><div class="p-2 rounded bg-dark border border-secondary">
            <div class="small text-white-50">${label}</div><div class="fw-bold">${value}</div></div></div>`));
    setHtml($('posShiftDenoms'), denominations.map((d) => html`
        <div class="col-3"><div class="input-group input-group-sm"><span class="input-group-text">$${d}</span>
            <input type="number" class="form-control shift-denom" min="0" data-denom="${d}" aria-label="${d} 元張數"></div></div>`));
    updateCounted();
}

function updateCounted() {
    let total = 0;
    document.querySelectorAll('.shift-denom').forEach((el) => { total += num(el.dataset.denom) * num(el.value); });
    $('posShiftCounted').textContent = total;
    const diff = total - num(shiftReport && shiftReport.expected_cash);
    const el = $('posShiftDiff');
    el.textContent = `${diff >= 0 ? '+' : ''}${diff}`;
    el.className = diff === 0 ? 'text-success' : (diff < 0 ? 'text-danger' : 'text-warning');
}

async function openShift() {
    try {
        const res = await api('/api/pos/shift');
        shiftReport = res.shift;
        if (Array.isArray(res.denominations)) denominations = res.denominations;
        renderShift();
        modalOf('posShiftModal').show();
    } catch (err) {
        alert(`❌ ${err.message}`);
    }
}

$('posShiftOpenForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
        const res = await api('/api/pos/shift/open', { json: { opening_cash: num($('posShiftOpening').value) } });
        shiftReport = res.shift;
        renderShift();
    } catch (err) {
        alert(`❌ ${err.message}`);
    }
});

let moveBusy = false;
$('posShiftMoveForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (moveBusy) return;   // 連點 / 連按 Enter 不重複登記零用金異動
    const form = ev.currentTarget;
    const btn = form.querySelector('[type="submit"]');
    moveBusy = true;
    if (btn) btn.disabled = true;
    try {
        const res = await api('/api/pos/shift/movement', {
            json: { kind: $('posShiftMoveKind').value, amount: num($('posShiftMoveAmount').value), note: $('posShiftMoveNote').value },
        });
        shiftReport = res.shift;
        $('posShiftMoveAmount').value = '';
        $('posShiftMoveNote').value = '';
        renderShift();
    } catch (err) {
        alert(`❌ ${err.message}`);
    } finally {
        moveBusy = false;
        if (btn) btn.disabled = false;
        // 按鈕停用時焦點會掉到 body，對話框就收不到 Esc：把焦點放回表單
        if (!form.contains(document.activeElement)) $('posShiftMoveAmount').focus();
    }
});

$('posShiftCloseForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const denoms = {};
    document.querySelectorAll('.shift-denom').forEach((el) => { if (num(el.value)) denoms[el.dataset.denom] = num(el.value); });
    const counted = num($('posShiftCounted').textContent);
    if (!window.confirm(`確定關班？實點現金 $${counted}，帳差 ${$('posShiftDiff').textContent}`)) return;
    try {
        const res = await api('/api/pos/shift/close', {
            json: { denominations: denoms, counted_cash: counted, note: $('posShiftNote').value, print: $('posShiftPrint').checked },
        });
        shiftReport = null;
        setShiftBadge(false);
        $('posShiftOpen').classList.add('d-none');
        const r = res.report;
        const box = $('posShiftResult');
        box.classList.remove('d-none');
        setHtml(box, html`
            <div class="alert ${r.difference === 0 ? 'alert-success' : 'alert-warning'}">
                班別 #${r.shift_id} 已關班：應有現金 $${r.expected_cash}，實點 $${r.counted_cash}，帳差 ${r.difference >= 0 ? '+' : ''}${r.difference}
                ${res.printed ? '(已送出列印)' : ''}
            </div>
            <a class="btn btn-outline-light btn-sm" href="/admin/shifts/${res.shift_id}" target="_blank" rel="noopener">查看交班報表</a>`);
    } catch (err) {
        alert(`❌ ${err.message}`);
    }
});
$('posShiftDenoms').addEventListener('input', updateCounted);

// ======================================================================
// 會員儲值
// ======================================================================
function openTopup() {
    const member = state.member;
    if (!member) {
        alert('⚠️ 請先綁定會員');
        return;
    }
    $('posTopupName').textContent = member.name;
    $('posTopupBalance').textContent = num(member.wallet_balance);
    $('posTopupAmount').value = '';
    $('posTopupMsg').textContent = '';
    resetCardFields('posTopup');
    modalOf('posTopupModal').show();
}

// ======================================================================
// 禮物卡：售出、查詢餘額
// ======================================================================
function openGiftCards() {
    $('posGiftSellMsg').textContent = '';
    $('posGiftLookupMsg').textContent = '';
    $('posGiftAmount').value = '';
    if ($('posGiftMethod')) resetCardFields('posGift');
    modalOf('posGiftModal').show();
}

let giftRequest = null;   // { id, sig }：同樣內容重送 (逾時後再按一次) 沿用同一個 request_id 供後端去重，成功後才換新的
if ($('posGiftSellForm')) {
    $('posGiftMethod').addEventListener('change', () => syncCardFields('posGift'));
    $('posGiftSellForm').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const amount = num($('posGiftAmount').value);
        const last4 = cardLast4('posGift', amount);
        if (last4 === null) return;
        const payload = { amount, payment_method: $('posGiftMethod').value,
                          payment_info: { auth_code: $('posGiftAuth').value, card_last4: last4 }, print: $('posGiftPrint').checked };
        const sig = JSON.stringify(payload);
        if (!giftRequest || giftRequest.sig !== sig) giftRequest = { id: Kiosk.newOrderId(), sig };
        const requestId = giftRequest.id;
        const btn = $('posGiftSellBtn');
        btn.disabled = true;
        try {
            const res = await withApproval((extra) => api('/api/pos/gift_cards/sell', {
                json: { ...payload, request_id: requestId, ...extra },
            }));
            giftRequest = null;   // 同一個視窗接著賣下一張用新的 request_id
            cardInputs('posGift').forEach((el) => { el.value = ''; });
            setHtml($('posGiftSellMsg'), html`<span class="text-success">✅ 已售出 $${res.card.initial_amount}</span>
                <div class="fs-5 fw-bold text-warning mt-1" id="posGiftNewCode">${res.card.code.replace(/(\d{4})(?=\d)/g, '$1 ')}</div>
                <div class="text-white-50">${res.printed ? '已列印卡號' : '請將卡號交給顧客'}</div>`);
        } catch (err) {
            if (!err.cancelled) $('posGiftSellMsg').textContent = `❌ ${err.message}`;
        } finally {
            btn.disabled = false;
        }
    });
    $('posGiftLookupForm').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        try {
            const res = await api('/api/pos/gift_cards/lookup', { json: { code: $('posGiftLookupCode').value } });
            const c = res.card;
            $('posGiftLookupMsg').textContent = `${c.masked}：餘額 $${c.balance}${c.status === 'void' ? ' (已作廢)' : ''}`;
        } catch (err) {
            $('posGiftLookupMsg').textContent = `❌ ${err.message}`;
        }
    });
}

$('posTopupMethod').addEventListener('change', () => syncCardFields('posTopup'));

let topupBusy = false;
let topupRequest = null;   // { id, sig }：同樣內容重送 (例如逾時後再按一次) 沿用同一個 request_id 供後端去重，成功後才換新的
$('posTopupForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const member = state.member;
    if (!member || topupBusy) return;   // 連點 / 連按 Enter 不重複儲值
    const btn = ev.currentTarget.querySelector('[type="submit"]');
    const amount = num($('posTopupAmount').value);
    const last4 = cardLast4('posTopup', amount);
    if (last4 === null) return;
    const payload = { user_id: member.id, amount, payment_method: $('posTopupMethod').value,
                      payment_info: { auth_code: $('posTopupAuth').value, card_last4: last4 } };
    const sig = JSON.stringify(payload);
    if (!topupRequest || topupRequest.sig !== sig) topupRequest = { id: Kiosk.newOrderId(), sig };
    topupBusy = true;
    if (btn) btn.disabled = true;
    try {
        const res = await api('/api/pos/wallet/topup', { json: { ...payload, request_id: topupRequest.id } });
        topupRequest = null;
        // 成功後清空金額與刷卡資料：視窗仍開著時再按一次不會用同樣金額重複加值
        $('posTopupAmount').value = '';
        cardInputs('posTopup').forEach((el) => { el.value = ''; });
        $('posTopupMsg').textContent = `✅ ${res.message}`;
        $('posTopupBalance').textContent = res.member.wallet_balance;
        bindMember(res.member);
    } catch (err) {
        $('posTopupMsg').textContent = `❌ ${err.message}`;
    } finally {
        topupBusy = false;
        if (btn) btn.disabled = false;
    }
});

Kiosk.on(document, 'click', {
    openGiftCards: () => openGiftCards(),
    giftPreset: (el) => { $('posGiftAmount').value = el.dataset.value; },
    openShift: () => openShift(),
    openTopup: () => openTopup(),
    topupPreset: (el) => { $('posTopupAmount').value = el.dataset.value; },
});
