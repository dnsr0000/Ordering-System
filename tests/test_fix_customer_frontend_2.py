"""
修正小組 customer-frontend-2 的回歸測試 (顧客點餐頁的前端模組，以 node 執行內嵌的 ES module 腳本；沒有 node 時跳過)：
- B-249 套餐卡片 / 確認加入連點兩下不會重複加入
- R2-093 寄杯「兌換一份」不加入售完的餐點
- R2-092 套餐視窗的單點價 / 總價依通路與會員價
- B-256 套餐可選與否以即時配餐供應判斷 (不看頁面載入時的 combo.is_available)
- B-246 優惠碼以「扣掉促銷後的金額」驗證；R2-094 連線錯誤時保留已套用的優惠碼並回傳失敗
- B-258 英文 rw_usable 為空字串時不顯示中文「可用」
- B-255 / R2-091 輪詢更新會員價、只改通路價也會重算購物車；B-256 快速卡片恢復供應時還原點擊動作
- B-259 語音點餐不加入即時菜單上已售完的餐點
- R2-086 線上點餐不啟用點餐機的閒置自動登出
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='需要 node 才能執行前端模組測試')


def _url(rel):
    return (ROOT / rel).as_uri()


def _run(page_data, body):
    """建立 Kiosk / document 替身後載入 customer/ 模組執行 body (body 內以 await import 載入模組)"""
    script = STUB.replace('__PAGE_DATA__', json.dumps(page_data, ensure_ascii=False)) + body + '\nprocess.exit(0);\n'
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script], capture_output=True, text=True,
                            encoding='utf-8', cwd=str(ROOT), timeout=60, env={**os.environ})
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


# 任意 DOM 元素的替身：未設定的屬性 / 方法都回傳可再呼叫的替身 (只測邏輯，不測畫面)
STUB = r"""
import assert from 'node:assert/strict';
const anyFn = new Proxy(function () {}, {
    get: (t, k) => (k === Symbol.iterator ? [][Symbol.iterator].bind([]) : (k === Symbol.toPrimitive ? () => '' : anyFn)),
    apply: () => anyFn,
});
const els = {};
function el(id) {
    if (!els[id]) {
        const store = { id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '', dataset: {} };
        els[id] = new Proxy(store, { get: (t, k) => (k in t ? t[k] : anyFn), set: (t, k, v) => { t[k] = v; return true; } });
    }
    return els[id];
}
const calls = { setHtml: [], hideModalThen: 0, alerts: [], api: [] };
globalThis.alert = (m) => calls.alerts.push(String(m));
globalThis.document = { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {}, createElement: () => el('created' + Math.random()), body: el('body') };
const memory = {};
globalThis.Kiosk = {
    html: (strings, ...vals) => strings.reduce((s, str, i) => s + str + (i < vals.length ? (Array.isArray(vals[i]) ? vals[i].flat(Infinity).join('') : String(vals[i] ?? '')) : ''), ''),
    setHtml: (target, content) => calls.setHtml.push([target, Array.isArray(content) ? content.flat(Infinity).join('') : String(content)]),
    api: async (url, opts) => { calls.api.push([url, opts && opts.json]); return globalThis.apiImpl(url, opts && opts.json); },
    $: (id) => el(id), modal: () => ({ show() {}, hide() {} }), setText: () => {}, toggleRow: () => {},
    money: (n) => `$${n}`, pageData: () => (__PAGE_DATA__), CART_KEY: 'kiosk_cart',
    hideModalThen: () => { calls.hideModalThen += 1; },
    store: { get: (k, d) => (k in memory ? memory[k] : d), set: (k, v) => { memory[k] = v; }, remove: (k) => { delete memory[k]; } },
    poll: () => ({ stop() {} }), pollJson: async () => null,
};
globalThis.apiImpl = async () => ({});
globalThis.__calls = calls;
globalThis.__el = el;
"""


def _item(item_id, name, price, **extra):
    data = {'id': item_id, 'name': name, 'category': '主餐', 'price': price, 'original_price': price, 'is_available': True,
            'modifiers': 'none', 'sizes': [], 'combos': [],
            'prices': {'dine_in': price, 'takeout': price, 'online': price, 'delivery': price},
            'member_prices': {'dine_in': price, 'takeout': price, 'online': price, 'delivery': price}}
    data.update(extra)
    return data


def _page(items, **extra):
    data = {'loggedIn': True, 'isGuest': False, 'ui': {}, 'pro': {}, 'menu': {'items': items, 'modifiers': [], 'reward': {}}}
    data.update(extra)
    return data


def test_pick_combo_double_tap_and_use_pass_sold_out():
    combo = {'id': 7, 'name': 'A 餐', 'price': 50, 'sides': [], 'choices': []}
    items = [_item(1, '牛肉麵', 120, combos=[combo]), _item(2, '美式咖啡', 60, is_available=False)]
    page = _page(items, passes=[{'id': 9, 'menu_item_id': 2, 'name': '美式寄杯', 'remaining': 5}])
    _run(page, f"""
