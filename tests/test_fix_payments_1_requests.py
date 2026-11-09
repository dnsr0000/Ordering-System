"""
跨檔請求處理者 (payments-1) 的回歸測試：payment_service 的作廢 / 退款 / 發票作廢修正。
資料自行建立，不依賴其他測試檔。
"""
from datetime import datetime

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.enums import PaymentMethod
from app.models.member import GiftCard, MemberPass, PassProduct
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus, Refund
from app.models.user import User
from app.services import einvoice_ecpay as ecpay
from app.services import monitor_service, payment_service
from tests.support import (cancel_order, checkout, default_tenant_id, line, make_item, make_member, pos_checkout,
                           tenant_request)

POS = '/api/pos/checkout'
KEY, IV = ecpay.TEST_MERCHANT[1], ecpay.TEST_MERCHANT[2]


@pytest.fixture
def alerts(monkeypatch):
    out = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: out.append(msg))
    return out


def _cash_order(app, staff_client, name, price=100, user_id=None):
    iid = make_item(app, name, price=price, stock=20)
    r = checkout(staff_client, [line(iid)], url=POS, cash_received=price, **({'user_id': user_id} if user_id else {}))
    assert r.status_code == 200, r.get_json()
    return r.get_json()['order_id']


def _set_order(app, order_id, **values):
    with app.app_context():
        db.session.execute(Order.__table__.update().where(Order.__table__.c.id == order_id).values(**values))
        db.session.commit()


# ---------------------------------------------------------------------------
# B-001：未收款就取消、但已先扣儲值金的訂單要退回儲值金
# ---------------------------------------------------------------------------
def test_unpaid_stored_value_order_cancel_refunds_wallet(app, staff_client, admin_client):
    uid = make_member(app, '請求修正儲值退回', '0969100001', wallet_balance=0)
    oid = _cash_order(app, staff_client, '請求修正儲值未付', price=50, user_id=uid)
    _set_order(app, oid, payment_method=PaymentMethod.STORED_VALUE.value, payment_status='pending', wallet_used=50)
    assert cancel_order(admin_client, oid).status_code == 200
    with app.app_context():
        assert db.session.get(User, uid).wallet_balance == 50
        assert db.session.get(Order, oid).payment_status == 'expired'


# ---------------------------------------------------------------------------
# B-083：寄杯購買訂單部分退款收回未使用次數；超過未使用價值時整筆回滾
# ---------------------------------------------------------------------------
def test_partial_refund_of_pass_sale_revokes_uses(app):
    from app.services import pass_service
    tid = default_tenant_id(app)
    item = make_item(app, '請求修正寄杯餐點', price=90, stock=50)
    uid = make_member(app, '請求修正寄杯會員', '0969100002')
    with tenant_request(app, tid):
        product = PassProduct(tenant_id=tid, name='請求修正寄杯', menu_item_id=item, uses=3, price=240, is_active=True)
        db.session.add(product)
        db.session.commit()
        mp, order = pass_service.sell(tid, product.id, db.session.get(User, uid), 'Cash', 'fix_pos', cash_received=500)
        db.session.commit()
        pass_id, order_id = mp.id, order.id
        pass_service.redeem(db.session.get(MemberPass, pass_id), 1, None, 'fix_pos')
        db.session.commit()
        payment_service.partial_refund(order_id, 160, '退兩杯', tenant_id=tid)
        db.session.commit()
        mp = db.session.get(MemberPass, pass_id)
        db.session.refresh(mp)
        assert mp.remaining == 0
        with pytest.raises(AppError) as err:
            payment_service.partial_refund(order_id, 80, '已兌換的也退', tenant_id=tid)
        assert err.value.status == 409
        assert db.session.get(Order, order_id).refunded_amount == 160, '寄杯收回失敗時退款金額要回滾'


