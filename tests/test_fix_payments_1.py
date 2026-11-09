"""payments-1 修正的回歸測試：付款方式設定讀取 (背景工作)、儲值金開關、部分退款驗證、發票調整、綠界自動退刷"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice, Refund
from app.services import payment_service
from tests.support import checkout, default_tenant_id, get_or_create_tenant, line, make_item, make_member, tenant_request

POS = '/api/pos/checkout'


def _invoices(app, order_id):
    with app.app_context():
        return [(i.status, i.amount) for i in Invoice.query.filter_by(order_id=order_id)
                .execution_options(skip_tenant_filter=True).order_by(Invoice.id).all()]


@pytest.fixture
def einvoice_manual(store_settings):
    """啟用電子發票但不自動開立：發票停在「待開立」，不會呼叫綠界"""
    store_settings('einvoice', enabled=True, stage=True, fallback_email='shop@example.com', auto_issue=False)


# ---------------------------------------------------------------------------
# B-026 / R2-009：付款方式設定
# ---------------------------------------------------------------------------
def test_allowed_methods_and_card_details_work_without_request_context(app, store_settings):
    """背景排程 (團體預購自動送單) 只有 app_context：帶 tenant_id 時可讀取設定"""
    tid = default_tenant_id(app)
    store_settings('card_terminal', require_auth_code=True)
    with app.app_context():
        assert 'Cash' in payment_service.allowed_methods('kiosk', tid)
        assert payment_service.validate_card_details({'auth_code': 'ab 1234'}, True, tid)[0] == 'AB1234'
        with pytest.raises(Exception, match='授權碼'):
            payment_service.validate_card_details({}, True, tid)


def test_wallet_disabled_removes_stored_value_from_every_channel(app, store_settings, staff_client):
    tid = default_tenant_id(app)
    store_settings('payments', pos_methods=['Cash', '儲值金'], kiosk_methods=['Cash', '儲值金'])
    store_settings('member', wallet_enabled=False)
    with app.app_context():
        assert '儲值金' not in payment_service.allowed_methods('pos', tid)
        assert '儲值金' not in payment_service.allowed_methods('kiosk', tid)
    uid = make_member(app, '停用儲值金會員', '0966150001', wallet_balance=500)
    iid = make_item(app, '停用儲值金餐', price=100, stock=10)
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='儲值金', user_id=uid)
    assert r.status_code == 400 and '未開放' in r.get_json()['message']
    store_settings('member', wallet_enabled=True)
    with app.app_context():
        assert '儲值金' in payment_service.allowed_methods('pos', tid)


# ---------------------------------------------------------------------------
# B-050 / B-038：部分退款的輸入驗證
# ---------------------------------------------------------------------------
def test_partial_refund_rejects_bad_amount_and_ignores_method_for_single_tender(app, admin_client):
    iid = make_item(app, '退款驗證餐', price=200, stock=10)
    oid = checkout(admin_client, [line(iid)], url=POS, cash_received=200).get_json()['order_id']
    for bad in ('abc', [1], {'a': 1}):
        r = admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': bad, 'reason': 'x'})
        assert r.status_code == 400, (bad, r.status_code)
    r = admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 50, 'reason': 'x', 'method': '儲值金'})
    assert r.status_code == 200
    with app.app_context():
        refund = Refund.query.filter_by(order_id=oid).execution_options(skip_tenant_filter=True).one()
        assert (refund.method, refund.status) == ('Cash', 'done'), '現金訂單一律退現金'


# ---------------------------------------------------------------------------
# B-027 / B-039 / B-036：電子發票
# ---------------------------------------------------------------------------
def test_stored_value_order_gets_invoice(app, staff_client, store_settings, einvoice_manual):
    store_settings('member', wallet_enabled=True)
    uid = make_member(app, '儲值發票會員', '0966150002', wallet_balance=1000)
    iid = make_item(app, '儲值發票餐', price=150, stock=10)
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='儲值金', user_id=uid)
    assert r.status_code == 200, r.get_json()
    assert _invoices(app, r.get_json()['order_id']) == [('pending', 150)]


def test_partial_refund_before_issue_reduces_invoice(app, admin_client, einvoice_manual):
    iid = make_item(app, '開立前退款餐', price=300, stock=10)
    oid = checkout(admin_client, [line(iid)], url=POS, cash_received=300).get_json()['order_id']
    assert _invoices(app, oid) == [('pending', 300)]
    assert admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 100, 'reason': '少一樣'}).status_code == 200
    assert _invoices(app, oid) == [('pending', 200)]
    assert admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 200, 'reason': '全退'}).status_code == 200
    assert _invoices(app, oid) == [('void', 0)]


def test_split_wallet_refund_does_not_allowance_legacy_invoice(app, admin_client, store_settings, einvoice_manual,
                                                               monkeypatch):
    """舊版混合付款發票不含儲值金：退回儲值金不折讓；其他方式的折讓不超過發票剩餘金額"""
    from app.services import einvoice_ecpay
    allowances = []
    monkeypatch.setattr(einvoice_ecpay, 'allowance', lambda iid, amount, reason='': allowances.append((iid, amount)))
    store_settings('member', wallet_enabled=True)
    uid = make_member(app, '混合發票會員', '0966150003', wallet_balance=1000)
    iid = make_item(app, '混合發票餐', price=150, stock=10)
    r = checkout(admin_client, [line(iid)], url=POS, payment_method='混合付款', user_id=uid, cash_received=100,
                 payment_info={'tenders': [{'method': 'Cash', 'amount': 100}, {'method': '儲值金', 'amount': 50}]})
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    assert _invoices(app, oid) == [('pending', 150)], '混合付款的儲值金也開進發票'
    with app.app_context():
        inv = Invoice.query.filter_by(order_id=oid).execution_options(skip_tenant_filter=True).one()
        inv.status, inv.amount, inv.invoice_no, inv.invoice_date = 'issued', 100, 'AB00000001', datetime.now()
        db.session.commit()
        inv_id = inv.id
    r = admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 50, 'reason': '退儲值金', 'method': '儲值金'})
    assert r.status_code == 200 and allowances == []
    r = admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 100, 'reason': '退現金', 'method': 'Cash'})
    assert r.status_code == 200 and allowances == [(inv_id, 100)]


def test_cancel_voids_failed_invoice(app, admin_client, einvoice_manual):
    iid = make_item(app, '開立失敗取消餐', price=120, stock=10)
    oid = checkout(admin_client, [line(iid)], url=POS, cash_received=120).get_json()['order_id']
    with app.app_context():
        inv = Invoice.query.filter_by(order_id=oid).execution_options(skip_tenant_filter=True).one()
        inv.status = 'failed'
        db.session.commit()
    assert admin_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled'}).status_code == 200
    assert _invoices(app, oid)[0][0] == 'void'


# ---------------------------------------------------------------------------
# B-049：桌位現金結帳後作廢
# ---------------------------------------------------------------------------
def test_cancel_cash_settled_table_order_is_refunded(app):
    from app.models.table import DiningTable
    from app.models.tenant import StaffAccount
    from app.services import table_service
    from app.services.order_service import change_order_status, process_checkout
    tid = default_tenant_id(app)
    iid = make_item(app, '桌位作廢餐', price=90, stock=10)
    with tenant_request(app, tid):
        table = DiningTable(tenant_id=tid, name='FIX付款桌')
        db.session.add(table)
        db.session.commit()
        order, _ = process_checkout(tenant_id=tid, items=[line(iid)], dining_table=table)
        oid = order.id
        cashier = StaffAccount(tenant_id=tid, username='fix_pay_cashier', role='staff', password_hash='x')
        table_service.settle_table(tid, table.id, 'Cash', 0, cashier)
        owner = StaffAccount(tenant_id=tid, username='fix_pay_owner', role='owner', password_hash='x')
        change_order_status(oid, 'Cancelled', owner, tid)
    with app.app_context():
        assert db.session.get(Order, oid).payment_status == 'refunded'
        refund = Refund.query.filter_by(order_id=oid).execution_options(skip_tenant_filter=True).one()
        assert (refund.method, refund.status) == ('Cash', 'done')


# ---------------------------------------------------------------------------
# R2-014：標記退款完成
# ---------------------------------------------------------------------------
def _refund_required_order(app, tenant_id, total=500, **kw):
    with app.app_context():
        o = Order(tenant_id=tenant_id, total_price=total, payment_method='Credit Card', order_source='Online',
                  status='Cancelled', payment_status='refund_required', payment_trade_no=kw.pop('trade_no', 'T000001'),
                  refunded_amount=total, **kw)
        db.session.add(o)
        db.session.commit()
        return o.id


def _add_refund(app, order_id, kind, amount, status):
    with app.app_context():
        o = db.session.get(Order, order_id)
        r = Refund(tenant_id=o.tenant_id, order_id=order_id, kind=kind, amount=amount, method='Credit Card', status=status)
        db.session.add(r)
        db.session.commit()
        return r.id


def test_mark_refund_done_keeps_refund_required_until_all_done(app):
    oid = _refund_required_order(app, default_tenant_id(app))
    first = _add_refund(app, oid, 'partial', 100, 'manual')
    second = _add_refund(app, oid, 'cancel', 400, 'manual')
    with app.app_context():
        payment_service.mark_refund_done(first, 'tester')
        db.session.commit()
        assert db.session.get(Order, oid).payment_status == 'refund_required'
        payment_service.mark_refund_done(second, 'tester')
        db.session.commit()
        assert db.session.get(Order, oid).payment_status == 'refunded'


# ---------------------------------------------------------------------------
# B-028 / R2-006 / B-040 / B-041：綠界自動退刷
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_ecpay(monkeypatch):
    from app.services import payment_ecpay
    calls = []
    monkeypatch.setattr(payment_service, 'ecpay_integrated', lambda order, method=None: True)
    monkeypatch.setattr(payment_ecpay, 'is_enabled', lambda: True)
    monkeypatch.setattr(payment_ecpay, 'refund', lambda order, amount, post=None: calls.append((order.id, amount))
                        or f'綠界退刷成功 ${amount}')
    return calls


def test_auto_refund_only_completes_the_cancel_refund(app, fake_ecpay):
    oid = _refund_required_order(app, default_tenant_id(app))
    partial = _add_refund(app, oid, 'partial', 100, 'failed')
    cancel = _add_refund(app, oid, 'cancel', 400, 'manual')
    with app.app_context():
        ok, _msg = payment_service.auto_refund_online_order(oid)
        assert ok and fake_ecpay == [(oid, 400)]
        assert db.session.get(Refund, cancel).status == 'done'
        assert db.session.get(Refund, partial).status == 'failed', '失敗的部分退款不可被標成已退款'
        assert db.session.get(Order, oid).payment_status == 'refunded'


def test_auto_refund_skips_ecpay_when_already_fully_refunded(app, fake_ecpay):
    oid = _refund_required_order(app, default_tenant_id(app), total=300)
    _add_refund(app, oid, 'partial', 300, 'done')
    with app.app_context():
        ok, _msg = payment_service.auto_refund_online_order(oid)
        assert ok and fake_ecpay == [], '先前已全額部分退款：不可再用訂單總額退刷'
        assert db.session.get(Order, oid).payment_status == 'refunded'


def test_auto_refund_claims_order_before_calling_ecpay(app, fake_ecpay):
    oid = _refund_required_order(app, default_tenant_id(app))
    _add_refund(app, oid, 'cancel', 500, 'manual')
    with app.app_context():
        o = db.session.get(Order, oid)
        o.payment_note = f'{payment_service.AUTO_REFUNDING}{datetime.now():%Y-%m-%d %H:%M:%S}'
        db.session.commit()
        ok, _msg = payment_service.auto_refund_online_order(oid)
        assert not ok and fake_ecpay == [], '另一個請求正在退刷：不重複退刷'
        o = db.session.get(Order, oid)
        o.payment_note = f'{payment_service.AUTO_REFUNDING}2000-01-01 00:00:00'   # 逾時的處理中標記 (中斷)
        db.session.commit()
        ok, _msg = payment_service.auto_refund_online_order(oid)
        assert ok and fake_ecpay == [(oid, 500)]


def test_run_auto_refunds_is_not_blocked_by_other_tenants(app, fake_ecpay, store_settings):
    other = get_or_create_tenant(app, 'fix-pay1-noauto')
    blockers = [_refund_required_order(app, other, trade_no=f'TB{i:04d}') for i in range(3)]
    mine = _refund_required_order(app, default_tenant_id(app), trade_no='TMINE01')
    _add_refund(app, mine, 'cancel', 500, 'manual')
    store_settings('payments', ecpay_auto_refund=True)
    try:
        with app.app_context():
            assert payment_service.run_auto_refunds(limit=2) >= 1
            assert (mine, 500) in fake_ecpay
            assert not any(oid in blockers for oid, _a in fake_ecpay), '未開啟自動退刷的店家不退刷'
    finally:
        with app.app_context():
            for oid in blockers + [mine]:
                db.session.get(Order, oid).payment_status = 'refunded'
            db.session.commit()
