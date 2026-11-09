"""修正小組 order-lifecycle-kitchen-1 的回歸測試：訂單狀態機、取消 / 單品作廢、換日結案、今天的訂單條件"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.user import User
from app.services import order_lifecycle
from tests.support import checkout, default_tenant_id, line, make_item, make_member, tenant_request

POS = '/api/pos/checkout'


def _status(client, oid, status):
    return client.post(f'/api/orders/{oid}/status', json={'status': status})


def _void(client, oid, item_id, qty=1, reason='測試作廢'):
    return client.post(f'/api/pos/orders/{oid}/void_item', json={'item_id': item_id, 'quantity': qty, 'reason': reason})


def _order(app, **kw):
    """直接建立一張訂單 (含明細)：items 為 [(menu_item_id, 名稱, 單價, 份數, 其他欄位)]"""
    items = kw.pop('items', [])
    with app.app_context():
        o = Order(tenant_id=default_tenant_id(app), table_number=kw.pop('table_number', 'OLK1'),
                  pickup_number=kw.pop('pickup_number', 1), **kw)
        o.items = [OrderItem(tenant_id=o.tenant_id, menu_item_id=mid, item_name=name, price=price, quantity=qty, **extra)
                   for mid, name, price, qty, extra in items]
        db.session.add(o)
        db.session.commit()
        return o.id, {i.item_name: i.id for i in o.items}


# ---------------------------------------------------------------------------
# B-056：換日結案不結待付款的線上單；隔天才付款的線上單算「今天的訂單」
# ---------------------------------------------------------------------------
def test_auto_close_skips_unpaid_online_orders_and_paid_today_counts_as_today(app):
    yesterday = datetime.now() - timedelta(days=1, hours=1)
    unpaid, _ = _order(app, total_price=100, status='Pending', payment_status='pending', created_at=yesterday)
    paid_late, _ = _order(app, total_price=100, status='Pending', payment_status='paid', created_at=yesterday,
                          paid_at=datetime.now())
    with app.app_context():
        order_lifecycle.auto_close_stale_orders()
        assert db.session.get(Order, unpaid).status == OrderStatus.PENDING
        start, end = order_lifecycle.todays_range()
        ids = {o.id for o in Order.query.filter(Order.id.in_([unpaid, paid_late]),
                                                order_lifecycle.todays_orders_condition(start, end, include_future=False))}
        assert ids == {paid_late}


# ---------------------------------------------------------------------------
# B-061：待付款 / 扣款中的訂單不可出餐
# ---------------------------------------------------------------------------
def test_unpaid_order_cannot_be_completed(app, admin_client, kitchen_client):
    for pay in ('pending', 'authorizing', 'expired'):
        oid, _ = _order(app, total_price=100, status='Pending', payment_status=pay)
        assert _status(admin_client, oid, 'Completed').status_code == 409
        assert _status(kitchen_client, oid, 'Completed').status_code == 409
        with app.app_context():
            assert db.session.get(Order, oid).status == OrderStatus.PENDING


# ---------------------------------------------------------------------------
# B-076：錯誤訊息顯示中文狀態
# ---------------------------------------------------------------------------
def test_status_error_message_uses_chinese_labels(app, admin_client):
    oid, _ = _order(app, total_price=100, status='Completed')
    r = _status(admin_client, oid, 'Completed')
    assert r.status_code == 409
    assert '已出餐' in r.get_json()['message'] and 'Completed' not in r.get_json()['message']


# ---------------------------------------------------------------------------
# B-065：取消待付款的線上單 → 付款狀態改為逾時 (不再顯示付款連結)
# ---------------------------------------------------------------------------
def test_cancel_pending_payment_order_marks_payment_expired(app, admin_client):
    oid, _ = _order(app, total_price=100, status='Pending', payment_status='pending')
    assert _status(admin_client, oid, 'Cancelled').status_code == 200
    with app.app_context():
        assert db.session.get(Order, oid).payment_status == 'expired'


# ---------------------------------------------------------------------------
# B-079：取消已結帳的會員訂單會重算累積消費
# ---------------------------------------------------------------------------
def test_cancel_settled_member_order_recomputes_total_spent(app, admin_client):
    uid = make_member(app, 'OLK1累積消費', '0955100079')
    oid, _ = _order(app, total_price=300, status='Completed', payment_status='settled', user_id=uid)
    with app.app_context():
        db.session.get(User, uid).total_spent = 999
        db.session.commit()
    assert _status(admin_client, oid, 'Cancelled').status_code == 200
    with app.app_context():
        assert db.session.get(User, uid).total_spent == 0


# ---------------------------------------------------------------------------
# B-078：已取餐的訂單作廢預設不回補庫存 (已出餐的照舊回補)
# ---------------------------------------------------------------------------
def test_cancel_picked_up_order_does_not_restock_by_default(app, admin_client):
    mid = make_item(app, 'OLK1已取餐回補', price=50, stock=10)
    picked, _ = _order(app, total_price=100, status='PickedUp', items=[(mid, 'OLK1已取餐回補', 50, 2, {})])
    done, _ = _order(app, total_price=100, status='Completed', items=[(mid, 'OLK1已取餐回補', 50, 2, {})])
    assert _status(admin_client, picked, 'Cancelled').status_code == 200
    with app.app_context():
        assert db.session.get(MenuItem, mid).stock == 10
    assert _status(admin_client, done, 'Cancelled').status_code == 200
    with app.app_context():
        assert db.session.get(MenuItem, mid).stock == 12


# ---------------------------------------------------------------------------
# B-062：作廢與取消同時發生 (檢查後才被取消) 不可再回補庫存
# ---------------------------------------------------------------------------
def test_void_item_rechecks_cancelled_status_inside_lock(app):
    mid = make_item(app, 'OLK1作廢競態', price=50, stock=10)
    oid, lines = _order(app, total_price=100, status='Pending',
                        items=[(mid, 'OLK1作廢競態', 50, 2, {})])
    tid = default_tenant_id(app)
    with app.app_context():
        with tenant_request(app, tid):
            item = db.session.get(OrderItem, lines['OLK1作廢競態'])
            assert item.order.status == OrderStatus.PENDING   # 已載入的物件仍是製作中
            # 其他人 (另一個連線) 剛把訂單取消並提交；目前工作階段的物件不會自動更新
            with db.engine.begin() as conn:
                conn.execute(update(Order.__table__).where(Order.__table__.c.id == oid)
                             .values(status=OrderStatus.CANCELLED))
            with pytest.raises(AppError) as exc:
                order_lifecycle.void_item(tid, item.id, 1, '競態')
            assert exc.value.status == 409 and '取消' in exc.value.message
            db.session.rollback()
        it = db.session.get(OrderItem, lines['OLK1作廢競態'])
        assert (it.quantity, it.voided_qty) == (2, 0)
        assert db.session.get(MenuItem, mid).stock == 10


# ---------------------------------------------------------------------------
# B-063：作廢紅利兌換品退回點數，之後整張取消不重複退
# ---------------------------------------------------------------------------
def test_void_reward_item_refunds_points_once(app, admin_client):
    main = make_item(app, 'OLK1兌換主餐', price=100, stock=20)
    reward = make_item(app, 'OLK1兌換品', price=60, stock=20, is_reward=True, reward_points=30)
    uid = make_member(app, 'OLK1兌換會員', '0955100063', points=100)
    r = checkout(admin_client, [line(main), line(reward, is_reward=True)], url=POS, user_id=uid, cash_received=1000)
    assert r.status_code == 200, r.get_data(as_text=True)
    oid = r.get_json()['order_id']
    with app.app_context():
        after_checkout = db.session.get(User, uid).points
        reward_line = OrderItem.query.filter_by(order_id=oid, menu_item_id=reward).one().id
    assert _void(admin_client, oid, reward_line).status_code == 200
    with app.app_context():
        assert db.session.get(User, uid).points == after_checkout + 30
    assert _status(admin_client, oid, 'Cancelled').status_code == 200
    with app.app_context():
        assert db.session.get(User, uid).points == 100   # 兌換點數退回一次、贈點追回


# ---------------------------------------------------------------------------
# R2-017：作廢後只剩贈品 (0 元) 時要改用取消訂單
# ---------------------------------------------------------------------------
def test_void_all_paid_items_leaving_only_gift_is_rejected(app, admin_client):
    main = make_item(app, 'OLK1贈品主餐', price=100, stock=20)
    gift = make_item(app, 'OLK1贈品', price=30, stock=20)
    oid, lines = _order(app, total_price=200, status='Pending',
                        items=[(main, 'OLK1贈品主餐', 100, 2, {}), (gift, 'OLK1贈品', 0, 1, {'is_gift': True})])
    assert _void(admin_client, oid, lines['OLK1贈品主餐'], qty=1).status_code == 200
    r = _void(admin_client, oid, lines['OLK1贈品主餐'], qty=1)
    assert r.status_code == 409
    with app.app_context():
        assert db.session.get(OrderItem, lines['OLK1贈品主餐']).quantity == 1


# ---------------------------------------------------------------------------
# B-064：桌邊未結帳訂單作廢後，贈點依新應付金額重算、服務費依比例降低
# ---------------------------------------------------------------------------
def test_void_on_unpaid_order_recomputes_points_and_service_charge(app, admin_client):
    from app.services.reward_service import get_or_create_reward_setting
    a = make_item(app, 'OLK1桌邊A', price=600, stock=20)
    b = make_item(app, 'OLK1桌邊B', price=400, stock=20)
    uid = make_member(app, 'OLK1桌邊會員', '0955100064')
    with app.app_context():
        spp = max(1, get_or_create_reward_setting(default_tenant_id(app), commit=False).spend_per_point or 100)
    oid, lines = _order(app, total_price=1100, service_charge=100, status='Pending', payment_status='unpaid',
                        user_id=uid, points_earned=1100 // spp,
                        items=[(a, 'OLK1桌邊A', 600, 1, {}), (b, 'OLK1桌邊B', 400, 1, {})])
    r = _void(admin_client, oid, lines['OLK1桌邊A'])
    assert r.status_code == 200, r.get_json()
    with app.app_context():
        o = db.session.get(Order, oid)
        assert o.total_price == 440 and o.service_charge == 40
        assert o.points_earned == 440 // spp


# ---------------------------------------------------------------------------
# R2-021：一鍵出餐的訂單編號遇到 '²' 或超長數字不可 500
# ---------------------------------------------------------------------------
def test_complete_all_ignores_non_ascii_digits_and_huge_numbers(app):
    with app.app_context():
        assert order_lifecycle.complete_all_pending_orders(default_tenant_id(app), ['²', 10 ** 20, '9' * 25]) == 0


# ---------------------------------------------------------------------------
# B-066：營業日換日時間可設定 (跨午夜營業)
# ---------------------------------------------------------------------------
def test_business_day_start_hour(app):
    saved = app.config.get('BUSINESS_DAY_START_HOUR')
    try:
        with app.app_context():
            app.config['BUSINESS_DAY_START_HOUR'] = 0
            start, end = order_lifecycle.todays_range()
            assert (start.hour, start.date(), end - start) == (0, datetime.now().date(), timedelta(days=1))
            app.config['BUSINESS_DAY_START_HOUR'] = 4
            start, end = order_lifecycle.todays_range()
            assert start.hour == 4 and start <= datetime.now() < end
    finally:
        if saved is None:
            app.config.pop('BUSINESS_DAY_START_HOUR', None)
        else:
            app.config['BUSINESS_DAY_START_HOUR'] = saved
