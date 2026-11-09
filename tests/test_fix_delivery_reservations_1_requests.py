"""跨檔請求 (delivery-reservations-1) 的回歸測試：B-066 / B-067 / B-092 / R2-036 / B-287 / B-046"""
import hashlib
import hmac
from datetime import date, datetime, time, timedelta

import pytest

from app.extensions import db, limiter
from app.models.reservation import Reservation, WaitlistEntry
from app.routes import reservations as reservations_routes
from app.services import delivery_service, deposit_service, order_service, reservation_service
from tests.support import default_tenant_id, get_or_create_tenant, tenant_request


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def fake_refund(monkeypatch):
    calls = []

    def refund(r, reason=''):
        calls.append(reason)
        r.deposit_status, r.deposit_note = 'refunded', reason
        return 'refunded'
    monkeypatch.setattr(deposit_service, 'refund', refund)
    return calls


def _paid_reservation(app, hours_ahead=2, deposit_status='paid', name='店家取消測試'):
    with app.app_context():
        r = Reservation(tenant_id=default_tenant_id(app), name=name, phone='0968777000', party_size=2,
                        reserved_at=(datetime.now() + timedelta(hours=hours_ahead)).replace(microsecond=0),
                        source='online', deposit_amount=200, deposit_status=deposit_status,
                        deposit_trade_no=f'T{datetime.now():%H%M%S%f}'[:20])
        db.session.add(r)
        db.session.commit()
        return r.id


# B-046 / B-431：簽章含非 ASCII 字元不拋 TypeError
def test_uber_signature_non_ascii_returns_false():
    body = b'{"x":1}'
    good = hmac.new(b'secret', body, hashlib.sha256).hexdigest()
    assert delivery_service.verify_uber_signature(body, good, 'secret') is True
    assert delivery_service.verify_uber_signature(body, good.upper(), 'secret') is True
    assert delivery_service.verify_uber_signature(body, 'café', 'secret') is False


# B-067：可訂位時段跨午夜 (18:00~00:30)
def test_reservation_slots_cross_midnight(app, store_settings, no_rate_limit):
    store_settings('reservation', online_enabled=True, open_time='18:00', close_time='00:30', slot_minutes=30,
                   capacity_per_slot=20, max_party_size=4, days_ahead=30, deposit_per_person=0, no_show_limit=0)
    day = date.today() + timedelta(days=5)
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        slots = [s[0] for s in reservation_service.slots_for(day)]
    nxt = day + timedelta(days=1)
    assert slots[0] == datetime.combine(day, time(18, 0))
    assert slots[-1] == datetime.combine(nxt, time(0, 30))
    assert datetime.combine(nxt, time(0, 0)) in slots
    c = app.test_client()
    c.get('/?shop=default')
    assert f'{nxt}T00:30:00' in c.get(f'/reserve?day={day}').get_data(as_text=True)
    r = c.post('/reserve', data={'slot': f'{nxt}T00:30:00', 'name': '宵夜訂位', 'phone': '0977123450', 'party_size': '2'})
    assert r.status_code == 302 and '/reserve/' in r.headers['Location']
    with app.app_context():
        assert Reservation.query.filter_by(name='宵夜訂位').count() == 1


# B-092：店家因素取消，不論時數全額退訂金
def test_board_cancel_by_store_refunds(app, staff_client, store_settings, fake_refund):
    store_settings('reservation', deposit_refund_hours=24)
    rid = _paid_reservation(app, hours_ahead=2)
    staff_client.post('/reservations', data={'action': 'reservation_status', 'reservation_id': rid,
                                             'status': 'cancelled', 'by_store': '1', 'expected_status': 'booked'})
    with app.app_context():
        r = db.session.get(Reservation, rid)
        assert r.status == 'cancelled' and r.deposit_status == 'refunded'
    assert fake_refund == ['店家因素取消']
    # 沒帶 by_store：期限內取消仍沒收
    rid2 = _paid_reservation(app, hours_ahead=2, name='一般取消測試')
    staff_client.post('/reservations', data={'action': 'reservation_status', 'reservation_id': rid2,
                                             'status': 'cancelled', 'expected_status': 'booked'})
    with app.app_context():
        assert db.session.get(Reservation, rid2).deposit_status == 'forfeited'


# R2-036：退款失敗後可重試或標記人工退款
def test_board_deposit_retry_and_manual(app, staff_client, fake_refund):
    rid = _paid_reservation(app, hours_ahead=30, deposit_status='refund_failed', name='重試退款測試')
    staff_client.post('/reservations', data={'action': 'deposit_retry', 'reservation_id': rid})
    with app.app_context():
        assert db.session.get(Reservation, rid).deposit_status == 'refunded'
    rid2 = _paid_reservation(app, hours_ahead=30, deposit_status='refund_failed', name='人工退款測試')
    staff_client.post('/reservations', data={'action': 'deposit_manual', 'reservation_id': rid2})
    with app.app_context():
        r = db.session.get(Reservation, rid2)
        assert r.deposit_status == 'refunded' and 'pos_user' in (r.deposit_note or '')


# B-066：候位依營業日 (跨午夜營業的店換日時間前仍是前一天)
def test_waitlist_uses_business_date(app, staff_client, monkeypatch, no_rate_limit):
    yesterday = date.today() - timedelta(days=1)
    start = datetime.combine(yesterday, time(4, 0))
    fake = lambda: (start, start + timedelta(days=1))  # noqa: E731
    monkeypatch.setattr(order_service, 'todays_range', fake)
    monkeypatch.setattr(reservations_routes, 'todays_range', fake)
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/waitlist', data={'name': '午夜候位', 'phone': '', 'party_size': '2'})
    assert r.status_code == 302
    with app.app_context():
        e = WaitlistEntry.query.filter_by(name='午夜候位').first()
        assert e.business_date == yesterday
    assert '午夜候位' in staff_client.get('/reservations').get_data(as_text=True)


# B-287：依 token 開啟的頁面切到紀錄所屬店家
def test_token_pages_use_record_tenant(app, monkeypatch):
    seen = []
    real = reservations_routes.render_template

    def spy(name, **kw):
        from flask import g
        seen.append(g.tenant_id)
        return real(name, **kw)
    monkeypatch.setattr(reservations_routes, 'render_template', spy)
    other = get_or_create_tenant(app, 'fix-dr1-req-shop')
    with app.app_context():
        r = Reservation(tenant_id=other, name='語言測試', phone='0968777001', party_size=2, source='online',
                        reserved_at=(datetime.now() + timedelta(days=2)).replace(microsecond=0))
        e = WaitlistEntry(tenant_id=other, number=1, business_date=date.today() - timedelta(days=30),
                          name='語言候位', phone='', party_size=2)
        db.session.add_all([r, e])
        db.session.commit()
        token, wtoken = r.token, e.token
    c = app.test_client()
    c.get('/?shop=default')
    assert c.get(f'/reserve/{token}').status_code == 200
    assert seen[-1] == other
    assert c.get(f'/waitlist/{wtoken}').status_code == 200
    assert seen[-1] == other
