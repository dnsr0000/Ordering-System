"""
修正小組 customer-frontend-1 的回歸測試：顧客點餐頁的購物車 / 結帳 (static/js/customer/cart.js、checkout.js)
與點餐機斷線暫存 (static/js/kiosk-offline.js)。

前端部分以 node 執行 (以假的 Kiosk / document 載入 ES module 與 kiosk-offline.js)；沒有 node 時跳過。
"""
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.support import guest_client, make_item

ROOT = Path(__file__).resolve().parent.parent

SPEC = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

const ROOT = process.argv[2];
const mod = (rel) => pathToFileURL(`${ROOT}/${rel}`).href;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fakeEl() {
    return {
        value: '', checked: false, textContent: '', innerHTML: '', disabled: false, selectedIndex: -1, options: [],
        dataset: {}, style: {}, classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
        addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
        append() {}, appendChild() {}, remove() {}, setAttribute() {}, removeAttribute() {},
    };
}

// ---------------------------------------------------------------- 顧客點餐頁 (ES module)
const els = {};
globalThis.document = {
    getElementById: (id) => (id in els ? els[id] : (els[id] = fakeEl())),
    querySelectorAll: () => [], querySelector: () => null, createElement: () => fakeEl(), body: fakeEl(),
};
const alerts = [];
const texts = {};
const htmls = {};
let reloads = 0;
let apiHandler = async () => ({});
let idSeq = 0;
const stored = {};
globalThis.alert = (m) => alerts.push(String(m));
globalThis.confirm = () => true;
globalThis.window = globalThis;
globalThis.location = { reload() { reloads += 1; }, href: '/' };
globalThis.bootstrap = { Offcanvas: { getOrCreateInstance: () => ({ show() {} }), getInstance: () => null } };
const PAGE = {
    loggedIn: true, isGuest: false, lang: 'en', online: false, pro: {}, coupons: [],
    ui: { pay_card: 'Credit card', pay_cash: 'Cash', pay_wallet: 'Stored value', pay_gift_card: 'Gift card' },
    passes: [{ id: 7, menu_item_id: 5, name: '美式寄杯', remaining: 2 }],
    menu: {
        modifiers: [],
        reward: { isEnabled: false, pointsPerDollar: 1, maxDiscount: 0, spendPerPoint: 100 },
        items: [
            { id: 1, name: '漢堡', category: '主餐', price: 100, is_available: true, sizes: [], i18n: { en: { name: 'Burger' }, ja: { name: '' } },
              combos: [{ id: 10, name: 'A 套餐', price: 50, is_available: true,
                         sides: [{ id: 100, menu_item_id: 2, name: '薯條', is_available: true }],
                         choices: [{ group: '飲料', options: [
                             { id: 200, menu_item_id: 3, name: '可樂', extra_price: 0, is_available: true },
                             { id: 201, menu_item_id: 4, name: '紅茶', extra_price: 5, is_available: true }] }] }] },
            { id: 2, name: '薯條', category: '點心', price: 40, is_available: true, combos: [] },
            { id: 3, name: '可樂', category: '飲料', price: 30, is_available: true, combos: [] },
            { id: 4, name: '紅茶', category: '飲料', price: 30, is_available: true, combos: [] },
            { id: 5, name: '美式', category: '飲料', price: 60, is_available: true, combos: [] },
            { id: 6, name: '雞塊', category: '點心', price: 50, is_available: true, combos: [], can_be_add_on: true, add_on_price: 10 },
        ],
    },
};
globalThis.Kiosk = {
    html: (s, ...v) => s.reduce((a, x, i) => a + x + (i < v.length ? (Array.isArray(v[i]) ? v[i].flat(9).join('') : (v[i] ?? '')) : ''), ''),
    api: (...args) => apiHandler(...args),
    $: (id) => document.getElementById(id),
    modal: () => ({ show() {}, hide() {} }),
    setText: (id, v) => { texts[id] = v; },
    toggleRow() {},
    pageData: () => PAGE,
    store: { get: (k, d = null) => (k in stored ? stored[k] : d), set: (k, v) => { stored[k] = JSON.parse(JSON.stringify(v)); }, remove: (k) => { delete stored[k]; } },
    CART_KEY: 'kiosk_cart',
    newOrderId: () => `oid-${++idSeq}`,
    setHtml: (el, v) => { el.innerHTML = Array.isArray(v) ? v.flat(9).join('') : String(v); },
    money: (x) => `$${x}`,
    t: (k, f) => f,
    pad3: (n) => String(n).padStart(3, '0'),
    PAYMENT_STATUS: {},
    poll() {},
    pollJson: async () => null,
};

