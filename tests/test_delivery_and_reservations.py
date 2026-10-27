"""外送平台 Webhook (Uber Eats 簽章 / foodpanda 權杖，模擬平台 API)、外送訂單規則，以及訂位 / 現場候位 / 叫號看板"""
import hashlib
import hmac
import json
import re
from datetime import date, datetime, timedelta

import pytest

from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice
from app.models.reservation import Reservation, WaitlistEntry
from app.services import delivery_service
from tests.support import assert_flash, make_item

UBER_SECRET = 'uber-client-secret'


@pytest.fixture
def delivery_on(store_settings, monkeypatch):
    store_settings('delivery', ubereats_enabled=True, ubereats_client_id='cid', ubereats_client_secret=UBER_SECRET,
                   ubereats_store_id='store-uuid-1', foodpanda_enabled=True, foodpanda_token='fp-token',
                   foodpanda_vendor_code='v1ab')
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


def _uber_post(client, event, secret=UBER_SECRET):
    raw = json.dumps(event).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post('/webhooks/ubereats', data=raw, content_type='application/json',
                       headers={'X-Uber-Signature': sig})


def _orders_by_ref(app, ref):
    with app.app_context():
        return [{'id': o.id, 'source': o.order_source, 'type': o.order_type, 'method': o.payment_method,
                 'total': o.total_price, 'note': o.note, 'items': [(i.item_name, i.quantity, i.price) for i in o.items]}
                for o in Order.query.filter_by(external_ref=ref).all()]


def test_uber_eats_webhook_creates_and_accepts(app, delivery_on, store_settings):
    calls, carts = delivery_on
    store_settings('einvoice', enabled=True)
    iid = make_item(app, 'Uber 雞腿便當', price=120, stock=20, external_code='UE-CHK', delivery_price=150)
    carts['ord-1'] = {'display_id': 'A1B2C', 'cart': {'special_instructions': '不要辣', 'items': [
        {'external_data': 'UE-CHK', 'title': '雞腿便當', 'quantity': 2,
         'selected_modifier_groups': [{'selected_items': [{'title': '加蛋'}]}]}]}}
    event = {'event_type': 'orders.notification', 'meta': {'user_id': 'store-uuid-1', 'resource_id': 'ord-1'},
             'resource_href': 'https://api.uber.com/v2/eats/order/ord-1'}
    c = app.test_client()
    assert _uber_post(c, event, secret='wrong').status_code == 401
    assert c.post('/webhooks/ubereats', data=b'not json', content_type='application/json').status_code == 400
    assert _uber_post(c, event).status_code == 200
    orders = _orders_by_ref(app, 'A1B2C')
    assert len(orders) == 1
    o = orders[0]
    assert (o['source'], o['type'], o['method'], o['total'], o['note']) == ('UberEats', '外送', '平台代收', 300, '不要辣')
    accept = [c for c in calls if c[1].endswith('/accept_pos_order')]
    assert accept and accept[-1][2]['headers']['Authorization'] == 'Bearer uber-token'
    with app.app_context():
        assert Invoice.query.filter_by(order_id=o['id']).count() == 0, '外送平台訂單由平台開立發票'
    # 重送同一事件不重複建單
    assert _uber_post(c, event).status_code == 200
    assert len(_orders_by_ref(app, 'A1B2C')) == 1
    # 找不到品項 → 拒單並通知
    carts['ord-2'] = {'display_id': 'ZZ999', 'cart': {'items': [{'external_data': 'NOPE', 'title': '不存在的餐', 'quantity': 1}]}}
    _uber_post(c, dict(event, meta={'user_id': 'store-uuid-1', 'resource_id': 'ord-2'},
                       resource_href='https://api.uber.com/v2/eats/order/ord-2'))
    assert _orders_by_ref(app, 'ZZ999') == []
    assert any(c[1].endswith('/ord-2/deny_pos_order') for c in calls)
    # 不屬於任何店家的 store id
    assert _uber_post(c, dict(event, meta={'user_id': 'other-store', 'resource_id': 'x'})).status_code == 404


