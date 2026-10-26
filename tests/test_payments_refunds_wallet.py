"""收款細節與退款：刷卡授權碼、付款方式設定、LINE Pay 掃碼扣款 / 退款 (模擬 API)、部分退款與作廢核准、儲值金"""
import pytest

from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.payment import PaymentRecord, Refund
from app.models.user import User
from app.models.member import WalletLedger
from app.services import linepay
from tests.support import assert_flash, checkout, line, login_staff, make_item, make_member

POS = '/api/pos/checkout'
OTK = '123456789012345678'


@pytest.fixture
def linepay_api(monkeypatch, store_settings):
    """啟用 LINE Pay 並以假 API 取代 HTTP；calls 記錄每次呼叫，responses 可依路徑指定回應"""
    store_settings('linepay', enabled=True, channel_id='1234567890', channel_secret='secret-key')
    calls, responses = [], {}

    def fake_request(method, url, headers, json_body=None, timeout=25):
        calls.append((method, url, json_body, headers))
        for suffix, resp in responses.items():
            if url.endswith(suffix):
                return resp(json_body) if callable(resp) else resp
        if url.endswith('/oneTimeKeys/pay'):
            return {'returnCode': '0000', 'info': {'transactionId': '2026102300001'}}
        if url.endswith('/refund'):
            return {'returnCode': '0000', 'info': {'refundTransactionId': '2026102399999'}}
        return {'returnCode': '0000', 'info': {'status': 'COMPLETE'}}

    monkeypatch.setattr(linepay, '_request', fake_request)
    return calls, responses


def _get(app, model, pk):
    with app.app_context():
        obj = db.session.get(model, pk)
        return {c.name: getattr(obj, c.name) for c in model.__table__.columns}


def _payments(app, order_id):
    with app.app_context():
        return [(p.kind, p.method, p.amount, p.reference, p.card_last4)
                for p in PaymentRecord.query.filter_by(order_id=order_id).order_by(PaymentRecord.id).all()]


def _refunds(app, order_id):
    with app.app_context():
        return [(r.kind, r.amount, r.method, r.status) for r in Refund.query.filter_by(order_id=order_id).order_by(Refund.id).all()]


# ---------------------------------------------------------------------------
# 付款方式與刷卡機記帳
# ---------------------------------------------------------------------------
def test_card_terminal_requires_auth_code(app, staff_client, store_settings):
    iid = make_item(app, '刷卡測試餐', price=120, stock=20)
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='Credit Card')
    assert r.status_code == 400 and r.get_json()['need_card_auth'] is True
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='Credit Card',
                 payment_info={'auth_code': 'ab12 34', 'card_last4': '4321'})
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    assert _payments(app, oid) == [('sale', 'Credit Card', 120, 'AB1234', '4321')]
    store_settings('card_terminal', require_auth_code=False, require_last4=True)
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='Credit Card', payment_info={'card_last4': '12'})
    assert r.status_code == 400 and '末四碼' in r.get_json()['message']


def test_payment_methods_follow_settings(app, staff_client, store_settings):
    iid = make_item(app, '付款方式設定餐', price=50, stock=20)
    store_settings('payments', pos_methods=['Cash'])
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='街口支付')
    assert r.status_code == 400 and '未開放' in r.get_json()['message']
    assert checkout(staff_client, [line(iid)], url=POS, payment_method='Cash', cash_received=100).status_code == 200
    # 平台代收只能用在外送單
    assert checkout(staff_client, [line(iid)], url=POS, payment_method='平台代收').status_code == 400


# ---------------------------------------------------------------------------
# LINE Pay (Offline API) 掃碼扣款
# ---------------------------------------------------------------------------
def test_linepay_pay_success(app, staff_client, linepay_api):
    calls, _ = linepay_api
    iid = make_item(app, 'LINE Pay 成功餐', price=160, stock=10)
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='Line Pay', payment_info={'one_time_key': OTK})
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    order = _get(app, Order, oid)
    assert order['payment_status'] == '' and order['payment_reference'] == '2026102300001'
    method, url, body, headers = calls[-1]
    assert url == 'https://sandbox-api-pay.line.me/v2/payments/oneTimeKeys/pay'
    assert body['amount'] == 160 and body['oneTimeKey'] == OTK and body['orderId'] == order['uuid']
    assert headers['X-LINE-ChannelId'] == '1234567890' and headers['X-LINE-ChannelSecret'] == 'secret-key'
    assert _payments(app, oid) == [('sale', 'Line Pay', 160, '2026102300001', '')]
    # 付款碼格式錯誤：不建單、不呼叫 API
    n = len(calls)
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='Line Pay', payment_info={'one_time_key': 'abc'})
    assert r.status_code == 400 and '付款碼' in r.get_json()['message'] and len(calls) == n


