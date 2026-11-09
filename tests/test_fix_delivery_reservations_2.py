"""修正小組 delivery-reservations-2 的回歸測試：外送平台 Webhook、外送員 GPS 連結 / 位置回報、派車狀態機、訂位頁文字"""
import os

import pytest

from app.extensions import db
from app.models.order import DeliveryPosition, Order
from tests.support import line, make_item

ZONES = '近:30:200\n遠:60:400'
ADDR = {'address': '台南市東區大學路 1 號 3 樓', 'phone': '0912-345-678', 'zone': '近'}
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _home(client, items):
    return client.post('/api/pos/checkout', json={'items': items, 'payment_method': 'Cash', 'order_type': '外送到府',
                                                    'cash_received': 100000, 'delivery': ADDR})


@pytest.fixture
def gps_on(store_settings):
    store_settings('own_delivery', enabled=True, zones=ZONES, free_over=1000, drivers='阿明\n小華',
                   channels=['pos', 'online'], gps_tracking=True, gps_interval=10)


@pytest.fixture
def delivery_on(store_settings):
    store_settings('delivery', ubereats_enabled=True, ubereats_client_id='cid', ubereats_client_secret='fix-secret',
                   ubereats_store_id='fix-store-1', foodpanda_enabled=True, foodpanda_token='fix-fp-token',
                   foodpanda_vendor_code='fixv1')


def _assigned_order(app, staff_client, name):
    item = make_item(app, name, price=220)
    oid = _home(staff_client, [line(item)]).get_json()['order_id']
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '阿明'})
    with app.app_context():
        o = db.session.get(Order, oid)
        return oid, o.driver_token, o.uuid


# ---------------------------------------------------------------------------
# B-431：Webhook 對非預期輸入回 400 / 401，不再 500
# ---------------------------------------------------------------------------
def test_webhooks_reject_malformed_input_without_500(app, delivery_on):
    c = app.test_client()
    for body in (b'[]', b'"x"', b'{"meta": "x"}', b'{"meta": [1]}'):
        assert c.post('/webhooks/ubereats', data=body, content_type='application/json').status_code == 400, body
    event = b'{"meta": {"user_id": "fix-store-1"}}'
    r = c.post('/webhooks/ubereats', data=event, content_type='application/json', headers={'X-Uber-Signature': 'café'})
    assert r.status_code == 401
    r = c.post('/webhooks/foodpanda/fixv1', json={'code': 'x'}, headers={'Authorization': 'Bearer café'})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# B-419 / R2-166：線上端不接收平台 Webhook；後台頁的網址用 PUBLIC_BASE_URL
# ---------------------------------------------------------------------------
def test_webhooks_disabled_on_hub_and_admin_page_uses_public_base(app, admin_client, delivery_on, monkeypatch):
    monkeypatch.setitem(app.config, 'PUBLIC_BASE_URL', 'https://shop.example.com')
    page = admin_client.get('/admin/delivery').get_data(as_text=True)
    assert 'https://shop.example.com/webhooks/ubereats' in page
    assert 'https://shop.example.com/webhooks/foodpanda/fixv1' in page
    assert 'http://localhost/webhooks' not in page
    assert 'PUBLIC_BASE_URL' not in page, '已設定 https 公開網址時不提示'

    monkeypatch.setitem(app.config, 'PUBLIC_BASE_URL', '')
    page = admin_client.get('/admin/delivery').get_data(as_text=True)
    assert 'PUBLIC_BASE_URL' in page, '只有區網 http 網址時提示設定公開網址'

    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    c = app.test_client()
    assert c.post('/webhooks/ubereats', data=b'{}', content_type='application/json').status_code == 404
    assert c.post('/webhooks/foodpanda/fixv1', json={'code': 'x'},
                  headers={'Authorization': 'Bearer fix-fp-token'}).status_code == 404
    r = admin_client.get('/admin/delivery')
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert '/webhooks/ubereats' not in page and '線上端不接收外送平台訂單' in page


