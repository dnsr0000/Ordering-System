/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：套餐可否選擇、「單點 / 升級套餐」卡片的版面。
 */

/** 套餐可選：套餐本身未停售、固定配餐都有供應、每個任選群組至少一個選項有供應 (isItemAvailable(menu_item_id, side)) */
export function comboAvailable(combo, isItemAvailable) {
    return combo.is_available !== false
        && (combo.sides || []).every((s) => isItemAvailable(s.menu_item_id, s))
        && (combo.choices || []).every((g) => (g.options || []).some((o) => isItemAvailable(o.menu_item_id, o)));
}

/**
 * 卡片外觀 (兩頁的 class 與文字不同，由頁面傳入；內容一律為 Kiosk.html 片段或純文字)：
 *   cardCls 卡片、attrs 卡片上的 data-* 屬性 (Kiosk.html 片段)、headerCls 徽章與價格那一列、
 *   badge / price / title / desc 上半部內容、footerCls / footer 下方按鈕列
 */
export function comboCardHtml({ cardCls, attrs = '', headerCls, badge, price, title, desc, footerCls, footer }) {
    return Kiosk.html`
        <div class="col-md-6">
            <div class="${cardCls}" ${attrs}>
                <div>
                    <div class="${headerCls}">
                        ${badge}
                        ${price}
                    </div>
                    ${title}
                    ${desc}
                </div>
                <div class="${footerCls}">${footer}</div>
            </div>
        </div>`;
}