def test_linepay_pay_failure_cancels_and_restores_stock(app, staff_client, linepay_api):
    _, responses = linepay_api
    responses['/oneTimeKeys/pay'] = {'returnCode': '1172', 'returnMessage': 'Existing same orderId.'}
    iid = make_item(app, 'LINE Pay 失敗餐', price=90, stock=5)
    r = checkout(staff_client, [line(iid, 2)], url=POS, payment_method='Line Pay', payment_info={'one_time_key': OTK})
    assert r.status_code == 402 and r.get_json()['payment_failed'] is True and '1172' in r.get_json()['message']
    with app.app_context():
        assert db.session.get(MenuItem, iid).stock == 5
        order = Order.query.filter(Order.items.any(menu_item_id=iid)).order_by(Order.id.desc()).first()
        assert order.status == 'Cancelled' and PaymentRecord.query.filter_by(order_id=order.id).count() == 0


def test_linepay_timeout_checks_order_status(app, staff_client, linepay_api, monkeypatch):
    calls, responses = linepay_api

    def timeout(_body):
        raise TimeoutError('read timeout')
    responses['/oneTimeKeys/pay'] = timeout
    responses['/check'] = {'returnCode': '0000', 'info': {'status': 'COMPLETE', 'transactionId': 'TX-AFTER-TIMEOUT'}}
    iid = make_item(app, 'LINE Pay 逾時餐', price=70, stock=5)
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='Line Pay', payment_info={'one_time_key': OTK})
    assert r.status_code == 200, r.get_json()
    assert _get(app, Order, r.get_json()['order_id'])['payment_reference'] == 'TX-AFTER-TIMEOUT'
    assert calls[-1][1].endswith('/check')


# ---------------------------------------------------------------------------
# 部分退款與作廢
# ---------------------------------------------------------------------------
def test_partial_refund_cash_needs_approval_and_limit(app, staff_client, admin_client):
    iid = make_item(app, '部分退款現金餐', price=100, stock=20)
    oid = checkout(staff_client, [line(iid, 2)], url=POS, cash_received=200).get_json()['order_id']
    url = f'/api/pos/orders/{oid}/refund'
    r = staff_client.post(url, json={'amount': 50, 'reason': '少一份配菜'})
    assert r.status_code == 403 and r.get_json()['need_approval'] is True
    login_staff(app, 'refund_mgr', 'manager')
    r = staff_client.post(url, json={'amount': 50, 'reason': '少一份配菜', 'approver_username': 'refund_mgr',
                                     'approver_password': 'StaffPass!2026'})
    assert r.status_code == 200 and r.get_json()['status'] == 'done'
    assert admin_client.post(url, json={'amount': 10}).status_code == 400            # 必須填原因
    r = admin_client.post(url, json={'amount': 151, 'reason': '超過'})
    assert r.status_code == 409 and '$150' in r.get_json()['message']
    assert admin_client.post(url, json={'amount': 150, 'reason': '全退'}).status_code == 200
    order = _get(app, Order, oid)
    assert order['refunded_amount'] == 200 and order['status'] != 'Cancelled'
    assert _refunds(app, oid) == [('partial', 50, 'Cash', 'done'), ('partial', 150, 'Cash', 'done')]
    assert [p[2] for p in _payments(app, oid)] == [200, -50, -150]
    with app.app_context():
        r = Refund.query.filter_by(order_id=oid).first()
        assert r.approved_by == 'refund_mgr' and r.staff_username == 'pos_user'
    # 已全額退款後取消訂單不再重複退
    assert admin_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled'}).status_code == 200
    assert len(_refunds(app, oid)) == 2


def test_partial_refund_linepay_auto_and_retry(app, admin_client, linepay_api):
    calls, responses = linepay_api
    iid = make_item(app, 'LINE Pay 退款餐', price=300, stock=10)
    oid = checkout(admin_client, [line(iid)], url=POS, payment_method='Line Pay',
                   payment_info={'one_time_key': OTK}).get_json()['order_id']
    r = admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 120, 'reason': '餐點有誤'})
    assert r.status_code == 200 and r.get_json()['status'] == 'pending'
    # 提交後執行 (測試設定 DEFERRED_INLINE) → 已完成
    assert _refunds(app, oid) == [('partial', 120, 'Line Pay', 'done')]
    assert calls[-1][2] == {'refundAmount': 120} and f'/orders/{_get(app, Order, oid)["uuid"]}/refund' in calls[-1][1]
    # 第二次退款失敗 → failed，後台重試成功
    responses['/refund'] = {'returnCode': '1165', 'returnMessage': 'A transaction has already been refunded.'}
    assert admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 30, 'reason': '再退'}).status_code == 200
    assert _refunds(app, oid)[-1] == ('partial', 30, 'Line Pay', 'failed')
    with app.app_context():
        rid = Refund.query.filter_by(order_id=oid, amount=30).first().id
    del responses['/refund']
    r = admin_client.post(f'/admin/refunds/{rid}/action', data={'action': 'retry'})
    assert r.status_code == 302
    assert _refunds(app, oid)[-1] == ('partial', 30, 'Line Pay', 'done')
    page = admin_client.get('/admin/refunds').get_data(as_text=True)
    assert f'#{oid}' in page and '餐點有誤' in page


