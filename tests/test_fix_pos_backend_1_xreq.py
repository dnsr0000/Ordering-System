"""跨檔請求回歸測試 (pos-backend-1)：交班統計對訂位訂金的分類 (R2-030)"""
from types import SimpleNamespace

from app.models.payment import PaymentKind
from app.services.shift_service import summarize_payments


def _rec(kind, method, amount):
    return SimpleNamespace(kind=kind, method=method, amount=amount)


def test_summarize_payments_deposit_is_prepaid_and_forfeit_ignored():
    out = summarize_payments([
        _rec(PaymentKind.SALE, 'Cash', 300),
        _rec(PaymentKind.DEPOSIT, 'Cash', 500),
        _rec(PaymentKind.DEPOSIT_FORFEIT, 'Cash', 500),
        _rec(PaymentKind.DEPOSIT_FORFEIT, '訂金沒收', 200),
    ])
    cash = out['Cash']
    assert cash['sales'] == 300 and cash['count'] == 1
    assert cash['topups'] == 500          # 訂金算預收，不算銷售
    assert cash['movements'] == 0
    assert cash['net'] == 800             # 沒收不是金流，不重複計入
    assert '訂金沒收' not in out
