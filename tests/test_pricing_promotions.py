"""結帳金額規則：通路價、供應時段、改價與整單折扣 (主管核准)、服務費、會員等級、促銷活動 (含預覽 API)"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.member import MemberTier
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.promotion import Promotion
from app.models.user import User
from tests.support import checkout, default_tenant_id, guest_client, line, login_staff, make_item, make_member

POS = '/api/pos/checkout'


def _order(app, order_id):
    with app.app_context():
        o = db.session.get(Order, order_id)
        return {c.name: getattr(o, c.name) for c in Order.__table__.columns} | {
            'items': [(i.item_name, i.price, i.quantity, i.is_gift, i.original_price) for i in o.items]}


def test_channel_prices_takeout_and_kiosk(app, staff_client):
    iid = make_item(app, '通路價牛肉麵', price=150, stock=50, takeout_price=140, delivery_price=180)
    r = checkout(staff_client, [line(iid)], url=POS, order_type='外帶', cash_received=200)
    assert r.status_code == 200 and r.get_json()['total_price'] == 140
    r = checkout(staff_client, [line(iid)], url=POS, order_type='內用', cash_received=200)
    assert r.get_json()['total_price'] == 150
    r = checkout(guest_client(app), [line(iid)], order_type='外帶')
    assert r.get_json()['total_price'] == 140
    # 外送單 (POS 手動登錄) 用外送價、平台代收
    r = staff_client.post('/api/pos/delivery_order', json={'items': [line(iid)], 'platform': 'foodpanda', 'external_ref': 'FP-0001'})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['total_price'] == 180 and d['payment_method'] == '平台代收' and d['order_type'] == '外送'
    assert staff_client.post('/api/pos/delivery_order', json={'items': [line(iid)], 'platform': 'foodpanda',
                                                              'external_ref': 'FP-0001'}).status_code == 409


def test_service_window_blocks_checkout(app, staff_client):
    now = datetime.now()
    closed = (now + timedelta(hours=2)).strftime('%H:%M'), (now + timedelta(hours=3)).strftime('%H:%M')
    iid = make_item(app, '早餐限定蛋餅', price=40, stock=20, avail_start=closed[0], avail_end=closed[1])
    if closed[0] < closed[1]:   # 測試時間太接近午夜時跳過 (跨日時段另有測試)
        r = checkout(staff_client, [line(iid)], url=POS)
        assert r.status_code == 400 and '目前不供應' in r.get_json()['message']
    iid2 = make_item(app, '季節限定冰', price=60, stock=20, sale_until=(now - timedelta(days=1)).date())
    assert '已停售' in checkout(staff_client, [line(iid2)], url=POS).get_json()['message']
    with app.app_context():
        item = MenuItem(name='跨午夜宵夜', price=1, avail_start='22:00', avail_end='02:00', tenant_id=default_tenant_id(app))
        assert item.service_status(now.replace(hour=23, minute=0))[0] is True
        assert item.service_status(now.replace(hour=1, minute=30))[0] is True
        assert item.service_status(now.replace(hour=12, minute=0))[0] is False
        item.avail_days = '67'
        monday = now - timedelta(days=now.weekday())
        assert item.service_status(monday.replace(hour=23))[0] is False


def test_price_override_needs_manager_approval(app, staff_client, admin_client):
    iid = make_item(app, '改價測試餐', price=100, stock=20)
    payload = [dict(line(iid), override_price=80)]
    r = checkout(staff_client, payload, url=POS)
    assert r.status_code == 403 and r.get_json()['need_approval'] is True
    login_staff(app, 'price_mgr', 'manager')
    r = checkout(staff_client, payload, url=POS, approver_username='price_mgr', approver_password='StaffPass!2026')
    assert r.status_code == 200, r.get_json()
    o = _order(app, r.get_json()['order_id'])
    assert o['total_price'] == 80 and o['approved_by'] == 'price_mgr' and o['items'][0][4] == 100
    # 有折扣權限的店長 / 負責人不需要另外核准
    assert checkout(admin_client, payload, url=POS).status_code == 200
    # 點餐機不可改價
    assert checkout(guest_client(app), payload).status_code == 403


def test_order_discount_threshold_and_approval(app, staff_client, store_settings):
    iid = make_item(app, '整單折扣餐', price=200, stock=20)
    store_settings('pos', discount_approval_percent=10)
    r = checkout(staff_client, [line(iid)], url=POS, order_discount={'type': 'percent', 'value': 10})
    assert r.status_code == 200 and r.get_json()['total_price'] == 180   # 10% 以內不需核准
    r = checkout(staff_client, [line(iid)], url=POS, order_discount={'type': 'amount', 'value': 50})
    assert r.status_code == 403   # 25% 超過門檻
    r = checkout(staff_client, [line(iid)], url=POS, order_discount={'type': 'amount', 'value': 50},
                 approver_username='price_mgr', approver_password='StaffPass!2026')
    assert r.status_code == 200 and r.get_json()['total_price'] == 150 and r.get_json()['manual_discount'] == 50


def test_service_charge_on_dine_in_only(app, staff_client, store_settings):
    iid = make_item(app, '服務費測試餐', price=300, stock=20)
    store_settings('pos', service_charge_percent=10, service_charge_scope=['pos_dine_in'])
    d = checkout(staff_client, [line(iid)], url=POS, order_type='內用', cash_received=400).get_json()
    assert (d['total_price'], d['service_charge'], d['subtotal']) == (330, 30, 300)
    d = checkout(staff_client, [line(iid)], url=POS, order_type='外帶', cash_received=400).get_json()
    assert (d['total_price'], d['service_charge']) == (300, 0)
    # 點餐機不在範圍內
    assert checkout(guest_client(app), [line(iid)]).get_json()['service_charge'] == 0
    o = _order(app, d['order_id'])
    assert o['total_price'] + o['discount_amount'] - o['service_charge'] == 300


def test_member_tier_discount_and_points_multiplier(app, staff_client):
    tid = default_tenant_id(app)
    with app.app_context():
        db.session.add_all([MemberTier(tenant_id=tid, name='一般', min_spend=0),
                            MemberTier(tenant_id=tid, name='VIP測試', min_spend=500, points_multiplier=2, discount_percent=10)])
        db.session.commit()
    uid = make_member(app, '等級會員', '0955100200')
    iid = make_item(app, '等級測試餐', price=600, stock=20)
    with app.app_context():
        from app.models.user import RewardSetting
        per = RewardSetting.query.filter_by(tenant_id=tid).first().spend_per_point or 100   # 其他測試可能改過回饋比例
    d1 = checkout(staff_client, [line(iid)], url=POS, user_id=uid, cash_received=1000).get_json()
    assert d1['total_price'] == 600 and d1['points_earned'] == 600 // per       # 第一筆還不是 VIP
    with app.app_context():
        user = db.session.get(User, uid)
        assert user.tier.name == 'VIP測試' and user.total_spent >= 600
    d2 = checkout(staff_client, [line(iid)], url=POS, user_id=uid, cash_received=1000).get_json()
    assert d2['total_price'] == 540 and d2['member_discount'] == 60 and d2['points_earned'] == 540 // per * 2
    # 取消後累積消費重算 (等級只升不降)
    staff = login_staff(app, 'tier_owner', 'owner')
    assert staff.post(f"/api/orders/{d2['order_id']}/status", json={'status': 'Cancelled'}).status_code == 200
    with app.app_context():
        user = db.session.get(User, uid)
        assert user.total_spent == 600 and user.tier.name == 'VIP測試'
        # 移除本測試的等級 (其他測試檔共用資料庫)
        User.query.filter(User.tier_id.isnot(None)).update({'tier_id': None})
        MemberTier.query.filter(MemberTier.name.in_(('一般', 'VIP測試'))).delete()
        db.session.commit()


def _promo(app, **kw):
    with app.app_context():
        p = Promotion(tenant_id=default_tenant_id(app), **kw)
        db.session.add(p)
        db.session.commit()
        return p.id


def _drop_promos(app, *ids):
    with app.app_context():
        Promotion.query.filter(Promotion.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()


def test_promotions_bogo_nth_threshold_and_gift(app, staff_client):
    tea = make_item(app, '促銷綠茶', price=30, stock=100, category='促銷飲料')
    milk = make_item(app, '促銷奶茶', price=50, stock=100, category='促銷飲料')
    gift = make_item(app, '促銷贈品餅乾', price=20, stock=5, category='點心')
    with app.app_context():
        gift_uuid = db.session.get(MenuItem, gift).uuid
    p1 = _promo(app, name='飲料買二送一', kind='bogo', target_type='category', target_value='促銷飲料', buy_qty=2, get_qty=1, priority=10)
    p2 = _promo(app, name='滿200送餅乾', kind='threshold_gift', threshold=200, gift_item_uuid=gift_uuid)
    try:
        # 3 杯：50, 50, 30 → 最便宜的 30 免費
        r = checkout(staff_client, [line(milk, 2), line(tea, 1)], url=POS)
        d = r.get_json()
        assert d['promotion_discount'] == 30 and d['total_price'] == 100 and '飲料買二送一' in d['promotion_names']
        # 預覽 API 與結帳結果一致
        preview = staff_client.post('/api/promotions/preview', json={'items': [line(milk, 2), line(tea, 1)], 'channel': 'pos'}).get_json()
        assert preview['discount'] == 30 and preview['names'] == ['飲料買二送一']
        # 滿額贈：贈品以 0 元加入並扣庫存
        d = checkout(staff_client, [line(milk, 6)], url=POS, cash_received=1000).get_json()
        assert d['promotion_discount'] == 100   # 6 杯買二送一 = 送 2 杯
        o = _order(app, d['order_id'])
        assert ('促銷贈品餅乾', 0, 1, True, None) in o['items']
        with app.app_context():
            assert db.session.get(MenuItem, gift).stock == 4
    finally:
        _drop_promos(app, p1, p2)
    p3 = _promo(app, name='第二杯半價', kind='nth_discount', target_type='items', target_value='', nth=2, percent=50)
    with app.app_context():
        Promotion.query.filter_by(id=p3).update({'target_value': db.session.get(MenuItem, milk).uuid})
        db.session.commit()
    p4 = _promo(app, name='滿300折50', kind='threshold_discount', threshold=300, amount=50, stack_with_coupon=False)
    try:
        d = checkout(staff_client, [line(milk, 2)], url=POS).get_json()
        assert d['promotion_discount'] == 25 and d['total_price'] == 75
        d = checkout(staff_client, [line(milk, 8)], url=POS, cash_received=1000).get_json()
        # 第二杯半價 4 次 = 100；剩 300 達門檻再折 50
        assert d['promotion_discount'] == 150 and d['total_price'] == 250
    finally:
        _drop_promos(app, p3, p4)


def test_promotion_schedule_channel_and_member_only(app, staff_client):
    iid = make_item(app, '限時促銷餐', price=100, stock=50)
    tomorrow = (datetime.now() + timedelta(days=1)).date()
    p1 = _promo(app, name='明天才開始', kind='threshold_discount', threshold=1, amount=10, start_date=tomorrow)
    p2 = _promo(app, name='只限線上', kind='threshold_discount', threshold=1, amount=10, channels='online')
    p3 = _promo(app, name='會員限定', kind='threshold_discount', threshold=1, amount=10, member_only=True)
    try:
        assert checkout(staff_client, [line(iid)], url=POS).get_json()['promotion_discount'] == 0
        uid = make_member(app, '促銷會員', '0955100201')
        assert checkout(staff_client, [line(iid)], url=POS, user_id=uid).get_json()['promotion_discount'] == 10
    finally:
        _drop_promos(app, p1, p2, p3)


def test_promotion_admin_crud(app, admin_client):
    r = admin_client.post('/admin/promotions', data={'action': 'save', 'name': '後台建立的活動', 'kind': 'bogo',
                                                    'target_type': 'all', 'buy_qty': '1', 'get_qty': '1', 'is_active': '1',
                                                    'stack_with_coupon': '1', 'days': list('1234567')})
    assert r.status_code == 302
    with app.app_context():
        p = Promotion.query.filter_by(name='後台建立的活動').first()
        assert p is not None and p.is_active
        pid = p.id
    assert '後台建立的活動' in admin_client.get('/admin/promotions').get_data(as_text=True)
    admin_client.post('/admin/promotions', data={'action': 'delete', 'promotion_id': pid})
    with app.app_context():
        assert db.session.get(Promotion, pid) is None
    r = admin_client.post('/admin/promotions', data={'action': 'save', 'name': '缺贈品', 'kind': 'threshold_gift',
                                                    'threshold': '100', 'target_type': 'all'}, follow_redirects=True)
    assert '請選擇贈品' in r.get_data(as_text=True)
