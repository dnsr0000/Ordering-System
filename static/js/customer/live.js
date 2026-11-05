/*
 * 顧客自助點餐 (Kiosk) 頁面 (customer.js 的模組)：菜單「可點 / 售完 / 價格」的即時更新。
 * 每 30 秒向 /api/menu/availability 輪詢 (帶 If-None-Match；沒變化時伺服器回 304、這裡什麼都不做)，
 * 有變化時只改動卡片的售完標示、按鈕與 menuItems 的 is_available / 價格，不重新整理頁面。
 */
import { $, html, menuItems, pageData, state, ui } from './core.js';
import { repriceCart } from './cart.js';

const POLL_MS = 30000;
const pollState = {};   // Kiosk.pollJson 保存 ETag
function soldOutBadge(hint) {
    const span = document.createElement('span');
    span.className = 'badge bg-secondary ms-1 live-sold-out';
    span.textContent = hint || ui.sold_out || '已售完';
    return span;
}

function applyCard(row, item, info) {
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
    if (Number(item.price) !== Number(info.price)) {
        const priceEl = row.querySelector('.fs-5');
        if (priceEl) priceEl.textContent = `$${info.price}`;
        row.dataset.price = String(info.price);
    }
}

export function applyAvailability(payload) {
    let changed = 0;
    Object.entries(payload).forEach(([id, info]) => {
        const item = menuItems.get(Number(id));
        if (!item) return;
        const was = { available: item.is_available, price: item.price };
        const row = document.querySelector(`#menuList .menu-row[data-id="${id}"]`);
        if (row) applyCard(row, item, info);
        item.is_available = Boolean(info.available);
        item.price = Number(info.price);
        item.original_price = Number(info.original_price);
        if (info.prices) item.prices = info.prices;
        if (was.available !== item.is_available || was.price !== item.price) changed += 1;
    });
    // 快速加入卡片 (熱門 / 常點) 與購物車裡的售完標示
    document.querySelectorAll('.quick-card[data-id]').forEach((card) => {
        const item = menuItems.get(Number(card.dataset.id));
        if (!item) return;
        card.classList.toggle('card-sold-out', !item.is_available);
        card.classList.toggle('card-available', item.is_available);
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
    if (document.hidden) return;
    try {
        const payload = await Kiosk.pollJson('/api/menu/availability', pollState);   // 304 (沒變化) 時回傳 null
        if (payload) applyAvailability(payload);
    } catch (err) { /* 離線或伺服器錯誤時下一輪再試 */ }
}

export function startAvailabilityPolling() {
    if (!pageData.loggedIn || !$('menuList')) return;
    setInterval(pollAvailability, POLL_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) pollAvailability(); });
}
