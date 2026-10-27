"""
其他「多個通路 / 多台機器同時用到同一份資源」的情況：
- 套餐：配餐與單點共用庫存、取消時回補「當時賣出的配餐」(之後改了或刪了套餐也一樣)、配餐停售或不在供應時段、
  線上套餐訂單送到分店時依下單當時的配餐扣庫存。
- 同一位會員在線上端同時下多張單：集點卡只集滿一次、推薦好友獎勵只發一次。
- 線上訂位同時搶最後的座位、兩台 POS 同時開班、關班的同時有人結帳。
- 分店之間同步前同時使用同一張禮物卡 / 儲值金 / 集點卡：同步後一致並通知店家。
"""
import time
from datetime import date, datetime, timedelta

import pytest

from app.extensions import db, limiter
from app.models.menu import ComboOption, ComboOptionItem, MenuItem
from app.models.order import Order
from tests.support import checkout, default_tenant_id, guest_client, line, login_staff, make_item, run_together

POS = '/api/pos/checkout'


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


def _combo(app, main_id, side_ids, name='測試套餐', price=50):
    with app.app_context():
        tid = default_tenant_id(app)
        combo = ComboOption(tenant_id=tid, main_item_id=main_id, name=name, additional_price=price)
        db.session.add(combo)
        db.session.flush()
        for pos, sid in enumerate(side_ids):
            db.session.add(ComboOptionItem(tenant_id=tid, combo_option_id=combo.id, menu_item_id=sid, position=pos))
        db.session.commit()
        return combo.id


def _stock(app, item_id):
    with app.app_context():
        return db.session.get(MenuItem, item_id).stock


# ===========================================================================
# 套餐
# ===========================================================================
def test_combo_sides_and_standalone_sales_share_stock_across_channels(app, admin_client, no_rate_limit):
    """薯條只剩 5 份：點餐機 / POS / 桌邊同時點「漢堡套餐 (含薯條)」與「單點薯條」→ 薯條總共只賣 5 份"""
    burger = make_item(app, '套餐搶購堡', price=120, stock=50)
    fries = make_item(app, '套餐搶購薯條', price=50, stock=5)
    combo = _combo(app, burger, [fries], '搶購套餐')
    login_staff(app, 'combo_pos', 'staff')

    def kiosk_combo():
        return checkout(guest_client(app), [line(burger, combo_option_id=combo)])

    def kiosk_fries():
        return checkout(guest_client(app), [line(fries)])

    def pos_combo():
        return checkout(login_staff(app, 'combo_pos'), [line(burger, combo_option_id=combo)], url=POS, cash_received=500)

    def pos_fries():
        return checkout(login_staff(app, 'combo_pos'), [line(fries, 2)], url=POS, cash_received=500)

    results = run_together(*([kiosk_combo] * 3 + [kiosk_fries] * 3 + [pos_combo] * 2 + [pos_fries] * 2))
    for r in results:
        assert r.status_code in (200, 400, 409), r.get_data(as_text=True)[:200]
        if r.status_code != 200:
            assert '薯條' in r.get_json()['message'], r.get_json()
    ok = [n for n, r in enumerate(results) if r.status_code == 200]
    sold_combo = sum(1 for n in ok if n < 3 or 6 <= n < 8)
    sold_fries = sum(1 for n in ok if 3 <= n < 6) + sum(2 for n in ok if n >= 8)
    assert sold_fries + sold_combo == 5 and _stock(app, fries) == 0
    assert _stock(app, burger) == 50 - sold_combo


def test_cancel_restores_the_sides_that_were_sold_even_if_combo_changed(app, admin_client):
    """套餐賣出後店家把配餐換掉 (或刪掉套餐)：取消舊訂單要回補「當時賣出的配餐」，不是現在的配餐"""
    burger = make_item(app, '改套餐堡', price=120, stock=20)
    cola, tea = make_item(app, '改套餐可樂', price=30, stock=20), make_item(app, '改套餐紅茶', price=30, stock=20)
    combo = _combo(app, burger, [cola], '可改套餐')
    login_staff(app, 'combo_mgr', 'manager')
    mgr = login_staff(app, 'combo_mgr')
    first = checkout(mgr, [line(burger, combo_option_id=combo)], url=POS, cash_received=500).get_json()['order_id']
    second = checkout(mgr, [line(burger, 2, combo_option_id=combo)], url=POS, cash_received=500).get_json()['order_id']
    assert (_stock(app, cola), _stock(app, tea)) == (17, 20)

    with app.app_context():   # 店家把套餐的飲料由可樂改成紅茶
        db.session.query(ComboOptionItem).filter_by(combo_option_id=combo).update({'menu_item_id': tea})
        db.session.commit()
    assert mgr.post(f'/api/orders/{first}/status', json={'status': 'Cancelled'}).status_code == 200
    assert (_stock(app, cola), _stock(app, tea)) == (18, 20), '回補當時賣出的可樂，紅茶不變'

    with app.app_context():   # 套餐整個刪除
        db.session.delete(db.session.get(ComboOption, combo))
        db.session.commit()
    assert mgr.post(f'/api/orders/{second}/status', json={'status': 'Cancelled'}).status_code == 200
    assert (_stock(app, cola), _stock(app, tea), _stock(app, burger)) == (20, 20, 20)


