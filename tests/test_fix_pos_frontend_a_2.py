"""
修正小組 pos-frontend-a-2 的回歸測試 (前端純邏輯，以 node 執行內嵌的 ES module 腳本；沒有 node 時跳過)：
- B-228 購物車合併鍵含改價：改過價的列不併入新加的份數
- B-229 可得點數乘上會員等級贈點倍數 (與後端 order_checkout 相同的取整)
- B-216 POS 選「外送到府」時價格通路為外帶 (不套內用價 / 內用服務費)
- R2-074 配餐 / 任選的客製化括號內不含「、」與加料文字 (後端以「、」切段時括號不會被切斷)
"""
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='需要 node 才能執行前端純邏輯測試')


def _url(rel):
    return (ROOT / rel).as_uri()


def _run(script):
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script], capture_output=True, text=True,
                            encoding='utf-8', cwd=str(ROOT), timeout=60, env={**os.environ})
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


# Kiosk 全域的最小替身 (core.js / modifiers.js 在載入時讀取)
KIOSK_STUB = """
const els = {};
globalThis.Kiosk = {
    html: (strings, ...vals) => strings.reduce((s, str, i) => s + str + (i < vals.length ? String(vals[i] ?? '') : ''), ''),
    setHtml: () => {}, api: async () => ({}), $: (id) => els[id] || null, modal: () => ({}), pageData: () => ({}),
    hideModalThen: () => {},
};
globalThis.__els = els;
"""


def test_line_key_includes_override_price():
    _run(f"""
import assert from 'node:assert/strict';
import {{ lineKey, mergeLine }} from '{_url('static/js/shared/cartline.js')}';
const base = {{ menu_item_id: 1, quantity: 1, customization: '少冰/半糖', modifier_ids: [], price: 50 }};
const cart = [{{ ...base, price: 0, override_price: 0 }}];
assert.notEqual(lineKey(cart[0]), lineKey(base));
mergeLine(cart, {{ ...base }});
assert.equal(cart.length, 2);
assert.equal(cart[0].quantity, 1);
assert.equal(cart[1].price, 50);
// 沒改價的同品項照常合併
mergeLine(cart, {{ ...base }});
assert.equal(cart.length, 2);
assert.equal(cart[1].quantity, 2);
""")


def test_earned_points_multiplier():
    _run(f"""
import assert from 'node:assert/strict';
import {{ computeTotals }} from '{_url('static/js/shared/totals.js')}';
// 後端：int((final - delivery) // spp * multiplier)
assert.equal(computeTotals({{ subtotal: 350, spendPerPoint: 100 }}).earned, 3);
assert.equal(computeTotals({{ subtotal: 350, spendPerPoint: 100, pointsMultiplier: 2 }}).earned, 6);
assert.equal(computeTotals({{ subtotal: 350, spendPerPoint: 100, pointsMultiplier: 1.2 }}).earned, Math.trunc(3 * 1.2));
assert.equal(computeTotals({{ subtotal: 350, spendPerPoint: 100, pointsMultiplier: 0 }}).earned, 0);
assert.equal(computeTotals({{ subtotal: 350, spendPerPoint: 100, pointsMultiplier: null }}).earned, 3);
assert.equal(computeTotals({{ subtotal: 350, spendPerPoint: 100, deliveryFee: 60, pointsMultiplier: 1.5 }}).earned, 4);
assert.equal(computeTotals({{ subtotal: 350, earnsPoints: false, pointsMultiplier: 2 }}).earned, 0);
""")


def test_pos_channel_home_delivery_is_takeout():
    _run(KIOSK_STUB + f"""
import assert from 'node:assert/strict';
const {{ channel, state }} = await import('{_url('static/js/pos/core.js')}');
__els.posTypeTakeout = {{ checked: false }};
__els.posTypeHome = {{ checked: false }};
assert.equal(channel(), 'dine_in');
__els.posTypeHome.checked = true;
assert.equal(channel(), 'takeout');
__els.posTypeHome.checked = false;
__els.posTypeTakeout.checked = true;
assert.equal(channel(), 'takeout');
state.deliveryMode = true;
assert.equal(channel(), 'delivery');
delete __els.posTypeHome;
state.deliveryMode = false;
__els.posTypeTakeout.checked = false;
assert.equal(channel(), 'dine_in');
""")


def test_combo_side_customization_has_no_list_separator_inside_brackets():
    _run(KIOSK_STUB + f"""
import assert from 'node:assert/strict';
const mods = await import('{_url('static/js/shared/modifiers.js')}');
// 最小 DOM 替身：區塊以 data-key 查找，區塊內以 data-mod 查找
const select = (value) => ({{ value }});
const addon = (id) => ({{ dataset: {{ id: String(id) }} }});
const section = ({{ ice, sugar, spicy, addons = [] }}) => ({{
    querySelector(sel) {{
        const m = /data-mod="(\\w+)"/.exec(sel);
        const name = m && m[1];
        if (name === 'ice' && ice) return select(ice);
        if (name === 'sugar' && sugar) return select(sugar);
        if (name === 'spicy' && spicy) return select(spicy);
        return null;
    }},
    querySelectorAll() {{ return addons.map(addon); }},
}});
const sections = {{
    main: section({{ spicy: '小辣' }}),
    side0: section({{ addons: [7, 8] }}),
    'choice0-31': section({{ ice: '少冰', sugar: '半糖' }}),
}};
const root = {{
    querySelector(sel) {{
        if (sel.includes('[data-choice-group="0"]')) return {{ value: '31' }};
        const m = /data-key="([^"]+)"/.exec(sel);
        if (m) return sections[m[1]] || null;
        return null;
    }},
}};
const names = {{ 7: '起司', 8: '蛋' }};
const r = mods.readModifierModal(root, {{
    hasMain: true, sides: [{{ name: '薯條' }}], choices: [{{ group: '飲料', options: [{{ id: 31, name: '珍珠奶茶' }}] }}],
    describeAddon: (id) => `${{names[id]}}(+10)`,
}});
assert.deepEqual(r.texts, ['小辣', '起司(+10)', '蛋(+10)', '珍珠奶茶(少冰/半糖)']);
assert.deepEqual(r.modifierIds, [7, 8]);
assert.deepEqual(r.choiceIds, [31]);
// 每個片段以「、」切開後括號都完整
for (const part of r.texts.join('、').split('、')) {{
    assert.equal((part.match(/\\(/g) || []).length, (part.match(/\\)/g) || []).length, part);
}}
// 點餐機 (不傳 describeAddon)：只收加料 ID
const k = mods.readModifierModal(root, {{ hasMain: true, sides: [{{ name: '薯條' }}], choices: [{{ group: '飲料', options: [{{ id: 31, name: '珍珠奶茶' }}] }}] }});
assert.deepEqual(k.texts, ['小辣', '珍珠奶茶(少冰/半糖)']);
assert.deepEqual(k.modifierIds, [7, 8]);
""")
