"""外送員 GPS 追蹤：指派時產生憑證與手機連結、外送員頁與位置回報、顧客追蹤頁顯示位置、結束後拒絕、線上端同步"""
import pytest

from app.extensions import db
from app.models.order import DeliveryPosition, Order
from tests.support import line, make_item
from tests.test_home_delivery import ADDR, ZONES, _home


@pytest.fixture
def gps_on(store_settings):
    store_settings('own_delivery', enabled=True, zones=ZONES, free_over=1000, drivers='阿明\n小華',
                   channels=['pos', 'online'], gps_tracking=True, gps_interval=10)


def _order(app, oid):
    with app.app_context():
        o = db.session.get(Order, oid)
        return {'uuid': o.uuid, 'token': o.driver_token, 'status': o.delivery_status}


def test_driver_token_link_and_position_flow(app, staff_client, gps_on):
    item = make_item(app, 'GPS 便當', price=220)
    oid = _home(staff_client, [line(item)], ADDR).get_json()['order_id']
    assert _order(app, oid)['token'] == '', '指派前沒有憑證'

    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '阿明'})
    info = _order(app, oid)
    token = info['token']
    assert len(token) == 36
    board = staff_client.get('/admin/dispatch').get_data(as_text=True)
    assert f'/driver/{token}' in board and '外送員手機連結' in board and '尚無定位' in board

    public = app.test_client()
    page = public.get(f'/driver/{token}')
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert '大學路' in text and '開始回報位置' in text and '阿明' in text
    assert public.get('/driver/not-a-token').status_code == 404
    assert public.get(f'/driver/{token[:-1]}x').status_code == 404

    # 已指派 (尚未出發) 就可回報；顧客頁只在「配送中」顯示位置
    r = public.post(f'/api/driver/{token}/position', json={'lat': 22.9972, 'lng': 120.2169, 'accuracy': 12})
    assert r.status_code == 200 and r.get_json()['active'] is True and r.get_json()['interval'] == 10
    track = public.get(f'/api/track/{info["uuid"]}').get_json()
    assert track['driver_position'] is None

    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'depart'})
    public.post(f'/api/driver/{token}/position', json={'lat': 22.9980, 'lng': 120.2200, 'accuracy': 8})
    track = public.get(f'/api/track/{info["uuid"]}').get_json()
    pos = track['driver_position']
    assert pos['lat'] == 22.998 and pos['lng'] == 120.22 and pos['accuracy'] == 8
    assert pos['map_url'].startswith('https://www.google.com/maps?q=22.998,120.22') and pos['age_seconds'] >= 0
    board = staff_client.get('/admin/dispatch').get_data(as_text=True)
    assert '最後定位' in board
    page = public.get(f'/track/{info["uuid"]}').get_data(as_text=True)
    assert 'trackDriverBox' in page

    # 不合法的座標
    assert public.post(f'/api/driver/{token}/position', json={'lat': 123, 'lng': 0}).status_code == 400
    assert public.post(f'/api/driver/{token}/position', json={'lat': 'x', 'lng': 1}).status_code == 400

    # 送達後：不再接受回報、顧客頁不再顯示位置、派車頁不再顯示連結
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'complete'})
    assert public.post(f'/api/driver/{token}/position', json={'lat': 22.99, 'lng': 120.21}).status_code == 409
    assert public.get(f'/api/track/{info["uuid"]}').get_json()['driver_position'] is None
    assert public.get(f'/api/driver/{token}').get_json()['active'] is False
    assert f'/driver/{token}' not in staff_client.get('/admin/dispatch').get_data(as_text=True)


def test_positions_pruned_and_token_requires_feature(app, staff_client, gps_on, store_settings):
    from app.services import delivery_tracking_service as tracking
    item = make_item(app, 'GPS 修剪便當', price=220)
    oid = _home(staff_client, [line(item)], ADDR).get_json()['order_id']
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '小華'})
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'depart'})
    info = _order(app, oid)
    with app.app_context():
        o = db.session.get(Order, oid)
        for i in range(DeliveryPosition.MAX_PER_ORDER + 20):
            tracking.record_position(o, 22.9 + i * 1e-5, 120.2, 5)
        db.session.commit()
        assert DeliveryPosition.query.filter_by(order_uuid=info['uuid']).count() == DeliveryPosition.MAX_PER_ORDER
        latest = tracking.latest(o)
        assert latest['lat'] == round(22.9 + (DeliveryPosition.MAX_PER_ORDER + 19) * 1e-5, 6)
        assert len(tracking.trail(o, limit=10)) == 10
        assert round(tracking.distance_km(22.9972, 120.2169, 23.0, 120.2169), 2) == 0.31

    # 關閉 GPS 回報：外送員頁與 API 都 404
    store_settings('own_delivery', enabled=True, zones=ZONES, drivers='小華', channels=['pos'], gps_tracking=False)
    public = app.test_client()
    assert public.get(f'/driver/{info["token"]}').status_code == 404
    assert public.post(f'/api/driver/{info["token"]}/position', json={'lat': 22.9, 'lng': 120.2}).status_code == 404
    assert public.get(f'/api/track/{info["uuid"]}').get_json()['driver_position'] is None


