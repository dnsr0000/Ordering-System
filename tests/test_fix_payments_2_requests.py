"""
跨檔請求 (payments-2) 的回歸測試：綠界線上付款 (payment_ecpay) 與電子發票 (einvoice_ecpay)。
以假的綠界 API 取代 HTTP；資料自行建立，不依賴其他測試檔。
"""
import uuid as uuidlib
from datetime import datetime

import pytest

from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus
from app.services import einvoice_ecpay as ecpay
from tests.support import checkout, default_tenant_id, line, make_item

POS = '/api/pos/checkout'
KEY, IV = ecpay.TEST_MERCHANT[1], ecpay.TEST_MERCHANT[2]


@pytest.fixture
def fake_ecpay(monkeypatch, store_settings):
    """啟用電子發票 (測試環境、不自動開立) 並以假的綠界 API 取代 HTTP。handlers[路徑] 可指定回應 (dict / 函式)。"""
    store_settings('einvoice', enabled=True, stage=True, fallback_email='shop@example.com', auto_issue=False)
    calls, handlers = [], {}

    def fake_post(url, payload, timeout):
        path = url.replace(ecpay.STAGE_URL, '')
        data = ecpay.decrypt_data(payload['Data'], KEY, IV)
        calls.append((path, data))
        handler = handlers.get(path)
        if callable(handler):
            handler = handler(data)
        if handler is not None:
            result = handler
        elif path == '/B2CInvoice/Issue':
            result = {'RtnCode': 1, 'InvoiceNo': f'PR{len(calls):08d}',
                      'InvoiceDate': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'RandomNumber': '1234'}
        elif path == '/B2CInvoice/GetIssue':
            result = {'RtnCode': 2000060, 'RtnMsg': '查無發票資料'}
        else:
            result = {'RtnCode': 1, 'RtnMsg': 'OK', 'IA_Allow_No': 'A000000000000001'}
        return {'TransCode': 1, 'Data': ecpay.encrypt_data(result, KEY, IV)}

    monkeypatch.setattr(ecpay, '_post', fake_post)
    return calls, handlers


def _pos_order(app, staff_client, name, price=100):
    iid = make_item(app, name, price=price, stock=20)
    r = checkout(staff_client, [line(iid)], url=POS, cash_received=price)
    assert r.status_code == 200, r.get_json()
    return r.get_json()['order_id']


def _invoice(app, order_id):
    with app.app_context():
        inv = Invoice.query.filter_by(order_id=order_id).order_by(Invoice.id.desc()).first()
        return {c.name: getattr(inv, c.name) for c in Invoice.__table__.columns} if inv else None


def _cancel_in_db(order_id):
    with db.engine.begin() as conn:
        conn.execute(Order.__table__.update().where(Order.__table__.c.id == order_id).values(status='Cancelled'))


def _pending_online(app, staff_client, name, **values):
    oid = _pos_order(app, staff_client, name, price=120)
    with app.app_context():
        o = db.session.get(Order, oid)
        o.payment_status = 'pending'
        o.payment_trade_no = None
        for k, v in values.items():
            setattr(o, k, v)
        db.session.commit()
        return oid, o.uuid, o.total_price


# ---------------------------------------------------------------------------
# B-056：修正前被換日自動結案 (Completed、completed_at 為空) 的待付款單，付款後改回製作中
# ---------------------------------------------------------------------------
def test_mark_paid_reopens_order_auto_closed_while_unpaid(app, staff_client):
    from app.services import payment_ecpay
    oid, uuid, total = _pending_online(app, staff_client, 'P2R 舊資料自動結案', status='Completed', completed_at=None)
    with app.app_context():
        assert payment_ecpay.mark_paid(uuid, 'TRADE-P2R-1', total) == 'paid'
        o = db.session.get(Order, oid)
        assert (o.status, o.payment_status) == ('Pending', 'paid'), '付款後要進分店廚房'
    # 真的出過餐 (有 completed_at) 的不改
    done_at = datetime.now()
    oid2, uuid2, total2 = _pending_online(app, staff_client, 'P2R 已出餐', status='Completed', completed_at=done_at)
    with app.app_context():
        assert payment_ecpay.mark_paid(uuid2, 'TRADE-P2R-2', total2) == 'paid'
        assert db.session.get(Order, oid2).status == 'Completed'


