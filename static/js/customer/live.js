/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：菜單「可點 / 售完 / 價格」的即時更新。
 * 每 30 秒向 /api/menu/availability 輪詢 (帶 If-None-Match；沒變化時伺服器回 304、這裡什麼都不做)，
 * 有變化時只改動卡片的售完標示、按鈕與 menuItems 的 is_available / 價格，不重新整理頁面。
 */
import { $, html, isGuest, menuItems, pageData, priceChannel, state, ui } from './core.js';
import { repriceCart } from './cart.js';

const POLL_MS = 30000;
const pollState = {};   // Kiosk.pollJson 保存 ETag
function soldOutBadge(hint) {
    const span = document.createElement('span');
    span.className = 'badge bg-secondary ms-1 live-sold-out';
    span.textContent = hint || ui.sold_out || '已售完';
    return span;
}

/** 目前價格通路 (線上端 online；門市依用餐方式 dine_in / takeout) 的一般價，與樣板 price_for(channel) 相同 */
function channelBase(item) {
    return Number((item.prices && item.prices[priceChannel()]) || item.price);
}

/** 會員看到的會員價 (與樣板 member_deal(channel) 相同：目前通路的會員價比一般價低時才顯示)；訪客或沒有會員價時為 null */
function memberDeal(item) {
    const ch = priceChannel();
    const base = channelBase(item);
    if (isGuest || !item.member_prices || item.member_prices[ch] === undefined) return null;
    const deal = Number(item.member_prices[ch]);
    return deal < base ? deal : null;
}

/** 卡片的排序 / 篩選價 (data-price)：與樣板 price_for(channel, 會員) 相同 */
function cardPrice(item) {
    const deal = memberDeal(item);
    return String(deal !== null ? deal : channelBase(item));
}

/** 卡片上的價格 (與 templates/customer.html 菜單卡片相同的三種樣式：會員價 / 特價 / 一般價) */
function priceHtml(item) {
    const price = Number(item.price);
    const base = channelBase(item);
    const deal = memberDeal(item);
    if (deal !== null) {
        return html`<del class="text-white-50 small me-1">$${base}</del>
            <span class="text-info fw-bold fs-5">$${deal}</span>
            <span class="badge bg-info text-dark">${ui.member_price_label || '會員價'}</span>`;
    }
    if (Number(item.original_price) > price) {
        return html`<del class="text-white-50 small me-1">$${item.original_price}</del>
            <span class="text-danger fw-bold fs-5">$${price}</span>`;
    }
    return html`<span class="text-success fw-bold fs-5">$${base}</span>`;
}

/**
 * 價格相關欄位 (單價、原價、各通路價、會員價) 的比對用字串：任何一項變了就要重畫卡片價格並重算購物車
 * (頁面載入的菜單與輪詢回應的通路順序不同，依通路名稱排序後再比)
 */
const sortedPrices = (p) => (p ? Object.keys(p).sort().map((ch) => [ch, Number(p[ch])]) : null);
const priceKey = (item) => JSON.stringify([Number(item.price), Number(item.original_price), sortedPrices(item.prices), sortedPrices(item.member_prices)]);

function applyCard(row, item, info, priceChanged) {
    const card = row.querySelector('.menu-card');
    const img = row.querySelector('.menu-img');
    const name = row.querySelector('.menu-name');
    const btn = row.querySelector('.add-to-cart-btn, button[disabled]');
    if (!card || !btn) return;
    const available = Boolean(info.available);
    card.classList.toggle('menu-card-sold-out', !available);
    if (img) img.classList.toggle('img-sold-out', !available);
    if (name) {
        name.querySelectorAll('.live-sold-out, .badge.bg-secondary.ms-1').forEach((b) => b.remove());
        if (!available) name.appendChild(soldOutBadge(info.hint));
    }
    if (available) {
        btn.className = 'btn add-to-cart-btn btn-sm';
        btn.disabled = false;
        btn.dataset.action = 'add';
        btn.dataset.id = String(item.id);
        Kiosk.setHtml(btn, html`<i class="bi bi-cart-plus"></i> ${ui.add_to_cart || '加入購物車'}`);
    } else {
        btn.className = 'btn btn-secondary btn-sm';
        btn.disabled = true;
        delete btn.dataset.action;
        Kiosk.setHtml(btn, html`<i class="bi bi-slash-circle"></i> ${info.hint || ui.sold_out || '已售完'}`);
    }
    if (priceChanged) {
        // 會員看到的是會員價那一格 (後面掛「會員價」徽章)：依會員身分整組重畫，不可只改第一個 .fs-5
        const priceEl = row.querySelector('.fs-5');
        if (priceEl && priceEl.parentElement) Kiosk.setHtml(priceEl.parentElement, priceHtml(item));
        row.dataset.price = cardPrice(item);
    }
}