const core = await import(mod('static/js/customer/core.js'));
const cart = await import(mod('static/js/customer/cart.js'));
const checkout = await import(mod('static/js/customer/checkout.js'));
const { state, menuItems } = core;
const setAvail = (id, v) => { menuItems.get(id).is_available = v; };
const combo = menuItems.get(1).combos[0];

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('逾時取消訂單說明原單已取消，並清楚標示重新下單', () => {
    const previousStatusBadge = Kiosk.statusBadge;
    Kiosk.statusBadge = () => '';
    PAGE.ui.reorder_expired_hint = 'Payment expired; reordering creates a new order at current prices.';
    PAGE.ui.reorder_expired = 'Place new order';
    const orderHtml = checkout.orderCardHtml({
        order_id: 77, user_order_no: 1, status: 'Cancelled', status_code: 'Cancelled', order_type: '內用',
        pickup_number: 77, payment_status: 'expired', payment_url: '', payment_note: '',
        can_reorder: true, items: [], points_used: 0, points_earned: 0, total_price: 50, created_at: '2026-11-14 12:00',
    });
    assert.ok(orderHtml.includes(PAGE.ui.reorder_expired_hint));
    assert.ok(orderHtml.includes(PAGE.ui.reorder_expired));
    assert.ok(!orderHtml.includes('order-pay'));
    if (previousStatusBadge) Kiosk.statusBadge = previousStatusBadge;
    else delete Kiosk.statusBadge;
});

test('R2-084：存檔裡的售完標記不沿用，以目前菜單為準', () => {
    assert.equal(cart.normalizeLine({ menu_item_id: 1, name: '漢堡', is_sold_out: true }).is_sold_out, false);
    assert.equal(cart.normalizeLine({ menu_item_id: 999, name: '已刪除' }).is_sold_out, true);
});

test('B-248：套餐固定配餐 / 選中的任選選項售完時，購物車列也標示售完', () => {
    const raw = { menu_item_id: 1, name: '漢堡', combo_option_id: 10, combo_choices: [200] };
    setAvail(2, false);
    assert.equal(cart.normalizeLine(raw).is_sold_out, true);
    setAvail(2, true);
    setAvail(3, false);
    assert.equal(cart.normalizeLine(raw).is_sold_out, true);
    assert.equal(cart.normalizeLine({ ...raw, combo_choices: [201] }).is_sold_out, false);   // 沒選的選項售完不影響
    setAvail(3, true);
    // 輪詢 (live.js → repriceCart) 也依配餐更新
    state.cart = [cart.normalizeLine(raw)];
    setAvail(2, false);
    cart.repriceCart();
    assert.equal(state.cart[0].is_sold_out, true);
    setAvail(2, true);
    cart.repriceCart();
    assert.equal(state.cart[0].is_sold_out, false);
});

test('B-247：重算價格 / 套用伺服器單價不換結帳編號，內容變更才換', () => {
    state.cart = [cart.makeLine(menuItems.get(1))];
    state.checkoutOrderId = 'keep-me';
    cart.repriceCart();
    assert.equal(state.checkoutOrderId, 'keep-me');
    checkout.applyServerPrices({ line_prices: [100], server_total: 100 });
    assert.equal(state.checkoutOrderId, 'keep-me');
    cart.addLine(cart.makeLine(menuItems.get(2)));
    assert.equal(state.checkoutOrderId, null);
});

test('B-251：寄杯兌換列按「+」不可超過剩餘次數，重新載入時超量的數量會調降', () => {
    state.cart = [cart.makeLine(menuItems.get(5), { passId: 7 })];
    alerts.length = 0;
    cart.changeQty(0, 1);
    assert.equal(state.cart[0].quantity, 2);
    cart.changeQty(0, 1);
    assert.equal(state.cart[0].quantity, 2);
    assert.equal(alerts.length, 1);
    state.cart[0].quantity = 4;   // 舊版存下的超量寄杯列
    cart.updateCartUI();
    assert.equal(state.cart[0].quantity, 2);
});

test('B-254：再點一次的任選對不回來 / 售完時改選第一個有供應的選項；固定配餐售完時略過', () => {
    assert.deepEqual(cart.availableChoiceIds(combo, [999]), [200]);
    setAvail(3, false);
    assert.deepEqual(cart.availableChoiceIds(combo, [200]), [201]);
    setAvail(4, false);
    assert.equal(cart.availableChoiceIds(combo, [200]), null);
    setAvail(3, true);
    setAvail(4, true);
    setAvail(2, false);
    assert.equal(cart.availableChoiceIds(combo, [200]), null);
    setAvail(2, true);
});