# ---------------------------------------------------------------------------
# R2-065：付款完成但取餐分店已停用 → 告警
# ---------------------------------------------------------------------------
def test_mark_paid_alerts_when_pickup_branch_is_disabled(app, staff_client, monkeypatch):
    from app.models.sync import SyncNode
    from app.services import monitor_service, payment_ecpay
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda message, **kw: alerts.append((message, kw)))
    tid = default_tenant_id(app)
    with app.app_context():
        off = SyncNode(tenant_id=tid, name='P2R 停用分店', token_hash=f'p2r-off-{uuidlib.uuid4().hex}', is_active=False)
        on = SyncNode(tenant_id=tid, name='P2R 營業分店', token_hash=f'p2r-on-{uuidlib.uuid4().hex}')
        db.session.add_all([off, on])
        db.session.commit()
        off_uuid, on_uuid = off.uuid, on.uuid
    oid, uuid, total = _pending_online(app, staff_client, 'P2R 停用分店單', node_uuid=on_uuid)
    with app.app_context():
        assert payment_ecpay.mark_paid(uuid, 'TRADE-P2R-ON', total) == 'paid'
    assert alerts == []
    oid, uuid, total = _pending_online(app, staff_client, 'P2R 停用分店單 2', node_uuid=off_uuid)
    with app.app_context():
        assert payment_ecpay.mark_paid(uuid, 'TRADE-P2R-OFF', total) == 'paid'
        assert db.session.get(Order, oid).payment_status == 'paid', '只警示，不改付款狀態'
    assert len(alerts) == 1 and 'P2R 停用分店' in alerts[0][0]
    assert alerts[0][1]['key'] == f'order-node-disabled-{uuid}' and alerts[0][1]['tenant_id'] == tid


# ---------------------------------------------------------------------------
# B-036：搶到發票後才發現訂單已取消 → 不送出；開立途中訂單被取消 → 開立後補作廢
# ---------------------------------------------------------------------------
def test_issue_invoice_skips_order_cancelled_after_claim(app, staff_client, fake_ecpay, monkeypatch):
    calls, _ = fake_ecpay
    oid = _pos_order(app, staff_client, 'P2R 搶到後已取消')
    inv = _invoice(app, oid)
    assert inv['status'] == InvoiceStatus.PENDING and inv['attempts'] == 0
    real = ecpay._order_cancelled
    seen = {'n': 0}

    def cancelled_after_first_check(i):
        seen['n'] += 1
        if seen['n'] == 1:   # 開立前的檢查通過後，訂單才被取消
            result = real(i)
            _cancel_in_db(oid)
            return result
        return real(i)

    monkeypatch.setattr(ecpay, '_order_cancelled', cancelled_after_first_check)
    n = len(calls)
    with app.app_context():
        assert ecpay.issue_invoice(inv['id']) == InvoiceStatus.VOID
    assert not any(path == '/B2CInvoice/Issue' for path, _ in calls[n:])
    inv = _invoice(app, oid)
    assert inv['status'] == InvoiceStatus.VOID and inv['attempts'] == 0 and '取消' in inv['last_error']


def test_issue_invoice_voids_when_order_cancelled_during_issue(app, staff_client, fake_ecpay):
    calls, handlers = fake_ecpay
    oid = _pos_order(app, staff_client, 'P2R 開立中取消')
    inv = _invoice(app, oid)

    def issue_then_cancel(data):
        _cancel_in_db(oid)   # 綠界處理期間店員取消訂單 (取消當下發票在開立中)
        return {'RtnCode': 1, 'InvoiceNo': 'PR99999999', 'RandomNumber': '9999',
                'InvoiceDate': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

    handlers['/B2CInvoice/Issue'] = issue_then_cancel
    with app.app_context():
        assert ecpay.issue_invoice(inv['id']) == InvoiceStatus.VOID
    assert calls[-1][0] == '/B2CInvoice/Invalid' and calls[-1][1]['InvoiceNo'] == 'PR99999999'
    assert _invoice(app, oid)['status'] == InvoiceStatus.VOID


# ---------------------------------------------------------------------------
# R2-010：折讓金額超過發票剩餘金額時不送綠界
# ---------------------------------------------------------------------------
def test_allowance_over_remaining_amount_is_rejected_locally(app, staff_client, fake_ecpay):
    calls, _ = fake_ecpay
    oid = _pos_order(app, staff_client, 'P2R 超額折讓', price=100)
    inv = _invoice(app, oid)
    with app.app_context():
        assert ecpay.issue_invoice(inv['id']) == InvoiceStatus.ISSUED
        n = len(calls)
        assert ecpay.allowance(inv['id'], 101) == InvoiceStatus.ISSUED
    assert len(calls) == n, '超過剩餘金額：不呼叫綠界'
    inv = _invoice(app, oid)
    assert inv['last_error'] == '折讓金額超過發票剩餘金額' and not inv['allowance_amount']
    with app.app_context():
        assert ecpay.allowance(inv['id'], 100) == InvoiceStatus.ALLOWANCE
    assert _invoice(app, oid)['allowance_amount'] == 100