# ---------------------------------------------------------------------------
# B-093：禮物卡已作廢時，部分退款改記人工退款，不退回卡片
# ---------------------------------------------------------------------------
def test_partial_refund_to_voided_gift_card_is_manual(app, staff_client, admin_client, store_settings):
    from app.models.member import GiftCardLedger
    store_settings('gift_card', enabled=True)
    r = staff_client.post('/api/pos/gift_cards/sell', json={'amount': 300, 'payment_method': 'Cash'})
    assert r.status_code == 200, r.get_json()
    code = r.get_json()['card']['code']
    item = make_item(app, '請求修正作廢卡部分退款', price=180)
    r = staff_client.post(POS, json={'items': [line(item)], 'payment_method': '禮物卡', 'order_type': '內用',
                                     'payment_info': {'gift_code': code}})
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    with app.app_context():
        card_id = GiftCard.query.filter_by(code=code).one().id
    admin_client.post('/admin/gift_cards', data={'action': 'void', 'card_id': str(card_id), 'reason': '遺失'})
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        refund = payment_service.partial_refund(oid, 50, '部分退款', tenant_id=tid)
        db.session.commit()
        assert refund.status == 'manual' and refund.completed_at is None
        assert GiftCardLedger.query.filter_by(gift_card_id=card_id, reason='refund').count() == 0


# ---------------------------------------------------------------------------
# R2-120：部分退款後會員累積消費立即扣回
# ---------------------------------------------------------------------------
def test_partial_refund_recomputes_member_spent(app, staff_client):
    uid = make_member(app, '請求修正累積消費', '0969100003')
    item = make_item(app, '請求修正累積消費餐點', price=100, stock=20)
    r = pos_checkout(staff_client, item, user_id=uid)
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        before = db.session.get(User, uid).total_spent
        payment_service.partial_refund(oid, 40, '部分退款', tenant_id=tid)
        db.session.commit()
        assert db.session.get(User, uid).total_spent == before - 40


# ---------------------------------------------------------------------------
# R2-053：桌位混合付款結帳的訂單作廢後全部當場退款 → 不留在「需退款」
# ---------------------------------------------------------------------------
def test_split_settled_order_cancel_all_cash_marks_refunded(app, staff_client):
    oid = _cash_order(app, staff_client, '請求修正桌位混合付款')
    _set_order(app, oid, payment_method=PaymentMethod.SPLIT.value, settlement_id=987654,
               payment_status='refund_required')
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        order = db.session.get(Order, oid)
        refund = payment_service.on_order_cancelled(order, reason='測試')
        db.session.commit()
        assert refund is not None and refund.status == 'done'
        assert db.session.get(Order, oid).payment_status == 'refunded'


# ---------------------------------------------------------------------------
# B-044 / B-457：process_refund 非預期錯誤改為失敗；線上端不處理分店的退款
# ---------------------------------------------------------------------------
def _linepay_refund(app, staff_client, name):
    oid = _cash_order(app, staff_client, name)
    tid = default_tenant_id(app)
    with app.app_context():
        r = Refund(tenant_id=tid, order_id=oid, kind='partial', amount=30, method=PaymentMethod.LINE_PAY.value,
                   reason='測試', status='pending', staff_username='fix')
        db.session.add(r)
        db.session.commit()
        return oid, r.id


def test_process_refund_unexpected_error_marks_failed(app, staff_client, monkeypatch, alerts):
    from app.services import linepay

    def boom(*a, **k):
        raise RuntimeError('decrypt failed')
    monkeypatch.setattr(linepay, 'refund', boom)
    _oid, rid = _linepay_refund(app, staff_client, '請求修正退款非預期錯誤')
    with tenant_request(app, default_tenant_id(app)):
        assert payment_service.process_refund(rid) == 'failed'
    with app.app_context():
        r = db.session.get(Refund, rid)
        assert r.status == 'failed' and '非預期錯誤' in r.provider_result
    assert alerts


