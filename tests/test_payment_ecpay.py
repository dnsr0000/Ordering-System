"""綠界線上付款：檢查碼、付款表單、付款通知 (只標記一次並在付款後才送分店)、瀏覽器回傳、逾時取消與查詢"""
from datetime import datetime, timedelta

import pytest

from tests.support import line
from tests.test_sync_hub_node import (
    Env,
    _add_item,
    _checkout,
    _item,
    _member,
    _order,
    _register_member,
)


@pytest.fixture(scope='module')
def env():
    e = Env()
    burger = _add_item(e.b, '付款測試漢堡', price=150, stock=20)
    _register_member(e.b, '付款會員', '0966000001', points=100)
    e.sync_all()
    e.burger_uuid = burger
    yield e
    e.close()


def _hub_burger(env):
    return _item(env.hub, env.burger_uuid)['id']


def _signed(app, params):
    from app.services import payment_ecpay
    with app.test_request_context('/'):
        _mid, key, iv, _base = payment_ecpay._cfg()
        return dict(params, CheckMacValue=payment_ecpay.check_mac_value(params, key, iv))


def _notify_params(order, **over):
    params = {'MerchantID': '3002607', 'MerchantTradeNo': 'KTEST000000000000001', 'RtnCode': '1', 'RtnMsg': '交易成功',
              'TradeNo': '2410221234567890', 'TradeAmt': str(order['total']), 'PaymentDate': '2026/10/22 12:00:00',
              'PaymentType': 'Credit_CreditCard', 'PaymentTypeChargeFee': '3', 'TradeDate': '2026/10/22 11:59:00',
              'SimulatePaid': '0', 'CustomField1': order['uuid'], 'CustomField2': '', 'CustomField3': '',
              'CustomField4': '', 'StoreID': ''}
    params.update(over)
    return params


def _online_order(env):
    return _checkout(env.hub, [line(_hub_burger(env), 1)], node_uuid=env.node_b_uuid,
                     user_id=_member(env.hub, '0966000001')['id'], pay=False)


# ---------------------------------------------------------------------------
# 檢查碼與付款通知
# ---------------------------------------------------------------------------
def test_check_mac_value_matches_ecpay_document_sample():
    """綠界文件 (檢查碼機制說明) 的範例"""
    from app.services.payment_ecpay import check_mac_value
    params = {'TradeDesc': '促銷方案', 'PaymentType': 'aio', 'MerchantTradeDate': '2023/03/12 15:30:23',
              'MerchantTradeNo': 'ecpay20230312153023', 'MerchantID': '3002607',
              'ReturnURL': 'https://www.ecpay.com.tw/receive.php', 'ItemName': 'Apple iphone 15',
              'TotalAmount': '30000', 'ChoosePayment': 'ALL', 'EncryptType': '1'}
    assert check_mac_value(params, 'pwFHCqoQZGmho4w6', 'EkRm7iFT261dpevs') == \
        '6C51C9E6888DE861FD62FB1DD17029FC742634498FD813DC43D4243B5685B840'


def test_pay_page_posts_signed_form_to_ecpay_stage(env):
    from app.services import payment_ecpay
    order = _online_order(env)
    client = env.hub.test_client()
    client.get('/?shop=default')
    assert client.post('/phone_login', json={'phone': '0966000001', 'pin': '2468'}).status_code == 200
    r = client.get(f"/pay/{order['uuid']}")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert 'action="https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5"' in page
    assert 'https://payment-stage.ecpay.com.tw' in r.headers['Content-Security-Policy'], '本頁允許表單送到綠界'
    assert '4311-9522-2222-2222' in page, '測試環境顯示測試卡號'
    import re
    fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
    with env.hub.test_request_context('/'):
        assert payment_ecpay.verify({k: v.replace('&amp;', '&') for k, v in fields.items()})
    assert fields['CustomField1'] == order['uuid'] and fields['TotalAmount'] == str(order['total'])
    assert fields['ReturnURL'].endswith('/payment/ecpay/notify') and len(fields['MerchantTradeNo']) == 20
    # 別人的訂單看不到
    other = env.hub.test_client()
    other.get('/?shop=default')
    assert other.get(f"/pay/{order['uuid']}").status_code == 404


def test_notify_marks_paid_once_and_releases_order_to_branch(env):
    order = _online_order(env)
    hub = env.hub.test_client()
    env.sync(env.b)
    assert _order(env.b, order['uuid']) is None, '待付款不送分店'

    bad = _notify_params(order)
    bad['CheckMacValue'] = 'X' * 64
    assert hub.post('/payment/ecpay/notify', data=bad).get_data(as_text=True) == '0|CheckMacValue Error'
    wrong_amount = _signed(env.hub, _notify_params(order, TradeAmt='1'))
    assert hub.post('/payment/ecpay/notify', data=wrong_amount).get_data(as_text=True) == '0|AmountMismatch'
    simulated = _signed(env.hub, _notify_params(order, SimulatePaid='1'))
    assert hub.post('/payment/ecpay/notify', data=simulated).get_data(as_text=True) == '1|OK'
    assert _order(env.hub, order['uuid'])['status'] == 'Pending'

    from app.models.order import Order
    with env.hub.app_context():
        assert Order.query.filter_by(uuid=order['uuid']).first().payment_status == 'pending', '模擬付款不算付款'
    good = _signed(env.hub, _notify_params(order))
    r = hub.post('/payment/ecpay/notify', data=good)
    assert r.get_data(as_text=True) == '1|OK' and r.mimetype == 'text/plain'
    assert hub.post('/payment/ecpay/notify', data=good).get_data(as_text=True) == '1|OK', '重送通知不影響'
    with env.hub.app_context():
        paid = Order.query.filter_by(uuid=order['uuid']).first()
        assert paid.payment_status == 'paid' and paid.payment_trade_no == '2410221234567890'
        assert paid.payment_method == 'Credit Card'

    env.sync(env.b)
    at_branch = _order(env.b, order['uuid'])
    assert at_branch is not None and at_branch['items'] and at_branch['pickup'] > 0, '付款後送到分店 (含明細)'