def test_driver_url_prefers_public_base(app, staff_client, gps_on, monkeypatch):
    from app.services import delivery_tracking_service as tracking
    item = make_item(app, 'GPS 網址便當', price=220)
    oid = _home(staff_client, [line(item)], ADDR).get_json()['order_id']
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '阿明'})
    token = _order(app, oid)['token']
    with app.test_request_context('/admin/dispatch', base_url='http://192.168.1.10:5000'):
        from app.models.tenant import Tenant
        from app.tenant_context import _use_tenant
        tenant = Tenant.query.filter_by(slug='default').first()
        _use_tenant(tenant, remember=False)
        saved = tenant.table_qr_base_url
        try:
            tenant.table_qr_base_url = ''
            o = db.session.get(Order, oid)
            assert tracking.driver_url(o) == f'http://192.168.1.10:5000/driver/{token}', '沒有設定時用目前請求的網址'
            tenant.table_qr_base_url = 'https://qr.example.com/'
            assert tracking.driver_url(o) == f'https://qr.example.com/driver/{token}', '店家的 QR code 網址優先於請求網址'
            monkeypatch.setitem(app.config, 'PUBLIC_BASE_URL', 'https://shop.example.com/')
            assert tracking.driver_url(o) == f'https://shop.example.com/driver/{token}', 'PUBLIC_BASE_URL 最優先'
        finally:
            tenant.table_qr_base_url = saved
            db.session.commit()


def test_driver_token_syncs_to_hub_and_positions_reported_there():
    """分店指派外送員 → 憑證同步到線上端 → 外送員手機回報到線上端 → 線上端的顧客追蹤頁看得到位置"""
    from app.services import settings_service
    from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, _member, _register_member, tenant_ctx
    env = Env()
    try:
        burger = _add_item(env.b, 'GPS 線上堡', price=220, stock=20)
        _register_member(env.b, 'GPS 線上會員', '0966700019')
        env.sync_all()
        with tenant_ctx(env.hub) as tenant:
            settings_service.put('own_delivery', {'enabled': True, 'zones': ZONES, 'channels': ['online'],
                                                  'drivers': '阿明', 'gps_tracking': True}, tenant_id=tenant.id)
            db.session.commit()
        hub = env.hub.test_client()
        hub.get('/?shop=default')
        order = _checkout(env.hub, [line(_item(env.hub, burger)['id'])], node_uuid=env.node_b_uuid,
                          user_id=_member(env.hub, '0966700019')['id'], order_type='外送到府', delivery=ADDR)
        env.sync(env.b)
        with tenant_ctx(env.b):
            from app.services import own_delivery_service
            o = Order.query.filter_by(uuid=order['uuid']).one()
            own_delivery_service.assign(o, '阿明')
            own_delivery_service.depart(o)
            db.session.commit()
            token = o.driver_token
        assert len(token) == 36
        env.sync(env.b)
        with env.hub.app_context():
            o = Order.query.filter_by(uuid=order['uuid']).one()
            assert (o.delivery_status, o.driver_token) == ('out', token)
        # 外送員的手機連到線上端回報
        assert hub.get(f'/driver/{token}').status_code == 200
        r = hub.post(f'/api/driver/{token}/position', json={'lat': 22.63, 'lng': 120.30, 'accuracy': 15})
        assert r.status_code == 200, r.get_json()
        pos = hub.get(f'/api/track/{order["uuid"]}').get_json()['driver_position']
        assert pos and pos['lat'] == 22.63 and pos['lng'] == 120.3
        # 分店送達後同步到線上端：線上端不再接受回報
        with tenant_ctx(env.b):
            from app.services import own_delivery_service
            from app.models.tenant import StaffAccount
            o = Order.query.filter_by(uuid=order['uuid']).one()
            own_delivery_service.complete(o, StaffAccount.query.first())
            db.session.commit()
        env.sync(env.b)
        assert hub.post(f'/api/driver/{token}/position', json={'lat': 22.63, 'lng': 120.30}).status_code == 409
    finally:
        env.close()
