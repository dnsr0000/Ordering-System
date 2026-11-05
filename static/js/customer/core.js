/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：共用：頁面資料、常數、狀態 (state)、localStorage 購物車、價格通路。
 * 由 static/js/customer.js 載入 (ES module)；DOM 小工具 ($、modal、setText、toggleRow) 直接取自 common.js 的 Kiosk，
 * 跨頁面共用的純邏輯在 static/js/shared/。
 */
import { filterAddons } from '../shared/addons.js';
import { channelPrice as sharedChannelPrice } from '../shared/pricing.js';

export const { html, api, $, modal, setText, toggleRow } = Kiosk;
export const IDLE_TIMEOUT_MS = 10 * 60 * 1000;   // 顧客閒置 10 分鐘自動登出
export const REWARD_LABEL = '[點數兌換]';

export const pageData = Kiosk.pageData();
export const pro = pageData.pro || {};
export const ui = pageData.ui || {};
// ---------------------------------------------------------------- localStorage 'kiosk_cart' (Kiosk.store：隱私模式時不會拋錯)
export function loadCart() {
    return Kiosk.store.get(Kiosk.CART_KEY, []) || [];
}
export function storeCart(lines) {
    Kiosk.store.set(Kiosk.CART_KEY, lines);
}
export function clearStoredCart() {
    Kiosk.store.remove(Kiosk.CART_KEY);
}

export const menu = pageData.menu || {};
export const rewardConfig = menu.reward || {};
export const isGuest = Boolean(pageData.isGuest);
// 線上點餐 (線上端)：必須先選取餐分店；取餐號由分店接單後才產生
export const isOnline = Boolean(pageData.online);
export const menuItems = new Map((menu.items || []).map((i) => [Number(i.id), i]));
export const modifierOptions = menu.modifiers || [];
export const modifierById = new Map(modifierOptions.map((m) => [Number(m.id), m]));
/** 可加購的品項 (加購價 add_on_price)：每次呼叫重新過濾，售完 (live.js 即時更新) 的加購品就不再推薦 */
export const availableAddons = () => filterAddons(menuItems.values());
/** 品項目前可點 (售完 / 停售的品項不可；不在菜單裡的品項以 fallback 判斷) */
export const isItemAvailable = (id, fallback = false) => {
    const it = menuItems.get(Number(id));
    return it ? Boolean(it.is_available) : fallback;
};
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
    acceptedTotal: null,          // 上次送出時伺服器回報的金額 (顧客已確認)
    featureSort: null,
    priceOrder: null,
    eta: 0,                       // 預估等候分鐘數 (/api/eta；0 = 未啟用或購物車為空)
    dietFilter: null,             // 飲食篩選 { veg, mild, allergens[] } (menu.js 由 localStorage 載入)
    giftCard: null,               // 查過餘額的禮物卡 { code, balance } (付款方式選禮物卡時送 payment_info.gift_code)
    lastOrderId: null,            // 剛結帳的訂單 (收據視窗寄電子收據用)
};

/** 價格通路：線上點餐 / 外帶 / 內用 (與後端 order_service._price_channel 相同) */
export function priceChannel() {
    if (isOnline) return 'online';
    const sel = $('orderType');
    return sel && sel.value === '外帶' ? 'takeout' : 'dine_in';
}
/** 登入會員 (非訪客) 且品項有會員價 (member_prices) 時，取會員價與通路價較低者 (shared/pricing.js) */
export function channelPrice(item) {
    return sharedChannelPrice(item, priceChannel(), !isGuest);
}
