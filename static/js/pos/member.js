/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：5. 會員查詢 / 候選 / PIN、寄杯兌換與售出。
 * 由 static/js/pos.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
import { $, api, html, itemMap, modalOf, num, pageData, posCfg, setHtml, state } from './core.js';
import { startConfigure } from './menu.js';
import { renderCart, schedulePromotionPreview } from './cart.js';
import { populateCouponSelect, recalcTotals, resetPromo, updateCashChange } from './totals.js';
import { syncLiveState } from './live.js';

// ======================================================================
// 5. 會員查詢 / 多筆候選 / PIN 設定
// ======================================================================
export async function lookupMember() {
    const q = ($('posMemberQuery').value || '').trim();
    if (!q) return;
    try {
        const res = await api('/api/pos/member_lookup', { json: { query: q } });
        if (res.multiple && Array.isArray(res.candidates)) {
            setHtml($('posMemberSelectModalBody'), res.candidates.map((c) => html`
                <div class="p-2 rounded bg-dark border border-secondary mb-2 d-flex justify-content-between align-items-center pos-member-candidate">
                    <div>
                        <div class="fw-bold text-white">${c.name} <span class="text-warning ms-1">${c.phone}</span></div>
                        <small class="text-white-50">可用紅利：${num(c.points)} 點 • 可用券：${(c.coupons || []).length} 張
                            ${c.has_pin ? '' : html`• <span class="text-danger">尚未設PIN</span>`}</small>
                    </div>
                    <button type="button" class="btn btn-sm btn-warning fw-bold text-dark" data-action="selectMember" data-id="${c.id}">
                        選擇此會員
                    </button>
                </div>`));
            modalOf('posMemberSelectModal').show();
            return;
        }
        bindMember(res.member);
    } catch (err) {
        alert(err.message);
    }
}

export async function selectMember(id) {
    try {
        const res = await api('/api/pos/member_lookup', { json: { member_id: Number(id) } });
        modalOf('posMemberSelectModal').hide();
        bindMember(res.member);
    } catch (err) {
        alert(err.message);
    }
}

export function bindMember(member) {
    if (!member) return;
    state.member = { ...member, points: num(member.points) };
    const badge = $('posMemberStatusBadge');
    badge.className = 'badge bg-success pos-xxs';
    badge.textContent = '已綁定會員';
    $('posMemberSearchBox').classList.add('d-none');
    const box = $('posMemberBoundBox');
    box.classList.remove('d-none');
    box.classList.add('d-flex');
    $('posMemberName').textContent = member.name;
    $('posMemberPhone').textContent = `(${member.phone})`;
    const tierBadge = $('posMemberTier');
    if (tierBadge) {
        tierBadge.classList.toggle('d-none', !member.tier);
        tierBadge.textContent = member.tier ? `${member.tier}${num(member.tier_discount) ? ` ${member.tier_discount}%off` : ''}` : '';
    }
    if ($('posMemberWallet')) $('posMemberWallet').textContent = `儲值 $${num(member.wallet_balance)}`;
    const stampBadge = $('posMemberStamps');
    if (stampBadge) {
        const st = member.stamps || {};
        stampBadge.classList.toggle('d-none', !st.enabled);
        stampBadge.textContent = st.enabled ? `${st.title} ${st.count}/${st.size}` : '';
    }
    schedulePromotionPreview();
    const ptsRow = $('posPointsRedeemRow');
    if (ptsRow) {
        ptsRow.classList.remove('d-none');
        ptsRow.classList.add('d-flex');
    }
    populateCouponSelect();
    renderMemberPasses();
    recalcTotals();
}

/** 綁定會員的寄杯：每按一次兌換一份 (不超過剩餘次數) */
export function renderMemberPasses() {
    const box = $('posMemberPasses');
    const passes = (state.member && state.member.passes) || [];
    box.classList.toggle('d-none', !passes.length);
    setHtml(box, passes.map((p) => html`<span class="badge bg-dark border border-info me-1 mb-1 pos-pass">
        ☕ ${p.name} 剩 ${num(p.remaining)}
        <button type="button" class="btn btn-sm btn-info py-0 px-1 ms-1 pos-xs" data-action="usePass" data-pass-id="${p.id}">兌換</button></span>`));
}

export function usePass(passId) {
    const p = ((state.member && state.member.passes) || []).find((x) => Number(x.id) === Number(passId));
    const item = p ? itemMap.get(Number(p.menu_item_id)) : null;
    if (!p || !item) return;
    const used = state.cart.filter((l) => Number(l.pass_id) === Number(p.id)).reduce((s, l) => s + num(l.quantity), 0);
    if (used >= num(p.remaining)) {
        alert(`⚠️ 寄杯「${p.name}」剩 ${num(p.remaining)} 次，已全部加入`);
        return;
    }
    startConfigure({ item, combo: null, mode: 'pass', passId: p.id });
}

