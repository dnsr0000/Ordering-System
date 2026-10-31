/*
 * POS 進階功能：主管核准、LINE Pay 掃碼、交班 / 零用金、會員儲值、作廢 / 部分退款、
 * 桌位分開結帳 / 換桌、加急、熱感機補印。對話框在 templates/_pos_pro_modals.html。
 * 與 pos.js 共用的狀態透過 window.PosApp 取得 (pos.js 於載入完成後設定)。
 */
(function () {
    'use strict';

    const { html, setHtml, api } = Kiosk;
    const $ = (id) => document.getElementById(id);
    const modalOf = (id) => bootstrap.Modal.getOrCreateInstance($(id));
    const num = (v) => Number(v) || 0;
    const app = () => window.PosApp || { state: { liveOrders: [], unpaidTables: [] }, syncLiveState() {} };
    const cfg = (Kiosk.pageData().pos) || {};

    // ======================================================================
    // 主管核准：fn(extra) 回傳 Promise；遇到 need_approval 時跳出視窗，主管輸入帳密後重試
    // ======================================================================
    let approvalResolver = null;
    let approvalOpen = false;

    function askApproval(message, retry) {
        return new Promise((resolve) => {
            approvalResolver = resolve;
            $('posApprovalSubmit').disabled = false;
            $('posApproverPass').value = '';
            if (retry && approvalOpen) {
                // 帳密錯誤：視窗保持開啟，直接顯示錯誤讓主管重新輸入
                $('posApprovalMsg').textContent = `❌ ${message}`;
                $('posApproverPass').focus();
                return;
            }
            $('posApprovalReason').textContent = message || '此操作需要主管核准';
            $('posApprovalMsg').textContent = '';
            approvalOpen = true;
            modalOf('posApprovalModal').show();
        });
    }

    function closeApproval() {
        if (!approvalOpen) return;
        approvalOpen = false;
        modalOf('posApprovalModal').hide();
    }

    $('posApprovalForm').addEventListener('submit', (ev) => {
        ev.preventDefault();
        const resolve = approvalResolver;
        approvalResolver = null;
        $('posApprovalSubmit').disabled = true;
        $('posApprovalMsg').textContent = '驗證中...';
        if (resolve) resolve({ approver_username: $('posApproverUser').value.trim(), approver_password: $('posApproverPass').value });
    });
    $('posApprovalModal').addEventListener('hidden.bs.modal', () => {
        approvalOpen = false;
        if (approvalResolver) {
            const resolve = approvalResolver;
            approvalResolver = null;
            resolve(null);
        }
    });
    $('posApprovalModal').addEventListener('shown.bs.modal', () => $('posApproverUser').focus());

    async function withApproval(fn) {
        let extra = {};
        try {
            for (let attempt = 0; attempt < 4; attempt++) {
                try {
                    return await fn(extra);
                } catch (err) {
                    if (!(err.status === 403 && err.data && err.data.need_approval)) throw err;
                    const creds = await askApproval(err.message, attempt > 0);
                    if (!creds) {
                        err.cancelled = true;
                        throw err;
                    }
                    extra = creds;
                }
            }
            throw new Error('主管核准失敗次數過多');
        } finally {
            closeApproval();
        }
    }

    // ======================================================================
    // 會員身分驗證：動用儲值金 / 點數 / 寄杯時後端回 need_member_pin → 會員輸入 PIN (或改由主管核准) 後重試
    // ======================================================================
    let memberVerifyResolver = null;
    let memberVerifyOpen = false;

    function askMemberPin(err, retry) {
        return new Promise((resolve) => {
            memberVerifyResolver = resolve;
            const data = err.data || {};
            $('posMemberVerifySubmit').disabled = false;
            $('posMemberVerifyPin').value = '';
            $('posMemberVerifyPin').disabled = data.has_pin === false;
            if (retry && memberVerifyOpen) {
                $('posMemberVerifyMsg').textContent = `❌ ${err.message}`;
                $('posMemberVerifyPin').focus();
                return;
            }
            $('posMemberVerifyReason').textContent = err.message || '動用儲值金 / 點數 / 寄杯需驗證會員身分';
            $('posMemberVerifyName').textContent = data.member_name || '';
            $('posMemberVerifyMsg').textContent = data.has_pin === false ? '此會員尚未設定 PIN：請先協助設定，或由主管核准' : '';
            memberVerifyOpen = true;
            modalOf('posMemberVerifyModal').show();
        });
    }

    function closeMemberVerify() {
        if (!memberVerifyOpen) return;
        memberVerifyOpen = false;
        modalOf('posMemberVerifyModal').hide();
    }

    /** fn(extra) 回傳 Promise；遇到 need_member_pin 時跳出視窗，會員輸入 PIN (member_pin) 或主管核准 (member_verify_override) 後重試 */
    async function withMemberVerify(fn) {
        let extra = {};
        try {
            for (let attempt = 0; attempt < 4; attempt++) {
                try {
                    return await fn(extra);
                } catch (err) {
                    if (!(err.data && err.data.need_member_pin)) throw err;
                    const answer = await askMemberPin(err, attempt > 0);
                    if (!answer) {
                        err.cancelled = true;
                        throw err;
                    }
                    extra = { ...extra, ...answer };
                }
            }
            throw new Error('會員驗證失敗次數過多');
        } finally {
            closeMemberVerify();
        }
    }

    $('posMemberVerifyForm').addEventListener('submit', (ev) => {
        ev.preventDefault();
        const pin = ($('posMemberVerifyPin').value || '').trim();
        if (!/^\d{4,6}$/.test(pin)) {
            $('posMemberVerifyMsg').textContent = '⚠️ PIN 碼為 4 至 6 位數字';
            return;
        }
        const resolve = memberVerifyResolver;
        memberVerifyResolver = null;
        $('posMemberVerifySubmit').disabled = true;
        $('posMemberVerifyMsg').textContent = '驗證中...';
        if (resolve) resolve({ member_pin: pin });
    });
    $('posMemberVerifyOverride').addEventListener('click', () => {
        const resolve = memberVerifyResolver;
        memberVerifyResolver = null;
        closeMemberVerify();
        if (resolve) resolve({ member_verify_override: true });
    });
    $('posMemberVerifyModal').addEventListener('hidden.bs.modal', () => {
        memberVerifyOpen = false;
        if (memberVerifyResolver) {
            const resolve = memberVerifyResolver;
            memberVerifyResolver = null;
            resolve(null);
        }
    });
    $('posMemberVerifyModal').addEventListener('shown.bs.modal', () => $('posMemberVerifyPin').focus());

    // ======================================================================
    // LINE Pay 掃碼 / 刷卡授權碼
    // ======================================================================
    let linePayResolver = null;

    function askLinePayKey(amount) {
        return new Promise((resolve) => {
            linePayResolver = resolve;
            $('posLinePayAmount').textContent = num(amount);
            $('posLinePayKey').value = '';
            modalOf('posLinePayModal').show();
        });
    }

    $('posLinePayForm').addEventListener('submit', (ev) => {
        ev.preventDefault();
        const key = $('posLinePayKey').value.replace(/\s+/g, '');
        if (!/^\d{12,20}$/.test(key)) {
            alert('⚠️ 付款碼格式不正確，請重新掃描');
            $('posLinePayKey').select();
            return;
        }
        const resolve = linePayResolver;
        linePayResolver = null;
        modalOf('posLinePayModal').hide();
        if (resolve) resolve(key);
    });
    $('posLinePayModal').addEventListener('hidden.bs.modal', () => {
        if (linePayResolver) {
            const resolve = linePayResolver;
            linePayResolver = null;
            resolve(null);
        }
    });
    $('posLinePayModal').addEventListener('shown.bs.modal', () => $('posLinePayKey').focus());

    /** 桌位結帳等場合的付款細節：刷卡授權碼、LINE Pay 付款碼；取消時回傳 null */
    async function paymentDetails(method, amount) {
        if (method === 'Credit Card') {
            const auth = window.prompt(`刷卡金額 $${num(amount)}：請輸入刷卡機簽單上的授權碼${cfg.cardRequireAuth ? '' : ' (可留空)'}`, '');
            if (auth === null) return null;
            if (cfg.cardRequireAuth && !auth.trim()) {
                alert('⚠️ 必須輸入授權碼');
                return null;
            }
            return { auth_code: auth.trim() };
        }
        if (method === 'Line Pay' && cfg.linePayIntegrated) {
            const key = await askLinePayKey(amount);
            return key ? { one_time_key: key } : null;
        }
        return {};
    }

    async function networkPrint(orderId) {
        if (!orderId) return;
        try {
            const res = await api(`/api/pos/orders/${orderId}/reprint`, { method: 'POST' });
            alert(`🖨️ ${res.message}`);
        } catch (err) {
            alert(`❌ ${err.message}`);
        }
    }

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
        const member = app().state.member;
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
        const member = app().state.member;
        if (!member) return;
        try {
            const res = await api('/api/pos/wallet/topup', {
                json: { user_id: member.id, amount: num($('posTopupAmount').value), payment_method: $('posTopupMethod').value,
                        payment_info: { auth_code: $('posTopupAuth').value } },
            });
            $('posTopupMsg').textContent = `✅ ${res.message}`;
            $('posTopupBalance').textContent = res.member.wallet_balance;
            app().bindMember(res.member);
        } catch (err) {
            $('posTopupMsg').textContent = `❌ ${err.message}`;
        }
    });

    // ======================================================================
    // 作廢 / 部分退款
    // ======================================================================
    let refundOrder = null;

    function openRefund(orderId, mode) {
        refundOrder = (app().state.liveOrders || []).find((o) => Number(o.id) === Number(orderId));
        if (!refundOrder) return;
        const unpaid = refundOrder.payment_status === 'unpaid';
        $('posRefundOrderId').textContent = refundOrder.id;
        $('posRefundMethod').textContent = refundOrder.payment_method;
        $('posRefundMax').textContent = num(refundOrder.refundable);
        $('posRefundAmount').value = '';
        $('posRefundReason').value = '';
        $('posRefundItemQty').value = 1;
        const items = (refundOrder.items || []).filter((it) => num(it.quantity) > 0 && it.id);
        setHtml($('posRefundItemSel'), items.map((it) => html`<option value="${it.id}" data-max="${num(it.quantity)}">${it.name} x${num(it.quantity)} ($${num(it.price)})</option>`));
        const tenders = Object.entries(refundOrder.tenders || {});
        setHtml($('posRefundTender'), tenders.map(([m, amt]) => html`<option value="${m}">退回：${m} (可退 $${num(amt)})</option>`));
        $('posRefundPartial').disabled = unpaid;
        $('posRefundVoid').disabled = unpaid;   // 桌邊未結帳的整單取消用訂單卡片上的「取消」
        (mode === 'item' || unpaid ? $('posRefundItem') : $('posRefundVoid')).checked = true;
        onRefundModeChange();
        modalOf('posRefundModal').show();
    }

    function onRefundModeChange() {
        const partial = $('posRefundPartial').checked;
        const item = $('posRefundItem').checked;
        $('posRefundAmount').classList.toggle('d-none', !partial);
        $('posRefundItemRow').classList.toggle('d-none', !item);
        const tenders = refundOrder && Object.keys(refundOrder.tenders || {}).length > 0;
        $('posRefundTender').classList.toggle('d-none', !(tenders && (partial || item)));
        $('posRefundModeLabel').textContent = partial ? '部分退款' : (item ? '作廢品項' : '作廢');
    }
    document.querySelectorAll('input[name="posRefundMode"]').forEach((r) => r.addEventListener('change', onRefundModeChange));

    $('posRefundForm').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        if (!refundOrder) return;
        const partial = $('posRefundPartial').checked;
        const reason = $('posRefundReason').value.trim();
        const id = refundOrder.id;
        try {
            let message;
            const method = $('posRefundTender').classList.contains('d-none') ? '' : $('posRefundTender').value;
            if ($('posRefundItem').checked) {
                const body = { item_id: num($('posRefundItemSel').value), quantity: num($('posRefundItemQty').value), reason, method };
                const res = await withApproval((extra) => api(`/api/pos/orders/${id}/void_item`, { json: { ...body, ...extra } }));
                message = `訂單 #${id}：${res.message}`;
            } else if (partial) {
                const amount = num($('posRefundAmount').value);
                const res = await withApproval((extra) => api(`/api/pos/orders/${id}/refund`, { json: { amount, reason, method, ...extra } }));
                message = res.message;
            } else {
                await withApproval((extra) => api(`/api/orders/${id}/status`, { json: { status: 'Cancelled', reason, ...extra } }));
                message = `訂單 #${id} 已作廢 (庫存、點數已回補，已收款會建立退款紀錄)`;
            }
            modalOf('posRefundModal').hide();
            alert(`✅ ${message}`);
        } catch (err) {
            if (!err.cancelled) alert(`❌ ${err.message}`);
        }
        app().syncLiveState();
    });

    async function toggleRush(btn) {
        try {
            await api(`/api/pos/orders/${btn.dataset.id}/rush`, { json: { is_rush: btn.dataset.rush !== '1' } });
        } catch (err) {
            alert(`❌ ${err.message}`);
        }
        app().syncLiveState();
    }

    // ======================================================================
    // 桌位分開結帳 / 換桌
    // ======================================================================
    let splitTable = null;
    const PAY_OPTIONS = (cfg.methods || ['Cash']).filter((m) => ['Cash', 'Credit Card', 'Line Pay', '街口支付', 'Apple Pay'].includes(m));

    function selectedOrders() {
        return [...document.querySelectorAll('.split-order:checked')].map((el) => Number(el.value));
    }

    function selectedTotal() {
        if (!splitTable) return 0;
        const ids = selectedOrders();
        return splitTable.orders.filter((o) => ids.includes(o.id)).reduce((s, o) => s + num(o.total_price), 0);
    }

    function paymentRowHtml(amount) {
        return html`
            <div class="row g-1 mb-1 split-pay-row">
                <div class="col-4"><select class="form-select form-select-sm split-method" aria-label="付款方式">
                    ${PAY_OPTIONS.map((m) => html`<option value="${m}">${m}</option>`)}</select></div>
                <div class="col-3"><input type="number" class="form-control form-control-sm split-amount" min="1" value="${amount}" aria-label="金額"></div>
                <div class="col-3"><input type="number" class="form-control form-control-sm split-received" min="0" placeholder="現金實收" aria-label="實收"></div>
                <div class="col-2"><button type="button" class="btn btn-sm btn-outline-danger w-100" data-action="splitRemoveRow" aria-label="移除"><i class="bi bi-x"></i></button></div>
            </div>`;
    }

    function renderPayRows(amounts) {
        setHtml($('posSplitPayments'), amounts.map((a) => paymentRowHtml(a)));
        updateSplitRemain();
    }

    function updateSplitRemain() {
        const total = selectedTotal();
        $('posSplitTotal').textContent = total;
        let paid = 0;
        document.querySelectorAll('.split-pay-row').forEach((row) => { paid += num(row.querySelector('.split-amount').value); });
        const remain = total - paid;
        const el = $('posSplitRemain');
        el.className = `small mb-2 ${remain === 0 ? 'text-success' : 'text-danger'}`;
        el.textContent = remain === 0 ? '付款金額合計正確' : `付款合計 $${paid}，${remain > 0 ? `尚差 $${remain}` : `多了 $${-remain}`}`;
    }

    async function openSplit(tableId) {
        splitTable = (app().state.unpaidTables || []).find((t) => String(t.table_id) === String(tableId));
        if (!splitTable) return;
        $('posSplitTable').textContent = splitTable.name;
        $('posSplitMsg').textContent = '';
        setHtml($('posSplitOrders'), splitTable.orders.map((o) => html`
            <div class="form-check">
                <input class="form-check-input split-order" type="checkbox" value="${o.id}" id="split-o-${o.id}" checked>
                <label class="form-check-label small" for="split-o-${o.id}">
                    #${String(o.pickup_number || o.id).padStart(3, '0')} ${o.created_at} — $${num(o.total_price)}
                    <span class="text-white-50">(${o.items.map((it) => `${it.name}x${it.quantity}`).join('、')})</span>
                </label>
            </div>`));
        renderPayRows([selectedTotal()]);
        try {
            const res = await api('/api/pos/tables');
            setHtml($('posMoveTarget'), res.tables.filter((t) => String(t.id) !== String(tableId))
                .map((t) => html`<option value="${t.id}">${t.name}</option>`));
        } catch (err) { /* 換桌清單載入失敗不影響結帳 */ }
        modalOf('posSplitModal').show();
    }

    function splitEqual(n) {
        const total = selectedTotal();
        const base = Math.floor(total / n);
        const amounts = Array.from({ length: n }, (_, i) => base + (i < total - base * n ? 1 : 0));
        renderPayRows(amounts);
    }

    $('posSplitOrders').addEventListener('change', () => {
        const rows = document.querySelectorAll('.split-pay-row');
        if (rows.length === 1) rows[0].querySelector('.split-amount').value = selectedTotal();
        updateSplitRemain();
    });
    $('posSplitPayments').addEventListener('input', updateSplitRemain);

    $('posSplitForm').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        if (!splitTable) return;
        const orderIds = selectedOrders();
        if (!orderIds.length) {
            $('posSplitMsg').textContent = '請至少勾選一張訂單';
            return;
        }
        const payments = [];
        for (const row of document.querySelectorAll('.split-pay-row')) {
            const method = row.querySelector('.split-method').value;
            const amount = num(row.querySelector('.split-amount').value);
            const p = { method, amount, received: method === 'Cash' ? num(row.querySelector('.split-received').value) : 0 };
            const extra = await paymentDetails(method, amount);
            if (extra === null) return;
            payments.push({ ...p, ...extra });
        }
        const invType = $('posSplitInvoiceType') ? $('posSplitInvoiceType').value : '';
        const invValue = $('posSplitInvoiceValue') ? $('posSplitInvoiceValue').value.trim() : '';
        const invoice = invType ? { type: invType, carrier: invValue, buyer_id: invValue, love_code: invValue } : null;
        try {
            const res = await api(`/api/pos/tables/${splitTable.table_id}/settle`, { json: { order_ids: orderIds, payments, invoice } });
            modalOf('posSplitModal').hide();
            const detail = res.payments.map((p) => `${p.method} $${p.amount}${p.change ? ` (找零 $${p.change})` : ''}`).join('、');
            alert(`✅ ${res.table} 桌結帳 ${res.orders.length} 張單，共 $${res.total}：${detail}`);
        } catch (err) {
            $('posSplitMsg').textContent = err.message;
        }
        app().syncLiveState();
    });

    async function moveTable() {
        if (!splitTable) return;
        const target = $('posMoveTarget').value;
        if (!target) return;
        const orderIds = selectedOrders();
        try {
            const res = await api(`/api/pos/tables/${splitTable.table_id}/move`, { json: { to_table_id: Number(target), order_ids: orderIds } });
            modalOf('posSplitModal').hide();
            alert(`✅ 已將 ${res.moved.length} 張訂單從 ${res.from} 桌移到 ${res.to} 桌`);
        } catch (err) {
            $('posSplitMsg').textContent = err.message;
        }
        app().syncLiveState();
    }

    // ======================================================================
    // 手動開錢櫃 (需「錢櫃 / 交班」權限，留稽核紀錄)
    // ======================================================================
    async function openDrawer() {
        try {
            const res = await withApproval((extra) => api('/api/pos/drawer', { json: { reason: '手動開啟', ...extra } }));
            alert(`✅ ${res.message}`);
        } catch (err) {
            if (!err.cancelled) alert(`❌ ${err.message}`);
        }
    }

    Kiosk.on(document, 'click', {
        openGiftCards: () => openGiftCards(),
        giftPreset: (el) => { $('posGiftAmount').value = el.dataset.value; },
        openDrawer: () => openDrawer(),
        openShift: () => openShift(),
        openTopup: () => openTopup(),
        topupPreset: (el) => { $('posTopupAmount').value = el.dataset.value; },
        openRefund: (el) => openRefund(el.dataset.id, el.dataset.mode),
        toggleRush: (el) => toggleRush(el),
        openSplit: (el) => openSplit(el.dataset.id),
        splitEqual: (el) => splitEqual(Number(el.dataset.n)),
        splitAddRow: () => {
            const holder = document.createElement('div');
            setHtml(holder, paymentRowHtml(0));
            $('posSplitPayments').appendChild(holder.firstElementChild);
            updateSplitRemain();
        },
        splitRemoveRow: (el) => {
            const row = el.closest('.split-pay-row');
            if (row && document.querySelectorAll('.split-pay-row').length > 1) row.remove();
            updateSplitRemain();
        },
        moveTable: () => moveTable(),
    });

    window.PosPro = { withApproval, withMemberVerify, askLinePayKey, paymentDetails, networkPrint };
})();
