/*
 * POS 交班 / 零用金、會員儲值、禮物卡售出與查詢 (ES module；對話框在 templates/_pos_pro_modals.html)。
 */
import { $, api, html, modalOf, num, setHtml, state } from './core.js';
import { bindMember } from './member.js';
import { withApproval } from './pro.js';

// ======================================================================
// 交班 / 零用金
// ======================================================================
let shiftReport = null;
let denominations = [1000, 500, 200, 100, 50, 10, 5, 1];

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

$('posShiftMoveForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
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
    modalOf('posTopupModal').show();
}

// ======================================================================
// 禮物卡：售出、查詢餘額
// ======================================================================
function openGiftCards() {
    $('posGiftSellMsg').textContent = '';
    $('posGiftLookupMsg').textContent = '';
    $('posGiftAmount').value = '';
    modalOf('posGiftModal').show();
}

if ($('posGiftSellForm')) {
    $('posGiftMethod').addEventListener('change', () => {
        $('posGiftAuth').classList.toggle('d-none', $('posGiftMethod').value !== 'Credit Card');
    });
    $('posGiftSellForm').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const btn = $('posGiftSellBtn');
        btn.disabled = true;
        try {
            const res = await withApproval((extra) => api('/api/pos/gift_cards/sell', {
                json: { amount: num($('posGiftAmount').value), payment_method: $('posGiftMethod').value,
                        payment_info: { auth_code: $('posGiftAuth').value }, print: $('posGiftPrint').checked, ...extra },
            }));
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

$('posTopupMethod').addEventListener('change', () => {
    $('posTopupAuth').classList.toggle('d-none', $('posTopupMethod').value !== 'Credit Card');
});

$('posTopupForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const member = state.member;
    if (!member) return;
    try {
        const res = await api('/api/pos/wallet/topup', {
            json: { user_id: member.id, amount: num($('posTopupAmount').value), payment_method: $('posTopupMethod').value,
                    payment_info: { auth_code: $('posTopupAuth').value } },
        });
        $('posTopupMsg').textContent = `✅ ${res.message}`;
        $('posTopupBalance').textContent = res.member.wallet_balance;
        bindMember(res.member);
    } catch (err) {
        $('posTopupMsg').textContent = `❌ ${err.message}`;
    }
});

Kiosk.on(document, 'click', {
    openGiftCards: () => openGiftCards(),
    giftPreset: (el) => { $('posGiftAmount').value = el.dataset.value; },
    openShift: () => openShift(),
    openTopup: () => openTopup(),
    topupPreset: (el) => { $('posTopupAmount').value = el.dataset.value; },
});
