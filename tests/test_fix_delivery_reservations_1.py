"""修正小組 delivery-reservations-1 的回歸測試：訂位 / 候位 (reservations) 與外送平台 (delivery_service)"""
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta

import pytest

from app.extensions import db, limiter
from app.models.order import Order
from app.models.reservation import Reservation, WaitlistEntry
from app.models.settings import StoreSetting
from app.models.tenant import Tenant
from app.services import delivery_service, monitor_service, settings_service
from tests.support import (assert_flash, default_tenant_id, get_or_create_tenant, login_staff, make_item,
                           tenant_request)

UBER_SECRET = 'fix-dr1-uber-secret'


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def alerts(monkeypatch):
    out = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: out.append(msg))
    return out


@pytest.fixture
def online(store_settings):
    store_settings('reservation', online_enabled=True, open_time='11:00', close_time='13:00', slot_minutes=30,
                   capacity_per_slot=20, max_party_size=4, days_ahead=30, deposit_per_person=0, no_show_limit=0)


def _customer(app):
    c = app.test_client()
    c.get('/?shop=default')
    return c


def _future(days=3):
    return date.today() + timedelta(days=days)


def _add_reservation(app, **kw):
    with app.app_context():
        values = dict(tenant_id=default_tenant_id(app), name='修正測試', phone='0968123000', party_size=2,
                      reserved_at=datetime.combine(_future(), datetime.min.time()).replace(hour=12), source='online')
        values.update(kw)
        r = Reservation(**values)
        db.session.add(r)
        db.session.commit()
        return r.id, r.token


def _reservation(app, rid):
    with app.app_context():
        return db.session.get(Reservation, rid)


# ---------------------------------------------------------------------------
# B-423 候位進度「前面還有幾組」不含其他店家
# ---------------------------------------------------------------------------
def test_waiting_ahead_ignores_other_tenants(app):
    other = get_or_create_tenant(app, 'fix-dr1-wait')
    day = date(2031, 3, 3)
    with app.app_context():
        tid = default_tenant_id(app)
        db.session.add(WaitlistEntry(tenant_id=other, number=1, business_date=day, party_size=2, status='waiting'))
        db.session.add(WaitlistEntry(tenant_id=tid, number=1, business_date=day, party_size=2, status='seated'))
        mine = WaitlistEntry(tenant_id=tid, number=2, business_date=day, party_size=2, status='waiting')
        db.session.add(mine)
        db.session.commit()
        token = mine.token
    data = app.test_client().get(f'/waitlist/{token}?format=json').get_json()
    assert data['ahead'] == 0


# ---------------------------------------------------------------------------
# B-433 帶時區的訂位時段不會 500
# ---------------------------------------------------------------------------
def test_timezone_aware_slot_is_not_500(app, online, no_rate_limit):
    c = _customer(app)
    local = datetime.combine(_future(), datetime.min.time()).replace(hour=12).astimezone()
    r = c.post('/reserve', data={'slot': local.isoformat(), 'name': '時區客', 'phone': '0968123001', 'party_size': '2'})
    assert r.status_code == 302 and '/reserve/' in r.headers['Location']
    with app.app_context():
        row = Reservation.query.filter_by(name='時區客').one()
        assert row.reserved_at.tzinfo is None and row.reserved_at.hour == 12


# ---------------------------------------------------------------------------
# B-434 線上訂位頁的日期不超過可預訂天數
# ---------------------------------------------------------------------------
def test_reserve_page_clamps_to_days_ahead(app, online):
    c = _customer(app)
    far = date.today() + timedelta(days=60)
    page = c.get(f'/reserve?day={far}').get_data(as_text=True)
    max_day = date.today() + timedelta(days=30)
    assert f'{max_day}T12:00:00' in page and f'{far}T' not in page


