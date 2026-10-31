/*
 * 顧客自助點餐 (Kiosk) 頁面 (templates/customer.html) 的進入點 (ES module)：事件綁定與啟動。
 * 各功能拆在 static/js/customer/ (core 共用狀態與工具、cart 購物車與加購、totals 促銷 / 金額 / 優惠券 / 點數、
 * menu 套餐 / 客製化 / 寄杯 / 篩選排序、checkout 結帳 / 收據 / 桌邊帳單 / 我的訂單)。
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
    callService, confirmModifiers, cyclePriceSort, filterMenu, pickCombo, resetFilters, startAdd, syncChoiceMods,
    toggleFeatureSort, triggerAddon, usePass,
} from './customer/menu.js';
import {
    INVOICE_PLACEHOLDERS, confirmCheckout, fetchMyOrders, finishOrder, openPreview, openTableBill, payTableBill,
    showMyOrders,
} from './customer/checkout.js';

// 桌邊呼叫服務 (掃桌位 QR code 後，登入前後都可使用)
Kiosk.on(document, 'click', { 'call-service': (el) => callService(el) });

// 未登入的歡迎頁：清掉上一位顧客留下的購物車 (登出、閒置登出都會回到這裡)，其餘功能不啟動
if (pageData.loggedIn) {
    main();
} else {
    clearStoredCart();
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
        'logout': () => clearStoredCart(),
        'sort': (el) => toggleFeatureSort(el.dataset.mode),
        'price-sort': () => cyclePriceSort(),
        'reset-filters': () => resetFilters(),
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
        if (!state.myOrdersTimer) state.myOrdersTimer = setInterval(fetchMyOrders, 3000);
    });
    $('historyModal').addEventListener('hidden.bs.modal', () => {
        clearInterval(state.myOrdersTimer);
        state.myOrdersTimer = null;
    });
    // ================================================================ 初始化
    state.cart = loadCart().map(normalizeLine).filter(Boolean);
    document.querySelectorAll('#menuList .menu-row').forEach((row, i) => { row.dataset.order = i; });
    renderCoupons(pageData.coupons);
    storeCart(state.cart);   // 寫回正規化後的格式
    repriceCart();     // 購物車留在瀏覽器的是加入當時的價格：以目前菜單重算 (店家可能已改價)
    // 點餐機無人操作自動登出；桌邊掃碼點餐是顧客自己的手機，不自動登出
    if (!pageData.table) Kiosk.idleLogout({ timeoutMs: IDLE_TIMEOUT_MS });
}