test('B-254：reorder 補上任選並去掉舊的群組文字', async () => {
    state.cart = [];
    apiHandler = async () => ({ lines: [{ menu_item_id: 1, combo_option_id: 10, combo_choices: [], customization: '飲料:舊名、少冰', quantity: 1 }], skipped: [] });
    await checkout.reorder(1);
    assert.equal(state.cart.length, 1);
    assert.deepEqual(state.cart[0].combo_choices, [200]);
    assert.equal(state.cart[0].customization, '少冰');
    setAvail(2, false);
    state.cart = [];
    await checkout.reorder(1);
    assert.equal(state.cart.length, 0);
    setAvail(2, true);
});

test('R2-087：英 / 日文介面顯示店家填的譯名，line.name 維持原名', () => {
    assert.equal(cart.itemName(1, '漢堡'), 'Burger');
    assert.equal(cart.itemName(2, '薯條'), '薯條');
    const line = cart.makeLine(menuItems.get(1));
    assert.equal(line.name, '漢堡');
    assert.ok(cart.cartLineHtml(line, 0).includes('Burger'));
});

test('R2-088：加購品售完被移除時提示售完，不是名額不足', async () => {
    await sleep(150);   // 前面測試排程的提示先跑完
    state.cart = [cart.makeLine(menuItems.get(1)), cart.makeLine(menuItems.get(6), { isAddon: true })];
    setAvail(6, false);
    alerts.length = 0;
    cart.updateCartUI();
    await sleep(150);
    setAvail(6, true);
    assert.equal(state.cart.length, 1);
    assert.equal(alerts.length, 1);
    assert.ok(alerts[0].includes('已售完') && alerts[0].includes('雞塊'), alerts[0]);
});

test('R2-089：預約時段過期 / 額滿 (refresh_slots) 時重新載入取得新時段', () => {
    reloads = 0;
    checkout.handleCheckoutError({ message: '選擇的取餐時間已過', data: { refresh_slots: true } });
    assert.equal(reloads, 1);
});

test('R2-090：收據的付款方式用翻譯文字', () => {
    checkout.showReceipt({ payment_method: 'Credit Card', items: [], subtotal: 0, discount_amount: 0, total_price: 0 });
    assert.equal(texts['receipt-payment'], 'Credit card');
    checkout.showReceipt({ payment_method: 'Line Pay', items: [], subtotal: 0, discount_amount: 0, total_price: 0 });
    assert.equal(texts['receipt-payment'], 'Line Pay');
});

test('B-252：外送到府的確認視窗顯示外送與地址，不是內用', async () => {
    state.cart = [cart.makeLine(menuItems.get(1))];
    const el = (id) => document.getElementById(id);
    el('orderType').value = '外送到府';
    el('paymentMethod').value = 'Cash';
    el('deliveryAddress').value = '台北市信義路 1 號';
    el('needCutlery').checked = true;
    await checkout.openPreview();
    const out = els['preview-items'].innerHTML;
    assert.ok(out.includes('台北市信義路 1 號'), out);
    assert.ok(!out.includes('內用'), out);
    el('orderType').value = '內用';
});

test('R2-094：送出前重新驗證優惠碼遇到連線錯誤時中止，保留優惠碼且不開確認視窗', async () => {
    state.cart = [cart.makeLine(menuItems.get(1))];
    document.getElementById('paymentMethod').value = 'Cash';
    state.activePromoCode = 'SAVE10';
    state.appliedPromoDiscount = 10;
    document.getElementById('preview-items').innerHTML = 'SENTINEL';
    alerts.length = 0;
    apiHandler = async () => { throw Object.assign(new Error('連線失敗'), { status: 0, data: {} }); };
    await checkout.openPreview();
    assert.equal(state.activePromoCode, 'SAVE10');
    assert.equal(state.appliedPromoDiscount, 10);
    assert.equal(els['preview-items'].innerHTML, 'SENTINEL');
    assert.ok(alerts.some((a) => a.includes('無法驗證優惠碼')), alerts.join('|'));
    state.activePromoCode = '';
    state.appliedPromoDiscount = 0;
});