# ---------------------------------------------------------------------------
# B-432 / B-436 / R2-162：配送結束後不再給地址電話；age=Infinity 不 500；位置回報免 CSRF
# ---------------------------------------------------------------------------
def test_driver_api_hides_personal_data_after_delivery(app, staff_client, gps_on, monkeypatch):
    oid, token, _ = _assigned_order(app, staff_client, '修正 GPS 便當')
    public = app.test_client()
    data = public.get(f'/api/driver/{token}').get_json()
    assert data['active'] is True and data['address'] == ADDR['address'] and data['phone']
    # 外送員是匿名 session：沒有 CSRF token 也能回報 (只憑網址裡的憑證)
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    r = public.post(f'/api/driver/{token}/position', json={'lat': 22.99, 'lng': 120.21})
    assert r.status_code == 200, r.get_data(as_text=True)
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', False)
    r = public.post(f'/api/driver/{token}/position', data='{"lat": 22.99, "lng": 120.21, "age": 1e999}',
                    content_type='application/json')
    assert r.status_code == 200
    r = public.post(f'/api/driver/{token}/position', data='{"lat": 22.99, "lng": 120.21, "age": Infinity}',
                    content_type='application/json')
    assert r.status_code == 200
    with app.app_context():
        o = db.session.get(Order, oid)
        from app.services import own_delivery_service
        own_delivery_service.depart(o)
        from app.models.tenant import StaffAccount
        own_delivery_service.complete(o, StaffAccount.query.first())
        db.session.commit()
    data = public.get(f'/api/driver/{token}').get_json()
    assert data['active'] is False and data['delivery_status'] == 'delivered'
    assert data['address'] == '' and data['phone'] == '' and data['items'] == [] and data['note'] == ''
    page = public.get(f'/driver/{token}').get_data(as_text=True)
    assert '大學路' not in page and '0912-345-678' not in page


# ---------------------------------------------------------------------------
# B-437：出發只能從已指派、送達只能從配送中
# ---------------------------------------------------------------------------
def test_depart_and_complete_require_previous_state(app, staff_client, gps_on):
    from app.errors import AppError
    from app.models.tenant import StaffAccount
    from app.services import own_delivery_service
    item = make_item(app, '修正狀態機便當', price=220)
    oid = _home(staff_client, [line(item)]).get_json()['order_id']
    with app.app_context():
        o = db.session.get(Order, oid)
        with pytest.raises(AppError) as exc:
            own_delivery_service.complete(o, StaffAccount.query.first())
        assert exc.value.status == 409
        db.session.rollback()
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '阿明'})
    with app.app_context():
        o = db.session.get(Order, oid)
        with pytest.raises(AppError) as exc:
            own_delivery_service.complete(o, StaffAccount.query.first())
        assert exc.value.status == 409, '未出發不可直接送達'
        db.session.rollback()
    assert staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'depart'}).status_code == 302
    with app.app_context():
        first = db.session.get(Order, oid).dispatched_at
        assert first is not None
    r = staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'depart'})
    assert r.status_code in (302, 409)
    with app.app_context():
        o = db.session.get(Order, oid)
        assert (o.delivery_status, o.dispatched_at) == ('out', first), '重複出發不覆寫出發時間'
        with pytest.raises(AppError) as exc:
            own_delivery_service.depart(o)
        assert exc.value.status == 409
        db.session.rollback()
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'complete'})
    with app.app_context():
        assert db.session.get(Order, oid).delivery_status == 'delivered'


# ---------------------------------------------------------------------------
# B-421：線上端不以本機的自有外送開關擋下分店產生的外送員連結
# ---------------------------------------------------------------------------
def test_hub_driver_link_ignores_hub_own_delivery_switch(app, staff_client, gps_on, store_settings, monkeypatch):
    oid, token, uuid = _assigned_order(app, staff_client, '修正線上端便當')
    store_settings('own_delivery', enabled=False, zones=ZONES, drivers='阿明', channels=['pos'], gps_tracking=False)
    public = app.test_client()
    assert public.get(f'/driver/{token}').status_code == 404, '門市主機仍依本機開關'
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    assert public.get(f'/driver/{token}').status_code == 200
    assert public.post(f'/api/driver/{token}/position', json={'lat': 22.99, 'lng': 120.21}).status_code == 200
    with app.app_context():
        assert DeliveryPosition.query.filter_by(order_uuid=uuid).count() >= 1
    assert public.get(f'/driver/{"0" * 36}').status_code == 404