def test_browser_return_does_not_touch_session(env):
    order = _online_order(env)
    browser = env.hub.test_client()
    r = browser.post('/payment/ecpay/result', data=_signed(env.hub, _notify_params(order)))
    assert r.status_code == 303 and r.headers['Location'].endswith(f"/payment/return/{order['uuid']}")
    assert 'Set-Cookie' not in r.headers, '跨站 POST 回來時不可覆蓋顧客的登入 Cookie'
    assert browser.post('/payment/ecpay/result', data={'CustomField1': order['uuid']}).status_code == 400


# ---------------------------------------------------------------------------
# 逾時未付款
# ---------------------------------------------------------------------------
def _age(env, order, minutes=30):
    from app.extensions import db
    from app.models.order import Order
    with env.hub.app_context():
        o = Order.query.filter_by(uuid=order['uuid']).first()
        o.created_at = datetime.now() - timedelta(minutes=minutes)
        o.payment_ref = 'KREF' + order['uuid'][:16].replace('-', '')
        db.session.commit()


def _expire(env, query):
    from app.services.payment_ecpay import expire_unpaid_orders
    with env.hub.app_context():
        return expire_unpaid_orders(force=True, query=query)


def test_unpaid_orders_expire_and_restore_stock_and_points(env):
    from app.extensions import db
    from app.models.order import Order
    from app.models.sync import NodeStock, SyncNode
    user_before = _member(env.hub, '0966000001')['points']
    order = _online_order(env)
    with env.hub.app_context():
        node_id = SyncNode.query.filter_by(uuid=env.node_b_uuid).first().id
        stock_after_order = db.session.get(NodeStock, (node_id, _hub_burger(env))).stock
    assert _member(env.hub, '0966000001')['points'] > user_before, '下單時已贈點'

    assert _expire(env, query=lambda ref: {'TradeStatus': '0'}) == 0, '未逾時不取消'
    _age(env, order)
    assert _expire(env, query=lambda ref: None) == 0, '查不到綠界結果時不冒險取消'
    assert _expire(env, query=lambda ref: {'TradeStatus': '0'}) == 1
    with env.hub.app_context():
        o = Order.query.filter_by(uuid=order['uuid']).first()
        assert o.status == 'Cancelled' and o.payment_status == 'expired'
        stock = db.session.get(NodeStock, (node_id, _hub_burger(env))).stock
        assert stock == stock_after_order + 1, '回補分店庫存快照'
    assert _member(env.hub, '0966000001')['points'] == user_before, '追回贈點'

    # 取消後才收到付款：標記需退款
    r = env.hub.test_client().post('/payment/ecpay/notify', data=_signed(env.hub, _notify_params(order)))
    assert r.get_data(as_text=True) == '1|OK'
    with env.hub.app_context():
        assert Order.query.filter_by(uuid=order['uuid']).first().payment_status == 'refund_required'


def test_expiry_checks_ecpay_before_cancelling(env):
    from app.models.order import Order
    order = _online_order(env)
    _age(env, order)
    assert _expire(env, query=lambda ref: {'TradeStatus': '1', 'TradeNo': 'Q123', 'TradeAmt': str(order['total'])}) == 0
    with env.hub.app_context():
        o = Order.query.filter_by(uuid=order['uuid']).first()
        assert o.payment_status == 'paid' and o.status == 'Pending', '綠界已付款 (通知遺失)：補記為已付款'


def test_payment_for_order_cancelled_by_staff_is_flagged_for_refund(env):
    """店員先取消待付款的訂單、之後才收到付款：不可變回已付款，要標記需退款"""
    from app.models.order import Order
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    from tests.test_sync_hub_node import tenant_ctx
    order = _online_order(env)
    with tenant_ctx(env.hub) as tenant:
        owner = StaffAccount(tenant_id=tenant.id, username='refund_owner', role='owner', password_hash='x')
        change_order_status(order['id'], 'Cancelled', owner, tenant.id)
    r = env.hub.test_client().post('/payment/ecpay/notify', data=_signed(env.hub, _notify_params(order)))
    assert r.get_data(as_text=True) == '1|OK'
    with env.hub.app_context():
        o = Order.query.filter_by(uuid=order['uuid']).first()
        assert (o.status, o.payment_status) == ('Cancelled', 'refund_required')


def test_paid_order_cancelled_later_is_flagged_for_refund(env):
    from app.models.order import Order
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    from tests.test_sync_hub_node import tenant_ctx
    order = _online_order(env)
    r = env.hub.test_client().post('/payment/ecpay/notify', data=_signed(env.hub, _notify_params(order)))
    assert r.get_data(as_text=True) == '1|OK'
    with tenant_ctx(env.hub) as tenant:
        owner = StaffAccount(tenant_id=tenant.id, username='refund_owner2', role='owner', password_hash='x')
        change_order_status(order['id'], 'Cancelled', owner, tenant.id)
    with env.hub.app_context():
        assert Order.query.filter_by(uuid=order['uuid']).first().payment_status == 'refund_required'
