"""POS 當日訂單輕量 API (/api/pos/today_orders)：權限、只回傳本店今日訂單、跨店隔離"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.tenant import StaffAccount, Tenant
from app.services.order_service import todays_range
from tests.conftest import ADMIN_USERNAME
from tests.support import (
    checkout,
    default_tenant_id,
    line,
    login_staff,
    make_item,
    new_client_order_id,
)

URL = '/api/pos/today_orders'
ROLE_USERS = {'owner': ADMIN_USERNAME, 'manager': 'm18_manager', 'staff': 'm18_pos', 'kitchen': 'm18_kitchen'}


def role_client(app, role):
    """每次重新登入 (共用的 session 員工 client 可能已被前面的模組登出或停用)"""
    return login_staff(app, ROLE_USERS[role], role)


@pytest.fixture(scope='module')
def staff_client(app):
    return role_client(app, 'staff')


@pytest.fixture(scope='module')
def seeded(app, staff_client):
    """本店：今日 POS 訂單 + 昨日訂單；B 店：今日訂單、餐點與員工"""
    tid = default_tenant_id(app)
    item_id = make_item(app, '模組18今日餐', price=80, stock=30)
    r = checkout(staff_client, [line(item_id, 2, customization='少冰')], url='/api/pos/checkout',
                 cash_received=200, client_order_id=new_client_order_id())
    assert r.status_code == 200, r.get_data(as_text=True)
    pos_order_id = r.get_json()['order_id']
    start, _end = todays_range()
    with app.app_context():
        yesterday = Order(tenant_id=tid, table_number='模組18昨日', total_price=10, status='PickedUp', pickup_number=7,
                          created_at=start - timedelta(hours=1))
        tb = Tenant(name='模組18B店', slug='m18-shop-b')
        db.session.add_all([yesterday, tb])
        db.session.flush()
        b_item = MenuItem(tenant_id=tb.id, name='模組18B店餐', category='主餐', price=50, stock=5, total_stock=5)
        b_order = Order(tenant_id=tb.id, table_number='模組18B店客', total_price=50, status='Pending', pickup_number=1,
                        created_at=datetime.now())
        db.session.add_all([b_item, b_order])
        db.session.flush()
        db.session.add(OrderItem(tenant_id=tb.id, order_id=b_order.id, menu_item_id=b_item.id, item_name='模組18B店餐',
                                 price=50, quantity=1))
        b_staff = StaffAccount(tenant_id=tb.id, username='m18_b_staff', role='staff', is_active=True)
        b_staff.set_password('StaffPass!2026')
        db.session.add(b_staff)
        db.session.commit()
        return dict(item_id=item_id, pos_order_id=pos_order_id, yesterday_id=yesterday.id,
                    b_item_id=b_item.id, b_order_id=b_order.id)


def test_requires_staff_roles(app):
    anon = app.test_client()
    anon.get('/?shop=default')
    assert anon.get(URL).status_code == 401
    r = role_client(app, 'kitchen').get(URL)
    assert r.status_code == 403 and r.get_json()['success'] is False


@pytest.mark.parametrize('role', ['owner', 'manager', 'staff'])
def test_pos_roles_allowed(app, role, seeded):
    r = role_client(app, role).get(URL)
    assert r.status_code == 200 and r.get_json()['success'] is True


def test_only_todays_orders_of_own_shop(app, staff_client, seeded):
    data = staff_client.get(URL).get_json()
    ids = [o['id'] for o in data['orders']]
    assert seeded['pos_order_id'] in ids
    assert seeded['yesterday_id'] not in ids and seeded['b_order_id'] not in ids
    assert ids == sorted(ids, reverse=True)  # 新訂單在前

    start, end = todays_range()
    tid = default_tenant_id(app)
    with app.app_context():
        expected = {o.id for o in Order.query.execution_options(skip_tenant_filter=True)
                    .filter(Order.tenant_id == tid, Order.created_at >= start, Order.created_at < end)}
    assert set(ids) == expected

    order = next(o for o in data['orders'] if o['id'] == seeded['pos_order_id'])
    assert {'id', 'pickup_number', 'user_name', 'order_type', 'order_source', 'cashier_username', 'cash_received',
            'cash_change', 'payment_method', 'need_cutlery', 'note', 'created_at', 'status', 'discount_amount',
            'total_price', 'items'} <= set(order)
    assert order['order_source'] == 'POS' and order['cashier_username'] == 'm18_pos'
    assert (order['total_price'], order['cash_received'], order['cash_change']) == (160, 200, 40)
    item = dict(order['items'][0])
    assert len(order['items']) == 1 and item.pop('id') > 0
    assert item == {'name': '模組18今日餐', 'item_name': '模組18今日餐', 'is_reward': False, 'is_gift': False,
                    'original_price': None, 'price': 80, 'quantity': 2, 'subtotal': 160,
                    'customization': '少冰', 'voided_qty': 0, 'void_note': ''}

    stock = {m['id']: m for m in data['menu_items']}
    assert stock[seeded['item_id']]['stock'] == 28 and stock[seeded['item_id']]['is_sold_out'] is False
    assert seeded['b_item_id'] not in stock


def test_other_shop_staff_sees_only_own_orders(app, seeded):
    client = login_staff(app, 'm18_b_staff')
    data = client.get(URL).get_json()
    assert [o['id'] for o in data['orders']] == [seeded['b_order_id']]
    assert data['orders'][0]['items'][0]['name'] == '模組18B店餐'
    assert [m['id'] for m in data['menu_items']] == [seeded['b_item_id']]


def test_sold_out_flag(app, staff_client, seeded):
    with app.app_context():
        db.session.get(MenuItem, seeded['item_id']).is_sold_out = True
        db.session.commit()
    try:
        stock = {m['id']: m for m in staff_client.get(URL).get_json()['menu_items']}
        assert stock[seeded['item_id']]['is_sold_out'] is True
    finally:
        with app.app_context():
            db.session.get(MenuItem, seeded['item_id']).is_sold_out = False
            db.session.commit()


def test_no_n_plus_one(app, staff_client, seeded):
    """訂單數增加時查詢次數不變 (明細以 selectinload 一次載入)"""
    from sqlalchemy import event

    with app.app_context():
        engine = db.engine

    def count_queries():
        # 每次請求都在自己的 app context 執行 (外層沒有 app context，g 不會跨請求保留快取)
        counter = []
        listener = lambda *a, **k: counter.append(1)  # noqa: E731
        event.listen(engine, 'before_cursor_execute', listener)
        try:
            assert staff_client.get(URL).status_code == 200
        finally:
            event.remove(engine, 'before_cursor_execute', listener)
        return len(counter)

    before = count_queries()
    for n in range(3):
        r = checkout(staff_client, [line(seeded['item_id'], 1)], url='/api/pos/checkout',
                     cash_received=100, client_order_id=new_client_order_id())
        assert r.status_code == 200
    assert count_queries() == before
