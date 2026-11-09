"""
修正小組 order-pricing-checkout-2 的回歸測試：
結帳份數上限與促銷分組計算 (B-006)、寄杯不可附套餐 / 升級規格 (B-007)、品項欄位型別 (B-021)、
批量發券排除拒絕行銷 (B-008)、撤回批次保留過期券 (R2-003)、跨午夜促銷的星期 (B-019)、線上訂單庫存不足的回補 (B-018)。
"""
import random
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.errors import AppError
from app.extensions import db
from tests.support import default_tenant_id, guest_client, make_item, make_member, tenant_request


# ---------------------------------------------------------------------------
# B-006：促銷依品項分組計算 (與逐份展開的結果相同)，份數有上限
# ---------------------------------------------------------------------------
def _promo(pid, kind, **kw):
    base = dict(id=pid, priority=0, is_active=True, start_date=None, end_date=None, days='1234567', start_time='',
                end_time='', channels='', member_only=False, kind=kind, threshold=0, amount=0, percent=None,
                target_type='all', target_value='', name=f'P{pid}', stack_with_coupon=True, buy_qty=1, get_qty=1, nth=2,
                gift_item_uuid='')
    base.update(kw)
    return SimpleNamespace(**base)


def _reference_item_discounts(regular, promos):
    """修正前的逐份展開演算法 (只算單品活動)，作為對照"""
    from app.services.promotion_service import _matches, _round
    units = [(ln.unit_price, idx) for idx, ln in enumerate(regular) for _ in range(max(0, int(ln.quantity)))]
    used = [False] * len(units)
    by_line = [0.0] * len(regular)
    amounts = []
    for promo in promos:
        pool = sorted((i for i, (_p, idx) in enumerate(units) if not used[i] and _matches(promo, regular[idx].menu_item)),
                      key=lambda i: -units[i][0])
        discount = 0.0
        if promo.kind == 'bogo':
            buy, get = max(1, promo.buy_qty or 1), max(1, promo.get_qty or 1)
            size = buy + get
            for start in range(0, len(pool) - size + 1, size):
                group = pool[start:start + size]
                for i in group:
                    used[i] = True
                for i in group[buy:]:
                    discount += units[i][0]
                    by_line[units[i][1]] += units[i][0]
        else:
            n = max(2, promo.nth or 2)
            pct = min(100, max(0, promo.percent if promo.percent is not None else 50))
            for start in range(0, len(pool) - n + 1, n):
                group = pool[start:start + n]
                for i in group:
                    used[i] = True
                off = units[group[-1]][0] * (100 - pct) / 100
                discount += off
                by_line[units[group[-1]][1]] += off
        if _round(discount) > 0:
            amounts.append((promo.name, _round(discount)))
    return amounts


def test_grouped_promotion_matches_unit_expansion():
    from app.services import promotion_service as ps
    rng = random.Random(20261109)
    cats = ['飲料', '主餐', '點心']
    for _ in range(200):
        lines = []
        for k in range(rng.randint(1, 6)):
            item = SimpleNamespace(uuid=f'u{k}', category=rng.choice(cats), id=k)
            lines.append(SimpleNamespace(menu_item=item, quantity=rng.randint(0, 9), unit_price=rng.choice([35, 45, 50, 55, 60, 89]),
                                         is_add_on=False, is_reward=False))
        promos = [
            _promo(1, 'bogo', priority=3, buy_qty=rng.randint(1, 3), get_qty=rng.randint(1, 2),
                   target_type='category', target_value=rng.choice(cats)),
            _promo(2, 'nth_discount', priority=2, nth=rng.randint(2, 4), percent=rng.choice([0, 33, 50, 70])),
            _promo(3, 'bogo', priority=1, buy_qty=1, get_qty=1),
        ]
        expected = _reference_item_discounts(lines, promos)
        got = ps.evaluate(lines, 'pos', promotions=promos)
        assert [(d['name'], d['amount']) for d in got.details] == expected


def test_grouped_promotion_handles_huge_quantity_quickly():
    from app.services import promotion_service as ps
    item = SimpleNamespace(uuid='u1', category='飲料', id=1)
    lines = [SimpleNamespace(menu_item=item, quantity=10 ** 12, unit_price=50, is_add_on=False, is_reward=False)]
    started = time.monotonic()
    result = ps.evaluate(lines, 'pos', promotions=[_promo(1, 'bogo', buy_qty=1, get_qty=1)])
    assert time.monotonic() - started < 1
    assert result.discount == 50 * 10 ** 12 // 2