const core = await import('{_url('static/js/customer/core.js')}');
const menu = await import('{_url('static/js/customer/menu.js')}');
// B-249：套餐卡片連點兩下只排一次「關閉後加入」
core.state.pendingItem = core.menuItems.get(1);
menu.pickCombo('7');
menu.pickCombo('7');
assert.equal(__calls.hideModalThen, 1);
assert.equal(core.state.pendingItem, null);
// R2-093：寄杯對應的餐點售完時不加入購物車
menu.usePass(9);
assert.equal(core.state.cart.length, 0);
assert.equal(__calls.alerts.length, 1);
assert.ok(__calls.alerts[0].includes('美式咖啡'));
""")


def test_combo_modal_uses_channel_and_member_price_and_live_side_availability():
    side = {'menu_item_id': 3, 'name': '紅茶', 'is_available': False}
    combo = {'id': 7, 'name': 'A 餐', 'price': 50, 'is_available': False, 'sides': [side], 'choices': []}
    beef = _item(1, '牛肉麵', 120, combos=[combo],
                 member_prices={'dine_in': 100, 'takeout': 100, 'online': 100, 'delivery': 100})
    tea = _item(3, '紅茶', 30)   # 載入時售完、之後已恢復供應 (menuItems 的即時狀態為可點)
    _run(_page([beef, tea]), f"""
const core = await import('{_url('static/js/customer/core.js')}');
const menu = await import('{_url('static/js/customer/menu.js')}');
menu.renderComboCards(core.menuItems.get(1));
const out = __calls.setHtml.at(-1)[1];
assert.ok(out.includes('$100'), out);          // R2-092：單點顯示會員價
assert.ok(out.includes('150'), out);           // 總價 = 會員價 + 套餐加價
assert.ok(!out.includes('$120'), out);
assert.ok(out.includes('data-combo-id="7"'), out);   // B-256：配餐已恢復供應，套餐可選
""")


def test_verify_promo_uses_after_promotion_base_and_keeps_code_on_network_error():
    _run(_page([_item(1, '套餐', 250)]), f"""
const core = await import('{_url('static/js/customer/core.js')}');
const totals = await import('{_url('static/js/customer/totals.js')}');
core.state.cart = [{{ menu_item_id: 1, price: 250, quantity: 2, modifier_ids: [] }}];
core.state.promotion = {{ discount: 100, names: [], gifts: [], blocks_coupon: false }};
globalThis.apiImpl = async () => ({{ valid: true, discount: 40, message: 'ok' }});
assert.equal(await totals.verifyPromo('PCT90'), true);
// B-246：以扣掉促銷後的金額驗證 (與後端結帳 c.remaining 相同)
const body = __calls.api.filter(([u]) => u === '/api/verify_promo').at(-1)[1];
assert.equal(body.subtotal, 400);
assert.equal(core.state.activePromoCode, 'PCT90');
assert.equal(core.state.appliedPromoDiscount, 40);
// R2-094：連線錯誤時保留已套用的代碼與折扣，回傳 false 讓下單前確認中止
globalThis.apiImpl = async () => {{ throw new Error('network'); }};
assert.equal(await totals.verifyPromo('PCT90', {{ silent: true }}), false);
assert.equal(core.state.activePromoCode, 'PCT90');
assert.equal(core.state.appliedPromoDiscount, 40);
// 代碼確實無效時仍清掉
globalThis.apiImpl = async () => ({{ valid: false, discount: 0, message: 'bad' }});
assert.equal(await totals.verifyPromo('PCT90', {{ silent: true }}), true);
assert.equal(core.state.activePromoCode, '');
assert.equal(core.state.appliedPromoDiscount, 0);
""")


def test_coupon_label_keeps_empty_english_suffix():
    page = _page([], ui={'rw_min_spend': 'Min. spend $', 'rw_usable': ''})
    _run(page, f"""
