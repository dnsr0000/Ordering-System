"""跨檔請求 (wallet-loyalty-giftcards-1) 的回歸測試：R2-030 訂位訂金的收款紀錄"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.payment import PaymentKind, PaymentRecord
from app.models.reservation import Reservation
from app.services import deposit_service, monitor_service, payment_ecpay
from tests.support import default_tenant_id, tenant_request


@pytest.fixture
def ecpay(app, monkeypatch, store_settings):
    store_settings('reservation', deposit_refund_hours=24, deposit_on_seat='refund')
    state = {'code': '1'}
    monkeypatch.setattr(payment_ecpay, '_post',
                        lambda url, data: {'RtnCode': state['code'], 'RtnMsg': 'OK' if state['code'] == '1' else '失敗'})
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: None)
    return state


def _reservation(app, trade, hours=48, deposit='pending'):
    with app.app_context():
        r = Reservation(tenant_id=default_tenant_id(app), name='訂金收款', phone='0968900199', party_size=4,
                        reserved_at=datetime.now() + timedelta(hours=hours), source='online', deposit_amount=400,
                        deposit_status=deposit, deposit_trade_no=trade,
                        deposit_provider_no='EC' + trade[-6:] if deposit != 'pending' else '')
        db.session.add(r)
        db.session.commit()
        return r.id, r.token


def _records(app, trade):
    with app.app_context():
        rows = PaymentRecord.query.filter_by(reference=trade).execution_options(skip_tenant_filter=True) \
            .order_by(PaymentRecord.id).all()
        return [(p.kind, p.amount, p.order_id, p.staff_username) for p in rows]


def _pay(app, trade, token):
    with tenant_request(app, default_tenant_id(app)):
        return deposit_service.mark_paid(trade, 'ECPAY' + trade[-6:], '400', f'R:{token}')


def _set_status(app, rid, status):
    from app.services import reservation_service
    with tenant_request(app, default_tenant_id(app)):
        r = db.session.get(Reservation, rid)
        reservation_service.set_reservation_status(r, status)
        db.session.commit()
        return r.deposit_status


def test_deposit_paid_and_refund_write_payment_records(app, ecpay):
    trade = 'KREQDEP000001'
    rid, token = _reservation(app, trade)
    assert _pay(app, trade, token) == 'paid'
    assert _pay(app, trade, token) == 'already'   # 綠界重送通知：不重複記收款
    assert _records(app, trade) == [(PaymentKind.DEPOSIT, 400, None, 'ecpay')]
    assert _set_status(app, rid, 'cancelled') == 'refunded'
    assert [r[:2] for r in _records(app, trade)] == [(PaymentKind.DEPOSIT, 400), (PaymentKind.DEPOSIT, -400)]


def test_deposit_forfeit_and_undo_write_records(app, ecpay):
    trade = 'KREQDEP000002'
    rid, token = _reservation(app, trade)
    assert _pay(app, trade, token) == 'paid'
    assert _set_status(app, rid, 'no_show') == 'forfeited'
    assert _set_status(app, rid, 'booked') == 'paid'
    assert [r[:2] for r in _records(app, trade)] == [
        (PaymentKind.DEPOSIT, 400), (PaymentKind.DEPOSIT_FORFEIT, 400), (PaymentKind.DEPOSIT_FORFEIT, -400)]


def test_deposit_refund_failed_then_manual_refund_records_once(app, ecpay):
    trade = 'KREQDEP000003'
    rid, token = _reservation(app, trade)
    assert _pay(app, trade, token) == 'paid'
    ecpay['code'] = '10200047'
    assert _set_status(app, rid, 'cancelled') == 'refund_failed'
    assert [r[:2] for r in _records(app, trade)] == [(PaymentKind.DEPOSIT, 400)]
    with tenant_request(app, default_tenant_id(app)):
        r = db.session.get(Reservation, rid)
        assert deposit_service.mark_refunded_manually(r, 'mgr') == 'refunded'
        db.session.commit()
    assert [r[:2] for r in _records(app, trade)] == [(PaymentKind.DEPOSIT, 400), (PaymentKind.DEPOSIT, -400)]


def test_legacy_paid_deposit_without_record_writes_nothing(app, ecpay):
    """更新前就已付款的訂位沒有收款紀錄：退款 / 沒收不可沖出負的預收款或憑空的收入"""
    trade = 'KREQDEP000004'
    rid, _token = _reservation(app, trade, deposit='paid')
    assert _set_status(app, rid, 'no_show') == 'forfeited'
    assert _set_status(app, rid, 'seated') == 'refunded'
    assert _records(app, trade) == []
