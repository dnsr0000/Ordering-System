"""修正小組 order-pricing-checkout-1 的回歸測試：整單折扣核准不在交易中途 commit、重送未付款 / 已取消的 POS 訂單、
系統內部建立的訂單 (團購) 在背景執行、會員等級贈點倍數、實收現金上限、LINE Pay $0 與發票資訊、線上端不收儲值金"""
from datetime import datetime, timedelta

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.member import MemberTier
from app.models.order import Order
from app.models.payment import Invoice, PaymentRecord
from app.models.user import User
from tests.support import (
    checkout, default_tenant_id, item_stock, line, login_staff, make_item, make_member, new_client_order_id,
    tenant_request,
)

POS = '/api/pos/checkout'


def _order(app, order_id):
    with app.app_context():
        o = db.session.get(Order, order_id)
        return dict(status=o.status, payment_status=o.payment_status, points_earned=o.points_earned,
                    approved_by=o.approved_by, total_price=o.total_price)


def _order_count(app):
    with app.app_context():
        return Order.query.filter_by(tenant_id=default_tenant_id(app)).count()


def test_discount_approval_failure_leaves_no_trace(app, store_settings):
    """B-002：主管密碼錯誤時不可已扣庫存 / 取號；正確密碼重送才成立，核准人寫入訂單"""
    store_settings('pos', discount_approval_percent=0)
    staff = login_staff(app, 'fix_opc1_staff', 'staff')
    login_staff(app, 'fix_opc1_mgr', 'manager')
    iid = make_item(app, '核准折扣測試餐', price=200, stock=10)
    before = _order_count(app)
    payload = dict(order_discount={'type': 'percent', 'value': 10}, client_order_id=new_client_order_id())
    r = checkout(staff, [line(iid, 2)], url=POS, **payload)
    assert r.status_code == 403 and r.get_json().get('need_approval')
    r = checkout(staff, [line(iid, 2)], url=POS, approver_username='fix_opc1_mgr', approver_password='wrong-pass', **payload)
    assert r.status_code == 403
    assert item_stock(app, iid) == 10 and _order_count(app) == before   # 失敗的核准沒有留下任何扣庫存 / 訂單
    # 核准正確但金額不符 (409)：同樣不可留下扣庫存
    r = checkout(staff, [line(iid, 2)], url=POS, approver_username='fix_opc1_mgr', approver_password='StaffPass!2026',
                 expected_total=999, **payload)
    assert r.status_code == 409 and item_stock(app, iid) == 10 and _order_count(app) == before
    r = checkout(staff, [line(iid, 2)], url=POS, approver_username='fix_opc1_mgr', approver_password='StaffPass!2026',
                 expected_total=360, **payload)
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data['total_price'] == 360 and data['manual_discount'] == 40
    assert item_stock(app, iid) == 8 and _order(app, data['order_id'])['approved_by'] == 'fix_opc1_mgr'


def test_replay_of_authorizing_or_cancelled_pos_order_is_not_a_receipt(app):
    """B-003：LINE Pay 扣款中 / 已取消的原單，以同一個 client_order_id 重送不可回傳「已結帳」收據"""
    staff = login_staff(app, 'fix_opc1_replay', 'staff')
    iid = make_item(app, '重送測試餐', price=100, stock=10)
    coid = new_client_order_id()
    r = checkout(staff, [line(iid)], url=POS, client_order_id=coid)
    assert r.status_code == 200
    oid = r.get_json()['order_id']
    with app.app_context():
        db.session.get(Order, oid).payment_status = 'authorizing'
        db.session.commit()
    r = checkout(staff, [line(iid)], url=POS, client_order_id=coid)
    assert r.status_code == 409 and r.get_json().get('still_authorizing')
    with app.app_context():
        o = db.session.get(Order, oid)
        o.payment_status, o.status = '', 'Cancelled'
        db.session.commit()
    r = checkout(staff, [line(iid)], url=POS, client_order_id=coid)
    assert r.status_code == 409 and r.get_json().get('payment_failed')
    # 一般已完成的訂單照常回傳原單 (replayed)
    coid2 = new_client_order_id()
    assert checkout(staff, [line(iid)], url=POS, client_order_id=coid2).status_code == 200
    r = checkout(staff, [line(iid)], url=POS, client_order_id=coid2)
    assert r.status_code == 200 and r.get_json()['replayed'] is True


def test_group_order_checkout_without_request_context(app, store_settings):
    """B-004 / B-009 / B-015：背景排程 (無請求情境) 送出團購、點餐機不收現金時也能建單；現場付款不先記收款"""
    from app.services.order_service import process_checkout
    store_settings('payments', kiosk_methods=['Credit Card'])
    iid = make_item(app, '背景團購餐', price=120, stock=10)
    tid = default_tenant_id(app)
    with app.app_context():
        order, replayed = process_checkout(
            tenant_id=tid, items=[line(iid, 2)], payment_method='Cash', order_type='外帶',
            table_input='團購 測試', client_order_id=new_client_order_id(), cashier_username='system',
            pickup_at=datetime.now() + timedelta(hours=3))
        assert not replayed and order.total_price == 240
        assert order.payment_status == 'unpaid'
        assert PaymentRecord.query.filter_by(order_id=order.id).count() == 0
    # 斷線補送 (skip_intake) 也不受點餐機付款方式設定限制
    with tenant_request(app, tid):
        order, _ = process_checkout(tenant_id=tid, items=[line(iid)], payment_method='Cash',
                                    client_order_id=new_client_order_id(), skip_intake=True)
        assert order.total_price == 120
    # 一般點餐機訂單仍受限制
    with tenant_request(app, tid):
        with pytest.raises(AppError):
            process_checkout(tenant_id=tid, items=[line(iid)], payment_method='Cash')


