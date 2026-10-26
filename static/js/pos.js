/*
 * 櫃檯 POS 收銀頁 (templates/pos.html) 的進入點 (ES module)：事件綁定與啟動。
 * 各功能拆在 static/js/pos/ (core 共用狀態與工具、menu 點餐與客製化、cart 購物車與加購、member 會員與寄杯、
 * totals 優惠與金額 / 付款方式、checkout 結帳與收據、live 掛單 / 當日訂單 / 桌位)。
 * - 菜單、加料、紅利設定與公開優惠券由 page-data 提供 (menu_service.storefront_payload)。
 * - 結帳送出結構化品項 {menu_item_id, quantity, modifier_ids, combo_option_id, is_add_on, is_reward, customization}，
 *   金額一律以後端計算為準，前端只做預覽。
 * - 所有動態內容以 Kiosk.html (自動跳脫) 或 textContent 寫入 DOM。
 */
import { $, afterHidden, itemMap, modalOf, state } from './pos/core.js';
import {
    chooseCombo, clearSearch, confirmModifier, filterMenu, pickItem, redeemItem, selectCategory, syncChoiceMods,
} from './pos/menu.js';
import { changeQty, clearCart, renderCart, triggerAddon } from './pos/cart.js';
import {
    bindMember, clearMember, lookupMember, openPassSell, openPinModal, selectMember, sellPass, submitPin,
    syncPassMethod, usePass,
} from './pos/member.js';
import {
    applyPromo, onInvoiceTypeChange, onOrderTypeChange, onPaymentChange, openCustomerDisplay, populateCouponSelect,
    quickCash, recalcTotals, setTable, togglePoints, updateCashChange,
} from './pos/totals.js';
import {
    addTender, applyLinePrice, applyOrderDiscount, openPriceModal, readTenders, resetLinePrice, showReceipt,
    submitOrder, syncTenderRows, toggleDeliveryMode,
} from './pos/checkout.js';
import {
    drawFloor, filterLiveOrders, getHeld, holdOrder, openHoldModal, openLiveOrders, openTables, recallHold,
    removeHold, reprint, saveHeld, serviceCallDone, setTablesView, settleTable, startLiveSync, syncLiveState,
    tableCardChanged, updateOrderStatus,
} from './pos/live.js';

