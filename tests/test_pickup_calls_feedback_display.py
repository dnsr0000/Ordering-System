"""
第三批專業功能：線上預約取餐時間、桌邊呼叫服務、待機輪播、大字模式、餐後評分 (含同步上傳)、熱量營養標示。
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.order import Order, OrderFeedback, ServiceCall
from tests.support import (assert_flash, checkout, default_tenant_id, guest_client, line, make_item, make_member,
                           member_client)
from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, _member, _order, _register_member, tenant_ctx

CARD = {'auth_code': 'A12345', 'card_last4': '4242'}


# ===========================================================================
# 預約取餐時間：時段產生與驗證
# ===========================================================================
def test_pickup_slots_follow_settings(app, store_settings):
    from app.errors import AppError
    from app.services import pickup_service
    tid = default_tenant_id(app)
    with app.app_context():
        assert pickup_service.slots(tid) == [], '預設不開放'
        with pytest.raises(AppError):
            pickup_service.parse('2026-10-23T12:00', tid)
        assert pickup_service.parse('', tid) is None
    store_settings('online_order', scheduled_pickup=True, open_time='10:00', close_time='12:00', slot_minutes=30,
                   lead_minutes=20, days_ahead=1)
    now = datetime(2026, 10, 23, 10, 5)
    with app.app_context():
        got = pickup_service.slots(tid, now=now)
        assert [t.strftime('%d %H:%M') for t, _ in got] == ['23 10:30', '23 11:00', '23 11:30', '23 12:00',
                                                          '24 10:00', '24 10:30', '24 11:00', '24 11:30', '24 12:00']
        assert got[0][1] == '今天 10:30' and got[4][1] == '明天 10:00'
        assert pickup_service.parse('2026-10-23T11:00', tid, now=now) == datetime(2026, 10, 23, 11, 0)
        for bad in ('2026-10-23T11:10', '2026-10-23T12:30', '2026-10-25T10:00', 'abc'):
            with pytest.raises(AppError):
                pickup_service.parse(bad, tid, now=now)
        with pytest.raises(AppError) as exc:
            pickup_service.parse('2026-10-23T10:00', tid, now=now)
        assert exc.value.status == 409, '已過的時段要求重新選擇'
        # 停留在結帳頁較久：最少提前時間打折扣仍可送出
        assert pickup_service.parse('2026-10-23T10:30', tid, now=datetime(2026, 10, 23, 10, 15))


# ===========================================================================
# 線上端：預約時間存入訂單並送到分店；分店的評分上傳線上端
# ===========================================================================
@pytest.fixture(scope='module')
def env():
    e = Env()
    e.item_uuid = _add_item(e.b, '預約便當', price=120, stock=40)
    _register_member(e.b, '預約會員', '0966400001')
    e.sync_all()
    yield e
    e.close()


def _hub_settings(env, key, **values):
    from app.services import settings_service
    with tenant_ctx(env.hub) as tenant:
        settings_service.put(key, dict(settings_service.get(key, tenant.id), **values), tenant_id=tenant.id)
        db.session.commit()


def test_online_order_with_pickup_time_reaches_branch(env):
    from app.errors import AppError
    hub_item = _item(env.hub, env.item_uuid)['id']
    user_id = _member(env.hub, '0966400001')['id']
    with pytest.raises(AppError):
        _checkout(env.hub, [line(hub_item)], node_uuid=env.node_b_uuid, user_id=user_id,
                  pickup_at=(datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:00'))
    _hub_settings(env, 'online_order', scheduled_pickup=True, open_time='00:00', close_time='23:59', slot_minutes=15,
                  lead_minutes=0, days_ahead=1)
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=12, minute=30, second=0, microsecond=0)
    order = _checkout(env.hub, [line(hub_item)], node_uuid=env.node_b_uuid, user_id=user_id,
                      pickup_at=tomorrow.strftime('%Y-%m-%dT%H:%M'))
    with env.hub.app_context():
        assert Order.query.filter_by(uuid=order['uuid']).first().pickup_at == tomorrow
    env.sync(env.b)
    with env.b.app_context():
        o = Order.query.filter_by(uuid=order['uuid']).first()
        assert o.pickup_at == tomorrow and o.pickup_at_label == tomorrow.strftime('%m/%d %H:%M')

    # 分店的廚房看板：明天的預約今天不顯示；POS 當日訂單顯示並標示預約
    from tests.support import login_staff
    kitchen = login_staff(env.b, 'b_kitchen', 'kitchen')
    ids = [x['id'] for x in kitchen.get('/api/kitchen_orders').get_json()['orders']]
    with env.b.app_context():
        oid = Order.query.filter_by(uuid=order['uuid']).first().id
    assert oid not in ids
    pos = login_staff(env.b, 'b_cashier', 'staff')
    live = next(x for x in pos.get('/api/pos/today_orders').get_json()['orders'] if x['id'] == oid)
    assert live['pickup_at'] == tomorrow.strftime('%m/%d %H:%M')

    # 隔天：昨天建立、預約今天的訂單不會被換日結案，而且出現在廚房看板
    from app.services.order_service import auto_close_stale_orders
    with env.b.app_context():
        o = db.session.get(Order, oid)
        o.created_at = datetime.now() - timedelta(days=1)
        o.pickup_at = datetime.now() + timedelta(minutes=30)
        db.session.commit()
        auto_close_stale_orders()
        assert db.session.get(Order, oid).status == 'Pending'
    card = next(x for x in kitchen.get('/api/kitchen_orders').get_json()['orders'] if x['id'] == oid)
    assert card['pickup_at'] == (datetime.now() + timedelta(minutes=30)).strftime('%H:%M')


def test_branch_feedback_is_uploaded_to_hub(env):
    from app.services import feedback_service
    order = _checkout(env.b, [line(_item(env.b, env.item_uuid)['id'])])
    with tenant_ctx(env.b):
        o = Order.query.filter_by(uuid=order['uuid']).first()
        o.status = 'PickedUp'
        db.session.commit()
        feedback_service.submit(o, 4, '好吃')
    env.sync(env.b)
    with env.hub.app_context():
        hub_order = Order.query.filter_by(uuid=order['uuid']).first()
        fb = OrderFeedback.query.filter_by(order_id=hub_order.id).execution_options(skip_tenant_filter=True).one()
        assert (fb.rating, fb.comment) == (4, '好吃')


# ===========================================================================
# 門市：桌邊呼叫服務
# ===========================================================================
def _new_table(app, admin_client, name):
    from app.models.table import DiningTable
    admin_client.post('/admin/tables', data={'action': 'create', 'names': name})
    with app.app_context():
        return DiningTable.query.filter_by(name=name).first().token


def test_table_service_call_flow(app, admin_client, staff_client):
    token = _new_table(app, admin_client, 'C9')
    phone = app.test_client()
    phone.get(f'/t/{token}')
    page = phone.get('/').get_data(as_text=True)
    assert 'id="serviceCallBox"' in page
    r = phone.post('/api/table/call', json={'kind': 'water'})
    assert r.status_code == 200 and r.get_json()['created'] and '加水' in r.get_json()['message']
    again = phone.post('/api/table/call', json={'kind': 'water'})
    assert again.get_json()['created'] is False, '未處理前不重複建立'
    assert phone.post('/api/table/call', json={'kind': 'bill'}).get_json()['created']
    assert phone.post('/api/table/call', json={'kind': 'dance'}).status_code == 400
    assert app.test_client().post('/api/table/call', json={'kind': 'help'}).status_code == 403, '需掃描桌位'

    calls = staff_client.get('/api/pos/today_orders').get_json()['service_calls']
    mine = [c for c in calls if c['table'] == 'C9']
    assert sorted(c['kind'] for c in mine) == ['bill', 'water']
    assert 'id="posCallsBtn"' in staff_client.get('/pos').get_data(as_text=True)
    left = staff_client.post(f"/api/pos/service_calls/{mine[0]['id']}/done").get_json()['service_calls']
    assert mine[0]['id'] not in [c['id'] for c in left]
    with app.app_context():
        handled = db.session.get(ServiceCall, mine[0]['id'])
        assert handled.handled_at is not None and handled.handled_by
    # 處理後可再次呼叫
    assert phone.post('/api/table/call', json={'kind': mine[0]['kind']}).get_json()['created']


def test_service_call_cannot_be_handled_by_other_shop(app, admin_client):
    from tests.support import other_shop_owner_client
    token = _new_table(app, admin_client, 'C8')
    phone = app.test_client()
    phone.get(f'/t/{token}')
    phone.post('/api/table/call', json={'kind': 'help'})
    with app.app_context():
        cid = ServiceCall.query.filter_by(kind='help').order_by(ServiceCall.id.desc()).first().id
    assert other_shop_owner_client(app).post(f'/api/pos/service_calls/{cid}/done').status_code == 404


# ===========================================================================
# 點餐機：待機輪播、大字模式、熱量標示
# ===========================================================================
def test_attract_carousel_on_kiosk_landing(app, store_settings):
    make_item(app, '輪播招牌飯', price=150, image_path='attract.jpg', is_recommended=True)
    page = app.test_client().get('/?shop=default').get_data(as_text=True)
    assert 'id="attractOverlay"' in page and 'js/kiosk-attract.js' in page
    assert '"seconds": 60' in page and '/media/menu/' in page
    assert 'id="a11yToggleLanding"' in page
    store_settings('menu', attract_seconds=0, accessibility_button=False)
    page = app.test_client().get('/?shop=default').get_data(as_text=True)
    assert 'id="attractOverlay"' not in page and 'a11yToggleLanding' not in page
    member_page = guest_client(app).get('/').get_data(as_text=True)
    assert 'id="attractOverlay"' not in member_page, '點餐中不輪播'


def test_calories_and_nutrition_shown_and_configurable(app, admin_client, store_settings):
    item = make_item(app, '熱量雞胸餐', price=160, category='輕食')
    form = {'action': 'item', 'item_id': str(item), 'avail_days': list('1234567'), 'calories': '520',
            'nutrition': '蛋白質 42g、脂肪 12g'}
    assert admin_client.post('/admin/menu_pro', data=form).status_code == 302
    page = guest_client(app).get('/').get_data(as_text=True)
    assert '520 kcal' in page and '蛋白質 42g' in page and 'id="cart-calories-row"' in page
    assert '"calories": 520' in page
    store_settings('menu', show_calories=False)
    page = guest_client(app).get('/').get_data(as_text=True)
    assert '520 kcal' not in page and 'menu-nutrition' not in page
    huge = dict(form, calories='999999')
    admin_client.post('/admin/menu_pro', data=huge)
    from app.models.menu import MenuItem
    with app.app_context():
        assert db.session.get(MenuItem, item).calories == 5000


# ===========================================================================
# 餐後評分
# ===========================================================================
def test_feedback_after_meal_with_google_link_and_low_rating_alert(app, staff_client, admin_client, store_settings,
                                                                   monkeypatch):
    from app.services import monitor_service
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append((msg, kw)))
    store_settings('feedback', enabled=True, google_review_url='https://g.page/r/test-review', review_min_rating=4,
                   alert_max_rating=2)
    item = make_item(app, '評分牛排', price=300)

    def new_order():
        r = staff_client.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash',
                                                           'order_type': '內用', 'cash_received': 300})
        return r.get_json()['order_id'], r.get_json()['order_uuid']

    oid, uuid = new_order()
    phone = app.test_client()
    data = phone.get(f'/api/track/{uuid}').get_json()
    assert data['feedback']['enabled'] is False, '出餐前不能評分'
    assert phone.post(f'/api/track/{uuid}/feedback', json={'rating': 5}).status_code == 409
    with app.app_context():
        db.session.get(Order, oid).status = 'Completed'
        db.session.commit()
    page = phone.get(f'/track/{uuid}').get_data(as_text=True)
    assert 'id="feedbackBox"' in page and '"enabled": true' in page
    assert phone.post(f'/api/track/{uuid}/feedback', json={'rating': 9}).status_code == 400
    r = phone.post(f'/api/track/{uuid}/feedback', json={'rating': 5, 'comment': '很棒'})
    assert r.status_code == 200 and r.get_json()['review_url'] == 'https://g.page/r/test-review'
    assert phone.post(f'/api/track/{uuid}/feedback', json={'rating': 1}).status_code == 409, '每張訂單一次'
    assert phone.get(f'/api/track/{uuid}').get_json()['feedback']['done'] is True
    assert not alerts

    oid2, uuid2 = new_order()
    with app.app_context():
        db.session.get(Order, oid2).status = 'PickedUp'
        db.session.commit()
    r = phone.post(f'/api/track/{uuid2}/feedback', json={'rating': 2, 'comment': '<b>太鹹</b>'})
    assert r.get_json()['review_url'] == '', '低分不邀請 Google 評論'
    assert alerts and '2 星' in alerts[0][0] and '太鹹' in alerts[0][0]

    page = admin_client.get('/admin/feedback').get_data(as_text=True)
    assert '&lt;b&gt;太鹹&lt;/b&gt;' in page and '很棒' in page
    assert 'href="/admin/feedback"' in admin_client.get('/admin/settings').get_data(as_text=True)

    store_settings('feedback', enabled=False)
    oid3, uuid3 = new_order()
    with app.app_context():
        db.session.get(Order, oid3).status = 'Completed'
        db.session.commit()
    assert phone.post(f'/api/track/{uuid3}/feedback', json={'rating': 5}).status_code == 404


def test_feedback_window_and_unknown_order(app, staff_client):
    item = make_item(app, '過期評分餐', price=50)
    r = staff_client.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash',
                                                       'order_type': '內用', 'cash_received': 50})
    oid, uuid = r.get_json()['order_id'], r.get_json()['order_uuid']
    with app.app_context():
        o = db.session.get(Order, oid)
        o.status, o.created_at = 'PickedUp', datetime.now() - timedelta(days=30)
        db.session.commit()
    assert app.test_client().post(f'/api/track/{uuid}/feedback', json={'rating': 5}).status_code == 409
    assert app.test_client().post('/api/track/short/feedback', json={'rating': 5}).status_code == 404