def test_combo_with_discontinued_or_off_hours_side_cannot_be_ordered(app):
    """配餐已停售 / 不在供應時段：套餐不能點 (主餐有檢查，配餐也要)"""
    burger = make_item(app, '停售配餐堡', price=120, stock=20)
    soup = make_item(app, '停售濃湯', price=40, stock=20, sale_until=date.today() - timedelta(days=1))
    combo = _combo(app, burger, [soup], '濃湯套餐')
    r = checkout(guest_client(app), [line(burger, combo_option_id=combo)])
    assert r.status_code == 400 and '停售濃湯' in r.get_json()['message']
    assert r.get_json().get('sold_out_items') == ['停售濃湯']
    hour = datetime.now().hour
    breakfast = make_item(app, '限時豆漿', price=25, stock=20, avail_start=f'{(hour + 2) % 24:02d}:00',
                          avail_end=f'{(hour + 3) % 24:02d}:00')
    combo2 = _combo(app, burger, [breakfast], '早餐套餐')
    r = checkout(guest_client(app), [line(burger, combo_option_id=combo2)])
    assert r.status_code == 400 and '限時豆漿' in r.get_json()['message']
    assert _stock(app, burger) == 20


# ===========================================================================
# 線上端 ↔ 分店
# ===========================================================================
@pytest.fixture(scope='module')
def env():
    from tests.test_sync_hub_node import Env, _add_item, _register_member
    e = Env()
    e.burger = _add_item(e.b, '跨店套餐堡', price=120, stock=50)
    e.cola = _add_item(e.b, '跨店套餐可樂', price=30, stock=50)
    e.tea = _add_item(e.b, '跨店套餐紅茶', price=30, stock=50)
    e.sides = [_add_item(e.b, f'跨店同會員小點{n}', price=100, stock=50) for n in range(6)]
    for n in range(4):
        _register_member(e.b, f'跨店同會員{n}', f'09689{n:05d}', points=0)
    e.sync_all()
    from app.models.user import RewardSetting
    from tests.test_sync_hub_node import tenant_ctx
    with tenant_ctx(e.hub):   # 不送點 (金額未達送點門檻)：結帳時不會因為加點而剛好鎖到會員資料列，確認集章 / 推薦本身有依序處理
        RewardSetting.query.first().spend_per_point = 100000
        db.session.commit()
    e.sync_all()
    yield e
    e.close()


def _uid(app, n):
    from tests.test_sync_hub_node import _member
    return _member(app, f'09689{n:05d}')['id']


def _put(app, key, values):
    from app.services import settings_service
    from tests.test_sync_hub_node import tenant_ctx
    with tenant_ctx(app) as tenant:
        settings_service.put(key, values, tenant_id=tenant.id)
        db.session.commit()


def _online(env, member_no, item, **kw):
    from tests.test_sync_hub_node import _checkout, _item
    return _checkout(env.hub, [line(_item(env.hub, item)['id'], **kw.pop('line_kw', {}))], node_uuid=env.node_b_uuid,
                     user_id=_uid(env.hub, member_no), **kw)


def _b_stock(env, item):
    from tests.test_sync_hub_node import _item
    return _item(env.b, item)['stock']


def test_online_combo_order_uses_the_sides_sold_even_if_combo_changed_before_branch_receives(env):
    """線上點了「堡 + 可樂」套餐，分店還沒收到前總部把飲料改成紅茶：分店扣的是可樂"""
    from tests.test_sync_hub_node import tenant_ctx
    with tenant_ctx(env.b) as tenant:
        burger = MenuItem.query.filter_by(uuid=env.burger).one()
        cola = MenuItem.query.filter_by(uuid=env.cola).one()
        combo = ComboOption(tenant_id=tenant.id, main_item_id=burger.id, name='跨店可樂套餐', additional_price=40)
        db.session.add(combo)
        db.session.flush()
        db.session.add(ComboOptionItem(tenant_id=tenant.id, combo_option_id=combo.id, menu_item_id=cola.id))
        db.session.commit()
        combo_uuid = combo.uuid
    env.sync_all()
    with tenant_ctx(env.hub):
        hub_combo = ComboOption.query.filter_by(uuid=combo_uuid).one().id
    before = (_b_stock(env, env.cola), _b_stock(env, env.tea))
    _online(env, 0, env.burger, line_kw={'combo_option_id': hub_combo})
    with tenant_ctx(env.hub):
        tea_id = MenuItem.query.filter_by(uuid=env.tea).one().id
        ComboOptionItem.query.filter_by(combo_option_id=hub_combo).one().menu_item_id = tea_id
        db.session.commit()
    env.sync(env.b, rounds=2)
    assert (_b_stock(env, env.cola), _b_stock(env, env.tea)) == (before[0] - 1, before[1])


