/*
 * POS 進階功能 (ES module，與其他 pos/ 模組共用 core.js 的狀態)：主管核准、會員身分驗證、LINE Pay 掃碼 / 刷卡授權碼、
 * 熱感機補印、手動開錢櫃、接單控制。交班 / 儲值 / 禮物卡在 shift.js，作廢退款 / 分桌換桌在 split.js；對話框在 templates/_pos_pro_modals.html。
 */
import { $, api, modalOf, num, posCfg } from './core.js';

const cfg = posCfg;

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

export async function withApproval(fn) {
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
export async function withMemberVerify(fn) {
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

export function askLinePayKey(amount) {
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
export async function paymentDetails(method, amount) {
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

export async function networkPrint(orderId) {
    if (!orderId) return;
    try {
        const res = await api(`/api/pos/orders/${orderId}/reprint`, { method: 'POST' });
        alert(`🖨️ ${res.message}`);
    } catch (err) {
        alert(`❌ ${err.message}`);
    }
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

// ======================================================================
// 接單控制 (暫停接單)：按鈕徽章 + 對話框；每分鐘向伺服器確認 (自動恢復時間到了會變回接單中)
// ======================================================================
function renderIntake(st) {
    const badge = $('posIntakeBadge');
    if (badge) {
        badge.className = `badge ${st.paused ? 'bg-danger' : 'bg-success'}`;
        badge.textContent = st.paused ? '已暫停' : '接單中';
    }
    const stateEl = $('posIntakeState');
    if (!stateEl) return;
    const until = st.resume_at ? st.resume_at.replace('T', ' ').slice(5) : '';
    stateEl.textContent = st.paused ? `目前已暫停接單${until ? `，${until} 自動恢復` : ' (手動恢復)'}` : '目前接單中';
    stateEl.className = `fw-bold mb-2 ${st.paused ? 'text-danger' : 'text-success'}`;
    $('posIntakeResumeBox').classList.toggle('d-none', !st.paused);
    $('posIntakePauseBox').classList.toggle('d-none', st.paused);
}

async function setIntake(paused, minutes) {
    $('posIntakeMsg').textContent = '';
    try {
        const st = await api('/api/pos/intake', { json: { paused, minutes } });
        renderIntake(st);
        $('posIntakeMsg').textContent = paused ? '已暫停接單' : '已恢復接單';
    } catch (err) {
        $('posIntakeMsg').textContent = `❌ ${err.message}`;
    }
}

async function refreshIntake() {
    try { renderIntake(await api('/api/pos/intake')); } catch (err) { /* 離線時保留目前顯示 */ }
}
if ($('posIntakeBtn')) {
    renderIntake(cfg.intake || { paused: false });
    setInterval(refreshIntake, 60000);
}

Kiosk.on(document, 'click', {
    openIntake: () => { refreshIntake(); modalOf('posIntakeModal').show(); },
    intakePause: (el) => setIntake(true, Number(el.dataset.minutes) || 0),
    intakeResume: () => setIntake(false, 0),
    openDrawer: () => openDrawer(),
});