/** 切換用餐方式 (內用 / 外帶) 後：依新的價格通路重畫菜單卡片價格與 data-price (價格篩選 / 排序用) */
export function refreshCardPrices() {
    document.querySelectorAll('#menuList .menu-row').forEach((row) => {
        const item = menuItems.get(Number(row.dataset.id));
        if (!item) return;
        const priceEl = row.querySelector('.fs-5');
        if (priceEl && priceEl.parentElement) Kiosk.setHtml(priceEl.parentElement, priceHtml(item));
        row.dataset.price = cardPrice(item);
    });
}

/** 快速加入卡片 (熱門 / 常點)：售完時拿掉點擊動作並加上售完徽章，恢復供應時還原 */
function applyQuickCard(card, item, info) {
    const available = Boolean(item.is_available);
    card.classList.toggle('card-sold-out', !available);
    card.classList.toggle('card-available', available);
    if (available) {
        card.dataset.action = 'add';
        card.setAttribute('role', 'button');
        card.setAttribute('tabindex', '0');
    } else {
        delete card.dataset.action;
        card.removeAttribute('role');
        card.removeAttribute('tabindex');
    }
    const name = card.querySelector('.fw-bold');
    if (name) {
        name.querySelectorAll('.live-sold-out, .badge.bg-secondary.ms-1').forEach((b) => b.remove());
        if (!available) name.appendChild(soldOutBadge(info && info.hint));
    }
}

export function applyAvailability(payload) {
    let changed = 0;
    Object.entries(payload).forEach(([id, info]) => {
        const item = menuItems.get(Number(id));
        if (!item) return;
        const wasAvailable = item.is_available;
        const wasPrice = priceKey(item);
        item.is_available = Boolean(info.available);
        item.price = Number(info.price);
        item.original_price = Number(info.original_price);
        if (info.prices) item.prices = info.prices;
        if (info.member_prices) item.member_prices = info.member_prices;
        const priceChanged = wasPrice !== priceKey(item);
        const row = document.querySelector(`#menuList .menu-row[data-id="${id}"]`);
        if (row) applyCard(row, item, info, priceChanged);
        // 只改外帶價 / 線上價 / 會員價 (內用單價不變) 時也要重算購物車
        if (wasAvailable !== item.is_available || priceChanged) changed += 1;
    });
    // 快速加入卡片 (熱門 / 常點) 與購物車裡的售完標示
    document.querySelectorAll('.quick-card[data-id]').forEach((card) => {
        const item = menuItems.get(Number(card.dataset.id));
        if (!item) return;
        applyQuickCard(card, item, payload[card.dataset.id]);
    });
    if (changed) {
        state.cart.forEach((l) => {
            const item = menuItems.get(l.menu_item_id);
            if (item) l.is_sold_out = !item.is_available;
        });
        repriceCart();
    }
    return changed;
}

export async function pollAvailability() {
    try {
        const payload = await Kiosk.pollJson('/api/menu/availability', pollState);   // 304 (沒變化) 時回傳 null
        if (payload) applyAvailability(payload);
    } catch (err) { /* 離線或伺服器錯誤時下一輪再試 */ }
}

export function startAvailabilityPolling() {
    if (!pageData.loggedIn || !$('menuList')) return;
    Kiosk.poll(pollAvailability, POLL_MS);   // 背景時暫停、切回來立即更新一次
}