# ---------------------------------------------------------------------------
# B-427 / B-420：分店的外送員連結：PUBLIC_BASE_URL → 總部網址 → 區網 QR 網址；線上點餐訂單一律用總部網址
# ---------------------------------------------------------------------------
def test_node_driver_url_prefers_hub_over_lan_qr_url(app, staff_client, gps_on, monkeypatch):
    from app.models.enums import OrderSource
    from app.models.tenant import Tenant
    from app.services import delivery_tracking_service as tracking
    from app.tenant_context import _use_tenant
    oid, token, _ = _assigned_order(app, staff_client, '修正分店網址便當')
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
    monkeypatch.setitem(app.config, 'SYNC_HUB_URL', 'https://hub.example.com')
    monkeypatch.setitem(app.config, 'PUBLIC_BASE_URL', '')
    with app.test_request_context('/admin/dispatch', base_url='http://192.168.1.10:5000'):
        tenant = Tenant.query.filter_by(slug='default').first()
        _use_tenant(tenant, remember=False)
        saved = tenant.table_qr_base_url
        try:
            tenant.table_qr_base_url = 'http://192.168.1.10:5000'
            o = db.session.get(Order, oid)
            assert tracking.driver_url(o) == f'https://hub.example.com/driver/{token}', '總部網址優先於區網 QR 網址'
            monkeypatch.setitem(app.config, 'PUBLIC_BASE_URL', 'https://shop.example.com')
            assert tracking.driver_url(o) == f'https://shop.example.com/driver/{token}', 'PUBLIC_BASE_URL 最優先'
            o.order_source = OrderSource.ONLINE
            assert tracking.driver_url(o) == f'https://hub.example.com/driver/{token}', '線上訂單回報到線上端'
            monkeypatch.setitem(app.config, 'SYNC_ROLE', 'standalone')
            assert tracking.driver_url(o) == f'https://shop.example.com/driver/{token}'
        finally:
            db.session.rollback()
            tenant = Tenant.query.filter_by(slug='default').first()
            tenant.table_qr_base_url = saved
            db.session.commit()


# ---------------------------------------------------------------------------
# B-439 / R2-169：訂位頁文字
# ---------------------------------------------------------------------------
def test_reservation_templates_use_dedicated_keys_and_hide_deposit_note():
    with open(os.path.join(ROOT, 'templates', 'reservation_pay.html'), encoding='utf-8') as fh:
        pay = fh.read()
    assert 'ui.rp_timeout_a' in pay
    with open(os.path.join(ROOT, 'templates', 'reservation_status.html'), encoding='utf-8') as fh:
        status = fh.read()
    assert 'deposit_note' not in status, '訂金說明 (含金流錯誤原文 / 員工名稱) 不顯示給顧客'
    assert "ui['rs_dep_' ~ r.deposit_status]" in status


# ---------------------------------------------------------------------------
# 跨檔請求 R2-002：線上端依取餐分店回報的自有外送設定驗證與計費 (沒有回報時沿用總部設定)
# ---------------------------------------------------------------------------
def test_own_delivery_uses_pickup_node_reported_config(app, store_settings):
    import json
    from types import SimpleNamespace

    from app.errors import AppError
    from app.services import own_delivery_service as od
    from tests.support import default_tenant_id
    store_settings('own_delivery', enabled=True, zones=ZONES, free_over=1000, channels=['pos', 'online'])
    tid = default_tenant_id(app)
    with app.app_context():
        legacy = SimpleNamespace()                                   # 舊版分店：模型沒有欄位
        for node in (None, legacy, SimpleNamespace(own_delivery_json=None), SimpleNamespace(own_delivery_json='{bad')):
            assert od.enabled_for('online', tid, node)
            assert od.validate(ADDR, 'online', tid, node)['zone'] == '近'
            assert od.fee_for('近', 300, tid, node) == 30 and od.fee_for('近', 1200, tid, node) == 0

        closed = SimpleNamespace(own_delivery_json=json.dumps({'enabled': False, 'channels': ['online'], 'zones': ZONES}))
        assert not od.enabled_for('online', tid, closed)
        with pytest.raises(AppError):
            od.validate(ADDR, 'online', tid, closed)

        pos_only = SimpleNamespace(own_delivery_json=json.dumps({'enabled': True, 'channels': ['pos']}))
        assert not od.enabled_for('online', tid, pos_only)

        own = SimpleNamespace(own_delivery_json=json.dumps(
            {'enabled': True, 'channels': ['online'], 'zones': '近:50:300', 'free_over': 0}))
        assert od.validate(ADDR, 'online', tid, own)['zone'] == '近'
        assert od.fee_for('近', 1200, tid, own) == 50, '分店不免運'
        with pytest.raises(AppError):
            od.fee_for('近', 250, tid, own)                          # 分店最低消費 300
        with pytest.raises(AppError):
            od.validate({**ADDR, 'zone': '遠'}, 'online', tid, own)   # 分店沒有「遠」區
        assert [z['name'] for z in od.zones(tid, own)] == ['近']
        assert [z['name'] for z in od.zones(tid)] == ['近', '遠'], '不傳分店時照舊讀總部設定'