# ---------------------------------------------------------------------------
# B-435 訂位時間過後顧客不能自行取消
# ---------------------------------------------------------------------------
def test_customer_cannot_cancel_after_reserved_time(app, online, no_rate_limit):
    rid, token = _add_reservation(app, name='過時客', reserved_at=datetime.now() - timedelta(hours=3))
    c = _customer(app)
    c.post(f'/reserve/{token}', data={'action': 'cancel'})
    assert_flash(c, '訂位時間已過')
    assert _reservation(app, rid).status == 'booked'


# ---------------------------------------------------------------------------
# B-430 過期畫面不能把已取消的訂位改成未到；已入座的候位不能重新叫號
# ---------------------------------------------------------------------------
def test_stale_board_cannot_change_finished_reservation(app):
    staff = login_staff(app, 'fix_dr1_staff', 'manager')
    rid, _token = _add_reservation(app, name='已取消客', status='cancelled')
    staff.post('/reservations', data={'action': 'reservation_status', 'reservation_id': rid, 'status': 'no_show'})
    assert_flash(staff, '請重新整理')
    assert _reservation(app, rid).status == 'cancelled'
    with app.app_context():
        e = WaitlistEntry(tenant_id=default_tenant_id(app), number=901, business_date=date(2031, 3, 4), party_size=2,
                          status='seated')
        db.session.add(e)
        db.session.commit()
        eid = e.id
    staff.post('/reservations', data={'action': 'waitlist_status', 'entry_id': eid, 'status': 'called'})
    assert_flash(staff, '請重新整理')
    with app.app_context():
        assert db.session.get(WaitlistEntry, eid).status == 'seated'


# ---------------------------------------------------------------------------
# B-438 重複送出只建立一筆訂位
# ---------------------------------------------------------------------------
def test_double_submit_creates_one_reservation(app, online, no_rate_limit):
    c = _customer(app)
    slot = f'{_future(4)}T11:30:00'
    data = {'slot': slot, 'name': '連按客', 'phone': '0968123002', 'party_size': '4'}
    first = c.post('/reserve', data=data)
    second = c.post('/reserve', data=data)
    assert first.status_code == second.status_code == 302
    assert first.headers['Location'] == second.headers['Location']
    with app.app_context():
        assert Reservation.query.filter_by(name='連按客').count() == 1


# ---------------------------------------------------------------------------
# R2-165 非整點的電話訂位也計入所在時段的名額；R2-168 店員代訂不受線上人數上限限制
# ---------------------------------------------------------------------------
def test_off_grid_staff_booking_counts_toward_slot(app, online):
    staff = login_staff(app, 'fix_dr1_staff', 'manager')
    day = _future(5)
    r = staff.post('/reservations', data={'action': 'reserve', 'date': day.isoformat(), 'time': '12:15',
                                          'name': '包廂整場', 'party_size': '20'})
    assert r.status_code == 302
    with app.app_context():
        assert Reservation.query.filter_by(name='包廂整場').one().party_size == 20
    page = _customer(app).get(f'/reserve?day={day}').get_data(as_text=True)
    assert f'value="{day}T12:00:00" disabled' in page
    c = _customer(app)
    c.post('/reserve', data={'slot': f'{day}T12:00:00', 'name': '超訂客', 'phone': '0968123003', 'party_size': '2'})
    assert_flash(c, '剩餘 0 位')


# ---------------------------------------------------------------------------
# B-429 綠界未設定正式金鑰時不成立需訂金的線上訂位
# ---------------------------------------------------------------------------
def test_deposit_reservation_refused_without_payment_keys(app, store_settings, monkeypatch, no_rate_limit):
    store_settings('reservation', online_enabled=True, open_time='11:00', close_time='13:00', slot_minutes=30,
                   capacity_per_slot=20, max_party_size=8, deposit_per_person=100, deposit_min_party=4, no_show_limit=0)
    monkeypatch.setitem(app.config, 'ECPAY_ALLOW_TEST_KEYS', False)
    monkeypatch.setitem(app.config, 'ECPAY_MERCHANT_ID', '')
    c = _customer(app)
    c.post('/reserve', data={'slot': f'{_future(6)}T12:00:00', 'name': '訂金客', 'phone': '0968123004', 'party_size': '4'})
    assert_flash(c, '請來電預約')
    with app.app_context():
        assert Reservation.query.filter_by(name='訂金客', phone='0968123004').count() == 0


