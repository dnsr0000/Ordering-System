"""修正小組 wallet-loyalty-giftcards-1 的回歸測試：寄杯退款 / 作廢、儲值加值、生日禮券、訂位訂金"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text, update

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.member import MemberPass, PassLedger, PassProduct
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.reservation import Reservation
from app.models.user import User
from app.services import deferred, monitor_service, payment_ecpay, settings_service
from tests.support import default_tenant_id, get_or_create_tenant, make_item, make_member, tenant_request


@pytest.fixture
def alerts(monkeypatch):
    out = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: out.append(msg))
    return out


def _product(app, tid, uses=3, price=240, name='修正寄杯'):
    item = make_item(app, f'{name}餐點', price=90, stock=50)
    with app.app_context():
        p = PassProduct(tenant_id=tid, name=name, menu_item_id=item, uses=uses, price=price, is_active=True)
        db.session.add(p)
        db.session.commit()
        return p.id


def _sell(app, tid, product_id, uid):
    from app.services import pass_service
    with tenant_request(app, tid):
        user = db.session.get(User, uid)
        mp, order = pass_service.sell(tid, product_id, user, 'Cash', 'fix_pos', cash_received=500)
        db.session.commit()
        return mp.id, order.id


def _refund_amount(order_id, amount):
    """模擬 partial_refund 的條件式更新 refunded_amount 之後呼叫寄杯收回"""
    from app.services import pass_service
    db.session.execute(update(Order).where(Order.id == order_id)
                       .values(refunded_amount=Order.refunded_amount + amount).execution_options(synchronize_session=False))
    return pass_service.on_partial_refund(db.session.get(Order, order_id), 'fix_pos')


# ---------------------------------------------------------------------------
# B-083 / R2-033：寄杯部分退款收回次數、作廢以條件式更新
# ---------------------------------------------------------------------------
def test_pass_partial_refund_revokes_unused_uses(app):
    from app.services import pass_service
    tid = default_tenant_id(app)
    product = _product(app, tid, name='退款寄杯')
    uid = make_member(app, '寄杯退款會員', '0968900001')
    pass_id, order_id = _sell(app, tid, product, uid)
    with tenant_request(app, tid):
        mp = db.session.get(MemberPass, pass_id)
        pass_service.redeem(mp, 1, None, 'fix_pos')        # 已兌換 1 杯
        db.session.commit()
        # 整筆作廢被擋下
        with pytest.raises(AppError) as err:
            pass_service.on_sale_order_cancelled(db.session.get(Order, order_id))
        assert err.value.status == 409
        db.session.rollback()
        # 退 $160 (2 杯) → 收回 2 次
        assert _refund_amount(order_id, 160) == 2
        db.session.commit()
        mp = db.session.get(MemberPass, pass_id)
        db.session.refresh(mp)
        assert (mp.remaining, mp.status) == (0, 'active')
        ledger = PassLedger.query.filter_by(member_pass_id=pass_id, reason='refund').one()
        assert (ledger.delta, ledger.order_id) == (-2, order_id)
        assert pass_service.member_passes(uid) == []
        # 已兌換的那一杯不能再退
        with pytest.raises(AppError) as err:
            _refund_amount(order_id, 80)
        assert err.value.status == 409
        db.session.rollback()


def test_pass_small_refunds_accumulate_and_cancel_after_refund(app):
    from app.services import pass_service
    tid = default_tenant_id(app)
    product = _product(app, tid, name='累計退款寄杯')
    uid = make_member(app, '累計退款會員', '0968900002')
    pass_id, order_id = _sell(app, tid, product, uid)
    with tenant_request(app, tid):
        assert _refund_amount(order_id, 10) == 1           # 無條件進位：先收回 1 次
        assert _refund_amount(order_id, 70) == 0           # 累計 $80 仍是 1 次，不重複收回
        db.session.commit()
        assert db.session.get(MemberPass, pass_id).remaining == 2
        # 部分退款後仍未兌換：可整筆作廢，作廢異動只記剩下的 2 次
        pass_service.on_sale_order_cancelled(db.session.get(Order, order_id))
        db.session.commit()
        mp = db.session.get(MemberPass, pass_id)
        db.session.refresh(mp)
        assert (mp.status, mp.remaining) == ('void', 0)
        total = sum(e.delta for e in PassLedger.query.filter_by(member_pass_id=pass_id).all())
        assert total == 0


def test_pass_full_partial_refund_voids_pass(app):
    tid = default_tenant_id(app)
    product = _product(app, tid, name='全額退款寄杯')
    uid = make_member(app, '全額退款會員', '0968900003')
    pass_id, order_id = _sell(app, tid, product, uid)
    with tenant_request(app, tid):
        assert _refund_amount(order_id, 240) == 3
        db.session.commit()
        mp = db.session.get(MemberPass, pass_id)
        db.session.refresh(mp)
        assert (mp.status, mp.remaining) == ('void', 0)


def test_pass_void_does_not_overwrite_concurrent_redeem(app):
    """作廢以「剩餘次數仍等於未使用次數」為條件：讀到的資料已被其他交易扣次時不會蓋掉 (改為 409)"""
    from app.services import pass_service
    tid = default_tenant_id(app)
    product = _product(app, tid, name='競態寄杯')
    uid = make_member(app, '競態寄杯會員', '0968900004')
    pass_id, order_id = _sell(app, tid, product, uid)
    with tenant_request(app, tid):
        db.session.get(MemberPass, pass_id)     # session 內留著 remaining=3 的舊資料
        db.session.execute(text('UPDATE member_pass SET remaining = remaining - 1 WHERE id = :i'), {'i': pass_id})
        with pytest.raises(AppError) as err:
            pass_service.on_sale_order_cancelled(db.session.get(Order, order_id))
        assert err.value.status == 409
        db.session.rollback()
        db.session.execute(text('UPDATE member_pass SET remaining = 3 WHERE id = :i'), {'i': pass_id})
        db.session.commit()


# ---------------------------------------------------------------------------
# B-089 / B-096 / B-095：寄杯售出
# ---------------------------------------------------------------------------
def test_pass_sell_long_name_no_kitchen_ticket(app, monkeypatch):
    from app.services import pass_service, print_service
    tid = default_tenant_id(app)
    product = _product(app, tid, name='長名寄杯')
    uid = make_member(app, '長' * 80, '0968900005')
    with tenant_request(app, tid):
        user = db.session.get(User, uid)
        _mp, order = pass_service.sell(tid, product, user, 'Cash', 'fix_pos', cash_received=500)
        assert len(order.table_number) == 50
        prints = [(a, k) for fn, a, k in db.session.info.get(deferred._PENDING, []) if fn is print_service.print_order]
        assert prints and all(k.get('kitchen') is False for _a, k in prints)
        db.session.rollback()


def test_pass_sell_and_topup_require_open_shift(app):
    from app.services import pass_service, wallet_service
    tid = get_or_create_tenant(app, 'fix-wallet-shift')
    uid = make_member(app, '開班會員', '0968900006', tenant_id=tid)
    item = make_item(app, '開班寄杯餐點', price=90, stock=50)
    with app.app_context():
        menu = db.session.get(MenuItem, item)
        menu.tenant_id = tid
        p = PassProduct(tenant_id=tid, name='開班寄杯', menu_item_id=item, uses=3, price=240, is_active=True)
        db.session.add(p)
        settings_service.put('pos', {'require_shift': True}, tenant_id=tid)
        settings_service.put('member', {'wallet_enabled': True}, tenant_id=tid)
        db.session.commit()
        product = p.id
    with tenant_request(app, tid):
        user = db.session.get(User, uid)
        with pytest.raises(AppError) as err:
            pass_service.sell(tid, product, user, 'Cash', 'fix_pos', cash_received=500)
        assert err.value.status == 409 and err.value.extra.get('need_shift')
        db.session.rollback()
        with pytest.raises(AppError) as err:
            wallet_service.topup(tid, user, 1000, 'Cash', 'fix_pos')
        assert err.value.status == 409 and err.value.extra.get('need_shift')
        db.session.rollback()


# ---------------------------------------------------------------------------
# B-099 / B-100：儲值加值的金額與付款方式
# ---------------------------------------------------------------------------
def test_topup_rejects_bad_amount_and_methods(app, staff_client, store_settings):
    store_settings('member', wallet_enabled=True, wallet_max_balance=0)
    uid = make_member(app, '加值驗證會員', '0968900007')
    for amount in ('1,000', 'abc'):
        r = staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': amount, 'payment_method': 'Cash'})
        assert r.status_code == 400, (amount, r.get_data(as_text=True))
    for method in ('禮物卡', 'ATM 轉帳', '超商代碼', '混合付款', '儲值金', '平台代收', 'bogus'):
        r = staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': 500, 'payment_method': method})
        assert r.status_code == 400, method
    r = staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': '300', 'payment_method': 'Line Pay'})
    assert r.status_code == 200 and r.get_json()['amount'] == 300
    with app.app_context():
        assert db.session.get(User, uid).wallet_balance == 300


# ---------------------------------------------------------------------------
# B-090：生日禮券過期
# ---------------------------------------------------------------------------
def test_birthday_gift_skips_expired_coupon(app, alerts):
    from app.services import wallet_service
    tid = get_or_create_tenant(app, 'fix-birthday')
    today = date.today()
    uid = make_member(app, '壽星', '0968900008', tenant_id=tid, birthday=today.replace(year=1990),
                      birthday_gift_year=0)
    with app.app_context():
        db.session.add(Coupon(tenant_id=tid, code='FIXBDAY', title='生日券', discount_type='fixed', discount_value=50,
                              valid_until=today - timedelta(days=1)))
        settings_service.put('member', {'birthday_enabled': True, 'birthday_coupon_code': 'FIXBDAY',
                                        'birthday_points': 0}, tenant_id=tid)
        db.session.commit()
    with tenant_request(app, tid):
        assert wallet_service.grant_birthday_gifts(tid, today) == 0
        assert db.session.get(User, uid).birthday_gift_year == 0      # 沒佔用今年的生日禮
        assert UserCoupon.query.filter_by(user_id=uid).execution_options(skip_tenant_filter=True).count() == 0
    assert any('FIXBDAY' in a and '活動已結束' in a for a in alerts)
    # 有設定點數：照發點數、不發過期券
    with app.app_context():
        settings_service.put('member', {'birthday_points': 30}, tenant_id=tid)
        db.session.commit()
    with tenant_request(app, tid):
        assert wallet_service.grant_birthday_gifts(tid, today) == 1
        u = db.session.get(User, uid)
        db.session.refresh(u)
        assert (u.birthday_gift_year, u.points) == (today.year, 30)
        assert UserCoupon.query.filter_by(user_id=uid).execution_options(skip_tenant_filter=True).count() == 0


# ---------------------------------------------------------------------------
# 訂位訂金：B-092 / R2-031 / R2-036 / R2-037
# ---------------------------------------------------------------------------
@pytest.fixture
def ecpay_calls(app, monkeypatch, store_settings):
    store_settings('reservation', deposit_refund_hours=24, deposit_on_seat='refund')
    calls, state = [], {'code': '1', 'seen': []}

    def fake_post(url, data):
        # 呼叫綠界時訂金的 refunding 狀態已提交 (另一條連線看得到)：不在寫入交易中等待外部回應
        with db.engine.connect() as conn:
            state['seen'].append(conn.execute(text('SELECT deposit_status FROM reservation WHERE deposit_trade_no = :t'),
                                              {'t': data['MerchantTradeNo']}).scalar())
        calls.append(data)
        return {'RtnCode': state['code'], 'RtnMsg': 'OK' if state['code'] == '1' else '退款失敗'}
    monkeypatch.setattr(payment_ecpay, '_post', fake_post)
    return calls, state


def _reservation(app, trade, hours=48, status='booked', deposit='paid'):
    tid = default_tenant_id(app)
    with app.app_context():
        r = Reservation(tenant_id=tid, name='訂金修正', phone='0968900099', party_size=4,
                        reserved_at=datetime.now() + timedelta(hours=hours), source='online', deposit_amount=400,
                        deposit_status=deposit, deposit_trade_no=trade, deposit_provider_no='EC' + trade[-6:],
                        status=status)
        db.session.add(r)
        db.session.commit()
        return r.id


def _set_status(app, rid, status, **kw):
    from app.services import reservation_service
    with tenant_request(app, default_tenant_id(app)):
        r = db.session.get(Reservation, rid)
        msg = reservation_service.set_reservation_status(r, status, **kw)
        db.session.commit()
        return msg, r.deposit_status


def test_deposit_refund_commits_before_calling_ecpay(app, ecpay_calls, alerts):
    calls, state = ecpay_calls
    rid = _reservation(app, 'KFIXDEP000001')
    msg, deposit = _set_status(app, rid, 'cancelled')
    assert deposit == 'refunded' and '將退回' in msg
    assert state['seen'][-1] == 'refunding'


def test_deposit_forfeited_restored_when_status_reverted(app, ecpay_calls, alerts):
    calls, _state = ecpay_calls
    rid = _reservation(app, 'KFIXDEP000002')
    assert _set_status(app, rid, 'no_show')[1] == 'forfeited'
    n = len(calls)
    msg, deposit = _set_status(app, rid, 'seated')       # 誤標未到 → 改回入座：依入座規則退還保證金
    assert deposit == 'refunded' and '已退還' in msg and len(calls) == n + 1
    rid2 = _reservation(app, 'KFIXDEP000003', hours=2)
    assert _set_status(app, rid2, 'cancelled')[1] == 'forfeited'
    msg, deposit = _set_status(app, rid2, 'booked')
    assert deposit == 'paid' and '恢復' in msg


def test_deposit_store_cancel_refunds_within_limit(app, ecpay_calls):
    from app.services import deposit_service
    rid = _reservation(app, 'KFIXDEP000004', hours=2)
    with tenant_request(app, default_tenant_id(app)):
        r = db.session.get(Reservation, rid)
        msg = deposit_service.on_status(r, 'cancelled', by_store=True)
        db.session.commit()
        assert r.deposit_status == 'refunded' and '將退回' in msg


def test_deposit_refund_failed_retry_and_manual(app, ecpay_calls, alerts):
    from app.services import deposit_service
    calls, state = ecpay_calls
    state['code'] = '10200047'
    rid = _reservation(app, 'KFIXDEP000005')
    rid2 = _reservation(app, 'KFIXDEP000006')
    assert _set_status(app, rid, 'cancelled')[1] == 'refund_failed'
    assert _set_status(app, rid2, 'cancelled')[1] == 'refund_failed'
    state['code'] = '1'
    with tenant_request(app, default_tenant_id(app)):
        r = db.session.get(Reservation, rid)
        assert deposit_service.retry_refund(r) == 'refunded'
        db.session.commit()
        r2 = db.session.get(Reservation, rid2)
        assert deposit_service.mark_refunded_manually(r2, 'mgr') == 'refunded'
        db.session.commit()
        assert '人工退款' in r2.deposit_note
        with pytest.raises(AppError):
            deposit_service.mark_refunded_manually(r2, 'mgr')
        db.session.rollback()


def test_month_stats_excludes_next_month(app):
    from app.services import deposit_service
    tid = default_tenant_id(app)
    today = date(2031, 11, 30)
    with app.app_context():
        for when, name in ((datetime(2031, 11, 30, 19), 'stat-nov'), (datetime(2031, 12, 1, 19), 'stat-dec')):
            db.session.add(Reservation(tenant_id=tid, name=name, phone='0968900100', party_size=4, reserved_at=when,
                                       deposit_amount=500, deposit_status='forfeited', status='cancelled'))
        db.session.commit()
    with tenant_request(app, tid):
        assert deposit_service.month_stats(tid, today)['forfeited'] == 500
        assert deposit_service.month_stats(tid, date(2031, 12, 5))['forfeited'] == 500


# ---------------------------------------------------------------------------
# 跨檔請求：B-013 / R2-109 寄杯實收上限、B-097 寄杯重算、B-008 / B-342 生日推播、B-230 儲值冪等
# ---------------------------------------------------------------------------
def test_pass_sell_rejects_excessive_cash(app):
    from app.services import pass_service
    tid = default_tenant_id(app)
    product = _product(app, tid, name='實收上限寄杯')
    uid = make_member(app, '實收上限會員', '0968900201')
    with tenant_request(app, tid):
        user = db.session.get(User, uid)
        for cash in (240 + 100001, 10 ** 9):
            with pytest.raises(AppError) as err:
                pass_service.sell(tid, product, user, 'Cash', 'fix_pos', cash_received=cash)
            assert '多出太多' in err.value.message
            db.session.rollback()
        _mp, order = pass_service.sell(tid, product, user, 'Cash', 'fix_pos', cash_received=240 + 100000)
        assert (order.cash_received, order.cash_change) == (100240, 100000)
        db.session.rollback()


def test_pass_recompute_from_ledger(app):
    from app.services import pass_service
    tid = default_tenant_id(app)
    product = _product(app, tid, name='重算寄杯')
    uid = make_member(app, '重算寄杯會員', '0968900202')
    pass_id, _order_id = _sell(app, tid, product, uid)
    with tenant_request(app, tid):
        mp = db.session.get(MemberPass, pass_id)
        db.session.add(PassLedger(tenant_id=tid, member_pass_id=pass_id, delta=-4, reason='redeem'))
        db.session.flush()
        negative = pass_service.recompute([pass_id])
        assert [p.id for p in negative] == [pass_id]
        db.session.refresh(mp)
        assert mp.remaining == -1
        db.session.rollback()


def test_birthday_message_respects_opt_out_and_pushes_after_commit(app, monkeypatch):
    from app.services import line_service, wallet_service
    tid = get_or_create_tenant(app, 'fix-birthday-push')
    today = date.today()
    ok = make_member(app, '壽星推播', '0968900203', tenant_id=tid, birthday=today.replace(year=1991, day=1),
                     birthday_gift_year=0, line_user_id='Ufixpush1')
    opt = make_member(app, '壽星拒收', '0968900204', tenant_id=tid, birthday=today.replace(year=1992, day=1),
                      birthday_gift_year=0, line_user_id='Ufixpush2', marketing_opt_out=True)
    with app.app_context():
        settings_service.put('member', {'birthday_enabled': True, 'birthday_points': 20, 'birthday_coupon_code': '',
                                        'birthday_message': '{name} 生日快樂'}, tenant_id=tid)
        db.session.commit()
    pushed = []

    def fake_push(tenant_id, line_user_id, text_):
        # 推播時生日禮已提交：另一條連線看得到 birthday_gift_year
        with db.engine.connect() as conn:
            year = conn.execute(text('SELECT birthday_gift_year FROM "user" WHERE line_user_id = :l'),
                                {'l': line_user_id}).scalar()
        pushed.append((line_user_id, year, text_))
        return True
    monkeypatch.setattr(line_service, 'push_text', fake_push)
    with tenant_request(app, tid):
        assert wallet_service.grant_birthday_gifts(tid, today) == 2
        assert pushed == [('Ufixpush1', today.year, '壽星推播 生日快樂')]
        for uid in (ok, opt):
            u = db.session.get(User, uid)
            db.session.refresh(u)
            assert (u.birthday_gift_year, u.points) == (today.year, 20)


def test_topup_request_id_is_idempotent(app):
    from app.models.payment import PaymentKind, PaymentRecord
    from app.services import wallet_service
    if not hasattr(PaymentRecord, 'request_id'):
        pytest.skip('payment_record.request_id 欄位尚未加入')
    tid = get_or_create_tenant(app, 'fix-topup-idem')
    uid = make_member(app, '冪等加值會員', '0968900205', tenant_id=tid)
    with app.app_context():
        settings_service.put('member', {'wallet_enabled': True, 'wallet_max_balance': 0,
                                        'topup_bonus': '1000:100'}, tenant_id=tid)
        settings_service.put('pos', {'require_shift': False}, tenant_id=tid)
        db.session.commit()
    with tenant_request(app, tid):
        user = db.session.get(User, uid)
        assert wallet_service.topup(tid, user, 1000, 'Cash', 'fix_pos', request_id='rid-fix-1') == (1000, 100)
        db.session.commit()
        assert wallet_service.topup(tid, user, 1000, 'Cash', 'fix_pos', request_id='rid-fix-1') == (1000, 100)
        db.session.commit()
        db.session.refresh(user)
        assert user.wallet_balance == 1100
        assert PaymentRecord.query.filter_by(tenant_id=tid, kind=PaymentKind.TOPUP) \
            .execution_options(skip_tenant_filter=True).count() == 1
        assert wallet_service.topup(tid, user, 1000, 'Cash', 'fix_pos', request_id='rid-fix-2') == (1000, 100)
        db.session.commit()
        db.session.refresh(user)
        assert user.wallet_balance == 2200