def test_tier_points_multiplier_respects_switch_and_zero(app, store_settings):
    """B-012 / B-020：關閉會員等級後不套用贈點倍數；倍數 0 = 不累積點數"""
    tid = default_tenant_id(app)
    with app.app_context():
        double = MemberTier(tenant_id=tid, name='修正倍數二', min_spend=99999999, points_multiplier=2)
        zero = MemberTier(tenant_id=tid, name='修正倍數零', min_spend=99999999, points_multiplier=0)
        db.session.add_all([double, zero])
        db.session.commit()
        double_id, zero_id = double.id, zero.id
        from app.models.user import RewardSetting
        per = RewardSetting.query.filter_by(tenant_id=tid).first().spend_per_point or 100
    u1 = make_member(app, '倍數會員一', '0955300401', tier_id=double_id)
    u2 = make_member(app, '倍數會員二', '0955300402', tier_id=zero_id)
    iid = make_item(app, '倍數測試餐', price=1000, stock=20)
    staff = login_staff(app, 'fix_opc1_tier', 'staff')
    store_settings('member', tiers_enabled=False)
    d = checkout(staff, [line(iid)], url=POS, user_id=u1, cash_received=1000).get_json()
    assert d['total_price'] == 1000 and _order(app, d['order_id'])['points_earned'] == 1000 // per
    store_settings('member', tiers_enabled=True)
    d = checkout(staff, [line(iid)], url=POS, user_id=u1, cash_received=1000).get_json()
    assert _order(app, d['order_id'])['points_earned'] == 1000 // per * 2
    d = checkout(staff, [line(iid)], url=POS, user_id=u2, cash_received=1000).get_json()
    assert _order(app, d['order_id'])['points_earned'] == 0


def test_cash_received_has_upper_bound(app):
    """B-013：實收現金誤按出天文數字時拒絕結帳 (不會存入超出 INTEGER 的金額)"""
    staff = login_staff(app, 'fix_opc1_cash', 'staff')
    iid = make_item(app, '實收上限測試餐', price=100, stock=10)
    r = checkout(staff, [line(iid)], url=POS, cash_received=10000000000)
    assert r.status_code == 400
    assert item_stock(app, iid) == 10
    r = checkout(staff, [line(iid)], url=POS, cash_received=1000)
    assert r.status_code == 200 and r.get_json()['cash_change'] == 900


@pytest.fixture
def linepay_on(monkeypatch, store_settings):
    from app.services import linepay
    store_settings('linepay', enabled=True, channel_id='123', channel_secret='secret')
    calls = []

    def fake_pay(tenant_id, order_id, amount, one_time_key):
        calls.append(amount)
        if int(amount) <= 0:
            raise linepay.LinePayError('金額錯誤', 'X')
        return 'TX' + str(len(calls))
    monkeypatch.setattr(linepay, 'pay', fake_pay)
    return calls


def test_linepay_zero_total_is_not_charged(app, linepay_on):
    """R2-004：應付 $0 時不向 LINE Pay 扣 0 元，訂單直接成立"""
    mgr = login_staff(app, 'fix_opc1_lp_mgr', 'manager')
    iid = make_item(app, 'LINE Pay 零元餐', price=100, stock=10)
    r = checkout(mgr, [line(iid)], url=POS, payment_method='Line Pay', order_discount={'type': 'percent', 'value': 100},
                 payment_info={'one_time_key': '123456789012'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['total_price'] == 0 and linepay_on == []
    o = _order(app, r.get_json()['order_id'])
    assert o['status'] != 'Cancelled' and o['payment_status'] == ''


def test_linepay_keeps_invoice_info(app, linepay_on, store_settings):
    """B-016：LINE Pay 扣款成功後開立的發票保留收銀輸入的載具"""
    store_settings('einvoice', enabled=True, stage=True, auto_issue=False)
    staff = login_staff(app, 'fix_opc1_lp', 'staff')
    iid = make_item(app, 'LINE Pay 發票餐', price=150, stock=10)
    r = checkout(staff, [line(iid)], url=POS, payment_method='Line Pay', payment_info={'one_time_key': '123456789012'},
                 invoice={'type': 'mobile', 'carrier': '/ABC+123'})
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    assert linepay_on == [150]
    with app.app_context():
        invs = Invoice.query.filter_by(order_id=oid).all()
        assert len(invs) == 1
        assert invs[0].status == 'pending' and invs[0].carrier_num == '/ABC+123'


def test_hub_rejects_wallet_payment(app, monkeypatch):
    """B-001：線上端 (線上付款) 不接受儲值金 / 禮物卡付款 (避免扣儲值金後又導去綠界付全額)"""
    from app.services.order_service import process_checkout
    from app.sync import runtime as sync_runtime
    tid = default_tenant_id(app)
    uid = make_member(app, '線上儲值會員', '0955300403')
    with app.app_context():
        db.session.get(User, uid).wallet_balance = 1000
        db.session.commit()
    iid = make_item(app, '線上儲值測試餐', price=120, stock=10)
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    for method in ('儲值金', '禮物卡'):
        with tenant_request(app, tid):
            with pytest.raises(AppError) as exc:
                process_checkout(tenant_id=tid, items=[line(iid)], payment_method=method, user_id=uid)
            assert '線上點餐不支援' in exc.value.message
    with app.app_context():
        assert db.session.get(User, uid).wallet_balance == 1000
    assert item_stock(app, iid) == 10
