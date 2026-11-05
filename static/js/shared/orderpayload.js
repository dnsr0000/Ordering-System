/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：送給後端的品項結構 (結帳 / 促銷預覽 / 預估等候)，與延遲執行的預覽排程。
 */

/** 購物車品項 → 後端 _parse_lines 的結構 (override_price 只有 POS 改價時才有，undefined 會被 JSON 省略) */
export function payloadLine(l) {
    return {
        menu_item_id: l.menu_item_id,
        quantity: l.quantity,
        modifier_ids: l.modifier_ids,
        combo_option_id: l.combo_option_id,
        combo_choices: l.combo_choices || [],
        pass_id: l.pass_id || 0,
        is_add_on: l.is_add_on,
        is_reward: l.is_reward,
        customization: l.customization,
        size: l.size || '',
        override_price: l.override_price,
    };
}

/**
 * 購物車變動後延遲 delayMs 再執行 run (連續變動只跑最後一次)；run(isCurrent) 收到序號檢查函式，
 * 非同步回應回來時以 isCurrent() 判斷是否已被更新的一次取代 (過期結果丟棄)。
 *   const preview = createPreviewScheduler(async (isCurrent) => { const res = await api(...); if (isCurrent()) ...; });
 *   preview.schedule();   // 延遲執行       preview.run();   // 立即執行       preview.cancel();   // 取消並讓進行中的失效
 */
export function createPreviewScheduler(run, delayMs = 300) {
    let timer = null;
    let seq = 0;
    const start = () => {
        clearTimeout(timer);
        timer = null;
        const mine = ++seq;
        return run(() => mine === seq);
    };
    return {
        schedule() {
            clearTimeout(timer);
            timer = setTimeout(start, delayMs);
        },
        run: start,
        cancel() {
            clearTimeout(timer);
            timer = null;
            seq++;
        },
    };
}
