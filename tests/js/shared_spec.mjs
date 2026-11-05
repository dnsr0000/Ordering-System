/*
 * static/js/shared/ 純邏輯模組的單元測試 (由 tests/test_shared_js_logic.py 以 node 執行)：
 * 金額順序與進位、加購配額、單價、購物車行合併鍵、品項 payload、發票欄位、預覽排程序號。
 * 這些規則同時給顧客點餐頁與 POS 使用，且必須與後端 process_checkout 一致，所以固定在這裡。
 */
import assert from 'node:assert/strict';
import { computeTotals, deliveryFee, maxPointsCash } from '../../static/js/shared/totals.js';
import { addonsFeasible, enforceAddonQuota, filterAddons, qualifiesForAddon, remainingAddonQuota } from '../../static/js/shared/addons.js';
import { channelPrice, comboChoiceInfo, pickSize, sizeDelta, unitPrice } from '../../static/js/shared/pricing.js';
import { applyLinePrices, lineKey, mergeLine, passRemaining } from '../../static/js/shared/cartline.js';
import { createPreviewScheduler, payloadLine } from '../../static/js/shared/orderpayload.js';
import { INVOICE_PLACEHOLDERS, invoicePayload } from '../../static/js/shared/invoice.js';

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ---------------------------------------------------------------- 金額
test('computeTotals：促銷 → 優惠券 → 會員折扣 → 點數 → 服務費 → 進位 → 外送費', () => {
    const r = computeTotals({
        subtotal: 1000, promotionDiscount: 100, couponDiscount: 50, tierPct: 10,
        points: { enabled: true, available: 500, requested: 500, ppd: 10, maxDiscount: 0 },
        servicePct: 10, serviceApplies: true, deliveryFee: 30, spendPerPoint: 100,
    });
    // 1000 - 100 - 50 = 850；會員 10% = 85 → 765；點數 500 點 = 50 元 → 715；服務費 71.5 → 72；外送 30
    assert.equal(r.promotionDiscount, 100);
    assert.equal(r.promo, 50);
    assert.equal(r.tier, 85);
    assert.equal(r.pointsDiscount, 50);
    assert.equal(r.pointsUsed, 500);
    assert.equal(r.service, 72);
    assert.equal(r.final, 715 + 72 + 30);
    assert.equal(r.earned, Math.floor((r.final - 30) / 100));
});

test('computeTotals：百分比優惠券留下 .5 時點數上限 = 四捨五入後的應付 (與後端相同)', () => {
    const r = computeTotals({ subtotal: 101, tierPct: 50, points: { enabled: true, available: 10000, requested: 10000, ppd: 10 } });
    // 101 * 50% = 50.5 → 上限 floor(50.5 + 0.5) = 51 元
    assert.equal(r.maxCash, 51);
    assert.equal(r.pointsDiscount, 51);
    assert.equal(r.final, 0);
    assert.equal(maxPointsCash(50.5, 0), 51);
    assert.equal(maxPointsCash(50.5, 20), 20);
});

test('computeTotals：促銷封鎖優惠券、整單折扣 (金額 / 百分比)、點數不能超過可用與輸入取整', () => {
    assert.equal(computeTotals({ subtotal: 200, promotionDiscount: 20, blocksCoupon: true, couponDiscount: 50 }).promo, 0);
    assert.equal(computeTotals({ subtotal: 200, manual: { type: 'amount', value: 500 } }).manual, 200);
    assert.equal(computeTotals({ subtotal: 200, manual: { type: 'percent', value: 25 } }).manual, 50);
    const r = computeTotals({ subtotal: 200, points: { enabled: true, available: 35, requested: 99.9, ppd: 10 } });
    assert.equal(r.maxPoints, 35);
    assert.equal(r.pointsRequested, 35);
    assert.equal(r.pointsDiscount, 3);
    assert.equal(r.pointsUsed, 30);
    assert.equal(computeTotals({ subtotal: 200, points: { enabled: false, available: 35, requested: 35, ppd: 10 } }).pointsDiscount, 0);
});

test('deliveryFee：達免運門檻免收', () => {
    assert.equal(deliveryFee(299, 60, 300), 60);
    assert.equal(deliveryFee(300, 60, 300), 0);
    assert.equal(deliveryFee(50, 60, 0), 60);
});

