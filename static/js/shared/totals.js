/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：金額計算 (與後端 process_checkout 相同的順序與進位規則)。
 * 小計 → 促銷活動 → 優惠券 (促銷活動可封鎖) → 會員等級折扣 → 整單折扣 (POS) → 點數折抵 → 服務費 → 外送費。
 * 純邏輯、不碰 DOM：頁面讀取畫面上的值後呼叫 computeTotals，再把結果寫回畫面。
 */

/** 點數可折抵的現金上限 = 四捨五入後的應付金額 (百分比優惠券留下的 .5 也折得掉)，再受每單上限限制 */
export function maxPointsCash(remaining, maxDiscount = 0) {
    let maxCash = Math.floor(remaining + 0.5);
    if (Number(maxDiscount) > 0) maxCash = Math.min(maxCash, Number(maxDiscount));
    return maxCash;
}

/** 外送到府的外送費：商品小計達免運門檻免收 */
export function deliveryFee(subtotal, fee, freeOver) {
    const over = Number(freeOver) || 0;
    return over && subtotal >= over ? 0 : Number(fee) || 0;
}

/**
 * 回傳每一個中間值 (皆為數字)：
 *   promotionDiscount 促銷、promo 優惠券、tier 會員折扣、manual 整單折扣、pointsDiscount 點數折抵金額、
 *   pointsUsed 實際扣的點數、pointsRequested 修正後的輸入點數、maxPoints 可用點數上限、service 服務費、delivery 外送費、
 *   final 應付、earned 本單可得點數 (乘上 pointsMultiplier：會員等級贈點倍數，預設 1)
 * points：{ enabled, available, requested, ppd (幾點折 1 元), maxDiscount (每單上限，0 = 不限) }
 * manual：{ type: 'amount' | 'percent', value } 或 null
 */
export function computeTotals({
    subtotal = 0, promotionDiscount = 0, blocksCoupon = false, couponDiscount = 0, tierPct = 0, manual = null, points = null,
    servicePct = 0, serviceApplies = false, deliveryFee: delivery = 0, spendPerPoint = 100, earnsPoints = true, pointsMultiplier = 1,
} = {}) {
    const promotion = Math.min(Number(promotionDiscount) || 0, subtotal);
    const afterPromotion = Math.max(0, subtotal - promotion);
    const promo = blocksCoupon ? 0 : Math.min(Number(couponDiscount) || 0, afterPromotion);
    let remaining = Math.max(0, afterPromotion - promo);
    const pct = Number(tierPct) || 0;
    // 與後端相同：會員折扣、整單折扣都先四捨五入成整數再從剩餘金額扣 (否則 .5 的情況前後端差 1 元，結帳回 409)
    const tier = pct > 0 ? Math.floor(remaining * Math.min(100, pct) / 100 + 0.5) : 0;
    remaining = Math.max(0, remaining - tier);
    let manualDiscount = 0;
    if (manual) {
        const v = Number(manual.value) || 0;
        const raw = manual.type === 'percent' ? remaining * Math.min(100, v) / 100 : Math.min(v, remaining);
        manualDiscount = Math.floor(raw + 0.5);
        remaining = Math.max(0, remaining - manualDiscount);
    }
    let pointsDiscount = 0;
    let pointsUsed = 0;
    let pointsRequested = 0;
    let maxPoints = 0;
    let maxCash = 0;
    if (points && points.enabled) {
        const ppd = Math.max(1, Number(points.ppd) || 1);
        maxCash = maxPointsCash(remaining, points.maxDiscount);
        maxPoints = Math.min(Math.max(0, Number(points.available) || 0), maxCash * ppd);
        pointsRequested = Math.min(Math.max(Math.floor(Number(points.requested) || 0), 0), maxPoints);
        pointsDiscount = Math.min(Math.floor(pointsRequested / ppd), maxCash);
        pointsUsed = pointsDiscount * ppd;
    }
    const afterDiscounts = Math.max(0, remaining - pointsDiscount);
    const svcPct = Number(servicePct) || 0;
    const service = serviceApplies && svcPct > 0 ? Math.floor(afterDiscounts * svcPct / 100 + 0.5) : 0;
    const deliveryAmount = Number(delivery) || 0;
    // 與後端相同：應付金額四捨五入到整數元
    const final = Math.floor(afterDiscounts + 0.5) + service + deliveryAmount;
    // 與後端相同：(應付 - 外送費) // 每幾元 1 點 × 會員等級贈點倍數，再取整 (倍數 0 = 此等級不累積點數；沒給時視為 1)
    const mult = pointsMultiplier === null || pointsMultiplier === '' || Number.isNaN(Number(pointsMultiplier))
        ? 1 : Math.max(0, Number(pointsMultiplier));
    const earned = earnsPoints
        ? Math.floor(Math.floor((final - deliveryAmount) / Math.max(1, Number(spendPerPoint) || 100)) * mult) : 0;
    return {
        subtotal, promotionDiscount: promotion, promo, tier, manual: manualDiscount, pointsDiscount, pointsUsed, pointsRequested,
        maxPoints, maxCash, service, delivery: deliveryAmount, final, earned,
    };
}