# ---------------------------------------------------------------------------
# R2-163 訂金付款頁顯示實際剩餘時間；期限將到時不產生付款表單
# ---------------------------------------------------------------------------
def test_deposit_pay_page_shows_remaining_minutes(app):
    _rid, token = _add_reservation(app, name='晚付客', party_size=4, deposit_amount=400, deposit_status='pending',
                                   deposit_trade_no='KFIXDR1PAY01', created_at=datetime.now() - timedelta(minutes=25))
    html = app.test_client().get(f'/reserve/{token}/pay').get_data(as_text=True)
    assert '請於 4 分鐘內' in html or '請於 5 分鐘內' in html
    _rid, token2 = _add_reservation(app, name='逾時客', party_size=4, deposit_amount=400, deposit_status='pending',
                                    deposit_trade_no='KFIXDR1PAY02', created_at=datetime.now() - timedelta(minutes=29))
    r = app.test_client().get(f'/reserve/{token2}/pay')
    assert r.status_code == 410 and 'ecpay-form' not in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# B-422 已登入員工 / 已註冊裝置不受取號頻率限制
# ---------------------------------------------------------------------------
def test_waitlist_rate_limit_exempts_staff_and_devices(app, monkeypatch):
    from app.routes import reservations
    from app.services import device_service
    with tenant_request(app, default_tenant_id(app)):
        assert reservations._trusted_waitlist_client() is False
        monkeypatch.setattr(device_service, 'current', lambda: object())
        assert reservations._trusted_waitlist_client() is True