// ---------------------------------------------------------------- 加購配額
const items = [
    { id: 1, name: '牛肉堡', category: '主餐', price: 100 },
    { id: 2, name: '薯條', category: '點心', can_be_add_on: true, is_available: true, addon_trigger_type: 'any' },
    { id: 3, name: '可樂', category: '飲料', can_be_add_on: true, is_available: true, addon_trigger_type: 'category', addon_trigger_target: '主餐' },
    { id: 4, name: '售完加購', category: '點心', can_be_add_on: true, is_available: false },
    { id: 5, name: '只配牛肉堡', category: '點心', can_be_add_on: true, is_available: true, addon_trigger_type: 'item', addon_trigger_target: '牛肉堡' },
];
const byId = new Map(items.map((i) => [i.id, i]));
const rules = { categoryOf: (l) => byId.get(l.menu_item_id).category, addonDef: (id) => filterAddons(items).find((a) => a.id === Number(id)) };
const main = (qty, id = 1) => ({ menu_item_id: id, name: byId.get(id).name, quantity: qty });
const addon = (qty, id) => ({ menu_item_id: id, quantity: qty, is_add_on: true });

test('filterAddons / qualifiesForAddon：售完或關閉加購的不列入；any / category / item 觸發條件', () => {
    assert.deepEqual(filterAddons(items).map((a) => a.id), [2, 3, 5]);
    assert.equal(qualifiesForAddon(main(1), byId.get(2), rules.categoryOf), true);
    assert.equal(qualifiesForAddon(main(1), byId.get(3), rules.categoryOf), true);
    assert.equal(qualifiesForAddon(main(1), byId.get(5), rules.categoryOf), true);
    assert.equal(qualifiesForAddon(addon(1, 2), byId.get(2), rules.categoryOf), false, '加購品本身不是正價餐點');
    assert.equal(qualifiesForAddon({ ...main(1), is_reward: true }, byId.get(2), rules.categoryOf), false, '紅利兌換不算');
    assert.equal(qualifiesForAddon({ menu_item_id: 3, quantity: 1 }, byId.get(2), rules.categoryOf), false, '飲料不是主餐');
});

test('addonsFeasible / remainingAddonQuota：每份主餐最多配一份加購 (二分圖配對)', () => {
    const cart = [main(2)];
    assert.equal(remainingAddonQuota(cart, byId.get(2), rules), 2);
    assert.equal(addonsFeasible(cart, [byId.get(2), byId.get(3)], rules.categoryOf), true);
    assert.equal(addonsFeasible(cart, [byId.get(2), byId.get(3), byId.get(5)], rules.categoryOf), false);
    cart.push(addon(2, 2));
    assert.equal(remainingAddonQuota(cart, byId.get(3), rules), 0);
});

test('enforceAddonQuota：配不到的份數自動減量、售完的加購品歸零，回傳是否有調整', () => {
    const cart = [main(1), addon(2, 2), addon(1, 4)];
    const r = enforceAddonQuota(cart, rules);
    assert.equal(r.changed, true);
    assert.deepEqual(r.lines.map((l) => [l.menu_item_id, l.quantity]), [[1, 1], [2, 1]]);
    assert.equal(enforceAddonQuota([main(3), addon(2, 2)], rules).changed, false);
});

// ---------------------------------------------------------------- 單價
test('channelPrice / sizeDelta / comboChoiceInfo / unitPrice', () => {
    const item = { price: 100, prices: { takeout: 90, online: 0 }, member_prices: { dine_in: 80, takeout: 95 },
                   sizes: [{ name: 'M', delta: 0 }, { name: 'L', delta: 15 }] };
    assert.equal(channelPrice(item, 'dine_in', false), 100);
    assert.equal(channelPrice(item, 'dine_in', true), 80);
    assert.equal(channelPrice(item, 'takeout', true), 90, '會員價較高時取通路價');
    assert.equal(channelPrice(item, 'online', false), 100, '通路價 0 視為未設定');
    assert.equal(pickSize(item, 'XL'), 'M');
    assert.equal(sizeDelta(item, 'L'), 15);
    assert.equal(sizeDelta({ sizes: [] }, 'L'), 0);
    const combo = { price: 39, choices: [{ group: '飲料', options: [{ id: 7, name: '紅茶', extra_price: 0 }, { id: 8, name: '拿鐵', extra_price: 20 }] }] };
    assert.deepEqual(comboChoiceInfo(combo, ['8']), { ids: [8], extra: 20, labels: ['飲料:拿鐵(+20)'] });
    assert.equal(unitPrice({ item, channel: 'dine_in', isMember: true, size: 'L', combo, choiceIds: [8], modifierTotal: 10 }), 80 + 15 + 39 + 20 + 10);
    assert.equal(unitPrice({ item: { price: 10, sizes: [{ name: 'S', delta: -30 }] }, channel: 'dine_in' }), 0, '規格減價不得低於 0');
});

