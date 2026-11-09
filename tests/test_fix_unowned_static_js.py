"""跨檔請求 unowned:static/js：POS 到店付款收款 (pos/orders.js)、平面圖已停用桌位、顧客頁 message_i18n 的回歸檢查

前端沒有瀏覽器測試環境時以原始碼檢查關鍵行為 (每個檢查對應一個 bug 編號)。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts):
    with open(os.path.join(ROOT, 'static', 'js', *parts), encoding='utf-8') as fh:
        return fh.read()


def test_pos_pickup_pay_module():
    """B-005 / B-015：到店付款 (pay_at_pickup) 的訂單顯示「到店付款・未收款」與「收款」按鈕，收款呼叫單張訂單的 settle"""
    src = _src('pos', 'orders.js')
    assert len(src.split('\n')) <= 500
    assert '<script' not in src and 'innerHTML' not in src
    assert 'export function unpaidBadge' in src and 'export function pickupPayButton' in src
    assert '到店付款・未收款' in src and 'o.pay_at_pickup' in src
    assert 'data-action="openPickupPay"' in src and 'openPickupPay:' in src
    assert re.search(r"api\(`/api/pos/orders/\$\{encodeURIComponent\(order\.id\)\}/settle`", src)
    # 付款資料沿用桌位結帳：付款方式選單、刷卡授權碼 / LINE Pay 付款碼 (paymentDetails)、畫面金額供後端核對
    assert 'TABLE_PAYMENTS' in src and 'paymentDetails(method, expectedTotal)' in src
    assert 'expected_total: expectedTotal' in src


def test_floor_plan_marks_inactive_tables():
    """R2-055：已停用但仍有未結帳訂單的桌位 (inactive) 加上 is-inactive 與「已停用」小標，金額照常顯示"""
    src = _src('floor-plan.js')
    assert "t.inactive ? ' is-inactive' : ''" in src
    assert '已停用' in src
    assert "t.status === 'occupied' ? html`<div class=\"fp-meta\">$${t.total}" in src


def test_promo_success_message_uses_i18n():
    """B-121：優惠碼驗證成功的訊息採用 message_i18n (英 / 日文顧客頁)"""
    assert 'message: res.message_i18n || res.message' in _src('shared', 'promo.js')


def test_otp_send_message_uses_i18n():
    """B-486：寄送簡訊驗證碼成功的訊息採用 message_i18n"""
    assert 'msg.textContent = data.message_i18n || data.message;' in _src('member-extras.js')