# ---------------------------------------------------------------------------
# 外送平台
# ---------------------------------------------------------------------------
@pytest.fixture
def uber(store_settings, monkeypatch):
    store_settings('delivery', ubereats_enabled=True, ubereats_client_id='cid', ubereats_client_secret=UBER_SECRET,
                   ubereats_store_id='fix-dr1-store', foodpanda_enabled=True, foodpanda_token='fp-token',
                   foodpanda_vendor_code='fix-dr1-fp')
    calls, carts = [], {}

    def fake_http(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == delivery_service.UBER_TOKEN_URL:
            return 200, {'access_token': 'uber-token'}
        if method == 'GET' and '/v2/eats/order/' in url:
            return 200, carts[url.rsplit('/', 1)[-1]]
        return 204, {}
    monkeypatch.setattr(delivery_service, '_http', fake_http)
    return calls, carts


def _uber_post(client, event):
    raw = json.dumps(event).encode()
    sig = hmac.new(UBER_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post('/webhooks/ubereats', data=raw, content_type='application/json', headers={'X-Uber-Signature': sig})


def _uber_event(resource_id, event_type='orders.notification'):
    return {'event_type': event_type, 'meta': {'user_id': 'fix-dr1-store', 'resource_id': resource_id},
            'resource_href': f'https://api.uber.com/v2/eats/order/{resource_id}'}


def test_uber_display_id_collision_and_platform_cancel(app, uber, no_rate_limit, alerts):
    calls, carts = uber
    item = make_item(app, '撞號便當', price=100, stock=20, external_code='FIX-DR1-BOX')
    for rid in ('fix-dr1-ue-1', 'fix-dr1-ue-2'):
        carts[rid] = {'display_id': 'FXDUP', 'cart': {'items': [{'external_data': 'FIX-DR1-BOX', 'title': '便當', 'quantity': 1}]}}
    c = app.test_client()
    assert _uber_post(c, _uber_event('fix-dr1-ue-1')).status_code == 200
    with app.app_context():   # 第一張單建立已超過比對時間範圍 (例如數天前)
        o = Order.query.filter_by(external_ref='FXDUP').one()
        o.created_at = datetime.now() - timedelta(days=3)
        db.session.commit()
    assert _uber_post(c, _uber_event('fix-dr1-ue-1')).status_code == 200      # 同一張單重送：不重複建單
    assert _uber_post(c, _uber_event('fix-dr1-ue-2')).status_code == 200      # 不同的單、相同 display_id
    from app.models.menu import MenuItem
    with app.app_context():
        orders = Order.query.filter_by(external_ref='FXDUP').order_by(Order.id).all()
        assert len(orders) == 2
        second_id = orders[1].id
        stock = db.session.get(MenuItem, item).stock
    assert stock == 18
    # 平台取消第二張單：本店訂單取消並回補庫存
    assert _uber_post(c, _uber_event('fix-dr1-ue-2', 'orders.cancel')).status_code == 200
    with app.app_context():
        assert db.session.get(Order, second_id).status == 'Cancelled'
        assert db.session.get(MenuItem, item).stock == stock + 1
    assert any('平台已取消' in a for a in alerts)
    assert calls


def test_manual_delivery_same_ref_on_different_platforms(app, no_rate_limit):
    item = make_item(app, '手動外送便當', price=100, stock=20)
    pos = login_staff(app, 'fix_dr1_pos', 'manager')
    line = {'menu_item_id': item, 'quantity': 1}
    r1 = pos.post('/api/pos/delivery_order', json={'platform': 'Lalamove', 'external_ref': 'FXR1001', 'items': [line]})
    assert r1.status_code == 200
    with tenant_request(app, default_tenant_id(app)):
        from app.models.enums import OrderSource
        tid = default_tenant_id(app)
        assert delivery_service.platform_order_uuid(tid, OrderSource.DELIVERY, 'FXR1001', 'Lalamove') != \
            delivery_service.platform_order_uuid(tid, OrderSource.DELIVERY, 'FXR1001', 'GrabFood')
        assert delivery_service.find_platform_order(tid, OrderSource.DELIVERY, 'FXR1001', platform='GrabFood') is None
        # Uber / foodpanda 的識別不受 platform 參數影響 (相容既有訂單)
        assert delivery_service.platform_order_uuid(tid, OrderSource.FOODPANDA, 'x', 'foodpanda') == \
            delivery_service.platform_order_uuid(tid, OrderSource.FOODPANDA, 'x')


@pytest.fixture
def second_shop_delivery(app):
    created = []

    def make(slug, active=True, **values):
        tid = get_or_create_tenant(app, slug)
        with app.app_context():
            t = db.session.get(Tenant, tid)
            t.is_active = active
            settings_service.put('delivery', values, tenant_id=tid)
            db.session.commit()
        created.append(tid)
        return tid
    yield make
    with app.app_context():
        for tid in created:
            StoreSetting.query.filter_by(tenant_id=tid, key='delivery').delete()
            db.session.get(Tenant, tid).is_active = True
        db.session.commit()


def test_inactive_tenant_does_not_receive_platform_orders(app, second_shop_delivery):
    tid = second_shop_delivery('fix-dr1-closed', active=False, foodpanda_enabled=True, foodpanda_token='t',
                               foodpanda_vendor_code='fix-dr1-closed-v')
    with app.app_context():
        assert delivery_service.tenant_for('foodpanda', 'foodpanda_vendor_code', 'fix-dr1-closed-v') is None
        db.session.get(Tenant, tid).is_active = True
        db.session.commit()
        assert delivery_service.tenant_for('foodpanda', 'foodpanda_vendor_code', 'fix-dr1-closed-v') == tid


def test_ambiguous_unconfigured_platform_alerts_and_tester_refuses(app, second_shop_delivery, alerts):
    a = second_shop_delivery('fix-dr1-amb-a', foodpanda_enabled=True, foodpanda_token='t')
    b = second_shop_delivery('fix-dr1-amb-b', foodpanda_enabled=True, foodpanda_token='t')
    with app.app_context():
        assert delivery_service.tenant_for('foodpanda', 'foodpanda_vendor_code', 'whatever') is None
    assert any('都沒有填寫' in m for m in alerts)
    with tenant_request(app, a):
        with pytest.raises(Exception) as exc:
            delivery_service._test(settings_service.get('delivery', a))
        assert '店家代碼' in str(exc.value)
    assert b