def test_online_orders_for_one_member_complete_stamp_card_once(env):
    """會員已有 2 章 (集滿 3 章送 50 點)，同時下 6 張線上訂單 (不同餐點)：共 8 章 → 只集滿 2 次、送 2 次 50 點、剩 2 章"""
    from app.models.member import StampLedger
    from app.models.sync import PointLedger
    from tests.test_sync_hub_node import tenant_ctx
    _put(env.hub, 'stamps', {'enabled': True, 'card_size': 3, 'mode': 'order', 'min_spend': 0, 'reward_points': 50})
    uid = _uid(env.hub, 1)
    with tenant_ctx(env.hub) as tenant:
        db.session.add(StampLedger(tenant_id=tenant.id, user_id=uid, delta=2, reason='earn'))
        db.session.commit()

    run_together(*[lambda n=n: _online(env, 1, env.sides[n]) for n in range(6)])
    with tenant_ctx(env.hub):
        completes = StampLedger.query.filter_by(user_id=uid, reason='complete').count()
        total = db.session.query(db.func.sum(StampLedger.delta)).filter_by(user_id=uid).scalar()
        rewards = PointLedger.query.filter_by(user_id=uid, reason='stamp_card').count()
    _put(env.hub, 'stamps', {'enabled': False})
    assert (completes, rewards, total) == (2, 2, 2), '2 + 6 章 = 集滿 2 次，剩 2 章'


def test_concurrent_first_online_orders_award_referral_once(env):
    """被推薦的新會員同時下 6 張「第一筆」線上訂單：推薦獎勵只發一次"""
    from app.models.sync import PointLedger
    from app.models.user import User
    from tests.test_sync_hub_node import tenant_ctx
    _put(env.hub, 'member', {'referral_enabled': True, 'referee_points': 30, 'referrer_points': 50,
                             'referral_min_spend': 0})
    new, referrer = _uid(env.hub, 2), _uid(env.hub, 3)
    with tenant_ctx(env.hub):
        db.session.get(User, new).referred_by_uuid = db.session.get(User, referrer).uuid
        db.session.commit()
    run_together(*[lambda n=n: _online(env, 2, env.sides[n]) for n in range(6)])
    with tenant_ctx(env.hub):
        referred = PointLedger.query.filter_by(user_id=new, reason='referred').count()
        referral = PointLedger.query.filter_by(user_id=referrer, reason='referral').count()
    _put(env.hub, 'member', {'referral_enabled': False})
    assert (referred, referral) == (1, 1)


def test_gift_card_and_wallet_spent_at_two_branches_before_sync_are_reported(env, monkeypatch):
    """同一張禮物卡、同一位會員的儲值金在兩家分店同步前各用光一次：同步後各處餘額一致 (負數)，並通知店家"""
    from app.models.member import GiftCard
    from app.services import giftcard_service, monitor_service, wallet_service
    from tests.test_sync_hub_node import _checkout, _item, tenant_ctx
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append(msg))
    for node in (env.a, env.b):
        _put(node, 'gift_card', {'enabled': True})
        _put(node, 'member', {'wallet_enabled': True})
    with tenant_ctx(env.a) as tenant:
        code = giftcard_service.sell(tenant.id, 100, 'Cash', 'a_cashier').code
        db.session.commit()
    a_uid, b_uid = _uid(env.a, 0), _uid(env.b, 0)
    with tenant_ctx(env.a) as tenant:
        wallet_service.change_balance(tenant.id, a_uid, 100, 'topup')
        db.session.commit()
    env.sync_all()
    side_a, side_b = _item(env.a, env.sides[0])['id'], _item(env.b, env.sides[0])['id']
    for app_, side, uid in ((env.a, side_a, a_uid), (env.b, side_b, b_uid)):
        _checkout(app_, [line(side)], payment_method='禮物卡', order_source='POS', payment_info={'gift_code': code})
        _checkout(app_, [line(side)], payment_method='儲值金', order_source='POS', user_id=uid)
    env.sync_all()
    for app_ in (env.hub, env.a, env.b):
        with tenant_ctx(app_):
            card = GiftCard.query.filter_by(code=code).one()
            assert giftcard_service.balance(card) == -100
    with env.hub.app_context():
        from app.models.user import User
        assert User.query.filter_by(phone='0968900000').one().wallet_balance == -100
    assert any('禮物卡' in a and '-100' in a for a in alerts), alerts
    assert any('儲值金' in a and '跨店同會員0' in a for a in alerts), alerts