// ======================================================================
// 11. 事件綁定
// ======================================================================
Kiosk.on(document, 'click', {
    category: (el) => selectCategory(el),
    clearSearch: () => clearSearch(),
    pickItem: (el) => pickItem(el),
    redeemItem: (el) => redeemItem(itemMap.get(Number(el.dataset.itemId))),
    chooseCombo: (el) => chooseCombo(el),
    confirmModifier: () => confirmModifier(),
    addon: (el) => triggerAddon(el),
    cartQty: (el) => changeQty(Number(el.dataset.idx), Number(el.dataset.delta)),
    clearCart: () => clearCart(),
    lookupMember: () => lookupMember(),
    selectMember: (el) => selectMember(el.dataset.id),
    clearMember: () => clearMember(),
    openPinModal: () => openPinModal(),
    applyPromo: () => applyPromo(),
    setTable: (el) => setTable(el.dataset.value),
    quickCash: (el) => quickCash(el.dataset.mode),
    holdOrder: () => holdOrder(),
    submitOrder: () => submitOrder(),
    openHold: () => openHoldModal(),
    recallHold: (el) => recallHold(Number(el.dataset.idx)),
    removeHold: (el) => removeHold(Number(el.dataset.idx)),
    openLiveOrders: () => openLiveOrders(),
    tablesView: (el) => setTablesView(el.dataset.view),
    usePass: (el) => usePass(el.dataset.passId),
    openPassSell: () => openPassSell(),
    addTender: () => addTender(),
    removeTender: (el) => {
        const rows = document.querySelectorAll('#posTenderRows .pos-tender-row');
        if (rows.length > 2) el.closest('.pos-tender-row').remove();
        syncTenderRows();
    },
    openTables: () => openTables(),
    settleTable: (el) => settleTable(el),
    liveFilter: (el) => filterLiveOrders(el),
    orderStatus: (el) => updateOrderStatus(el.dataset.id, el.dataset.status),
    reprint: (el) => reprint(el.dataset.id),
    printReceipt: () => window.print(),
    printReceiptNetwork: () => window.PosPro.networkPrint(state.lastOrderId),
    priceLine: (el) => openPriceModal(Number(el.dataset.idx)),
    resetLinePrice: () => resetLinePrice(),
    openOrderDiscount: () => {
        if (!state.cart.length) { alert('⚠️ 請先點選餐點'); return; }
        modalOf('posDiscountModal').show();
    },
    clearOrderDiscount: () => { state.orderDiscount = null; recalcTotals(); },
    discountPreset: (el) => {
        document.querySelector(`input[name="posDiscountType"][value="${el.dataset.type}"]`).checked = true;
        $('posDiscountValue').value = el.dataset.value;
    },
    toggleDeliveryMode: () => toggleDeliveryMode(),
    openCustomerDisplay: () => openCustomerDisplay(),
    openServiceCalls: () => { syncLiveState(); modalOf('posCallsModal').show(); },
    serviceCallDone: (el) => serviceCallDone(el.dataset.id),
});
$('posPriceForm').addEventListener('submit', applyLinePrice);
$('posDiscountForm').addEventListener('submit', applyOrderDiscount);
if ($('posInvoiceType')) $('posInvoiceType').addEventListener('change', onInvoiceTypeChange);
$('posModifierModalBody').addEventListener('change', (ev) => {
    if (ev.target.matches('.pos-choice')) syncChoiceMods();
});
$('posTenderRows').addEventListener('input', syncTenderRows);
if ($('posFloorArea')) $('posFloorArea').addEventListener('change', drawFloor);
$('posPassMethod').addEventListener('change', syncPassMethod);
$('posPassSellForm').addEventListener('submit', sellPass);
$('posTenderRows').addEventListener('change', syncTenderRows);
$('posTenderForm').addEventListener('submit', (ev) => {
    ev.preventDefault();
    state.tenders = readTenders();
    afterHidden('posTenderModal', () => submitOrder());
});
if ($('posRush')) $('posRush').addEventListener('change', () => {});

window.PosApp = { state, syncLiveState, bindMember, recalcTotals, renderCart, modalOf, showReceipt };

$('posSearchInput').addEventListener('input', filterMenu);
if ($('posTablesGrid')) {
    $('posTablesGrid').addEventListener('input', tableCardChanged);
    $('posTablesGrid').addEventListener('change', tableCardChanged);
}
$('posMemberQuery').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
        ev.preventDefault();
        lookupMember();
    }
});
$('posPromoInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
        ev.preventDefault();
        applyPromo();
    }
});
$('posMemberCouponSelect').addEventListener('change', (ev) => {
    $('posPromoInput').value = ev.target.value || '';
    applyPromo();
});
document.querySelectorAll('input[name="posOrderType"]').forEach((r) => r.addEventListener('change', onOrderTypeChange));
if ($('posHomeZone')) $('posHomeZone').addEventListener('change', recalcTotals);
$('posPaymentMethod').addEventListener('change', onPaymentChange);
$('posCashReceived').addEventListener('input', updateCashChange);
const pointsToggle = $('posUsePointsToggle');
if (pointsToggle) pointsToggle.addEventListener('change', togglePoints);
const pointsInput = $('posPointsInput');
if (pointsInput) pointsInput.addEventListener('input', recalcTotals);
$('posPinForm').addEventListener('submit', submitPin);
$('posPinModal').addEventListener('shown.bs.modal', () => $('posPinInput').focus());
// 收據顯示後聚焦「關閉」按鈕，按 Enter 即可關閉並接續下一筆
$('posReceiptModal').addEventListener('shown.bs.modal', () => $('rcpt-close-btn').focus());

document.addEventListener('keydown', (ev) => {
    if (ev.key === 'F2') {
        ev.preventDefault();
        submitOrder();
    } else if (ev.key === 'F4') {
        ev.preventDefault();
        holdOrder();
    } else if (ev.key === 'Escape' && document.activeElement === $('posSearchInput')) {
        clearSearch();
    }
});

// ======================================================================
// 啟動
// ======================================================================
Kiosk.startClock($('pos-clock'));
populateCouponSelect();
saveHeld(getHeld());
onPaymentChange();
renderCart();
syncLiveState();
startLiveSync();
