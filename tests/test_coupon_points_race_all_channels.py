"""
各通路同時使用同一張優惠券、同一位會員的點數：自助點餐機 (會員登入)、桌邊掃碼、POS 綁定會員、紅利商城兌換，
以及線上點餐 (線上端) 與分店現場 (兩個資料庫) 之間在同步前同時使用。
驗證：一張券只折抵一次、點數不會被重複扣成負數 (同一個資料庫內)、搶輸的一方得到明確的錯誤 (不會默默改用原價成立訂單)、
異動帳合計 = 點數；跨資料庫無法即時互鎖的情況，同步後要收斂一致並通知店家處理。
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.extensions import db, limiter
from app.models.coupon import Coupon, UserCoupon
from app.models.order import Order
from app.models.sync import PointLedger
from app.models.user import User
from tests.support import checkout, default_tenant_id, line, login_staff, make_item, make_member, member_client, \
    run_together

POS = '/api/pos/checkout'
PIN = '2468'


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def points_on(admin_client):
    """1 點折 1 元、單筆不設上限、每 1000 元送 1 點 (測試金額都不會送點)"""
    r = admin_client.post('/admin/update_reward_setting', data=dict(
        is_enabled='1', points_per_dollar='1', max_discount_per_order='0', spend_per_point='1000'))
    assert r.status_code == 302


def _coupon(code, value=40, **kw):
    """在目前的 app context 建立優惠券範本 (預設為會員專屬、現折 value 元)"""
    kw.setdefault('member_only', True)
    kw.setdefault('is_reward', False)
    cp = Coupon(code=code, title=f'{code} 測試券', discount_type='fixed', discount_value=value, **kw)
    db.session.add(cp)
    db.session.flush()
    return cp


def _give(coupon, user_id, n=1):
    for _ in range(n):
        db.session.add(UserCoupon(tenant_id=coupon.tenant_id, user_id=user_id, coupon_id=coupon.id, code=coupon.code,
                                  source='admin'))


def _store_coupon(app, code, holder=None, **kw):
    with app.app_context():
        cp = _coupon(code, tenant_id=default_tenant_id(app), **kw)
        if holder:
            _give(cp, holder)
        db.session.commit()
        return cp.id


def _member_state(app, uid):
    with app.app_context():
        u = db.session.get(User, uid)
        ledger = db.session.query(db.func.coalesce(db.func.sum(PointLedger.delta), 0)).filter_by(user_id=uid).scalar()
        return u.points, int(ledger)


def _orders_of(app, uid, item_id):
    from app.models.order import OrderItem
    with app.app_context():
        rows = Order.query.join(OrderItem, OrderItem.order_id == Order.id) \
            .filter(Order.user_id == uid, OrderItem.menu_item_id == item_id, Order.status != 'Cancelled').all()
        return [dict(id=o.id, source=o.order_source, coupon=o.coupon_code, uc=o.applied_user_coupon_id,
                     points=o.points_used or 0, total=o.total_price) for o in rows]


def _table_member(app, admin_client, table_name, phone, ip):
    """桌邊掃碼後登入會員的手機"""
    from app.models.table import DiningTable
    admin_client.post('/admin/tables', data={'action': 'create', 'names': table_name})
    with app.app_context():
        token = DiningTable.query.filter_by(name=table_name).one().token
    c = app.test_client()
    c.get(f'/t/{token}')
    r = c.post('/phone_login', json={'phone': phone, 'pin': PIN}, headers={'X-Forwarded-For': ip})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return c


def _assert_clear_failure(results, words):
    """搶輸的請求：4xx + 明確原因 (不可是 500)"""
    for kind, r in results:
        assert r.status_code in (200, 400, 409), (kind, r.status_code, r.get_data(as_text=True)[:200])
        if r.status_code != 200:
            msg = (r.get_json() or {}).get('message', '')
            assert any(w in msg for w in words), (kind, msg)


# ===========================================================================
# 門市：同一張會員券
# ===========================================================================
def test_one_member_coupon_used_from_every_channel_at_once(app, admin_client, no_rate_limit):
    """會員只有 1 張「現折 40」：點餐機 ×3、桌邊掃碼 ×2、POS ×3 同時送出 → 只有 1 張訂單折抵，其他明確失敗 (不會用原價成立)"""
    uid = make_member(app, '搶券會員', '0968800001', pin=PIN)
    _store_coupon(app, 'RACE40', holder=uid)
    item = make_item(app, '搶券便當', price=100, stock=100)
    login_staff(app, 'coupon_pos', 'staff')
    kiosks = [member_client(app, '0968800001', PIN, ip=f'10.88.1.{n}') for n in range(3)]
    tables = [_table_member(app, admin_client, f'CP{n}', '0968800001', f'10.88.2.{n}') for n in range(2)]

    def kiosk(c):
        return lambda: ('kiosk', checkout(c, [line(item)], promo_code='RACE40'))

    def table(c):
        return lambda: ('table', checkout(c, [line(item)], promo_code='RACE40'))

    def pos():
        return ('pos', checkout(login_staff(app, 'coupon_pos'), [line(item)], url=POS, user_id=uid,
                                promo_code='RACE40', cash_received=500))

    results = run_together(*([kiosk(c) for c in kiosks] + [table(c) for c in tables] + [pos] * 3))
    _assert_clear_failure(results, ('優惠券', '優惠代碼'))
    orders = _orders_of(app, uid, item)
    assert len(orders) == 1, f'只有用到券的那一筆成立，其他通路不可默默改用原價結帳：{orders}'
    assert orders[0]['coupon'] == 'RACE40' and orders[0]['uc'] and orders[0]['total'] == 60
    with app.app_context():
        assert UserCoupon.query.filter_by(user_id=uid, code='RACE40', is_used=True).count() == 1


def test_public_code_limited_per_member_is_not_exceeded_by_simultaneous_channels(app, admin_client, no_rate_limit):
    """公開代碼「每位會員限用 1 次」：同一位會員在點餐機與 POS 同時使用 → 只成立 1 次"""
    uid = make_member(app, '限用搶券會員', '0968800002', pin=PIN)
    _store_coupon(app, 'ONCE30', member_only=False, uses_per_member=1, value=30)
    item = make_item(app, '限用搶券餐', price=100, stock=100)
    login_staff(app, 'coupon_pos2', 'staff')
    kiosks = [member_client(app, '0968800002', PIN, ip=f'10.88.3.{n}') for n in range(3)]
    jobs = [lambda c=c: ('kiosk', checkout(c, [line(item)], promo_code='ONCE30')) for c in kiosks]
    jobs += [lambda: ('pos', checkout(login_staff(app, 'coupon_pos2'), [line(item)], url=POS, user_id=uid,
                                      promo_code='ONCE30', cash_received=500))] * 3
    results = run_together(*jobs)
    _assert_clear_failure(results, ('優惠代碼', '優惠券'))
    orders = _orders_of(app, uid, item)
    assert [o['coupon'] for o in orders] == ['ONCE30'], orders


# ===========================================================================
# 門市：同一位會員的點數
# ===========================================================================
def test_member_points_spent_from_every_channel_at_once(app, admin_client, points_on, no_rate_limit):
    """
    會員 300 點，同時：點餐機 ×3、POS ×3 各折抵 100 點，紅利商城兌換優惠券 ×2 與兌換商品 ×1 (各 100 點)。
    恰好 3 個成功；失敗的明確告知點數不足 / 已變動；不會出現「要折 100 點卻只折了一部分、用較高金額成立」的訂單。
    """
    uid = make_member(app, '搶點會員', '0968800003', points=300, pin=PIN)
    item = make_item(app, '搶點便當', price=100, stock=100)
    gift = make_item(app, '搶點兌換飲料', price=0, stock=100, is_reward=True, reward_points=100)
    reward_cp = _store_coupon(app, 'PTS100CP', member_only=False, is_reward=True, reward_points=100, value=10)
    login_staff(app, 'points_pos', 'staff')
    kiosks = [member_client(app, '0968800003', PIN, ip=f'10.88.4.{n}') for n in range(6)]

    jobs = [lambda c=c: ('kiosk', checkout(c, [line(item)], use_points=100)) for c in kiosks[:3]]
    jobs += [lambda: ('pos', checkout(login_staff(app, 'points_pos'), [line(item)], url=POS, user_id=uid,
                                      use_points=100, cash_received=500))] * 3
    jobs += [lambda c=c: ('mall', c.post('/api/redeem_reward', json={'coupon_id': reward_cp})) for c in kiosks[3:5]]
    jobs += [lambda: ('mall', kiosks[5].post('/api/redeem_reward', json={'item_id': gift}))]
    results = run_together(*jobs)
    _assert_clear_failure(results, ('點', '紅利'))

    ok = [k for k, r in results if r.status_code == 200]
    assert len(ok) == 3, f'300 點只夠 3 次：{[(k, r.status_code) for k, r in results]}'
    orders = _orders_of(app, uid, item)
    assert all(o['points'] == 100 and o['total'] == 0 for o in orders), f'不可部分折抵後以較高金額成立：{orders}'
    points, ledger = _member_state(app, uid)
    assert points == ledger == 0, (points, ledger)


def test_coupon_and_points_with_simultaneous_cancellation(app, admin_client, points_on, no_rate_limit):
    """
    一張訂單用了券 + 100 點；取消它的同時，其他通路也搶著用同一張券與點數。
    不論誰先：券最多被一張未取消的訂單使用、券的狀態與訂單一致、點數 = 200 - 未取消訂單用掉的點數 ≥ 0 = 異動帳合計。
    """
    uid = make_member(app, '取消搶券會員', '0968800004', points=200, pin=PIN)
    _store_coupon(app, 'UNDO40', holder=uid)
    item = make_item(app, '取消搶券餐', price=150, stock=100)
    login_staff(app, 'undo_pos', 'staff')
    login_staff(app, 'undo_mgr', 'manager')
    first = checkout(login_staff(app, 'undo_pos'), [line(item)], url=POS, user_id=uid, promo_code='UNDO40',
                     use_points=100, cash_received=500)
    assert first.status_code == 200 and first.get_json()['total_price'] == 10
    first_id = first.get_json()['order_id']
    kiosks = [member_client(app, '0968800004', PIN, ip=f'10.88.5.{n}') for n in range(3)]

    jobs = [lambda: ('cancel', login_staff(app, 'undo_mgr').post(f'/api/orders/{first_id}/status',
                                                                 json={'status': 'Cancelled'}))]
    jobs += [lambda c=c: ('kiosk', checkout(c, [line(item)], promo_code='UNDO40', use_points=100)) for c in kiosks]
    jobs += [lambda: ('pos', checkout(login_staff(app, 'undo_pos'), [line(item)], url=POS, user_id=uid,
                                      promo_code='UNDO40', use_points=100, cash_received=500))] * 2
    results = run_together(*jobs)
    assert results[0][1].status_code == 200
    _assert_clear_failure(results[1:], ('優惠券', '點'))

    orders = _orders_of(app, uid, item)
    with app.app_context():
        uc = UserCoupon.query.filter_by(user_id=uid, code='UNDO40').one()
        users_of_coupon = [o for o in orders if o['uc'] == uc.id]
        assert len(users_of_coupon) <= 1 and uc.is_used == bool(users_of_coupon), (orders, uc.is_used)
    assert all(o['coupon'] == 'UNDO40' and o['points'] == 100 for o in orders), '成立的訂單都是完整折抵'
    points, ledger = _member_state(app, uid)
    assert points == ledger == 200 - sum(o['points'] for o in orders) >= 0


# ===========================================================================
# 線上端 ↔ 分店：兩個資料庫在同步前同時使用
# ===========================================================================
@pytest.fixture(scope='module')
def env():
    from app.models.user import RewardSetting
    from tests.test_sync_hub_node import Env, _add_item, _register_member, tenant_ctx
    e = Env()
    e.meal = _add_item(e.b, '跨店搶券便當', price=150, stock=500)
    e.sides = [_add_item(e.b, f'跨店搶券小菜{n}', price=150, stock=50) for n in range(6)]
    for n in range(6):
        _register_member(e.b, f'跨店搶券會員{n}', f'09688{n:05d}', points=100)
    e.sync_all()
    with tenant_ctx(e.hub):
        rs = RewardSetting.query.first()
        rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point = True, 1, 0, 1000
        db.session.commit()
    e.sync_all()
    yield e
    e.close()


def _uid(app, n):
    from tests.test_sync_hub_node import _member
    return _member(app, f'09688{n:05d}')['id']


def _hub_coupon(env, code, holder_no=None, **kw):
    """在線上端建立券 (並發給會員)，同步到兩家分店"""
    from tests.test_sync_hub_node import tenant_ctx
    holder = _uid(env.hub, holder_no) if holder_no is not None else None
    with tenant_ctx(env.hub) as tenant:
        cp = _coupon(code, tenant_id=tenant.id, **kw)
        if holder:
            _give(cp, holder)
        db.session.commit()
    env.sync_all()


def _online(env, member_no, item=None, **kw):
    from tests.test_sync_hub_node import _checkout, _item
    return _checkout(env.hub, [line(_item(env.hub, item or env.meal)['id'])], node_uuid=env.node_b_uuid,
                     user_id=_uid(env.hub, member_no), **kw)


def _branch(env, member_no, **kw):
    from tests.test_sync_hub_node import _checkout, _item
    return _checkout(env.b, [line(_item(env.b, env.meal)['id'])], order_source='POS', cashier_username='b_pos',
                     cash_received=1000, user_id=_uid(env.b, member_no), **kw)


def _coupon_used(app, code):
    from tests.test_sync_hub_node import tenant_ctx
    with tenant_ctx(app):
        return [uc.is_used for uc in UserCoupon.query.filter_by(code=code).all()]


def _points(app, member_no):
    from tests.test_sync_hub_node import tenant_ctx
    uid = _uid(app, member_no)
    with tenant_ctx(app):
        ledger = db.session.query(db.func.coalesce(db.func.sum(PointLedger.delta), 0)).filter_by(user_id=uid).scalar()
        return db.session.get(User, uid).points, int(ledger)


@pytest.fixture
def alerts(monkeypatch):
    from app.services import monitor_service
    got = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: got.append(msg))
    return got


def test_same_coupon_used_online_and_at_branch_before_sync_is_reported(env, alerts):
    """同一張券在分店現場與線上點餐 (尚未同步) 各用一次：兩張訂單都已成立，同步後券在各處都是已使用，並通知店家"""
    _hub_coupon(env, 'XDUP40', holder_no=0)
    at_branch = _branch(env, 0, promo_code='XDUP40')
    online = _online(env, 0, promo_code='XDUP40')
    assert at_branch['total'] == 110 and online['total'] == 110
    env.sync(env.b, rounds=2)
    assert _coupon_used(env.hub, 'XDUP40') == [True] and _coupon_used(env.b, 'XDUP40') == [True]
    assert any('XDUP40' in a and '重複使用' in a for a in alerts), alerts


def test_coupon_returned_by_cancel_does_not_hide_use_at_other_branch(env, alerts):
    """
    分店用了券 (尚未同步)；線上端同一張券的訂單被取消、券退回 (時間較新)。
    同步後不可因「較新的退回」把券改回未使用 (分店那張訂單仍在用)，也不能再被線上使用。
    """
    from app.errors import AppError
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    from tests.test_sync_hub_node import _order, tenant_ctx
    _hub_coupon(env, 'XBACK40', holder_no=1)
    _branch(env, 1, promo_code='XBACK40')
    online = _online(env, 1, promo_code='XBACK40')
    online_id = _order(env.hub, online['uuid'])['id']
    with tenant_ctx(env.hub) as tenant:
        owner = StaffAccount(tenant_id=tenant.id, username='race_hub_owner', role='owner', password_hash='x')
        change_order_status(online_id, 'Cancelled', owner, tenant.id)
    assert _coupon_used(env.hub, 'XBACK40') == [False]
    env.sync(env.b, rounds=2)
    assert _coupon_used(env.hub, 'XBACK40') == [True], '分店訂單仍使用這張券'
    assert _coupon_used(env.b, 'XBACK40') == [True]
    with pytest.raises(AppError, match='優惠券'):
        _online(env, 1, promo_code='XBACK40')


def test_points_spent_online_and_at_branch_before_sync_are_reported(env, alerts):
    """
    100 點同時在分店與線上各折 100 (兩個資料庫無法即時互鎖)：兩筆異動都保留、各處合計一致 (-100)，
    通知店家，且負數期間不能再折抵或兌換。
    """
    from app.errors import AppError
    _branch(env, 2, use_points=100)
    _online(env, 2, use_points=100)
    env.sync(env.b, rounds=2)
    assert _points(env.hub, 2) == (-100, -100) and _points(env.b, 2) == (-100, -100)
    assert any('點數' in a and '跨店搶券會員2' in a for a in alerts), alerts
    with pytest.raises(AppError, match='點'):
        _online(env, 2, use_points=10)


def test_online_orders_for_one_member_at_the_same_time(env):
    """
    線上端同一位會員同時下 6 張單 (各點不同餐點，不會因為同一個品項的庫存列而排隊)：
    3 張用「每人限 1 次」的公開代碼、3 張各折 50 點 (共 100 點)
    """
    from app.errors import AppError
    _hub_coupon(env, 'XONCE20', member_only=False, uses_per_member=1, value=20)

    def attempt(n):
        try:
            return _online(env, 3, item=env.sides[n],
                           **({'promo_code': 'XONCE20'} if n % 2 == 0 else {'use_points': 50}))
        except AppError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(attempt, range(6)))
    coupon_ok = [r for n, r in enumerate(results) if n % 2 == 0 and isinstance(r, dict)]
    points_ok = [r for n, r in enumerate(results) if n % 2 == 1 and isinstance(r, dict)]
    assert len(coupon_ok) == 1, results
    assert len(points_ok) == 2, results
    assert all(('優惠' in r.message or '點' in r.message) for r in results if not isinstance(r, dict))
    assert _points(env.hub, 3) == (0, 0)
    env.sync(env.b)
    assert _points(env.b, 3) == (0, 0)
