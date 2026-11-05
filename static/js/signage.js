/*
 * 數位看板 (門市上方大螢幕，免登入)：菜單與售完標示、促銷標語輪播、取餐 / 候位叫號。
 * 資料來自 /api/signage (帶 If-None-Match；沒變動時 304、不重畫)；訂單 / 售完 / 菜單修改由 SSE 通知後重新抓取，
 * 另外每 60 秒抓一次作為保險 (供應時段到點、SSE 斷線期間)。
 * 一個分類放不下一欄時依實際量到的欄高拆成「主餐 1/2、主餐 2/2」；欄位超過一頁時依設定秒數自動換頁。
 */
(function () {
    'use strict';

    const { html } = Kiosk;
    const config = Kiosk.pageData();
    const columns = Math.max(1, Number(config.columns) || 3);
    const pageMs = Math.max(3, Number(config.pageSeconds) || 10) * 1000;
    const promoMs = Math.max(3, Number(config.promoSeconds) || 8) * 1000;
    const FALLBACK_POLL_MS = 60000;

    const root = document.getElementById('signage');
    const main = document.getElementById('sgMain');
    const pagesEl = document.getElementById('sgPages');
    const promoBox = document.getElementById('sgPromo');
    const promoText = document.getElementById('sgPromoText');
    const readyEl = document.getElementById('sgReady');
    const calledEl = document.getElementById('sgCalled');
    const intakeEl = document.getElementById('sgIntake');
    const intakeText = document.getElementById('sgIntakeText');
    const placeholder = root.dataset.placeholder || '';

    root.style.setProperty('--sg-columns', String(columns));

    const poll = {};              // Kiosk.pollJson 保存的 ETag
    let categories = [];
    let promos = [];
    let page = 0;
    let promoIndex = 0;
    let capacity = Infinity;      // 一欄放得下幾個品項 (渲染後量測)
    let lastMenuSignature = null;
    let lastPickupSignature = null;

    const chunk = (list, size) => {
        const out = [];
        for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
        return out;
    };

    // ---------------------------------------------------------------- 菜單
    function tags(item) {
        const list = [];
        if (item.is_new) list.push(html`<span class="sg-tag sg-tag-new">新品</span>`);
        if (item.is_popular) list.push(html`<span class="sg-tag sg-tag-hot">人氣</span>`);
        if (item.spicy_level > 0) list.push(html`<span class="sg-tag sg-tag-spicy">${'辣'.repeat(Math.min(3, item.spicy_level))}</span>`);
        if (item.diet) list.push(html`<span class="sg-tag sg-tag-veg">素</span>`);
        return list.length ? html`<span class="sg-tags">${list}</span>` : '';
    }

    function stamp(item) {
        if (item.available) return '';
        if (item.hint) return html`<span class="sg-stamp is-hint">${item.hint}</span>`;
        return html`<span class="sg-stamp">售完<small>SOLD OUT</small></span>`;
    }

    function thumb(item) {
        if (!config.showImages || !item.image) return html`<span class="sg-thumb-blank"></span>`;
        return html`<img class="sg-thumb" src="${item.image}" data-fallback="${placeholder}" alt="" loading="lazy">`;
    }

    function renderItem(item) {
        return html`
            <div class="sg-item${item.available ? '' : ' is-sold-out'}" data-item-id="${item.id}">
                ${thumb(item)}
                <div class="sg-item-main">
                    <div class="sg-item-name">${item.name}${tags(item)}</div>
                    ${item.name_en ? html`<div class="sg-item-en">${item.name_en}</div>` : ''}
                </div>
                <div class="sg-price">$${item.price}${item.original_price ? html`<span class="sg-price-original">$${item.original_price}</span>` : ''}</div>
                ${stamp(item)}
            </div>`;
    }

    function renderPanel(panel) {
        return html`
            <section class="sg-category" data-category="${panel.name}">
                <div class="sg-category-head">
                    <span class="sg-category-name">${panel.name}</span>
                    ${panel.name_en ? html`<span class="sg-category-en">${panel.name_en}</span>` : ''}
                    ${panel.parts > 1 ? html`<span class="sg-category-part">${panel.part}/${panel.parts}</span>` : ''}
                </div>
                <div class="sg-items">${panel.items.map(renderItem)}</div>
            </section>`;
    }

    /** 分類 → 欄位：品項比一欄放得下的多時拆成數欄 */
    function panels() {
        const out = [];
        categories.forEach((cat) => {
            const parts = chunk(cat.items, Number.isFinite(capacity) ? capacity : cat.items.length || 1);
            parts.forEach((items, i) => out.push({ name: cat.name, name_en: cat.name_en, items, part: i + 1, parts: parts.length }));
        });
        return out;
    }

    /**
     * 量測目前畫面的欄高與品項高度，算出一欄能放幾個；數字變小就回傳 true (需要重新排版)。
     * 只會往下修 (不同頁的品項高度略有差異，允許變大會來回重排)；視窗大小改變時由 relayout 歸零重算。
     */
    function measure() {
        const list = main.querySelector('.sg-items');
        const items = main.querySelectorAll('.sg-item');
        if (!list || !items.length) return false;
        const style = getComputedStyle(list);
        const usable = list.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
        const tallest = Math.max(...Array.from(items, (el) => el.offsetHeight));
        const next = Math.max(1, Math.floor(usable / tallest));
        if (next >= capacity) return false;
        capacity = next;
        return true;
    }

    function relayout() {
        capacity = Infinity;
        page = 0;
        renderPage();
    }

    function renderPage() {
        const pages = chunk(panels(), columns);
        if (!pages.length) {
            Kiosk.setHtml(main, html`<div class="sg-empty"><i class="bi bi-cup-hot"></i><div>目前沒有可顯示的餐點</div></div>`);
            pagesEl.classList.add('d-none');
            return;
        }
        if (page >= pages.length) page = 0;
        Kiosk.setHtml(main, pages[page].map(renderPanel));
        pagesEl.classList.toggle('d-none', pages.length < 2);
        Kiosk.setHtml(pagesEl, pages.map((_p, i) => html`<span class="sg-page-dot${i === page ? ' is-active' : ''}"></span>`));
        if (measure()) renderPage();   // 第一次渲染後才知道欄高：需要拆欄時重排一次
    }

    function nextPage() {
        const total = Math.ceil(panels().length / columns);
        if (total < 2) return;
        page = (page + 1) % total;
        renderPage();
    }

    // ---------------------------------------------------------------- 促銷標語
    function showPromo() {
        promoBox.classList.toggle('d-none', promos.length === 0);
        if (!promos.length) return;
        if (promoIndex >= promos.length) promoIndex = 0;
        promoText.textContent = promos[promoIndex];
    }

    function nextPromo() {
        if (promos.length < 2) return;
        promoText.classList.add('is-fading');
        setTimeout(() => {
            promoIndex = (promoIndex + 1) % promos.length;
            showPromo();
            promoText.classList.remove('is-fading');
        }, 400);
    }

    // ---------------------------------------------------------------- 取餐叫號
    function renderPickup(pickup) {
        if (!readyEl || !pickup) return;
        const sig = `${pickup.ready.map((o) => o.id).join(',')}|${pickup.called.map((w) => w.id).join(',')}`;
        if (sig === lastPickupSignature) return;   // 沒變動就不重繪，避免號碼動畫重播
        lastPickupSignature = sig;
        Kiosk.setHtml(readyEl, pickup.ready.length
            ? pickup.ready.map((o) => html`<span class="sg-number" data-order-id="${o.id}">${Kiosk.pad3(o.number)}</span>`)
            : html`<span class="sg-pickup-none">—</span>`);
        calledEl.classList.toggle('d-none', pickup.called.length === 0);
        Kiosk.setHtml(calledEl, pickup.called.length
            ? [html`<span>候位</span>`, pickup.called.map((w) => html`<span class="sg-number">${w.label}</span>`)]
            : '');
    }

    function renderIntake(intake) {
        const paused = Boolean(intake && intake.paused);
        intakeEl.classList.toggle('d-none', !paused);
        intakeText.textContent = paused ? (intake.message || '暫停接單中') : '';
    }

    // ---------------------------------------------------------------- 載入
    async function load() {
        try {
            const data = await Kiosk.pollJson('/api/signage', poll);
            if (!data) return;   // 304：內容沒變
            const menuSignature = JSON.stringify(data.categories);
            if (menuSignature !== lastMenuSignature) {
                lastMenuSignature = menuSignature;
                categories = data.categories || [];
                renderPage();
            }
            const nextPromos = data.promos || [];
            if (JSON.stringify(nextPromos) !== JSON.stringify(promos)) {
                promos = nextPromos;
                promoIndex = 0;
                showPromo();
            }
            renderPickup(data.pickup);
            renderIntake(data.intake);
        } catch (err) {
            console.warn('看板同步失敗:', err.message);
        }
    }

    let resizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(relayout, 300);
    });

    Kiosk.startClock(document.getElementById('sgClock'));
    showPromo();
    load();
    Kiosk.onOrdersChanged(load);
    setInterval(load, FALLBACK_POLL_MS);
    setInterval(nextPage, pageMs);
    setInterval(nextPromo, promoMs);
})();
