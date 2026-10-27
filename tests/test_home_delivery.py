"""第六批：自有外送 (外送到府) — 配送區域與外送費、下單驗證、派車流程、追蹤頁、線上端與分店同步配送狀態"""
import pytest

from app.extensions import db
from app.models.order import Order
from tests.support import checkout, guest_client, line, login_staff, make_item, make_member

ZONES = '近:30:200\n遠:60:400'


def _home(client, items, delivery, user_id=None, **extra):
    return client.post('/api/pos/checkout', json={'items': items, 'payment_method': 'Cash', 'order_type': '外送到府',
                                                    'cash_received': 100000, 'delivery': delivery, 'user_id': user_id,
                                                    **extra})


ADDR = {'address': '台南市東區大學路 1 號 3 樓', 'phone': '0912-345-678', 'zone': '近'}


@pytest.fixture
def home_on(store_settings):
    store_settings('own_delivery', enabled=True, zones=ZONES, free_over=1000, drivers='阿明\n小華', channels=['pos', 'online'])


def test_home_delivery_fee_and_validation(app, staff_client, store_settings):
    item = make_item(app, '外送便當', price=125)
    assert _home(staff_client, [line(item, 2)], ADDR).status_code == 400, '未啟用'
    store_settings('own_delivery', enabled=True, zones=ZONES, free_over=1000, drivers='阿明', channels=['pos'])
    page = staff_client.get('/pos').get_data(as_text=True)
    assert 'id="posTypeHome"' in page and 'id="posHomeZone"' in page
    uid = make_member(app, '外送會員', '0966700001')
    r = _home(staff_client, [line(item, 2)], ADDR, user_id=uid)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert (body['total_price'], body['delivery_fee'], body['delivery_status']) == (280, 30, 'pending')
    from app.services.reward_service import get_or_create_reward_setting
    from tests.support import default_tenant_id
    with app.app_context():
        spp = max(1, get_or_create_reward_setting(default_tenant_id(app)).spend_per_point or 100)
    assert body['points_earned'] == 250 // spp, '外送費不計點'
    with app.app_context():
        o = db.session.get(Order, body['order_id'])
        assert (o.order_type, o.delivery_zone, o.delivery_phone, o.subtotal) == ('外送到府', '近', '0912-345-678', 250)
    assert _home(staff_client, [line(item, 1)], ADDR).status_code == 400, '未達最低消費'
    assert _home(staff_client, [line(item, 2)], dict(ADDR, zone='遠')).status_code == 400
    assert _home(staff_client, [line(item, 2)], dict(ADDR, phone='12')).status_code == 400
    assert _home(staff_client, [line(item, 2)], dict(ADDR, address='台南')).status_code == 400
    assert _home(staff_client, [line(item, 2)], dict(ADDR, zone='火星')).status_code == 400
    r = _home(staff_client, [line(item, 8)], dict(ADDR, zone='遠'))
    assert r.get_json()['delivery_fee'] == 0 and r.get_json()['total_price'] == 1000, '滿 $1000 免外送費'
    # 點餐機不能選外送到府
    assert checkout(guest_client(app), [line(item, 2)], order_type='外送到府', delivery=ADDR).status_code == 400


def test_dispatch_flow_and_tracking(app, staff_client, home_on):
    item = make_item(app, '派車便當', price=200)
    oid = _home(staff_client, [line(item)], ADDR).get_json()['order_id']
    with app.app_context():
        uuid = db.session.get(Order, oid).uuid
    page = staff_client.get('/admin/dispatch').get_data(as_text=True)
    assert f'data-order-id="{oid}"' in page and '阿明' in page and '大學路' in page
    assert staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'depart'}).status_code in (302, 400)
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '阿明'})
    with app.app_context():
        o = db.session.get(Order, oid)
        assert (o.delivery_status, o.driver_name) == ('assigned', '阿明')
    track = app.test_client().get(f'/api/track/{uuid}').get_json()
    assert track['delivery_label'] == '已指派外送員' and track['driver'] == '阿明'
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'depart'})
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'complete'})
    with app.app_context():
        o = db.session.get(Order, oid)
        assert (o.delivery_status, o.status) == ('delivered', 'PickedUp')
        assert o.dispatched_at and o.delivered_at
    assert app.test_client().get(f'/api/track/{uuid}').get_json()['delivery_label'] == '已送達'
    assert staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'fail'}).status_code in (302, 409)
    with app.app_context():
        assert db.session.get(Order, oid).delivery_status == 'delivered', '已送達不可再改'

    oid2 = _home(staff_client, [line(item)], ADDR).get_json()['order_id']
    staff_client.post('/admin/dispatch', data={'order_id': oid2, 'action': 'fail', 'reason': '聯絡不上'})
    with app.app_context():
        o = db.session.get(Order, oid2)
        assert o.delivery_status == 'failed' and '聯絡不上' in o.note
    kitchen = login_staff(app, 'dispatch_kitchen', 'kitchen')
    assert kitchen.get('/admin/dispatch').status_code in (302, 403)
    lives = staff_client.get('/api/pos/today_orders').get_json()['orders']
    assert next(x for x in lives if x['id'] == oid2)['delivery_status_label'] == '配送失敗'


def test_online_home_delivery_status_syncs_back_to_hub():
    from app.services import settings_service
    from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, _member, _order, _register_member, tenant_ctx
    env = Env()
    try:
        burger = _add_item(env.b, '線上外送堡', price=220, stock=20)
        _register_member(env.b, '線上外送會員', '0966700009')
        env.sync_all()
        with tenant_ctx(env.hub) as tenant:
            settings_service.put('own_delivery', {'enabled': True, 'zones': ZONES, 'channels': ['online'],
                                                  'drivers': '阿明'}, tenant_id=tenant.id)
            db.session.commit()
        hub = env.hub.test_client()
        hub.get('/?shop=default')
        order = _checkout(env.hub, [line(_item(env.hub, burger)['id'])], node_uuid=env.node_b_uuid,
                          user_id=_member(env.hub, '0966700009')['id'], order_type='外送到府', delivery=ADDR)
        with env.hub.app_context():
            o = Order.query.filter_by(uuid=order['uuid']).one()
            assert o.total_price == 250 and o.delivery_fee == 30
        env.sync(env.b)
        with tenant_ctx(env.b) as tenant:
            from app.services import own_delivery_service
            o = Order.query.filter_by(uuid=order['uuid']).one()
            assert o.delivery_address == ADDR['address'] and o.delivery_status == 'pending'
            own_delivery_service.assign(o, '阿明')
            own_delivery_service.depart(o)
            db.session.commit()
        env.sync(env.b)
        with env.hub.app_context():
            o = Order.query.filter_by(uuid=order['uuid']).one()
            assert (o.delivery_status, o.driver_name) == ('out', '阿明')
            assert o.dispatched_at is not None
    finally:
        env.close()
