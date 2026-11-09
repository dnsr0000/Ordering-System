"""
接單控制 (暫停接單 / 每時段訂單上限)、預估等候時間、再點一次。
"""
import time
from datetime import datetime, timedelta

import pytest

from tests.support import checkout, default_tenant_id, line, login_staff, make_item, make_member, member_client

pytestmark = pytest.mark.usefixtures('_per_test_isolation')


# ---------------------------------------------------------------------------
# 接單控制
# ---------------------------------------------------------------------------
def test_pause_blocks_kiosk_but_not_pos(app, store_settings):
    item = make_item(app, '暫停接單漢堡', price=90)
    pos = login_staff(app, 'intake_cashier', 'staff')
    guest = app.test_client()
    guest.get('/?shop=default')
    guest.post('/guest_login')
    # 先讓 store_settings 記下原值：中途斷言失敗時測試結束也會恢復接單 (否則後面的結帳測試全部 409)
    store_settings('order_intake', paused=False, resume_at='')

    # POS 一鍵暫停 (15 分鐘後自動恢復)
    r = pos.post('/api/pos/intake', json={'paused': True, 'minutes': 15})
    assert r.status_code == 200 and r.get_json()['paused'] is True and r.get_json()['resume_at']
    r = checkout(guest, [line(item)])
    assert r.status_code == 409 and r.get_json().get('intake_paused') is True
    assert '暫停接單' in r.get_json()['message'] or '稍後' in r.get_json()['message']
    # 點餐頁顯示橫幅
    assert 'intake-paused-banner' in guest.get('/').get_data(as_text=True)
    # POS 櫃檯照常結帳
    r = pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': '內用'})
    assert r.status_code == 200, r.get_data(as_text=True)
    # 斷線暫存單補送：未註冊的用戶端無法證明當時確實離線，照常套用暫停接單
    body = {'shop': 'default', 'code': 'AB12', 'queued_at': datetime.now().isoformat(timespec='seconds'),
            'order': {'client_order_id': 'offline-intake-0001', 'items': [line(item)], 'order_type': '內用'}}
    r = guest.post('/api/kiosk/offline_order', json=body)
    assert r.status_code == 409 and r.get_json().get('intake_paused') is True, r.get_data(as_text=True)
    # 已註冊的點餐機補送不受影響 (點餐機當時已收單)
    from flask import g

    from app.extensions import db
    from app.services import device_service
    tid = default_tenant_id(app)
    with app.app_context():
        g.tenant_id = tid
        d = device_service.create(tid, '暫停補送點餐機', 'kiosk')
        db.session.commit()
        d, raw = device_service.pair(tid, d.pair_code)
        db.session.commit()
    kiosk = app.test_client()
    kiosk.set_cookie('kiosk_device', raw)
    kiosk.get('/?shop=default')
    r = kiosk.post('/api/kiosk/offline_order', json=body)
    assert r.status_code == 200, r.get_data(as_text=True)

    # 恢復
    r = pos.post('/api/pos/intake', json={'paused': False})
    assert r.get_json()['paused'] is False
    assert checkout(guest, [line(item)]).status_code == 200
    assert 'intake-paused-banner' not in guest.get('/').get_data(as_text=True)
    store_settings('order_intake', paused=False, resume_at='')