// ---------------------------------------------------------------- 購物車行
test('lineKey / mergeLine：加料順序不影響合併；規格 / 套餐 / 寄杯 / 紅利不同就不合併', () => {
    const a = { menu_item_id: 1, quantity: 1, modifier_ids: [3, 2], size: 'L', customization: 'x' };
    const b = { menu_item_id: 1, quantity: 2, modifier_ids: [2, 3], size: 'L', customization: 'x' };
    assert.equal(lineKey(a), lineKey(b));
    assert.notEqual(lineKey(a), lineKey({ ...a, size: 'M' }));
    assert.notEqual(lineKey(a), lineKey({ ...a, combo_option_id: 5 }));
    assert.notEqual(lineKey(a), lineKey({ ...a, pass_id: 9 }));
    assert.notEqual(lineKey(a), lineKey({ ...a, is_reward: true }));
    const cart = [a];
    assert.equal(mergeLine(cart, b), a);
    assert.equal(a.quantity, 3);
    assert.equal(cart.length, 1);
    mergeLine(cart, { ...b, size: 'M' });
    assert.equal(cart.length, 2);
});

test('applyLinePrices / passRemaining', () => {
    const cart = [{ price: 1, override_price: 50 }, { price: 2 }];
    assert.equal(applyLinePrices(cart, [10, 20], { skip: (l) => l.override_price !== undefined }), true);
    assert.deepEqual(cart.map((l) => l.price), [1, 20]);
    assert.equal(applyLinePrices(cart, [10]), false, '長度不符不套用');
    assert.equal(passRemaining([{ pass_id: 4, quantity: 2 }, { pass_id: 5, quantity: 9 }], { id: 4, remaining: 5 }), 3);
});

// ---------------------------------------------------------------- payload / 發票 / 預覽排程
test('payloadLine：含 combo_choices 與 pass_id (促銷預覽與結帳送同一份)；override_price 沒有時不出現在 JSON', () => {
    const p = payloadLine({ menu_item_id: 1, quantity: 2, modifier_ids: [1], combo_option_id: 3, combo_choices: [8], pass_id: 4, is_add_on: false, is_reward: false, customization: 'c' });
    assert.deepEqual(p, { menu_item_id: 1, quantity: 2, modifier_ids: [1], combo_option_id: 3, combo_choices: [8], pass_id: 4, is_add_on: false, is_reward: false, customization: 'c', size: '', override_price: undefined });
    assert.equal('override_price' in JSON.parse(JSON.stringify(p)), false);
    assert.deepEqual(payloadLine({ menu_item_id: 1, quantity: 1 }).combo_choices, []);
});

test('invoicePayload：統編 / 愛心碼 / 載具；未選類型為 null', () => {
    assert.deepEqual(invoicePayload('company', ' 12345678 '), { type: 'company', buyer_id: '12345678' });
    assert.deepEqual(invoicePayload('donate', '919'), { type: 'donate', love_code: '919' });
    assert.deepEqual(invoicePayload('mobile', '/ABC+123'), { type: 'mobile', carrier: '/ABC+123' });
    assert.equal(invoicePayload('', 'x'), null);
    assert.ok(INVOICE_PLACEHOLDERS.citizen && INVOICE_PLACEHOLDERS.mobile);
});

test('createPreviewScheduler：只有最新一次的 isCurrent() 為真；cancel 讓進行中的失效', async () => {
    const seen = [];
    const s = createPreviewScheduler(async (isCurrent) => { await new Promise((r) => setTimeout(r, 5)); seen.push(isCurrent()); }, 1);
    const first = s.run();
    const second = s.run();
    await Promise.all([first, second]);
    assert.deepEqual(seen, [false, true]);
    const third = s.run();
    s.cancel();
    await third;
    assert.equal(seen[2], false);
});

// ---------------------------------------------------------------- 執行
let failed = 0;
for (const [name, fn] of tests) {
    try {
        await fn();
        console.log(`ok   ${name}`);
    } catch (err) {
        failed++;
        console.log(`FAIL ${name}\n     ${err.message}`);
    }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