/** 確認視窗與動態建立的 PIN 表單 (記錄事件處理函式，測試中手動觸發) */
function pinHarness() {
    const listeners = [];
    els.confirmModal = Object.assign(fakeEl(), { addEventListener: (ev, fn) => listeners.push([ev, fn]), querySelector: () => fakeEl() });
    const forms = [];
    document.createElement = () => {
        const f = Object.assign(fakeEl(), { on: {}, pin: '' });
        f.addEventListener = (ev, fn) => { f.on[ev] = fn; };
        f.querySelector = () => ({ value: f.pin, focus() {} });
        forms.push(f);
        return f;
    };
    const submit = (pin) => { const f = forms[forms.length - 1]; f.pin = pin; f.on.submit({ preventDefault() {} }); };
    const hide = () => listeners.filter(([ev]) => ev === 'hidden.bs.modal').forEach(([, fn]) => fn());
    return { forms, submit, hide };
}
const needPin = (msg) => Object.assign(new Error(msg), { status: 403, data: { need_pin: true } });

test('B-122：結帳回 403 need_pin 時在確認視窗輸入 PIN，帶 pin 以同一個 client_order_id 重送 (PIN 錯誤可再輸入)', async () => {
    const h = pinHarness();
    state.cart = [cart.makeLine(menuItems.get(1))];
    state.checkoutOrderId = null;
    state.submitting = false;
    document.getElementById('paymentMethod').value = '儲值金';
    const calls = [];
    apiHandler = async (url, opts) => {
        calls.push({ url, ...opts.json });
        if (calls.length === 1) throw needPin('使用儲值金需輸入 PIN 碼確認身分');
        if (opts.json.pin !== '1234') throw needPin('PIN 碼不正確');
        return { order_id: 77, items: [], subtotal: 100, discount_amount: 0, total_price: 100, payment_method: '儲值金' };
    };
    const done = checkout.confirmCheckout();
    await sleep(10);
    assert.equal(h.forms.length, 1);
    assert.ok(h.forms[0].innerHTML.includes('type="password"'), h.forms[0].innerHTML);
    h.submit('0000');
    await sleep(10);
    assert.equal(h.forms.length, 2);
    assert.ok(h.forms[1].innerHTML.includes('PIN 碼不正確'), h.forms[1].innerHTML);
    h.submit('1234');
    await done;
    assert.equal(calls.length, 3);
    assert.ok(calls.every((c) => c.url === '/submit_order' && c.client_order_id === calls[0].client_order_id));
    assert.deepEqual(calls.map((c) => c.pin), [undefined, '0000', '1234']);
    assert.equal(texts['receipt-order-id'], '#77');
    assert.equal(state.submitting, false);
});

test('B-122：關閉確認視窗 = 取消輸入 PIN，不重送並顯示訊息', async () => {
    const h = pinHarness();
    state.cart = [cart.makeLine(menuItems.get(1))];
    state.submitting = false;
    alerts.length = 0;
    let n = 0;
    apiHandler = async () => { n += 1; throw needPin('使用點數需輸入 PIN 碼確認身分'); };
    const done = checkout.confirmCheckout();
    await sleep(10);
    h.hide();
    await done;
    assert.equal(n, 1);
    assert.ok(alerts.some((a) => a.includes('需輸入 PIN')), alerts.join('|'));
    assert.equal(state.submitting, false);
    document.createElement = () => fakeEl();
    document.getElementById('paymentMethod').value = 'Cash';
});

// ---------------------------------------------------------------- kiosk-offline.js (一般 script)
function loadOffline({ offlinePage = false, api = async () => ({}), fetchImpl = async () => ({ ok: false }), path = '/' } = {}) {
    const store = {};
    const timers = [];
    const loc = { pathname: path, href: path, reloads: 0, reload() { this.reloads += 1; } };
    const ctx = {
        Kiosk: {
            store: { get: (k, d = null) => (k in store ? store[k] : d), set: (k, v) => { store[k] = JSON.parse(JSON.stringify(v)); } },
            t: (k, f) => f, api, pad3: (n) => String(n).padStart(3, '0'), modal: () => ({ show() {}, hide() {} }),
        },
        document: {
            currentScript: { dataset: { shop: 'default' } },
            getElementById: (id) => (id === 'offline-page' && offlinePage ? fakeEl() : null),
            createElement: () => fakeEl(), body: fakeEl(),
        },
        navigator: { onLine: true },
        setTimeout: () => 0,
        setInterval: (fn) => { timers.push(fn); return 0; },
        addEventListener() {},
        fetch: fetchImpl,
        location: loc,
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(`${ROOT}/static/js/kiosk-offline.js`, 'utf8'), ctx);
    return { KO: ctx.KioskOffline, timers, loc };
}
const cash = (extra = {}) => ({ payment_method: 'Cash', items: [{ menu_item_id: 1, quantity: 1 }], use_points: 0, promo_code: '', ...extra });