def test_parse_lines_rejects_huge_quantity(app):
    from app.services.order_pricing import MAX_LINE_QUANTITY, MAX_ORDER_QUANTITY, _parse_lines
    item_id = make_item(app, 'B006 份數上限', price=10, stock=10)
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        for qty in ('1e10', MAX_LINE_QUANTITY + 1):
            with pytest.raises(AppError):
                _parse_lines([{'menu_item_id': item_id, 'quantity': qty}], tid)
        assert _parse_lines([{'menu_item_id': item_id, 'quantity': MAX_LINE_QUANTITY}], tid)[0].quantity == MAX_LINE_QUANTITY
        many = [{'menu_item_id': item_id, 'quantity': MAX_LINE_QUANTITY}] * (MAX_ORDER_QUANTITY // MAX_LINE_QUANTITY + 1)
        with pytest.raises(AppError):
            _parse_lines(many, tid)


# ---------------------------------------------------------------------------
# B-007：寄杯兌換不可附帶套餐，也不可選非預設規格
# ---------------------------------------------------------------------------
def test_pass_line_cannot_take_combo_or_upgraded_size(app):
    from app.models.menu import ComboOption, ComboOptionItem
    from app.services.order_pricing import _parse_lines
    tid = default_tenant_id(app)
    drink = make_item(app, 'B007 寄杯拿鐵', price=60, stock=50, category='飲料', size_options='中杯:0, 大杯:+15')
    side = make_item(app, 'B007 薯條', price=40, stock=50, category='點心')
    with app.app_context():
        combo = ComboOption(tenant_id=tid, main_item_id=drink, name='B007 套餐', additional_price=50)
        db.session.add(combo)
        db.session.flush()
        db.session.add(ComboOptionItem(tenant_id=tid, combo_option_id=combo.id, menu_item_id=side, position=0))
        db.session.commit()
        combo_id = combo.id
    with tenant_request(app, tid):
        with pytest.raises(AppError, match='套餐'):
            _parse_lines([{'menu_item_id': drink, 'quantity': 1, 'pass_id': 7, 'combo_option_id': combo_id}], tid)
        with pytest.raises(AppError, match='預設規格'):
            _parse_lines([{'menu_item_id': drink, 'quantity': 1, 'pass_id': 7, 'size': '大杯'}], tid)
        ln = _parse_lines([{'menu_item_id': drink, 'quantity': 1, 'pass_id': 7}], tid)[0]
        assert ln.is_pass and ln.unit_price == 0 and ln.combo is None and not ln.sides
        ln = _parse_lines([{'menu_item_id': drink, 'quantity': 1, 'pass_id': 7, 'size': '中杯'}], tid)[0]
        assert ln.unit_price == 0
        # 一般購買照常可選套餐與大杯
        ln = _parse_lines([{'menu_item_id': drink, 'quantity': 1, 'combo_option_id': combo_id, 'size': '大杯'}], tid)[0]
        assert ln.unit_price == 60 + 15 + 50


# ---------------------------------------------------------------------------
# B-021：modifier_ids / combo_choices 不是陣列時回 400 (不是 500)
# ---------------------------------------------------------------------------
def test_non_list_modifier_ids_and_combo_choices_are_rejected(app):
    from app.models.menu import ComboOption
    from app.services.order_pricing import _parse_lines
    tid = default_tenant_id(app)
    main = make_item(app, 'B021 主餐', price=100, stock=50, modifiers='addons')
    with app.app_context():
        combo = ComboOption(tenant_id=tid, main_item_id=main, name='B021 套餐', additional_price=30)
        db.session.add(combo)
        db.session.commit()
        combo_id = combo.id
    with tenant_request(app, tid):
        with pytest.raises(AppError):
            _parse_lines([{'menu_item_id': main, 'quantity': 1, 'modifier_ids': 5}], tid)
        with pytest.raises(AppError):
            _parse_lines([{'menu_item_id': main, 'quantity': 1, 'combo_option_id': combo_id, 'combo_choices': 7}], tid)
        assert _parse_lines([{'menu_item_id': main, 'quantity': 1, 'modifier_ids': None}], tid)[0].unit_price == 100
    client = guest_client(app)
    r = client.post('/submit_order', json={'items': [{'menu_item_id': main, 'quantity': 1, 'modifier_ids': 5}],
                                           'payment_method': 'Cash', 'order_type': '內用'})
    assert r.status_code < 500


# ---------------------------------------------------------------------------
# B-019：只開放週五的跨午夜促銷，凌晨段算前一天
# ---------------------------------------------------------------------------
def test_cross_midnight_promotion_uses_previous_day_after_midnight():
    from app.services import promotion_service as ps
    promo = _promo(1, 'threshold_discount', days='5', start_time='22:00', end_time='02:00')
    fri, sat = datetime(2026, 11, 6), datetime(2026, 11, 7)   # 週五、週六
    assert ps.is_active(promo, 'pos', False, fri.replace(hour=23))
    assert ps.is_active(promo, 'pos', False, sat.replace(hour=1))        # 週五宵夜的後半段
    assert not ps.is_active(promo, 'pos', False, fri.replace(hour=1))    # 其實是週四深夜
    assert not ps.is_active(promo, 'pos', False, sat.replace(hour=23))
    promo.days, promo.end_date = '1234567', fri.date()
    assert ps.is_active(promo, 'pos', False, sat.replace(hour=1))        # 活動最後一晚延續到午夜後
    assert not ps.is_active(promo, 'pos', False, sat.replace(hour=23))


# ---------------------------------------------------------------------------
# B-008：批量發券與 LINE 通知排除拒絕行銷的會員
# ---------------------------------------------------------------------------
def test_batch_issue_targets_and_line_notice_skip_marketing_opt_out(app, monkeypatch):
    from app.models.coupon import Coupon
    from app.services import coupon_service, line_service
    tid = default_tenant_id(app)
    ok = make_member(app, 'B008 願意', '0968000001', line_user_id='U' + '1' * 32)
    out = make_member(app, 'B008 拒絕', '0968000002', line_user_id='U' + '2' * 32, marketing_opt_out=True)
    sent = []
    monkeypatch.setattr(line_service, 'messaging_enabled', lambda _tid: True)
    monkeypatch.setattr(line_service, 'multicast_text', lambda _tid, ids, _text: sent.append(list(ids)) or len(ids))
    from app.models.user import User
    with tenant_request(app, tid):
        try:
            for target in ('all', 'line'):
                ids = {u.id for u in coupon_service.resolve_targets(tid, target)[0]}
                assert ok in ids and out not in ids
            phones = {u.id for u in coupon_service.resolve_targets(tid, 'phones', phones_text='0968000001 0968000002')[0]}
            assert phones == {ok, out}   # 指定手機照常發券
            coupon = Coupon(tenant_id=tid, code='B008GIFT', title='B008 券', discount_value=10)
            db.session.add(coupon)
            db.session.flush()
            assert coupon_service.notify_line(tid, [ok, out], coupon, None) == 1
            assert sent == [['U' + '1' * 32]]
        finally:   # 共用的測試資料庫：移除這兩位會員，不影響其他測試的會員人數 / LINE 名單
            db.session.rollback()
            for uid in (ok, out):
                db.session.delete(db.session.get(User, uid))
            db.session.commit()


# ---------------------------------------------------------------------------
# R2-003：撤回發放批次只刪除未使用 (可用 / 尚未開始) 的券，已過期的保留
# ---------------------------------------------------------------------------
def test_revoke_batch_keeps_expired_coupons(app):
    from app.models.coupon import Coupon, UserCoupon
    from app.services import coupon_service
    tid = default_tenant_id(app)
    u1 = make_member(app, 'R2003 甲', '0968000011')
    u2 = make_member(app, 'R2003 乙', '0968000012')
    now = datetime.now()
    with tenant_request(app, tid):
        coupon = Coupon(tenant_id=tid, code='R2003', title='R2003 券', discount_value=10,
                        per_user_limit=1)
        db.session.add(coupon)
        db.session.flush()
        batch = uuid.uuid4().hex
        db.session.add_all([
            UserCoupon(tenant_id=tid, user_id=u1, coupon_id=coupon.id, code='R2003', created_at=now - timedelta(days=10),
                       expires_at=now - timedelta(days=1), source='batch', batch_id=batch),
            UserCoupon(tenant_id=tid, user_id=u2, coupon_id=coupon.id, code='R2003', created_at=now - timedelta(days=10),
                       expires_at=now + timedelta(days=5), source='batch', batch_id=batch),
        ])
        db.session.commit()
        unused = next(b for b in coupon_service.recent_batches(tid) if b['batch_id'] == batch)['unused']
        assert coupon_service.revoke_batch(tid, batch) == unused == 1
        db.session.commit()
        left = UserCoupon.query.filter_by(batch_id=batch).all()
        assert [uc.user_id for uc in left] == [u1]   # 過期券保留：每人上限照常計算
        from app.models.user import User
        result = coupon_service.issue(tid, coupon, [db.session.get(User, u1), db.session.get(User, u2)])
        assert result['issued'] == 1 and result['user_ids'] == [u2]
        db.session.rollback()


# ---------------------------------------------------------------------------
# B-018：線上訂單送達分店時沒扣到的份數，取消時不回補
# ---------------------------------------------------------------------------
def test_online_order_shortfall_is_not_restocked(app):
    from app.models.menu import MenuItem
    from app.services.order_stock import _change_stock, consume_stock_unchecked, restock_demands
    tid = default_tenant_id(app)
    item_id = make_item(app, 'B018 限量便當', price=100, stock=1)
    other_id = make_item(app, 'B018 紅茶', price=30, stock=10)
    order = SimpleNamespace(uuid=str(uuid.uuid4()))
    with tenant_request(app, tid):
        short = consume_stock_unchecked(tid, {item_id: 3, other_id: 2}, order_uuid=order.uuid)
        assert [(need, had) for _name, need, had in short] == [(3, 1)]
        db.session.commit()
        assert db.session.get(MenuItem, item_id).stock == 0
        # 部分作廢：先扣掉沒扣到的份數 (作廢 1 份便當不回補)
        assert restock_demands(order, {item_id: 1}) == {}
        # 整張取消：便當只回補實際扣到的 1 份，紅茶照常回補
        _change_stock(tid, restock_demands(order, {item_id: 2, other_id: 2}), +1)
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(MenuItem, item_id).stock == 1
        assert db.session.get(MenuItem, other_id).stock == 10
        # 紀錄已用完：之後的回補不再扣除
        assert restock_demands(order, {item_id: 1}) == {item_id: 1}