def test_hub_skips_refund_of_branch_order(app, staff_client, monkeypatch):
    from app.services import linepay
    from app.sync import runtime as sync_runtime
    called = []
    monkeypatch.setattr(linepay, 'refund', lambda *a, **k: called.append(a) or 'TX1')
    oid, rid = _linepay_refund(app, staff_client, '請求修正線上端分店退款')
    _set_order(app, oid, node_uuid='00000000-0000-0000-0000-0000000fix01')
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    with tenant_request(app, default_tenant_id(app)):
        assert payment_service.process_refund(rid) is None
    with app.app_context():
        assert db.session.get(Refund, rid).status == 'pending'
    assert called == [], '線上端不可替分店退款'


# ---------------------------------------------------------------------------
# B-031：曾送到綠界的待開立發票，取消訂單時先查詢綠界再作廢
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_ecpay(monkeypatch, store_settings):
    store_settings('einvoice', enabled=True, stage=True, fallback_email='shop@example.com')
    calls, handlers = [], {}

    def fake_post(url, payload, timeout):
        path = url.replace(ecpay.STAGE_URL, '')
        calls.append((path, ecpay.decrypt_data(payload['Data'], KEY, IV)))
        handler = handlers.get(path)
        if isinstance(handler, Exception):
            raise handler
        if handler is not None:
            result = handler
        elif path == '/B2CInvoice/GetIssue':
            result = {'RtnCode': 2000060, 'RtnMsg': '查無發票資料'}
        elif path == '/B2CInvoice/Issue':
            result = {'RtnCode': 1, 'InvoiceNo': 'ZZ10000001', 'RandomNumber': '1234',
                      'InvoiceDate': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        else:
            result = {'RtnCode': 1, 'RtnMsg': 'OK'}
        return {'TransCode': 1, 'Data': ecpay.encrypt_data(result, KEY, IV)}

    monkeypatch.setattr(ecpay, '_post', fake_post)
    return calls, handlers


def _order_invoice(app, oid):
    with app.app_context():
        inv = Invoice.query.filter_by(order_id=oid).order_by(Invoice.id.desc()).first()
        return inv.id, inv.status, inv.attempts


def test_cancel_with_previously_sent_invoice_voids_on_ecpay(app, staff_client, admin_client, fake_ecpay):
    calls, handlers = fake_ecpay
    handlers['/B2CInvoice/Issue'] = ConnectionError('timeout')
    oid = _cash_order(app, staff_client, '請求修正逾時發票作廢')
    inv_id, status, attempts = _order_invoice(app, oid)
    assert status == InvoiceStatus.PENDING and attempts > 0
    handlers['/B2CInvoice/GetIssue'] = {'RtnCode': 1, 'IIS_Number': 'QQ20000001', 'IIS_Random_Number': '2222',
                                        'IIS_Create_Date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    assert cancel_order(admin_client, oid).status_code == 200
    with app.app_context():
        assert db.session.get(Invoice, inv_id).status == InvoiceStatus.VOID
    assert any(p == '/B2CInvoice/Invalid' and d.get('InvoiceNo') == 'QQ20000001' for p, d in calls), \
        '綠界上其實已開立的發票要真的作廢'


def test_cancel_with_never_sent_invoice_voids_locally(app, staff_client, admin_client, fake_ecpay, store_settings):
    calls, _handlers = fake_ecpay
    store_settings('einvoice', auto_issue=False)
    oid = _cash_order(app, staff_client, '請求修正未送出發票作廢')
    inv_id, status, attempts = _order_invoice(app, oid)
    assert status == InvoiceStatus.PENDING and attempts == 0
    assert cancel_order(admin_client, oid).status_code == 200
    with app.app_context():
        assert db.session.get(Invoice, inv_id).status == InvoiceStatus.VOID
    assert not any(p == '/B2CInvoice/GetIssue' for p, _d in calls), '沒送出過的發票直接在本機作廢'