test('B-250：會員訂單不暫存', () => {
    const { KO } = loadOffline();
    assert.equal(KO.blockReason(cash()), '');
    assert.equal(KO.blockReason(cash(), { member: false }), '');
    assert.ok(KO.blockReason(cash(), { member: true }).includes('會員'));
});

test('R2-085：電子發票格式錯誤的訂單不暫存 (補送時會整張被拒)', () => {
    const { KO } = loadOffline();
    assert.ok(KO.blockReason(cash({ invoice: { type: 'mobile', carrier: '/ABC123' } })));
    assert.equal(KO.blockReason(cash({ invoice: { type: 'mobile', carrier: '/abc+123' } })), '');
    assert.ok(KO.blockReason(cash({ invoice: { type: 'company', buyer_id: '12345678' } })));
    assert.equal(KO.blockReason(cash({ invoice: { type: 'company', buyer_id: '04595257' } })), '');
    assert.ok(KO.blockReason(cash({ invoice: { type: 'donate', love_code: '12' } })));
    assert.equal(KO.blockReason(cash({ invoice: { type: 'donate', love_code: '168' } })), '');
    assert.ok(KO.blockReason(cash({ invoice: { type: 'citizen', carrier: 'AB123' } })));
    assert.equal(KO.blockReason(cash({ invoice: null })), '');
});

test('R2-095：暫存時間帶時區', () => {
    const { KO } = loadOffline();
    const entry = KO.enqueue({ ...cash(), client_order_id: 'abcd-1234' }, 100, 'x');
    assert.match(entry.queued_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$/);
    const back = new Date(entry.queued_at).getTime();
    assert.ok(Math.abs(Date.now() - back) < 5000);
});

test('B-260：英 / 日文訊息的 CSRF 失敗仍保留待補送', async () => {
    const err = Object.assign(new Error('Security check failed (the form token is invalid or expired)'),
        { status: 403, data: { message: '安全驗證失敗 (CSRF Token 無效或已過期)，請重新整理頁面後再試！' } });
    const { KO } = loadOffline({ api: async () => { throw err; } });
    KO.enqueue({ ...cash(), client_order_id: 'csrf-0001' }, 100, 'x');
    await KO.flush();
    const q = KO.load();
    assert.equal(q.pending.length, 1);
    assert.equal(q.failed.length, 0);
});

test('R2-096：離線頁在門市主機恢復回應後自動重新載入 (不必等 online 事件)', async () => {
    const down = loadOffline({ offlinePage: true, fetchImpl: async () => { throw new Error('down'); } });
    assert.equal(down.timers.length, 1);
    await down.timers[0]();
    assert.equal(down.loc.reloads, 0);
    const up = loadOffline({ offlinePage: true, fetchImpl: async () => ({ ok: true }) });
    await up.timers[0]();
    assert.equal(up.loc.reloads, 1);
    const direct = loadOffline({ offlinePage: true, fetchImpl: async () => ({ ok: true }), path: '/offline' });
    await direct.timers[0]();
    assert.equal(direct.loc.href, '/');
});

let failed = 0;
for (const [name, fn] of tests) {
    try {
        await fn();
        console.log(`ok   ${name}`);
    } catch (err) {
        failed += 1;
        console.log(`FAIL ${name}\n${err && err.stack}`);
    }
}
console.log(`${tests.length - failed}/${tests.length}`);
process.exit(failed ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node 才能執行前端測試')
def test_customer_cart_checkout_and_offline_queue_js(tmp_path):
    spec = tmp_path / 'customer_frontend_1_spec.mjs'
    spec.write_text(SPEC, encoding='utf-8')
    result = subprocess.run([shutil.which('node'), str(spec), ROOT.as_posix()], capture_output=True, text=True,
                            encoding='utf-8', cwd=str(ROOT), timeout=60, env=dict(os.environ, TZ='Asia/Taipei'))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FAIL' not in result.stdout, result.stdout


def test_offline_order_accepts_queued_at_from_device_in_other_timezone(app):
    """R2-095：平板時區設成 UTC 時，kiosk-offline.js 送出帶時區的時間 (+00:00)，伺服器換算後不會誤判為逾時"""
    item = make_item(app, f'時區暫存餐{uuid.uuid4().hex[:4]}', price=70, stock=20)
    queued = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    body = {'shop': 'default', 'code': 'TZ01', 'queued_at': queued,
            'order': {'client_order_id': str(uuid.uuid4()), 'items': [{'menu_item_id': item, 'quantity': 1}],
                      'payment_method': 'Cash', 'order_type': '內用'}}
    r = guest_client(app).post('/api/kiosk/offline_order', data=json.dumps(body), content_type='application/json')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['replayed'] is False