export function openPassSell() {
    if (!state.member) return;
    const products = pageData.passProducts || [];
    $('posPassMember').textContent = state.member.name;
    setHtml($('posPassProduct'), products.map((p) => html`<option value="${p.id}">${p.name} (${p.uses} 次) $${p.price}</option>`));
    const methods = [...$('posPaymentMethod').options].map((o) => o.value)
        .filter((m) => !['混合付款', '儲值金', '禮物卡'].includes(m) && !(m === 'Line Pay' && posCfg.linePayIntegrated));
    setHtml($('posPassMethod'), methods.map((m) => html`<option value="${m}">${m}</option>`));
    $('posPassCash').value = '';
    $('posPassAuth').value = '';
    $('posPassMsg').textContent = '';
    syncPassMethod();
    modalOf('posPassSellModal').show();
}

export function syncPassMethod() {
    const m = $('posPassMethod').value;
    $('posPassCash').classList.toggle('d-none', m !== 'Cash');
    $('posPassAuth').classList.toggle('d-none', m !== 'Credit Card');
}

export async function sellPass(ev) {
    ev.preventDefault();
    if (!state.member) return;
    $('posPassSellBtn').disabled = true;
    try {
        const res = await api('/api/pos/passes/sell', { json: {
            user_id: state.member.id, product_id: num($('posPassProduct').value), payment_method: $('posPassMethod').value,
            cash_received: num($('posPassCash').value), payment_info: { auth_code: $('posPassAuth').value.trim() } } });
        modalOf('posPassSellModal').hide();
        alert(`✅ ${res.message}${res.cash_change ? `，找零 $${res.cash_change}` : ''}`);
        await refreshBoundMember(false);
        syncLiveState();
    } catch (err) {
        $('posPassMsg').textContent = `❌ ${err.message}`;
    } finally {
        $('posPassSellBtn').disabled = false;
    }
}

/** 伺服器回報金額與畫面不同 (剛改價等)：以伺服器的單價更新購物車；仍有差異時記住伺服器金額，收銀確認後再送出 */
export function applyServerPrices(data, cartKey) {
    const prices = data.line_prices || [];
    if (prices.length === state.cart.length) {
        state.cart.forEach((l, i) => {
            if (l.override_price === undefined) l.price = num(prices[i]);
        });
    }
    renderCart({ revalidate: false });
    state.acceptedTotal = state.finalTotal === num(data.server_total) ? null : { key: cartKey, total: num(data.server_total) };
    if (state.acceptedTotal) $('posFinalTotal').textContent = state.acceptedTotal.total;
    updateCashChange();
}

/** 會員的券或點數剛在其他通路用掉：重新讀取會員 (點數、可用券)，清掉失效的優惠券，金額重算後由收銀再確認 */
export async function refreshBoundMember(dropPromo) {
    if (!state.member) return;
    if (dropPromo) resetPromo();
    try {
        const res = await api('/api/pos/member_lookup', { json: { member_id: Number(state.member.id) } });
        if (res.member) bindMember(res.member);
    } catch (e) {
        recalcTotals();
    }
}

export function clearMember() {
    state.member = null;
    state.cart = state.cart.filter((l) => !l.is_reward && !l.pass_id);   // 紅利兌換品、寄杯兌換必須綁定會員
    $('posMemberPasses').classList.add('d-none');
    $('posMemberQuery').value = '';
    const badge = $('posMemberStatusBadge');
    badge.className = 'badge bg-secondary pos-xxs';
    badge.textContent = '未綁定 (一般訪客)';
    $('posMemberSearchBox').classList.remove('d-none');
    const box = $('posMemberBoundBox');
    box.classList.add('d-none');
    box.classList.remove('d-flex');
    const ptsRow = $('posPointsRedeemRow');
    if (ptsRow) {
        ptsRow.classList.add('d-none');
        ptsRow.classList.remove('d-flex');
    }
    const toggle = $('posUsePointsToggle');
    if (toggle) toggle.checked = false;
    const ptsInput = $('posPointsInput');
    if (ptsInput) {
        ptsInput.value = 0;
        ptsInput.disabled = true;
    }
    populateCouponSelect();
    renderCart();
}

export function openPinModal() {
    if (!state.member) return;
    $('posPinMemberLabel').textContent = `${state.member.name} (${state.member.phone})`;
    $('posPinInput').value = '';
    $('posPinMsg').textContent = '';
    modalOf('posPinModal').show();
}

export async function submitPin(ev) {
    ev.preventDefault();
    if (!state.member) return;
    const pin = ($('posPinInput').value || '').trim();
    const msg = $('posPinMsg');
    if (!/^\d{4,6}$/.test(pin)) {
        msg.textContent = '⚠️ PIN 碼格式不正確，請輸入 4 至 6 位純數字！';
        return;
    }
    const btn = $('posPinSubmitBtn');
    btn.disabled = true;
    try {
        const res = await api('/api/pos/set_member_pin', { json: { user_id: state.member.id, pin } });
        state.member.has_pin = true;
        modalOf('posPinModal').hide();
        alert(res.message || '設定成功');
    } catch (err) {
        msg.textContent = err.message;
    } finally {
        btn.disabled = false;
    }
}
