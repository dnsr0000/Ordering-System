/*
 * 顧客點餐 (customer/) 與櫃檯 POS (pos/) 共用：電子發票載具輸入框提示與送出結構
 * (後端 einvoice_ecpay.normalize_invoice_info 讀 type / carrier / love_code / buyer_id)。
 */
export const INVOICE_PLACEHOLDERS = {
    mobile: '手機條碼 (/ABC1234)', company: '統一編號 8 碼', donate: '愛心碼', citizen: '自然人憑證條碼',
};

/** 發票類型 + 輸入值 → { type, carrier | love_code | buyer_id }；未選擇類型時 null */
export function invoicePayload(type, value) {
    const kind = String(type || '').trim();
    if (!kind) return null;
    const v = String(value || '').trim();
    if (kind === 'company') return { type: kind, buyer_id: v };
    if (kind === 'donate') return { type: kind, love_code: v };
    return { type: kind, carrier: v };
}