# ===========================================================================
# 訂位 / 交班
# ===========================================================================
def test_online_reservations_for_the_last_seats_at_the_same_time(app, store_settings, no_rate_limit):
    """每個時段 4 位，4 組 2 人同時線上訂同一時段 → 只成功 2 組"""
    from app.models.reservation import Reservation
    store_settings('reservation', online_enabled=True, open_time='11:00', close_time='13:00', slot_minutes=60,
                   capacity_per_slot=4, max_party_size=4)
    day = date.today() + timedelta(days=5)
    slot = f'{day}T11:00:00'
    clients = []
    for _ in range(4):
        c = app.test_client()
        c.get('/?shop=default')
        clients.append(c)
    results = run_together(*[lambda c=c, n=n: c.post('/reserve', data={
        'slot': slot, 'name': f'搶位{n}', 'phone': f'09779000{n:02d}', 'party_size': '2'}) for n, c in enumerate(clients)])
    assert all(r.status_code in (302, 400, 409) for r in results), [r.status_code for r in results]
    with app.app_context():
        booked = Reservation.query.filter(Reservation.reserved_at == datetime.fromisoformat(slot),
                                          Reservation.status == 'booked').all()
        assert sum(r.party_size for r in booked) == 4, [(r.name, r.party_size) for r in booked]


def _close_open_shifts(app):
    from app.models.shift import CashShift
    with app.app_context():
        for s in CashShift.query.filter_by(status='open').all():
            s.status, s.closed_at = 'closed', datetime.now()
        db.session.commit()


def test_two_terminals_opening_a_shift_at_once(app):
    from app.models.shift import CashShift
    _close_open_shifts(app)
    login_staff(app, 'shift_mgr', 'manager')
    results = run_together(*[lambda: login_staff(app, 'shift_mgr').post('/api/pos/shift/open', json={'opening_cash': 1000})
                             for _ in range(4)])
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409], [r.get_data(as_text=True)[:100] for r in results]
    with app.app_context():
        assert CashShift.query.filter_by(status='open').count() == 1
    _close_open_shifts(app)


def test_sale_during_shift_close_is_not_lost(app, monkeypatch):
    """
    關班計算應收現金的同時有人現金結帳：那筆錢要嘛算在被關的班 (報表含它)，要嘛不算 (留給下一班)，
    不能「掛在已關的班別上、卻不在報表裡」(交班短溢)。
    """
    from app.models.payment import PaymentRecord
    from app.models.shift import CashShift
    from app.services import shift_service
    _close_open_shifts(app)
    item = make_item(app, '交班中結帳餐', price=100, stock=50)
    login_staff(app, 'close_mgr', 'manager')
    login_staff(app, 'close_pos', 'staff')
    mgr = login_staff(app, 'close_mgr')
    assert mgr.post('/api/pos/shift/open', json={'opening_cash': 1000}).status_code == 200

    real_report = shift_service.shift_report

    def slow_report(shift):   # 計算報表時停一下，讓結帳剛好在這段時間送出
        report = real_report(shift)
        time.sleep(0.6)
        return report

    monkeypatch.setattr(shift_service, 'shift_report', slow_report)

    def close():
        return mgr.post('/api/pos/shift/close', json={'counted_cash': 1000})

    def sale():
        time.sleep(0.2)
        return checkout(login_staff(app, 'close_pos'), [line(item)], url=POS, cash_received=100)

    closed, sold = run_together(close, sale)
    assert closed.status_code == 200 and sold.status_code == 200, (closed.get_data(as_text=True)[:200],
                                                                    sold.get_data(as_text=True)[:200])
    with app.app_context():
        shift = CashShift.query.order_by(CashShift.id.desc()).first()
        attached = sum(p.amount for p in PaymentRecord.query.filter_by(shift_id=shift.id, method='Cash'))
        assert shift.expected_cash == 1000 + attached, '已關的班別上的每一筆現金都在報表的應收現金裡'
        order = db.session.get(Order, sold.get_json()['order_id'])
        assert order.shift_id in (shift.id, None)
        assert (order.shift_id == shift.id) == (attached == 100)
    _close_open_shifts(app)