def test_void_card_order_creates_manual_refund(app, staff_client, admin_client):
    iid = make_item(app, '作廢刷卡餐', price=250, stock=10)
    oid = checkout(staff_client, [line(iid)], url=POS, payment_method='Credit Card',
                   payment_info={'auth_code': 'A1B2C3'}).get_json()['order_id']
    r = staff_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled', 'reason': '客人不要了'})
    assert r.status_code == 403 and r.get_json()['need_approval'] is True
    assert admin_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled', 'reason': '客人不要了'}).status_code == 200
    assert _refunds(app, oid) == [('cancel', 250, 'Credit Card', 'manual')]
    with app.app_context():
        rid = Refund.query.filter_by(order_id=oid).first().id
        assert db.session.get(MenuItem, iid).stock == 10
    assert admin_client.post(f'/admin/refunds/{rid}/action', data={'action': 'done', 'note': 'EDC 已取消'}).status_code == 302
    assert _refunds(app, oid) == [('cancel', 250, 'Credit Card', 'done')]
    admin_client.post(f'/admin/refunds/{rid}/action', data={'action': 'done'})   # 重複標記：提示錯誤
    assert_flash(admin_client, '已完成退款')


def test_partial_refund_can_be_disabled(app, admin_client, store_settings):
    iid = make_item(app, '禁止部分退款餐', price=80, stock=10)
    oid = checkout(admin_client, [line(iid)], url=POS, cash_received=100).get_json()['order_id']
    store_settings('refund', allow_partial=False)
    r = admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 10, 'reason': 'x'})
    assert r.status_code == 400 and '不允許部分退款' in r.get_json()['message']


# ---------------------------------------------------------------------------
# 儲值金
# ---------------------------------------------------------------------------
def _wallet(app, uid):
    with app.app_context():
        user = db.session.get(User, uid)
        ledger = sum(x.delta for x in WalletLedger.query.filter_by(user_id=uid).all())
        return user.wallet_balance, ledger


def test_wallet_topup_pay_and_refund(app, staff_client, admin_client, store_settings):
    uid = make_member(app, '儲值會員', '0955000111')
    iid = make_item(app, '儲值金付款餐', price=180, stock=20)
    r = staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': 1000})
    assert r.status_code == 400 and '尚未啟用儲值金' in r.get_json()['message']
    store_settings('member', wallet_enabled=True, topup_bonus='1000:100, 3000:400', wallet_max_balance=5000)
    r = staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': 1000, 'payment_method': 'Cash'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['bonus'] == 100 and r.get_json()['member']['wallet_balance'] == 1100
    assert staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': 4000}).status_code == 400  # 超過上限
    assert staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': 500, 'payment_method': '儲值金'}).status_code == 400
    with app.app_context():
        assert PaymentRecord.query.filter_by(kind='topup', amount=1000).count() >= 1

    # 儲值金付款 (POS 一律可選)；需綁定會員
    assert checkout(staff_client, [line(iid)], url=POS, payment_method='儲值金').status_code == 400
    r = checkout(staff_client, [line(iid)], url=POS, payment_method='儲值金', user_id=uid)
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    assert _wallet(app, uid) == (920, 920)
    assert _get(app, Order, oid)['wallet_used'] == 180
    # 餘額不足：不建單、不扣款
    r = checkout(staff_client, [line(iid, 6)], url=POS, payment_method='儲值金', user_id=uid)
    assert r.status_code == 400 and r.get_json()['insufficient_wallet'] is True
    assert _wallet(app, uid) == (920, 920)

    # 部分退款與作廢退回儲值金
    assert admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 30, 'reason': '少料'}).status_code == 200
    assert _wallet(app, uid) == (950, 950)
    assert admin_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled'}).status_code == 200
    assert _wallet(app, uid) == (1100, 1100)
    assert _refunds(app, oid) == [('partial', 30, '儲值金', 'done'), ('cancel', 150, '儲值金', 'done')]


def test_wallet_topup_by_card_requires_auth(app, staff_client, store_settings):
    uid = make_member(app, '刷卡儲值會員', '0955000222')
    store_settings('member', wallet_enabled=True)
    r = staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': 500, 'payment_method': 'Credit Card'})
    assert r.status_code == 400 and r.get_json()['need_card_auth'] is True
    r = staff_client.post('/api/pos/wallet/topup', json={'user_id': uid, 'amount': 500, 'payment_method': 'Credit Card',
                                                         'payment_info': {'auth_code': 'ZX9876'}})
    assert r.status_code == 200 and r.get_json()['member']['wallet_balance'] == 500
