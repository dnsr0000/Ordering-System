"""
先前列為「尚未處理」的四種情況：
1. 店家改價與顧客 / 收銀送出之間的價差：畫面金額 (expected_total) 與實際不同時結帳失敗並回傳新單價，不會用畫面以外的金額成立。
2. 促銷贈品最後一份被同時搶走：訂單照常成立，只是不附贈 (不再整筆失敗)。
3. 跨店同步前同時發放推薦好友獎勵 / 同時集滿集點卡：線上端沖銷重複的推薦獎勵、集點卡變負數時通知。
4. 線上端執行點數到期時，分店同步前已用掉同一批點數：同步後依先進先出校正，不會多扣。
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db, limiter
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.sync import PointLedger
from tests.support import checkout, default_tenant_id, guest_client, line, login_staff, make_item, run_together

POS = '/api/pos/checkout'


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


def _set_price(app, item_id, price):
    with app.app_context():
        db.session.get(MenuItem, item_id).price = price
        db.session.commit()


# ===========================================================================
# 1. 改價與送出之間的價差
# ===========================================================================
def test_checkout_rejects_total_different_from_screen_and_returns_new_prices(app, staff_client):
    item = make_item(app, '改價測試堡', price=100, stock=50)
    g = guest_client(app)
    assert checkout(g, [line(item)], expected_total=100).status_code == 200
    _set_price(app, item, 120)
    for client, url, extra in ((guest_client(app), '/submit_order', {}), (staff_client, POS, {'cash_received': 500})):
        r = checkout(client, [line(item, 2)], url=url, expected_total=200, **extra)
        body = r.get_json()
        assert r.status_code == 409 and body['price_changed'], body
        assert (body['expected_total'], body['server_total'], body['line_prices']) == (200, 240, [120])
        assert '目前應付 $240' in body['message']
        ok = checkout(client, [line(item, 2)], url=url, expected_total=240, **extra)
        assert ok.status_code == 200 and ok.get_json()['total_price'] == 240
    # 沒有提供畫面金額 (外送平台、舊版畫面) 不檢查
    assert checkout(guest_client(app), [line(item)]).status_code == 200


def test_price_change_while_channels_are_checking_out(app, admin_client, no_rate_limit):
    """店家改價的同時各通路送出 (畫面還是舊價 100)：成立的訂單一定是畫面上的金額，其他明確要求確認新金額"""
    item = make_item(app, '同時改價便當', price=100, stock=100)
    login_staff(app, 'price_pos', 'staff')

    def kiosk():
        return checkout(guest_client(app), [line(item)], expected_total=100)

    def pos():
        return checkout(login_staff(app, 'price_pos'), [line(item)], url=POS, expected_total=100, cash_received=100)

    def change():
        _set_price(app, item, 130)

    results = run_together(*([kiosk] * 4 + [pos] * 4 + [change]))
    for r in results[:-1]:
        assert r.status_code in (200, 409), r.get_data(as_text=True)[:200]
        if r.status_code == 200:
            assert r.get_json()['total_price'] == 100, '只會以畫面上的金額成立'
        else:
            assert r.get_json()['server_total'] == 130


# ===========================================================================
# 2. 促銷贈品最後一份
# ===========================================================================
def test_last_promotion_gift_taken_by_another_order_does_not_block_checkout(app, no_rate_limit):
    from app.models.promotion import Promotion
    main = make_item(app, '贈品活動主餐', price=200, stock=50)
    gift = make_item(app, '限量贈品玩具', price=0, stock=1)
    with app.app_context():
        promo = Promotion(tenant_id=default_tenant_id(app), name='滿額送玩具', kind='threshold_gift', threshold=150,
                          gift_item_uuid=db.session.get(MenuItem, gift).uuid, channels='kiosk,pos')
        db.session.add(promo)
        db.session.commit()
        promo_id = promo.id
    login_staff(app, 'gift_pos', 'staff')
    try:
        jobs = [lambda: checkout(guest_client(app), [line(main)])] * 3
        jobs += [lambda: checkout(login_staff(app, 'gift_pos'), [line(main)], url=POS, cash_received=500)] * 3
        results = run_together(*jobs)
        assert all(r.status_code == 200 for r in results), [r.get_data(as_text=True)[:150] for r in results]
        ids = [r.get_json()['order_id'] for r in results]
        with app.app_context():
            with_gift = OrderItem.query.filter(OrderItem.order_id.in_(ids), OrderItem.menu_item_id == gift).count()
            notes = [db.session.get(Order, i).note for i in ids]
            assert db.session.get(MenuItem, gift).stock == 0
        assert with_gift == 1, '只有一張拿到最後一份贈品'
        # 送出時贈品還在、扣庫存時被搶走的訂單備註「贈品已送完」(之後才送出的訂單本來就沒有贈品)
        assert sum('贈品已送完：限量贈品玩具' in n for n in notes) <= 5, notes
    finally:
        with app.app_context():
            db.session.get(Promotion, promo_id).is_active = False
            db.session.commit()


# ===========================================================================
# 3、4. 線上端 ↔ 分店
# ===========================================================================
@pytest.fixture(scope='module')
def env():
    from app.models.user import RewardSetting
    from tests.test_sync_hub_node import Env, _add_item, _register_member, tenant_ctx
    e = Env()
    e.meal = _add_item(e.a, '跨店忠誠度便當', price=150, stock=500)
    for n in range(5):
        _register_member(e.a, f'跨店忠誠會員{n}', f'09687{n:05d}', points=0)
    e.sync_all()
    with tenant_ctx(e.hub):   # 可折抵、但這些金額不送點 (點數變化只來自測試本身)
        rs = RewardSetting.query.first()
        rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point = True, 1, 0, 100000
        db.session.commit()
    e.sync_all()
    yield e
    e.close()


def _uid(app, n):
    from tests.test_sync_hub_node import _member
    return _member(app, f'09687{n:05d}')['id']


def _put(apps, key, values):
    from app.services import settings_service
    from tests.test_sync_hub_node import tenant_ctx
    for app_ in apps:
        with tenant_ctx(app_) as tenant:
            settings_service.put(key, values, tenant_id=tenant.id)
            db.session.commit()


def _branch_order(app_, env, member_no, **kw):
    from tests.test_sync_hub_node import _checkout, _item
    return _checkout(app_, [line(_item(app_, env.meal)['id'])], order_source='POS', cashier_username='x_pos',
                     cash_received=1000, user_id=_uid(app_, member_no), **kw)


def _ledger(app_, member_no, reasons=None):
    from tests.test_sync_hub_node import tenant_ctx
    uid = _uid(app_, member_no)
    with tenant_ctx(app_):
        q = PointLedger.query.filter_by(user_id=uid)
        if reasons:
            q = q.filter(PointLedger.reason.in_(reasons))
        return sorted((r.reason, r.delta) for r in q)


def _points(app_, member_no):
    from tests.test_sync_hub_node import _member
    return _member(app_, f'09687{member_no:05d}')['points']


@pytest.fixture
def alerts(monkeypatch):
    from app.services import monitor_service
    got = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: got.append(msg))
    return got


def test_referral_awarded_at_two_branches_before_sync_is_undone_once(env, alerts):
    """被推薦的新會員在兩家分店同步前各完成「第一筆」：線上端沖銷重複的一次，同步後各處新會員 +30、推薦人 +50"""
    from app.models.user import User
    from tests.test_sync_hub_node import tenant_ctx
    _put((env.hub, env.a, env.b), 'member', {'referral_enabled': True, 'referee_points': 30, 'referrer_points': 50,
                                            'referral_min_spend': 0})
    new_uid, ref_uid = _uid(env.a, 0), _uid(env.a, 1)
    with tenant_ctx(env.a):
        db.session.get(User, new_uid).referred_by_uuid = db.session.get(User, ref_uid).uuid
        db.session.commit()
    env.sync_all()
    _branch_order(env.a, env, 0)
    _branch_order(env.b, env, 0)
    env.sync_all(rounds=2)
    for app_ in (env.hub, env.a, env.b):
        assert (_points(app_, 0), _points(app_, 1)) == (30, 50), app_.config['SYNC_ROLE']
    assert _ledger(env.hub, 0, ('referred', 'referred_dup')) == [('referred', 30), ('referred', 30), ('referred_dup', -30)]
    assert any('推薦好友獎勵' in a and '跨店忠誠會員0' in a for a in alerts), alerts
    env.sync_all()   # 再同步不會重複沖銷
    assert (_points(env.hub, 0), _points(env.hub, 1)) == (30, 50)
    _put((env.hub, env.a, env.b), 'member', {'referral_enabled': False})


def test_cancelling_the_duplicate_referral_order_does_not_claw_back_twice(env):
    """被沖銷過的那張重複訂單之後被取消：不再扣一次 (只扣回淨得的點數)"""
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    from tests.test_sync_hub_node import tenant_ctx
    uid = _uid(env.hub, 0)
    with tenant_ctx(env.hub):
        dup = PointLedger.query.filter_by(user_id=uid, reason='referred_dup').one()
        order_id = dup.order_id
        order_uuid = db.session.get(Order, order_id).uuid
    node = next(a for a in (env.a, env.b) if _q_order(a, order_uuid))
    with tenant_ctx(node) as tenant:
        oid = Order.query.filter_by(uuid=order_uuid).one().id
        owner = StaffAccount(tenant_id=tenant.id, username='dup_cancel_owner', role='owner', password_hash='x')
        change_order_status(oid, 'Cancelled', owner, tenant.id)
    env.sync_all(rounds=2)
    assert (_points(env.hub, 0), _points(env.hub, 1)) == (30, 50)


def _q_order(app_, uuid):
    with app_.app_context():
        return Order.query.filter_by(uuid=uuid).first() is not None


def test_stamp_card_completed_at_two_branches_before_sync_is_reported(env, alerts):
    """會員有 2 章 (集滿 3 章)，兩家分店同步前各消費一次、各自集滿：同步後章數一致 (負數)，並通知店家"""
    from app.models.member import StampLedger
    from app.services import loyalty_service
    from tests.test_sync_hub_node import tenant_ctx
    _put((env.hub, env.a, env.b), 'stamps', {'enabled': True, 'card_size': 3, 'mode': 'order', 'min_spend': 0,
                                            'reward_points': 10})
    uid = _uid(env.a, 2)
    with tenant_ctx(env.a) as tenant:
        db.session.add(StampLedger(tenant_id=tenant.id, user_id=uid, delta=2, reason='earn'))
        db.session.commit()
    env.sync_all()
    _branch_order(env.a, env, 2)
    _branch_order(env.b, env, 2)
    env.sync_all(rounds=2)
    for app_ in (env.hub, env.a, env.b):
        u = _uid(app_, 2)
        with tenant_ctx(app_):
            assert db.session.query(db.func.sum(StampLedger.delta)).filter_by(user_id=u).scalar() == -2
            assert loyalty_service.stamp_count(u) == 0
    assert any('集點卡' in a and '跨店忠誠會員2' in a for a in alerts), alerts
    _put((env.hub, env.a, env.b), 'stamps', {'enabled': False})


def test_points_spent_at_branch_before_hub_expires_them_are_not_deducted_twice(env):
    """
    會員 13 個月前得到 100 點 (有效期限 12 個月)。分店 (尚未同步) 折抵 100 點的同時，線上端執行到期扣了 100 點。
    同步後應為 0 點 (點數是被用掉、不是到期)，而不是 -100；正常到期時校正為 0。
    """
    from app.services import loyalty_service
    from tests.test_sync_hub_node import tenant_ctx
    _put((env.hub,), 'member', {'points_expire_months': 12})
    uid_b = _uid(env.b, 3)
    with tenant_ctx(env.b) as tenant:
        db.session.add(PointLedger(tenant_id=tenant.id, user_id=uid_b, delta=100, reason='earn',
                                   created_at=datetime.now() - timedelta(days=400)))
        db.session.commit()
    env.sync_all()
    from app.services.points_service import recompute_points
    for app_ in (env.hub, env.a, env.b):
        u = _uid(app_, 3)
        with tenant_ctx(app_):
            recompute_points([u])
            db.session.commit()
    assert _points(env.hub, 3) == 100 and _points(env.b, 3) == 100

    order = _branch_order(env.b, env, 3, use_points=100)       # 分店折抵 (離線、尚未同步)
    assert order['total'] == 50
    with tenant_ctx(env.hub) as tenant:
        assert loyalty_service.expire_points(tenant.id) == 1     # 線上端到期扣 100
    assert _points(env.hub, 3) == 0
    env.sync(env.b, rounds=2)
    env.sync(env.a)
    for app_ in (env.hub, env.a, env.b):
        assert _points(app_, 3) == 0, app_.config['SYNC_ROLE']
    assert ('expire_fix', 100) in _ledger(env.hub, 3)

    # 沒有同步延遲的一般到期：校正量為 0
    uid_hub = _uid(env.hub, 4)
    with tenant_ctx(env.hub) as tenant:
        db.session.add(PointLedger(tenant_id=tenant.id, user_id=uid_hub, delta=40, reason='earn',
                                   created_at=datetime.now() - timedelta(days=400)))
        db.session.commit()
        recompute_points([uid_hub])
        db.session.commit()
    before = _ledger(env.hub, 4)
    with tenant_ctx(env.hub) as tenant:
        loyalty_service.expire_points(tenant.id)
        assert loyalty_service.true_up_expiry(tenant.id, uid_hub) == 0
        db.session.commit()
    assert _ledger(env.hub, 4) == sorted(before + [('expire', -40)])
    _put((env.hub,), 'member', {'points_expire_months': 0})