def test_pause_auto_resumes_after_deadline(app, store_settings):
    from app.services import intake_service
    item = make_item(app, '自動恢復雞排', price=70)
    guest = app.test_client()
    guest.get('/?shop=default')
    guest.post('/guest_login')
    past = (datetime.now() - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M')
    store_settings('order_intake', paused=True, resume_at=past)
    with app.test_request_context('/'):
        assert intake_service.is_paused(default_tenant_id(app)) is False
    assert checkout(guest, [line(item)]).status_code == 200
    # 手動恢復的暫停沒有期限
    store_settings('order_intake', paused=True, resume_at='')
    assert checkout(guest, [line(item)]).status_code == 409
    store_settings('order_intake', paused=False, resume_at='')


def test_pause_api_requires_pos_role_and_audits(app, store_settings):
    from app.models.user import StaffAuditLog
    kitchen = login_staff(app, 'intake_kitchen', 'kitchen')
    assert kitchen.post('/api/pos/intake', json={'paused': True}).status_code in (302, 403)
    pos = login_staff(app, 'intake_cashier2', 'staff')
    pos.post('/api/pos/intake', json={'paused': True, 'minutes': 0})
    pos.post('/api/pos/intake', json={'paused': False})
    with app.app_context():
        actions = [a.action_type for a in StaffAuditLog.query.order_by(StaffAuditLog.id.desc()).limit(2).all()]
    assert set(actions) == {'INTAKE_PAUSE', 'INTAKE_RESUME'}
    store_settings('order_intake', paused=False, resume_at='')


def test_window_limit_counts_only_customer_orders(app, store_settings):
    item = make_item(app, '限量滷肉飯', price=60)
    pos = login_staff(app, 'intake_cashier3', 'staff')
    guest = app.test_client()
    guest.get('/?shop=default')
    guest.post('/guest_login')
    store_settings('order_intake', paused=False, resume_at='', window_minutes=60, max_orders_per_window=2)
    try:
        # 上限內的訂單由前面測試的同時段訂單決定：先數目前同時段已有幾張 (同一個資料庫共用)
        from app.services import intake_service
        # 時段以整點切：快到下一個時段時先等過整點，免得結帳途中換時段 (新時段從 0 張算起) 造成偶發失敗
        _start, end = intake_service.window_bounds(datetime.now(), 60)
        if (end - datetime.now()).total_seconds() < 30:
            time.sleep(max(0.0, (end - datetime.now()).total_seconds()) + 1)
        with app.test_request_context('/'):
            tid = default_tenant_id(app)
            have = intake_service.window_count(tid, datetime.now(), 60)
        store_settings('order_intake', max_orders_per_window=have + 1)
        assert checkout(guest, [line(item)]).status_code == 200
        r = checkout(guest, [line(item)])
        assert r.status_code == 409 and r.get_json().get('intake_full') is True
        # POS 不受上限影響，POS 的訂單也不算進額度
        r = pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': '內用'})
        assert r.status_code == 200
        with app.test_request_context('/'):
            assert intake_service.window_count(tid, datetime.now(), 60) == have + 1
    finally:
        store_settings('order_intake', max_orders_per_window=0)


def test_window_bounds():
    from app.services.intake_service import window_bounds
    start, end = window_bounds(datetime(2026, 10, 31, 12, 47), 15)
    assert start == datetime(2026, 10, 31, 12, 45) and end == datetime(2026, 10, 31, 13, 0)
    start, end = window_bounds(datetime(2026, 10, 31, 23, 59), 60)
    assert start == datetime(2026, 10, 31, 23, 0) and end == datetime(2026, 11, 1, 0, 0)


def test_settings_page_has_intake_section(app):
    owner = login_staff(app, 'intake_owner', 'owner')
    html = owner.get('/admin/settings').get_data(as_text=True)
    assert '接單控制' in html
    assert '預估等候時間' in owner.get('/admin/settings/kds').get_data(as_text=True)
    assert '每時段最多接幾張訂單' in owner.get('/admin/settings/order_intake').get_data(as_text=True)


# ---------------------------------------------------------------------------
# 預估等候時間
# ---------------------------------------------------------------------------
def test_eta_workload_and_estimate(app, store_settings):
    from app.services import eta_service
    from app.models.menu import MenuItem
    fast = make_item(app, 'ETA 紅茶', price=30, prep_minutes=2)
    slow = make_item(app, 'ETA 牛排', price=300, prep_minutes=12)
    plain = make_item(app, 'ETA 薯條', price=50)   # 未設定 → 預設 5 分鐘
    store_settings('kds', eta_enabled=True, eta_base_minutes=3, eta_default_prep=5, eta_parallel=3)
    with app.app_context():
        items = {i.id: i for i in MenuItem.query.filter(MenuItem.id.in_([fast, slow, plain])).all()}
        assert eta_service.workload([(items[fast], 1)], 5) == 2
        assert eta_service.workload([(items[plain], 2)], 5) == 6       # 5 + 1 份額外裝盤
        assert eta_service.workload([(items[fast], 1), (items[slow], 1)], 5) == 13
        assert eta_service.workload([], 5) == 0
    # 空廚房 (用很久以前的時間，不會有排隊中的訂單)
    tid = default_tenant_id(app)
    at = datetime(2001, 1, 1, 12, 0)
    with app.test_request_context('/'):
        assert eta_service.estimate(tid, [(items[slow], 1)], at) == 3 + 12
    store_settings('kds', eta_enabled=False)
    with app.test_request_context('/'):
        assert eta_service.estimate(tid, [(items[slow], 1)], at) == 0


def test_eta_on_checkout_receipt_track_and_my_orders(app, store_settings):
    from app.models.order import Order
    item = make_item(app, 'ETA 套餐', price=120, prep_minutes=8)
    uid = make_member(app, 'ETA 會員', '0977800011', pin='2468')
    store_settings('kds', eta_enabled=True, eta_base_minutes=3, eta_default_prep=5, eta_parallel=3)
    c = member_client(app, '0977800011', '2468')
    r = c.post('/api/eta', json={'items': [line(item, 2)]})
    assert r.status_code == 200 and r.get_json()['minutes'] >= 3 + 9 and r.get_json()['ready_at']
    r = checkout(c, [line(item)])
    assert r.status_code == 200
    data = r.get_json()
    assert data['eta'] and len(data['eta']) == 5
    with app.app_context():
        order = Order.query.filter_by(user_id=uid).order_by(Order.id.desc()).first()
        assert order.estimated_ready_at is not None and order.estimated_ready_at > order.created_at
        uuid = order.uuid
    # 我的訂單 / 出餐進度頁 / 廚房看板都看得到
    mine = c.get('/my_orders').get_json()
    assert mine[0]['eta'] == data['eta'] and mine[0]['can_reorder'] is True
    track = app.test_client().get(f'/api/track/{uuid}').get_json()
    assert track['eta'] == data['eta']
    kitchen = login_staff(app, 'eta_kitchen', 'kitchen')
    cards = kitchen.get('/api/kitchen_orders').get_json()['orders']
    assert any(o['eta'] == data['eta'] for o in cards)
    # 出餐後不再顯示預計時間
    kitchen.post(f'/api/orders/{order.id}/status', json={'status': 'Completed'})
    assert app.test_client().get(f'/api/track/{uuid}').get_json()['eta'] == ''
    # 排隊中的訂單讓下一張的預估變長
    make_item(app, 'ETA 慢餐', price=120, prep_minutes=30)
    big = login_staff(app, 'eta_cashier', 'staff')
    for _ in range(3):
        big.post('/api/pos/checkout', json={'items': [line(item, 3)], 'payment_method': 'Cash', 'order_type': '內用'})
    later = c.post('/api/eta', json={'items': [line(item)]}).get_json()['minutes']
    assert later > 3 + 8


def test_eta_disabled_returns_zero_and_prep_minutes_form(app, store_settings):
    item = make_item(app, 'ETA 關閉', price=20)
    store_settings('kds', eta_enabled=False)
    c = app.test_client()
    c.get('/?shop=default')
    c.post('/guest_login')
    assert c.post('/api/eta', json={'items': [line(item)]}).get_json()['minutes'] == 0
    r = checkout(c, [line(item)])
    assert r.status_code == 200 and r.get_json()['eta'] == ''
    # 菜單進階設定可設定製作時間
    owner = login_staff(app, 'eta_owner', 'owner')
    from app.extensions import db
    from app.models.menu import MenuItem
    page = owner.get('/admin/menu').get_data(as_text=True)
    assert 'name="prep_minutes"' in page
    owner.post('/admin/menu', data={'action': 'item', 'item_id': item, 'prep_minutes': '7',
                                        'avail_days': ['1', '2', '3', '4', '5', '6', '7']})
    with app.app_context():
        assert db.session.get(MenuItem, item).prep_minutes == 7


# ---------------------------------------------------------------------------
# 再點一次
# ---------------------------------------------------------------------------
def test_reorder_restores_lines_and_skips_unavailable(app):
    from app.extensions import db
    from app.models.menu import ComboOption, ComboOptionItem, MenuItem, ModifierOption
    from app.models.order import Order
    main = make_item(app, '再點牛肉麵', price=150, modifiers='addons', size_options='小碗:-20, 大碗:30')
    side = make_item(app, '再點小菜', price=40)
    gone = make_item(app, '再點停售', price=10)
    with app.app_context():
        tid = db.session.get(MenuItem, main).tenant_id
        mod = ModifierOption(tenant_id=tid, name='再點加蛋', price=15, is_active=True)
        combo = ComboOption(tenant_id=tid, main_item_id=main, name='再點套餐', additional_price=30)
        db.session.add_all([mod, combo])
        db.session.flush()
        db.session.add(ComboOptionItem(tenant_id=tid, combo_option_id=combo.id, menu_item_id=side, position=0))
        db.session.commit()
        mod_id, combo_id = mod.id, combo.id
    uid = make_member(app, '再點會員', '0977800022', pin='2468')
    c = member_client(app, '0977800022', '2468')
    r = checkout(c, [line(main, 2, size='大碗', modifier_ids=[mod_id], combo_option_id=combo_id, customization='不要香菜'),
                     line(gone)])
    assert r.status_code == 200, r.get_data(as_text=True)
    order_id = r.get_json()['order_id']
    with app.app_context():
        order = db.session.get(Order, order_id)
        assert any('[套餐: 再點套餐]' in i.customization and '大碗' in i.customization for i in order.items)
        db.session.get(MenuItem, gone).is_sold_out = True
        db.session.commit()

    r = c.get(f'/api/reorder/{order_id}')
    assert r.status_code == 200
    body = r.get_json()
    assert body['skipped'] == ['再點停售']
    assert len(body['lines']) == 1
    ln = body['lines'][0]
    assert ln['menu_item_id'] == main and ln['quantity'] == 2 and ln['size'] == '大碗'
    assert ln['modifier_ids'] == [mod_id] and ln['combo_option_id'] == combo_id and ln['customization'] == '不要香菜'
    assert ln['is_add_on'] is False
    # 別人的訂單看不到
    make_member(app, '再點別人', '0977800033', pin='2468')
    other = member_client(app, '0977800033', '2468')
    assert other.get(f'/api/reorder/{order_id}').status_code == 404
    # 訪客只能再點本次進場的訂單
    guest = app.test_client()
    guest.get('/?shop=default')
    guest.post('/guest_login')
    assert guest.get(f'/api/reorder/{order_id}').status_code == 404
    g = checkout(guest, [line(side)])
    assert guest.get(f"/api/reorder/{g.get_json()['order_id']}").get_json()['lines'][0]['menu_item_id'] == side
    assert uid


def test_reorder_skips_reward_gift_and_cancelled(app):
    from app.extensions import db
    from app.models.order import Order
    item = make_item(app, '再點飲料', price=50)
    uid = make_member(app, '再點會員二', '0977800044', pin='2468')
    c = member_client(app, '0977800044', '2468')
    order_id = checkout(c, [line(item)]).get_json()['order_id']
    with app.app_context():
        order = db.session.get(Order, order_id)
        order.items[0].is_reward = True
        db.session.commit()
    body = c.get(f'/api/reorder/{order_id}').get_json()
    assert body['lines'] == [] and body['skipped'] == ['再點飲料']
    mine = c.get('/my_orders').get_json()
    assert mine[0]['can_reorder'] is False
    assert uid