const totals = await import('{_url('static/js/customer/totals.js')}');
totals.renderCoupons([{{ code: 'ABC', title: 'Coupon', min_spend: 300 }}]);
const out = __calls.setHtml.find(([t]) => t.id === 'userCouponSelect')[1];
assert.ok(out.includes('Min. spend $300'), out);
assert.ok(!out.includes('可用'), out);
""")


def test_live_updates_member_and_channel_prices_and_quick_cards():
    tea = _item(1, '珍奶', 50, prices={'dine_in': 50, 'takeout': 50, 'online': 60, 'delivery': 50},
                member_prices={'dine_in': 45, 'takeout': 45, 'online': 55, 'delivery': 45})
    cake = _item(2, '蛋糕', 80, is_available=False)
    payload = {
        # 只改線上價與會員價 (內用單價不變)
        '1': {'available': True, 'hint': '', 'price': 50, 'original_price': 50,
              'prices': {'delivery': 50, 'dine_in': 50, 'online': 65, 'takeout': 50},
              'member_prices': {'delivery': 48, 'dine_in': 48, 'online': 60, 'takeout': 48}},
        '2': {'available': True, 'hint': '', 'price': 80, 'original_price': 80,
              'prices': {'delivery': 80, 'dine_in': 80, 'online': 80, 'takeout': 80},
              'member_prices': {'delivery': 80, 'dine_in': 80, 'online': 80, 'takeout': 80}},
    }
    same = {'1': payload['1']}
    _run(_page([tea, cake], online=True), f"""
const core = await import('{_url('static/js/customer/core.js')}');
const live = await import('{_url('static/js/customer/live.js')}');
// 載入時售完的快速卡片 (樣板仍輸出 data-id)
const card = {{ dataset: {{ id: '2' }}, attrs: {{}}, classList: {{ toggle() {{}} }},
    setAttribute(k, v) {{ this.attrs[k] = v; }}, removeAttribute(k) {{ delete this.attrs[k]; }}, querySelector: () => null }};
document.querySelectorAll = (sel) => (sel.startsWith('.quick-card') ? [card] : []);
core.state.cart = [{{ menu_item_id: 1, price: 55, quantity: 2, modifier_ids: [], combo_choices: [] }}];
const payload = {json.dumps(payload)};
assert.ok(live.applyAvailability(payload) >= 2);
assert.equal(core.menuItems.get(1).member_prices.online, 60);   // B-255：會員價跟著更新
assert.equal(core.state.cart[0].price, 60);                      // R2-091：只改通路價也重算 (線上會員價 60)
assert.equal(card.dataset.action, 'add');                        // B-256：恢復供應後可點
assert.equal(card.attrs.role, 'button');
// 內容相同 (通路順序不同) 不算變動
assert.equal(live.applyAvailability({json.dumps(same)}), 0);
""")


def test_voice_add_skips_items_sold_out_on_live_menu():
    items = [_item(1, '紅茶', 30, is_available=False), _item(2, '綠茶', 30)]
    _run(_page(items), f"""
const core = await import('{_url('static/js/customer/core.js')}');
const {{ voiceActions }} = await import('{_url('static/js/customer/voice.js')}');
__el('voiceText').value = '紅茶 綠茶';
globalThis.apiImpl = async () => ({{ text: '紅茶 綠茶', source: 'rules', unmatched: [], lines: [
    {{ menu_item_id: 1, name: '紅茶', quantity: 1, available: true }},
    {{ menu_item_id: 2, name: '綠茶', quantity: 1, available: true }},
] }});
await voiceActions['voice-parse']();
voiceActions['voice-add']();
assert.deepEqual(core.state.cart.map((l) => l.menu_item_id), [2]);
""")


def test_online_ordering_has_no_idle_logout():
    src = (ROOT / 'static' / 'js' / 'customer.js').read_text(encoding='utf-8')
    assert 'if (!pageData.table && !isOnline) Kiosk.idleLogout' in src


# ---------------------------------------------------------------- 跨檔請求 (第二輪)


def test_card_prices_follow_order_type_channel():
    """B-262：門市點餐機切換外帶後，卡片價與 data-price 改用外帶價 (會員價也依通路)"""
    items = [_item(1, '牛肉麵', 120, prices={'dine_in': 120, 'takeout': 110, 'online': 130, 'delivery': 120},
                   member_prices={'dine_in': 100, 'takeout': 105, 'online': 125, 'delivery': 100}),
             _item(2, '紅茶', 30, prices={'dine_in': 30, 'takeout': 25, 'online': 30, 'delivery': 30},
                   member_prices={'dine_in': 30, 'takeout': 25, 'online': 30, 'delivery': 30})]
    _run(_page(items), f"""
