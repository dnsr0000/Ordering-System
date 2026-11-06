/*
 * 顧客自助點餐 (Kiosk) 頁面 (templates/customer.html) 的進入點 (ES module)：事件綁定與啟動。
 * 各功能拆在 static/js/customer/ (core 共用狀態與工具、cart 購物車與加購、totals 促銷 / 金額 / 優惠券 / 點數、
 * menu 套餐 / 客製化 / 寄杯 / 篩選排序、checkout 結帳 / 收據 / 桌邊帳單 / 我的訂單 / 再點一次、live 售完即時更新、voice 語音點餐)。
 * 與 POS 共用的純邏輯在 static/js/shared/ (addons 加購配額、pricing 單價、cartline 合併 / 寄杯、modifiers 客製化視窗、
 * totals 金額、orderpayload 品項結構 / 預覽排程、promo 優惠碼驗證、invoice 發票、combos 套餐卡片)；
 * DOM 小工具與徽章 / 金額格式取自 common.js 的 Kiosk。
 * 菜單資料由後端以 JSON 嵌入 (page-data)，畫面上的使用者資料一律經 Kiosk.html 跳脫後才插入 DOM。
 *
 * 購物車存在 localStorage 'kiosk_cart' (紅利商城兌換的商品也會寫進來)，每一列格式：
 *   { menu_item_id, name, price(單價估算), quantity, customization, modifier_ids: [], combo_option_id,
 *     combo_name, is_add_on, is_reward, points_cost, can_addon, is_sold_out }
 * 金額僅供畫面預估，實際金額一律由後端依 menu_item_id / modifier_ids / combo_option_id 重新計算。
 */
import { $, IDLE_TIMEOUT_MS, clearStoredCart, loadCart, pageData, state, storeCart } from './customer/core.js';
import { changeQty, normalizeLine, removeLine, repriceCart } from './customer/cart.js';
import { applyPromoCode, calculateTotal, renderCoupons, syncLatestUserCoupons, togglePoints } from './customer/totals.js';
import {
    callService, confirmModifiers, cyclePriceSort, filterMenu, pickCombo, resetFilters, startAdd, syncChoiceMods, toggleDietFilter,
    toggleFeatureSort, triggerAddon, usePass,
} from './customer/menu.js';
import {
    checkGiftCard, confirmCheckout, fetchMyOrders, finishOrder, openPreview, openTableBill, payTableBill, reorder, sendReceipt,
    showMyOrders, togglePaymentExtras,
} from './customer/checkout.js';
import { startAvailabilityPolling } from './customer/live.js';
import { voiceActions } from './customer/voice.js';
import { INVOICE_PLACEHOLDERS } from './shared/invoice.js';

const MY_ORDERS_POLL_MS = 8000;   // 「我的訂單」視窗開著時的更新間隔 (顧客頁用輪詢，不用 SSE)

// 桌邊呼叫服務 (掃桌位 QR code 後，登入前後都可使用)
Kiosk.on(document, 'click', { 'call-service': (el) => callService(el) });
Kiosk.on(document, 'click', voiceActions);   // 語音點餐 (後台可關閉；沒有按鈕時不會觸發)

// 未登入的歡迎頁：清掉上一位顧客留下的購物車與飲食 / 過敏原篩選 (登出、閒置登出都會回到這裡)，其餘功能不啟動
if (pageData.loggedIn) {
    main();
} else {
    clearStoredCart();
    Kiosk.store.remove('kiosk_diet_filter');   // customer/menu.js 的 DIET_KEY：下一位顧客不沿用 (也不洩漏) 上一位的過敏原選擇
}

