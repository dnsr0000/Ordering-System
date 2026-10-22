"""多執行緒併發：限量商品不超賣、點數不雙花、POS 同時結帳與取消庫存精準"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.extensions import db, limiter
from app.models.menu import MenuItem
from tests.support import (
    checkout,
    guest_client,
    line,
    login_staff,
    make_item,
    make_member,
)


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


def test_no_overselling(app, no_rate_limit):
    """12 個執行緒同時搶購 5 份限量商品：成交 5、擋下 7、庫存歸零、取餐號不重複"""
    item_id = make_item(app, '限量龍蝦堡', price=300, stock=5)
    clients = [guest_client(app) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(lambda c: checkout(c, [line(item_id)]), clients))
    ok = [r.get_json() for r in results if r.status_code == 200]
    blocked = [r.get_json() for r in results if r.status_code == 400 and r.get_json().get('insufficient_stock')]
    assert len(ok) == 5 and len(blocked) == 7
    assert len({d['pickup_number'] for d in ok}) == 5
    with app.app_context():
        assert db.session.get(MenuItem, item_id).stock == 0


def test_points_double_spending(app, no_rate_limit):
    """會員 100 點，10 個請求同時兌換 60 點商品：只有 1 筆成功，剩 40 點"""
    make_member(app, '防雙花測試員', '0977000111', points=100, pin='1111')
    item_id = make_item(app, '60點高價兌換餐', price=120, stock=50, is_reward=True, reward_points=60, category='點心')
    clients = []
    for i in range(10):
        c = app.test_client()
        c.get('/?shop=default')
        assert c.post('/phone_login', json={'phone': '0977000111', 'pin': '1111'},
                      headers={'X-Forwarded-For': f'10.70.2.{i + 1}'}).status_code == 200
        clients.append(c)
    with ThreadPoolExecutor(max_workers=10) as ex:
        codes = list(ex.map(lambda c: c.post('/api/redeem_reward', json={'item_id': item_id}).status_code, clients))
    assert sorted(codes) == [200] + [400] * 9
    with app.app_context():
        from app.models.user import User
        assert User.query.filter_by(phone='0977000111').first().points == 40


def test_concurrent_pos_checkout_and_cancel(app, no_rate_limit):
    """6 台 POS 同時結帳 (-6) 與 4 筆同時取消 (+4)：庫存精準為 20 - 4 - 6 + 4 = 14，取餐號不重複"""
    item_id = make_item(app, 'POS併發測試餐', price=100, stock=20)
    pos = login_staff(app, 'pos_user')
    login_staff(app, 'mgr_user', 'manager')  # 先建立帳號，避免多個執行緒同時建立同名帳號
    pre_orders = [checkout(pos, [line(item_id)], url='/api/pos/checkout', cash_received=100).get_json()['order_id']
                  for _ in range(4)]

    def do_checkout(_):
        return 'checkout', checkout(login_staff(app, 'pos_user'), [line(item_id)], url='/api/pos/checkout', cash_received=100)

    def do_cancel(oid):
        return 'cancel', login_staff(app, 'mgr_user', 'manager').post(f'/api/orders/{oid}/status', json={'status': 'Cancelled'})

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(do_checkout, i) for i in range(6)] + [ex.submit(do_cancel, oid) for oid in pre_orders]
        results = [f.result() for f in futures]
    ok_checkout = [r.get_json() for kind, r in results if kind == 'checkout' and r.status_code == 200]
    ok_cancel = [r for kind, r in results if kind == 'cancel' and r.status_code == 200]
    assert len(ok_checkout) == 6 and len(ok_cancel) == 4
    assert len({d['pickup_number'] for d in ok_checkout}) == 6
    with app.app_context():
        assert db.session.get(MenuItem, item_id).stock == 14