const live = await import('{_url('static/js/customer/live.js')}');
const rows = [1, 2].map((id) => {{
    const parent = {{ id: 'price' + id }};
    return {{ dataset: {{ id: String(id), price: '' }}, parent, querySelector: (sel) => (sel === '.fs-5' ? {{ parentElement: parent }} : null) }};
}});
document.querySelectorAll = (sel) => (sel === '#menuList .menu-row' ? rows : []);
__el('orderType').value = '外帶';
live.refreshCardPrices();
const html1 = __calls.setHtml.find(([t]) => t === rows[0].parent)[1];
assert.ok(html1.includes('$110') && html1.includes('$105'), html1);   // 劃線外帶價 + 外帶會員價
assert.equal(rows[0].dataset.price, '105');
const html2 = __calls.setHtml.find(([t]) => t === rows[1].parent)[1];
assert.ok(html2.includes('$25') && !html2.includes('$30'), html2);
assert.equal(rows[1].dataset.price, '25');
__el('orderType').value = '內用';
__calls.setHtml.length = 0;
live.refreshCardPrices();
assert.equal(rows[0].dataset.price, '100');
assert.equal(rows[1].dataset.price, '30');
""")


def test_tier_points_multiplier_in_earned_points():
    """B-229：本單可得點數乘上會員等級贈點倍數 (與後端相同)"""
    page = _page([_item(1, '套餐', 250)], pro={'tierPointsMultiplier': 2})
    page['menu']['reward'] = {'isEnabled': True, 'spendPerPoint': 100, 'pointsPerDollar': 10}
    _run(page, f"""
const core = await import('{_url('static/js/customer/core.js')}');
const totals = await import('{_url('static/js/customer/totals.js')}');
core.state.cart = [{{ menu_item_id: 1, price: 250, quantity: 1, modifier_ids: [] }}];
const t = totals.computeTotals();
assert.equal(t.final, 250);
assert.equal(t.earned, 4);   // floor(250 / 100) x 2
""")


def test_voice_add_shows_hint_for_items_sold_out_after_parse():
    """B-136：解析後才在即時菜單售完的品項不加入，留在視窗標示「目前無法供應」"""
    items = [_item(1, '紅茶', 30), _item(2, '綠茶', 30)]
    _run(_page(items), f"""
const core = await import('{_url('static/js/customer/core.js')}');
const {{ voiceActions }} = await import('{_url('static/js/customer/voice.js')}');
__el('voiceText').value = '紅茶 綠茶';
globalThis.apiImpl = async () => ({{ text: '紅茶 綠茶', source: 'rules', unmatched: [], lines: [
    {{ menu_item_id: 1, name: '紅茶', quantity: 1, available: true }},
    {{ menu_item_id: 2, name: '綠茶', quantity: 1, available: true }},
] }});
await voiceActions['voice-parse']();
core.menuItems.get(1).is_available = false;   // live.js 輪詢後售完
__calls.setHtml.length = 0;
voiceActions['voice-add']();
assert.equal(core.state.cart.length, 0);   // 先重畫提示，不加入
const out = __calls.setHtml.find(([t]) => t.id === 'voiceLines')[1];
assert.ok(out.includes('目前無法供應'), out);
voiceActions['voice-add']();   // 顧客看過提示後再按一次：只加入可點的
assert.deepEqual(core.state.cart.map((l) => l.menu_item_id), [2]);
""")


def test_combo_and_modifier_titles_use_translated_name():
    """R2-087：套餐 / 客製化視窗的品名依語言顯示，送後端的品名仍是中文原名；B-486 桌邊呼叫顯示翻譯訊息"""
    combo = {'id': 7, 'name': 'Set A', 'price': 50, 'sides': [], 'choices': []}
    items = [_item(1, '牛肉麵', 120, combos=[combo], modifiers='spicy', i18n={'en': {'name': 'Beef Noodles'}})]
    _run(_page(items, lang='en'), f"""
const core = await import('{_url('static/js/customer/core.js')}');
const menu = await import('{_url('static/js/customer/menu.js')}');
menu.renderComboCards(core.menuItems.get(1));
assert.ok(__el('comboModalTitle').textContent.includes('Beef Noodles'));
assert.ok(__calls.setHtml.at(-1)[1].includes('Beef Noodles'));
menu.proceedToModifiers(core.menuItems.get(1), null, false);
assert.ok(__el('modifierModalTitle').textContent.includes('Beef Noodles'));
assert.equal(core.state.pendingSelection.item.name, '牛肉麵');
globalThis.apiImpl = async () => ({{ message: '已通知服務人員', message_i18n: 'Staff notified' }});
await menu.callService({{ dataset: {{ kind: 'water' }}, disabled: false }});
assert.ok(__calls.alerts.at(-1).includes('Staff notified'));
""")