function main() {
    // ================================================================ 事件綁定
    Kiosk.on(document, 'click', {
        'add': (el) => startAdd(el.dataset.id),
        'pick-combo': (el) => pickCombo(el.dataset.comboId),
        'use-pass': (el) => usePass(el.dataset.passId),
        'table-bill': () => openTableBill(),
        'table-pay': () => payTableBill(),
        'confirm-modifiers': () => confirmModifiers(),
        'addon': (el) => triggerAddon(el.dataset.id),
        'qty': (el) => changeQty(Number(el.dataset.index), Number(el.dataset.delta)),
        'remove': (el) => removeLine(Number(el.dataset.index)),
        'apply-promo': () => applyPromoCode(),
        'preview': () => openPreview(),
        'checkout': () => confirmCheckout(),
        'finish': () => finishOrder(),
        'my-orders': () => showMyOrders(),
        'reorder': (el) => reorder(el.dataset.id),
        'logout': () => clearStoredCart(),
        'sort': (el) => toggleFeatureSort(el.dataset.mode),
        'price-sort': () => cyclePriceSort(),
        'reset-filters': () => resetFilters(),
        'diet-filter': (el) => toggleDietFilter(el),
        'gift-card-check': () => checkGiftCard(),
        'receipt-email': () => sendReceipt('email'),
        'receipt-line': () => sendReceipt('line'),
    });
    Kiosk.on(document, 'input', {
        'filter': () => filterMenu(),
        'recalc': () => calculateTotal(),
    });
    Kiosk.on(document, 'change', {
        'filter': () => filterMenu(),
        'pick-coupon': (el) => {
            $('promoInput').value = el.value;
            applyPromoCode();
        },
        'toggle-points': () => togglePoints(),
        'payment-method': () => togglePaymentExtras(),
        'order-type': (el) => {
            $('takeoutExtraBox').classList.toggle('d-none', el.value !== '外帶' && el.value !== '外送到府');
            if ($('homeDeliveryBox')) $('homeDeliveryBox').classList.toggle('d-none', el.value !== '外送到府');
            repriceCart();
        },
        'recalc-delivery': () => calculateTotal(),
    });
    if ($('invoiceType')) {
        $('invoiceType').addEventListener('change', () => {
            const type = $('invoiceType').value;
            $('invoiceValue').classList.toggle('d-none', !INVOICE_PLACEHOLDERS[type]);
            $('invoiceValue').placeholder = INVOICE_PLACEHOLDERS[type] || '';
        });
    }
    // 卡片以鍵盤操作 (Enter / 空白鍵) 等同點擊
    document.addEventListener('keydown', (ev) => {
        if ((ev.key === 'Enter' || ev.key === ' ') && ev.target instanceof Element && ev.target.matches('[role="button"][data-action]')) {
            ev.preventDefault();
            ev.target.click();
        }
    });

    $('cart-sidebar').addEventListener('show.bs.offcanvas', syncLatestUserCoupons);
    $('modifierModal').addEventListener('hidden.bs.modal', () => { state.pendingSelection = null; });
    $('modifierModalBody').addEventListener('change', (ev) => {
        if (ev.target.matches('[data-mod="choice"]')) syncChoiceMods();
    });
    $('historyModal').addEventListener('shown.bs.modal', () => {
        if (!state.myOrdersTimer) state.myOrdersTimer = Kiosk.poll(fetchMyOrders, MY_ORDERS_POLL_MS);
    });
    $('historyModal').addEventListener('hidden.bs.modal', () => {
        if (state.myOrdersTimer) state.myOrdersTimer.stop();
        state.myOrdersTimer = null;
    });
    // ================================================================ 初始化
    state.cart = loadCart().map(normalizeLine).filter(Boolean);
    document.querySelectorAll('#menuList .menu-row').forEach((row, i) => { row.dataset.order = i; });
    if ($('dietFilterBar')) filterMenu();   // 套用上次記住的飲食篩選
    renderCoupons(pageData.coupons);
    storeCart(state.cart);   // 寫回正規化後的格式
    repriceCart();     // 購物車留在瀏覽器的是加入當時的價格：以目前菜單重算 (店家可能已改價)
    // 點餐機無人操作自動登出；桌邊掃碼點餐是顧客自己的手機，不自動登出
    if (!pageData.table) Kiosk.idleLogout({ timeoutMs: IDLE_TIMEOUT_MS });
    startAvailabilityPolling();   // 售完 / 恢復供應 / 改價不必重新整理 (每 30 秒，304 時不重畫)
}
