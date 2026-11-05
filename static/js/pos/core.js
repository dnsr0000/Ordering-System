/*
 * 櫃檯 POS 收銀頁 (pos.js 的模組)：共用：頁面資料、狀態 (state)、小工具。
 * 由 static/js/pos.js 載入 (ES module)；DOM 小工具 ($、modalOf、setHtml) 直接取自 common.js 的 Kiosk，
 * 跨頁面共用的純邏輯 (加購配額、單價、金額…) 在 static/js/shared/。
 */
import { channelPrice as sharedChannelPrice } from '../shared/pricing.js';

export const { html, setHtml, api, $ } = Kiosk;
export const modalOf = Kiosk.modal;

export const pageData = Kiosk.pageData();
export const itemMap = new Map((pageData.items || []).map((it) => [Number(it.id), it]));
export const modifierOptions = pageData.modifiers || [];
export const modifierById = new Map(modifierOptions.map((m) => [Number(m.id), m]));
export const rewardConfig = pageData.reward || {};
export const publicCoupons = pageData.publicCoupons || [];
export const posCfg = pageData.pos || {};
export const HOLD_STORAGE_KEY = `pos_held_orders_v2_tenant_${pageData.tenantId || 1}`;
export const REWARD_PREFIX = '🎁 [點數兌換] ';
export const SUBMIT_LABEL = html`<i class="bi bi-cash-stack me-1"></i> 收銀結帳 (F2)`;

export const state = {
    cart: [],              // 購物車品項 (見 makeLine)
    member: null,          // { id, name, phone, points, has_pin, coupons }
    category: 'all',
    promoCode: '',         // 已驗證成功的優惠碼
    promoDiscount: 0,
    comboItem: null,       // 正在選擇套餐的主餐
    pending: null,         // 正在客製化的品項 { item, combo, mode: normal | reward | addon }
    finalTotal: 0,
    checkout: null,        // { id: client_order_id, sig: 結帳內容 }：內容不變的重送沿用同一個 id，確保只建立一張單
    submitting: false,
    liveOrders: [],
    unpaidTables: [],      // 桌邊掃碼點餐尚未結帳的桌位
    liveFilter: 'all',
    orderDiscount: null,   // 整單折扣 { type: amount | percent, value }
    promotion: { discount: 0, names: [], gifts: [], blocks_coupon: false },
    deliveryMode: false,   // 外送單登錄模式 (平台代收)
    lastOrderId: null,
    totals: {},
};

// ======================================================================
// 小工具
// ======================================================================
export const num = (v) => Number(v) || 0;
export const sumBy = (list, fn) => list.reduce((s, x) => s + fn(x), 0);
export const isItemAvailable = (id) => {
    const it = itemMap.get(Number(id));
    return Boolean(it && it.is_available);
};
export const cartSubtotal = () => sumBy(state.cart, (l) => l.price * l.quantity);
export const cartRewardPoints = () => sumBy(state.cart.filter((l) => l.is_reward), (l) => l.points_cost * l.quantity);
export const displayName = (line) => (line.is_reward ? REWARD_PREFIX : '') + line.name;

/** 目前的價格通路：外送單 > 外帶 > 內用 (與後端 order_service._price_channel 相同) */
export function channel() {
    if (state.deliveryMode) return 'delivery';
    const takeout = $('posTypeTakeout');
    return takeout && takeout.checked ? 'takeout' : 'dine_in';
}
/** 綁定會員且品項有會員價 (member_prices) 時取較低者 (shared/pricing.js，與後端 MenuItem.price_for(member=True) 相同) */
export const channelPrice = (item) => sharedChannelPrice(item, channel(), Boolean(state.member));

/** 一個 Modal 關閉動畫結束後再執行 (避免兩個 Modal 交疊造成背景遮罩殘留) */
export function afterHidden(id, fn) {
    Kiosk.hideModalThen($(id), fn);
}
