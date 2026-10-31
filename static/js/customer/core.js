/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：共用：頁面資料、常數、狀態 (state)、localStorage 購物車、價格通路、小工具。
 * 由 static/js/customer.js 載入 (ES module)；共用狀態與工具在 core.js。
 */
export const { html, api } = Kiosk;
export const CART_KEY = 'kiosk_cart';
export const IDLE_TIMEOUT_MS = 10 * 60 * 1000;   // 顧客閒置 10 分鐘自動登出
export const NON_MAIN_CATEGORIES = ['飲料', '點心', '副餐', '配餐'];
export const REWARD_LABEL = '[點數兌換]';

export const pageData = Kiosk.pageData();
export const pro = pageData.pro || {};
export const ui = pageData.ui || {};
export const REWARD_DISPLAY_LABEL = ui.reward_item_label || REWARD_LABEL;
// ---------------------------------------------------------------- localStorage (隱私模式可能無法使用)
export function loadCart() {
    try { return JSON.parse(localStorage.getItem(CART_KEY) || '[]'); } catch (err) { return []; }
}
export function storeCart(lines) {
    try { localStorage.setItem(CART_KEY, JSON.stringify(lines)); } catch (err) { /* 忽略 */ }
}
export function clearStoredCart() {
    try { localStorage.removeItem(CART_KEY); } catch (err) { /* 忽略 */ }
}

export const $ = (id) => document.getElementById(id);
export const modal = (id) => bootstrap.Modal.getOrCreateInstance($(id));

export const menu = pageData.menu || {};
export const rewardConfig = menu.reward || {};
export const isGuest = Boolean(pageData.isGuest);
// 線上點餐 (線上端)：必須先選取餐分店；取餐號由分店接單後才產生
export const isOnline = Boolean(pageData.online);
export const menuItems = new Map((menu.items || []).map((i) => [Number(i.id), i]));
export const modifierOptions = menu.modifiers || [];
export const modifierById = new Map(modifierOptions.map((m) => [Number(m.id), m]));
// 可加購的品項 (加購價 add_on_price；觸發條件與後端 _addon_qualifies 同一套規則)
export const availableAddons = (menu.items || []).filter((i) => i.can_be_add_on && i.is_available);
/** 跨模組共用的狀態 (不可在其他模組重新指定；請改 state.xxx) */
export const state = {
    userPoints: Number(pageData.userPoints) || 0,
    cart: [],                     // 購物車品項 (進入點啟動時由 localStorage 載入並正規化；格式見 cart.js)
    appliedPromoDiscount: 0,
    activePromoCode: '',
    pendingItem: null,            // 目前選擇套餐中的餐點
    pendingSelection: null,       // 目前客製化中的品項 { item, combo, isAddon, sides }
    checkoutOrderId: null,        // 本次結帳的 client_order_id (重送沿用，購物車變動後重新產生)
    submitting: false,
    myOrdersTimer: null,
    promotion: { discount: 0, names: [], gifts: [], blocks_coupon: false },
    promotionSeq: 0,
    promotionTimer: null,
    acceptedTotal: null,          // 上次送出時伺服器回報的金額 (顧客已確認)
    featureSort: null,
    priceOrder: null,
};

/** 價格通路：線上點餐 / 外帶 / 內用 (與後端 order_service._price_channel 相同) */
export function priceChannel() {
    if (isOnline) return 'online';
    const sel = $('orderType');
    return sel && sel.value === '外帶' ? 'takeout' : 'dine_in';
}
export function channelPrice(item) {
    return Number((item.prices && item.prices[priceChannel()]) || item.price) || 0;
}

export function toggleRow(rowId, valueId, value) {
    const row = $(rowId);
    if (!row) return;
    row.classList.toggle('d-none', !(value > 0));
    setText(valueId, Math.round(value));
}

export function setText(id, value) {
    const el = $(id);
    if (el) el.textContent = value;
}
