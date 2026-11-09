/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：優惠碼 / 優惠券驗證 (帶請求序號，只採用最新一次的結果) 與訊息列。
 * 端點不同 (/api/verify_promo 與 /api/pos/verify_promo) 由頁面傳入。
 */

/**
 * const promo = createPromoVerifier('/api/verify_promo');
 * const res = await promo.verify({ promo_code, subtotal });   // null = 已有更新的請求或已被重置 (丟棄)
 * res = { valid, discount, message, error }：error 為連線 / 伺服器錯誤 (ApiError)，此時 valid 為 false
 * promo.invalidate()：清除優惠券時呼叫，讓進行中的驗證失效
 */
export function createPromoVerifier(endpoint) {
    let seq = 0;
    return {
        invalidate() { seq++; },
        async verify(body) {
            const mine = ++seq;
            try {
                const res = await Kiosk.api(endpoint, { json: body });
                if (mine !== seq) return null;
                // 顧客頁為英 / 日文時後端另附 message_i18n (錯誤路徑的 err.message 已由 Kiosk.api 採用 message_i18n)
                return { valid: Boolean(res.valid), discount: res.valid ? Number(res.discount) || 0 : 0, message: res.message_i18n || res.message, error: null };
            } catch (err) {
                if (mine !== seq) return null;
                return { valid: false, discount: 0, message: err.message, error: err };
            }
        },
    };
}

/** 驗證結果訊息列：成功綠色打勾、失敗紅色叉叉；text 為空時清空 */
export function renderPromoMessage(el, ok, text, baseCls) {
    if (!el) return;
    el.className = `${baseCls} ${ok ? 'text-success' : 'text-danger'}`;
    Kiosk.setHtml(el, text ? Kiosk.html`<i class="bi ${ok ? 'bi-check-circle' : 'bi-x-circle'} me-1"></i>${text}` : '');
}