def test_foodpanda_webhook(app, delivery_on):
    make_item(app, 'FP 排骨飯', price=110, stock=20, external_code='FP-PORK')
    c = app.test_client()
    payload = {'code': 'fp-9k2x', 'products': [{'remoteCode': 'FP-PORK', 'name': '排骨飯', 'quantity': 3,
                                                'comment': '飯少'}],
               'comments': {'customerComment': '放門口'}}
    url = '/webhooks/foodpanda/v1ab'
    assert c.post(url, json=payload).status_code == 401
    assert c.post(url, json=payload, headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert c.post('/webhooks/foodpanda/unknown', json=payload, headers={'Authorization': 'Bearer fp-token'}).status_code == 404
    r = c.post(url, json=payload, headers={'Authorization': 'Bearer fp-token'})
    assert r.status_code == 200
    oid = int(r.get_json()['remoteResponse']['remoteOrderId'])
    o = _orders_by_ref(app, 'fp-9k2x')[0]
    assert o['id'] == oid and o['source'] == 'foodpanda' and o['total'] == 330 and o['note'] == '放門口'
    bad = c.post(url, json={'code': 'fp-bad', 'products': [{'remoteCode': 'X', 'name': '不存在', 'quantity': 1}]},
                 headers={'Authorization': 'Bearer fp-token'})
    assert bad.status_code == 422 and '找不到品項' in bad.get_json()['reason']


def test_delivery_admin_page(app, admin_client, delivery_on):
    page = admin_client.get('/admin/delivery').get_data(as_text=True)
    assert '/webhooks/ubereats' in page and '/webhooks/foodpanda/v1ab' in page


def test_delivery_type_not_allowed_from_kiosk(app):
    from tests.support import checkout, guest_client, line
    iid = make_item(app, '點餐機外送測試', price=50, stock=5)
    r = checkout(guest_client(app), [line(iid)], order_type='外送')
    assert r.status_code == 400 and '外送' in r.get_json()['message']


# ---------------------------------------------------------------------------
# 訂位
# ---------------------------------------------------------------------------
def test_online_reservation_capacity(app, staff_client, store_settings):
    c = app.test_client()
    c.get('/?shop=default')
    assert c.get('/reserve').status_code == 404
    store_settings('reservation', online_enabled=True, open_time='11:00', close_time='13:00', slot_minutes=60,
                   capacity_per_slot=6, max_party_size=4)
    tomorrow = date.today() + timedelta(days=1)
    page = c.get(f'/reserve?day={tomorrow}').get_data(as_text=True)
    slot = f'{tomorrow}T12:00:00'
    assert slot in page and f'{tomorrow}T13:00:00' in page
    r = c.post('/reserve', data={'slot': slot, 'name': '訂位王', 'phone': '0977000001', 'party_size': '4'})
    assert r.status_code == 302
    token = r.headers['Location'].rsplit('/', 1)[-1]
    assert '訂位王' in c.get(f'/reserve/{token}').get_data(as_text=True)
    c.post('/reserve', data={'slot': slot, 'name': '第二組', 'phone': '0977000002', 'party_size': '3'})
    assert_flash(c, '剩餘 2 位')
    c.post('/reserve', data={'slot': slot, 'name': '人太多', 'phone': '0977000003', 'party_size': '5'})
    assert_flash(c, '1 ~ 4 位')
    c.post('/reserve', data={'slot': f'{tomorrow}T12:30:00', 'name': '錯時段', 'phone': '0977000003', 'party_size': '2'})
    assert_flash(c, '不開放')
    c.post('/reserve', data={'slot': slot, 'name': '電話錯', 'phone': '123', 'party_size': '2'})
    assert_flash(c, '手機號碼')
    # 顧客自行取消 → 名額釋出
    c.post(f'/reserve/{token}', data={'action': 'cancel'})
    with app.app_context():
        assert Reservation.query.filter_by(token=token).first().status == 'cancelled'
    assert c.post('/reserve', data={'slot': slot, 'name': '第二組', 'phone': '0977000002', 'party_size': '3'}).status_code == 302
    # 店員看板：新增訂位 (電話可空白)、入座
    staff_client.post('/reservations', data={'action': 'reserve', 'date': tomorrow.isoformat(), 'time': '18:30',
                                             'name': '電話訂位', 'party_size': '2'})
    board = staff_client.get(f'/reservations?day={tomorrow}').get_data(as_text=True)
    assert '電話訂位' in board and '第二組' in board
    with app.app_context():
        rid = Reservation.query.filter_by(name='電話訂位').first().id
    staff_client.post('/reservations', data={'action': 'reservation_status', 'reservation_id': rid, 'status': 'seated',
                                             'table_name': 'A5'})
    with app.app_context():
        r = db.session.get(Reservation, rid)
        assert (r.status, r.table_name) == ('seated', 'A5')


def test_waitlist_and_counter_board(app, staff_client, kitchen_client, store_settings):
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/waitlist', data={'name': '候位甲', 'phone': '', 'party_size': '2'})
    assert r.status_code == 302
    token_a = r.headers['Location'].rsplit('/', 1)[-1]
    r = c.post('/waitlist', data={'name': '候位乙', 'phone': '0977000010', 'party_size': '4'})
    token_b = r.headers['Location'].rsplit('/', 1)[-1]
    a = c.get(f'/waitlist/{token_a}?format=json').get_json()
    b = c.get(f'/waitlist/{token_b}?format=json').get_json()
    assert b['ahead'] == a['ahead'] + 1 and a['status'] == 'waiting'
    num_a = int(re.sub(r'\D', '', a['label']))
    assert int(re.sub(r'\D', '', b['label'])) == num_a + 1
    assert c.post('/waitlist', data={'name': 'x', 'party_size': '0'}).status_code == 302
    assert kitchen_client.get('/reservations').status_code == 403

    with app.app_context():
        eid = WaitlistEntry.query.filter_by(token=token_a).first().id
    staff_client.post('/reservations', data={'action': 'waitlist_status', 'entry_id': eid, 'status': 'called'})
    board = app.test_client().get('/api/counter_orders').get_json()
    assert a['label'] in [w['label'] for w in board['called_waitlist']]
    assert c.get(f'/waitlist/{token_b}?format=json').get_json()['ahead'] == b['ahead'] - 1
    staff_client.post('/reservations', data={'action': 'waitlist_status', 'entry_id': eid, 'status': 'seated',
                                             'table_name': 'B2'})
    assert c.get(f'/waitlist/{token_a}?format=json').get_json()['status'] == 'seated'
    assert a['label'] not in [w['label'] for w in app.test_client().get('/api/counter_orders').get_json()['called_waitlist']]
    store_settings('reservation', waitlist_public=False)
    assert c.get('/waitlist').status_code == 404