def test_own_delivery_node_config_for_menu_page(app, store_settings):
    """R2-002：點餐頁依取餐分店顯示外送選項 (node_config)；分店沒有回報時退回本機設定"""
    import json
    from types import SimpleNamespace

    from app.services import own_delivery_service as od
    from tests.support import default_tenant_id
    store_settings('own_delivery', enabled=True, zones=ZONES, free_over=1000, eta_minutes=40, channels=['pos', 'online'])
    tid = default_tenant_id(app)
    with app.app_context():
        for node in (None, SimpleNamespace(), SimpleNamespace(own_delivery_json='{bad')):
            cfg = od.node_config(node, tid)
            assert cfg['enabled'] and cfg['free_over'] == 1000 and cfg['eta_minutes'] == 40
            assert [z['name'] for z in cfg['zones']] == ['近', '遠']
        own = SimpleNamespace(own_delivery_json=json.dumps(
            {'enabled': True, 'channels': ['online'], 'zones': '近:50:300', 'free_over': 0, 'eta_minutes': 60}))
        cfg = od.node_config(own, tid)
        assert cfg == {'enabled': True, 'zones': [{'name': '近', 'fee': 50, 'min_spend': 300}],
                       'free_over': 0, 'eta_minutes': 60}
        closed = SimpleNamespace(own_delivery_json=json.dumps({'enabled': False, 'channels': ['online']}))
        assert od.node_config(closed, tid)['enabled'] is False
        # 回報格式為 {'online': 布林, 'zones': [已解析的區域]} 時也能讀
        summary = SimpleNamespace(own_delivery_json=json.dumps(
            {'enabled': True, 'online': False, 'zones': [{'name': '近', 'fee': 50, 'min_spend': 300}], 'free_over': 0}))
        assert od.node_config(summary, tid)['enabled'] is False
        assert od.fee_for('近', 400, tid, summary) == 50


# ---------------------------------------------------------------------------
# 跨檔請求 B-435：訂位時間過後，顧客訂位頁不顯示取消按鈕
# ---------------------------------------------------------------------------
def test_reservation_status_hides_cancel_after_reserved_time(app):
    from datetime import datetime, timedelta

    from app.models.reservation import Reservation
    from tests.support import default_tenant_id
    tid = default_tenant_id(app)
    with app.app_context():
        future = Reservation(tenant_id=tid, name='未來訂位', phone='0911000111', reserved_at=datetime.now() + timedelta(hours=3))
        past = Reservation(tenant_id=tid, name='過時訂位', phone='0911000112', reserved_at=datetime.now() - timedelta(hours=1))
        db.session.add_all([future, past])
        db.session.commit()
        tokens = future.token, past.token
    c = app.test_client()
    assert 'name="action" value="cancel"' in c.get(f'/reserve/{tokens[0]}').get_data(as_text=True)
    assert 'name="action" value="cancel"' not in c.get(f'/reserve/{tokens[1]}').get_data(as_text=True)


# ---------------------------------------------------------------------------
# 跨檔請求 R2-164：多店家都沒填 foodpanda 店家代碼時，後台不給 VENDOR 佔位網址
# ---------------------------------------------------------------------------
def test_admin_delivery_hides_placeholder_url_when_vendor_code_ambiguous(app, admin_client, store_settings):
    from app.models.settings import StoreSetting
    from app.services import settings_service
    from tests.support import get_or_create_tenant
    store_settings('delivery', foodpanda_enabled=True, foodpanda_token='fix-fp-token', foodpanda_vendor_code='')
    assert '/webhooks/foodpanda/VENDOR' in admin_client.get('/admin/delivery').get_data(as_text=True), '只有一家時照舊'
    other = get_or_create_tenant(app, 'fix-dr2-foodpanda')
    with app.app_context():
        settings_service.put('delivery', {'foodpanda_enabled': True, 'foodpanda_token': 'other-token'}, tenant_id=other)
        db.session.commit()
    try:
        r = admin_client.get('/admin/delivery')
        assert r.status_code == 200
        assert '/webhooks/foodpanda/VENDOR' not in r.get_data(as_text=True)
    finally:
        with app.app_context():
            StoreSetting.query.filter_by(tenant_id=other, key='delivery').execution_options(skip_tenant_filter=True).delete()
            db.session.commit()
