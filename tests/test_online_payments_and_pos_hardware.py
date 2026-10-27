"""
第二批專業功能：線上付款方式 (ATM / 超商代碼 / Apple Pay)、綠界自動退刷、錢櫃、客顯螢幕、會員動態條碼、餐點規格 (大小杯)。
"""
import re
from datetime import datetime, timedelta

import pytest

from tests.support import assert_flash, default_tenant_id, line, login_staff, make_item, make_member, member_client
from tests.test_payment_ecpay import _notify_params, _signed
from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, _member, _register_member, tenant_ctx


# ===========================================================================
# 線上端：付款方式、ATM / 超商代碼、綠界退刷
# ===========================================================================
@pytest.fixture(scope='module')
def env():
    e = Env()
    e.burger_uuid = _add_item(e.b, '線上付款漢堡', price=180, stock=50)
    _register_member(e.b, '付款方式會員', '0966300001', points=0)
    e.sync_all()
    yield e
    e.close()


def _hub_settings(env, key, **values):
    from app.extensions import db
    from app.services import settings_service
    with tenant_ctx(env.hub) as tenant:
        current = settings_service.get(key, tenant.id)
        settings_service.put(key, dict(current, **values), tenant_id=tenant.id)
        db.session.commit()


def _online_order(env):
    return _checkout(env.hub, [line(_item(env.hub, env.burger_uuid)['id'], 1)], node_uuid=env.node_b_uuid,
                     user_id=_member(env.hub, '0966300001')['id'], pay=False)


def _db_order(env, uuid):
    from app.models.order import Order
    return Order.query.filter_by(uuid=uuid).first()


def _form(env, order):
    from app.extensions import db
    from app.services import payment_ecpay
    with tenant_ctx(env.hub):
        o = _db_order(env, order['uuid'])
        fields = payment_ecpay.build_payment_form(o, 'https://shop.example.com')
        assert payment_ecpay.verify(fields)
        db.session.commit()
        return fields


def test_payment_form_offers_methods_chosen_in_settings(env):
    from app.services import payment_ecpay
    order = _online_order(env)
    fields = _form(env, order)
    assert fields['ChoosePayment'] == 'Credit' and 'IgnorePayment' not in fields, '預設只有信用卡'
    assert 'PaymentInfoURL' not in fields

    _hub_settings(env, 'payments', online_methods=['Credit', 'ApplePay', 'ATM', 'CVS'], atm_expire_days=2,
                  cvs_expire_minutes=180)
    fields = _form(env, order)
    assert fields['ChoosePayment'] == 'ALL'
    ignored = set(fields['IgnorePayment'].split('#'))
    assert ignored == set(payment_ecpay.ECPAY_METHODS) - {'Credit', 'ApplePay', 'ATM', 'CVS'}
    assert fields['PaymentInfoURL'] == 'https://shop.example.com/payment/ecpay/info'
    assert (fields['ExpireDate'], fields['StoreExpireDate']) == ('2', '180')

    _hub_settings(env, 'payments', online_methods=['ATM'])
    fields = _form(env, order)
    assert fields['ChoosePayment'] == 'ATM' and 'IgnorePayment' not in fields
    _hub_settings(env, 'payments', online_methods=['Credit', 'ATM', 'CVS'])


def test_payment_type_maps_to_payment_method():
    from app.services.payment_ecpay import method_for
    assert method_for('Credit_CreditCard') == 'Credit Card'
    assert method_for('ATM_LAND') == 'ATM 轉帳' and method_for('WebATM_TAISHIN') == 'ATM 轉帳'
    assert method_for('CVS_CVS') == '超商代碼'
    assert method_for('BARCODE_BARCODE') == '超商代碼'
    assert method_for('ApplePay') == 'Apple Pay'
    assert method_for(None) == 'Credit Card'


def test_atm_number_keeps_order_open_until_due_then_payment_completes(env):
    from app.models.payment import PaymentRecord
    from app.services.payment_ecpay import DUE_GRACE_MINUTES, expire_unpaid_orders
    order = _online_order(env)
    fields = _form(env, order)
    hub = env.hub.test_client()
    due = (datetime.now() + timedelta(days=1)).strftime('%Y/%m/%d')
    info = {'MerchantID': '3002607', 'MerchantTradeNo': fields['MerchantTradeNo'], 'RtnCode': '2', 'RtnMsg': 'Get VirtualAccount Succeeded',
            'TradeNo': '2410229999', 'TradeAmt': str(order['total']), 'PaymentType': 'ATM_TAISHIN',
            'TradeDate': datetime.now().strftime('%Y/%m/%d %H:%M:%S'), 'BankCode': '812', 'vAccount': '9103522175887271',
            'ExpireDate': due, 'CustomField1': order['uuid'], 'CustomField2': '', 'CustomField3': '', 'CustomField4': '',
            'StoreID': ''}
    bad = dict(info, CheckMacValue='0' * 64)
    assert hub.post('/payment/ecpay/info', data=bad).get_data(as_text=True) == '0|CheckMacValue Error'
    r = hub.post('/payment/ecpay/info', data=_signed(env.hub, info))
    assert r.get_data(as_text=True) == '1|OK' and r.mimetype == 'text/plain'
    with env.hub.app_context():
        o = _db_order(env, order['uuid'])
        assert o.payment_note == 'ATM 轉帳：銀行代碼 812 帳號 9103522175887271'
        assert o.payment_due_at.strftime('%Y/%m/%d %H:%M') == f'{due} 23:59'
        from app.extensions import db
        o.created_at = datetime.now() - timedelta(hours=3)   # 已超過一般付款逾時
        db.session.commit()
        assert expire_unpaid_orders(force=True, query=lambda ref: {'TradeStatus': '0'}) == 0, '繳費期限內不取消'
        assert _db_order(env, order['uuid']).payment_status == 'pending'

    # 會員的訂單頁顯示繳費資訊
    member = member_client(env.hub, '0966300001', '2468')
    page = member.get(f"/payment/return/{order['uuid']}").get_data(as_text=True)
    assert '等待繳費' in page and '9103522175887271' in page
    mine = next(o for o in member.get('/my_orders').get_json() if o['order_id'] == order['id'])
    assert mine['payment_note'].startswith('ATM') and mine['payment_url'].endswith(f"/payment/return/{order['uuid']}")

    # 顧客轉帳後綠界通知付款成功：付款方式記為 ATM
    paid = _signed(env.hub, _notify_params(order, PaymentType='ATM_TAISHIN', TradeNo='2410229999'))
    assert hub.post('/payment/ecpay/notify', data=paid).get_data(as_text=True) == '1|OK'
    with env.hub.app_context():
        o = _db_order(env, order['uuid'])
        assert (o.payment_status, o.payment_method) == ('paid', 'ATM 轉帳')
        rec = PaymentRecord.query.filter_by(order_id=o.id, kind='sale').execution_options(skip_tenant_filter=True).one()
        assert rec.method == 'ATM 轉帳'
    assert DUE_GRACE_MINUTES >= 10


def test_cvs_code_expires_after_due_plus_grace(env):
    from app.extensions import db
    from app.services.payment_ecpay import DUE_GRACE_MINUTES, expire_unpaid_orders
    order = _online_order(env)
    fields = _form(env, order)
    due = datetime.now() + timedelta(minutes=5)
    info = {'MerchantID': '3002607', 'MerchantTradeNo': fields['MerchantTradeNo'], 'RtnCode': '10100073',
            'RtnMsg': 'Get CVS Code Succeeded', 'TradeNo': '2410228888', 'TradeAmt': str(order['total']),
            'PaymentType': 'CVS_CVS', 'TradeDate': datetime.now().strftime('%Y/%m/%d %H:%M:%S'),
            'PaymentNo': 'LLL22251222', 'ExpireDate': due.strftime('%Y/%m/%d %H:%M:%S'), 'CustomField1': order['uuid'],
            'CustomField2': '', 'CustomField3': '', 'CustomField4': '', 'StoreID': ''}
    assert env.hub.test_client().post('/payment/ecpay/info', data=_signed(env.hub, info)).get_data(as_text=True) == '1|OK'
    with env.hub.app_context():
        o = _db_order(env, order['uuid'])
        assert o.payment_note == '超商代碼：LLL22251222'
        o.created_at = datetime.now() - timedelta(hours=1)
        db.session.commit()
        assert expire_unpaid_orders(force=True, query=lambda ref: {'TradeStatus': '0'}) == 0
        o = _db_order(env, order['uuid'])
        o.payment_due_at = datetime.now() - timedelta(minutes=DUE_GRACE_MINUTES + 1)
        db.session.commit()
        assert expire_unpaid_orders(force=True, query=lambda ref: {'TradeStatus': '0'}) == 1, '期限 (含緩衝) 過後取消'
        o = _db_order(env, order['uuid'])
        assert (o.status, o.payment_status) == ('Cancelled', 'expired')


def test_info_for_stale_trade_number_is_ignored(env):
    order = _online_order(env)
    _form(env, order)
    info = {'MerchantID': '3002607', 'MerchantTradeNo': 'KOLDTRADENUMBER00001', 'RtnCode': '2', 'TradeNo': '1',
            'TradeAmt': str(order['total']), 'PaymentType': 'ATM_TAISHIN', 'BankCode': '812', 'vAccount': '1',
            'ExpireDate': '2099/01/01', 'CustomField1': order['uuid']}
    assert env.hub.test_client().post('/payment/ecpay/info', data=_signed(env.hub, info)).get_data(as_text=True) == '1|OK'
    with env.hub.app_context():
        assert _db_order(env, order['uuid']).payment_note == '', '舊的付款嘗試不覆蓋目前的繳費資訊'


class FakeEcpay:
    """取代綠界 CreditDetail/DoAction：記錄送出的參數並回傳設定的結果"""

    def __init__(self, *codes):
        self.codes = list(codes) or ['1']
        self.calls = []

    def __call__(self, url, data):
        self.calls.append((url, dict(data)))
        code = self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]
        return {'MerchantID': data['MerchantID'], 'MerchantTradeNo': data['MerchantTradeNo'], 'TradeNo': data['TradeNo'],
                'RtnCode': code, 'RtnMsg': '成功' if code == '1' else '交易狀態不符'}


def _paid_then_cancelled(env):
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    order = _online_order(env)
    _form(env, order)
    paid = _signed(env.hub, _notify_params(order, TradeNo='2410227777' + order['uuid'][:4]))
    assert env.hub.test_client().post('/payment/ecpay/notify', data=paid).get_data(as_text=True) == '1|OK'
    with tenant_ctx(env.hub) as tenant:
        owner = StaffAccount(tenant_id=tenant.id, username='ecpay_refund_owner', role='owner', password_hash='x')
        change_order_status(order['id'], 'Cancelled', owner, tenant.id)
    return order


def test_cancelled_credit_order_is_refunded_automatically_when_enabled(env):
    from app.models.payment import Refund
    from app.services import payment_ecpay, payment_service
    order = _paid_then_cancelled(env)
    fake = FakeEcpay('1')
    with env.hub.app_context():
        assert _db_order(env, order['uuid']).payment_status == 'refund_required'
        assert payment_service.run_auto_refunds(ecpay_post=fake) == 0, '未開啟自動退刷'
        assert not fake.calls
    _hub_settings(env, 'payments', ecpay_auto_refund=True)
    with env.hub.app_context():
        assert payment_service.run_auto_refunds(ecpay_post=fake) == 1
        url, sent = fake.calls[0]
        assert url.endswith('/CreditDetail/DoAction')
        assert (sent['Action'], sent['TotalAmount']) == ('R', str(order['total']))
        o = _db_order(env, order['uuid'])
        assert sent['MerchantTradeNo'] == o.payment_ref and sent['TradeNo'] == o.payment_trade_no
        assert payment_ecpay.verify(sent), '請求帶正確的檢查碼'
        assert o.payment_status == 'refunded' and '退刷成功' in o.payment_note
        refunds = Refund.query.filter_by(order_id=o.id).execution_options(skip_tenant_filter=True).all()
        assert refunds and all(r.status == 'done' for r in refunds)
        assert payment_service.run_auto_refunds(ecpay_post=fake) == 0 and len(fake.calls) == 1, '不重複退刷'


def test_failed_refund_falls_back_to_void_then_waits_for_manual_retry(env):
    from app.services import payment_service
    _hub_settings(env, 'payments', ecpay_auto_refund=True)
    order = _paid_then_cancelled(env)
    fake = FakeEcpay('10200047')   # R 與 N 都失敗
    with env.hub.app_context():
        assert payment_service.run_auto_refunds(ecpay_post=fake) == 0
        assert [c[1]['Action'] for c in fake.calls] == ['R', 'N'], '全額退款失敗時改試放棄授權 (當日未關帳)'
        o = _db_order(env, order['uuid'])
        assert o.payment_status == 'refund_required' and o.payment_note.startswith('退刷失敗：')
        assert payment_service.run_auto_refunds(ecpay_post=fake) == 0 and len(fake.calls) == 2, '失敗後不自動重試'
        ok, msg = payment_service.auto_refund_online_order(o.id, ecpay_post=FakeEcpay('1'))
        assert ok and '綠界' in msg
        assert _db_order(env, order['uuid']).payment_status == 'refunded'


def test_admin_retry_button_and_atm_orders_need_manual_refund(env, monkeypatch):
    from app.extensions import db
    from app.services import payment_ecpay
    _hub_settings(env, 'payments', ecpay_auto_refund=False)
    order = _paid_then_cancelled(env)
    admin = login_staff(env.hub, 'hub_refund_admin', 'owner')
    page = admin.get('/admin/refunds').get_data(as_text=True)
    assert f"/admin/refunds/order/{order['id']}/ecpay" in page, '線上信用卡訂單顯示「綠界退刷」'
    calls = []
    monkeypatch.setattr(payment_ecpay, '_post', lambda url, data: calls.append(data) or {'RtnCode': '1'})
    r = admin.post(f"/admin/refunds/order/{order['id']}/ecpay")
    assert r.status_code == 302 and calls
    assert_flash(admin, '退刷成功')
    with env.hub.app_context():
        assert _db_order(env, order['uuid']).payment_status == 'refunded'

    # ATM 付款：無法系統退款
    atm = _paid_then_cancelled(env)
    with env.hub.app_context():
        o = _db_order(env, atm['uuid'])
        o.payment_method = 'ATM 轉帳'
        db.session.commit()
        with pytest.raises(payment_ecpay.EcpayRefundError):
            payment_ecpay.refund(o, o.total_price, post=FakeEcpay('1'))
    admin.post(f"/admin/refunds/order/{atm['id']}/ecpay")
    assert_flash(admin, '不是綠界信用卡')
    with env.hub.app_context():
        assert _db_order(env, atm['uuid']).payment_status == 'refund_required'


def test_partial_refund_of_online_credit_order_goes_through_ecpay(env):
    from app.models.payment import Refund
    from app.services import payment_service
    _hub_settings(env, 'payments', ecpay_auto_refund=True)
    order = _online_order(env)
    _form(env, order)
    paid = _signed(env.hub, _notify_params(order, TradeNo='2410226666' + order['uuid'][:4]))
    assert env.hub.test_client().post('/payment/ecpay/notify', data=paid).get_data(as_text=True) == '1|OK'
    with tenant_ctx(env.hub) as tenant:
        refund = payment_service.partial_refund(order['id'], 50, '少一份醬料', tenant_id=tenant.id)
        from app.extensions import db
        db.session.commit()
        assert refund.status == 'pending'
        rid = refund.id
    fake = FakeEcpay('1')
    with env.hub.app_context():
        assert payment_service.process_refund(rid, ecpay_post=fake) == 'done'
        assert fake.calls[0][1]['TotalAmount'] == '50' and fake.calls[0][1]['Action'] == 'R'
        from app.extensions import db
        assert db.session.get(Refund, rid).provider_result.startswith('綠界退刷成功')
    _hub_settings(env, 'payments', ecpay_auto_refund=False)


# ===========================================================================
# 門市：錢櫃、客顯、會員動態條碼、餐點規格
# ===========================================================================
@pytest.fixture
def sent(app, monkeypatch):
    from app.services import escpos
    out = []
    monkeypatch.setattr(escpos, 'send_raw', lambda host, port, data, timeout=5: out.append((host, data)))
    return out


DRAWER_KICK = b'\x1bp\x00\x19\xfa'
CARD = {'auth_code': 'A12345', 'card_last4': '4242'}


@pytest.fixture
def drawer_printer(app, admin_client):
    from app.extensions import db
    from app.models.printing import Printer, PrintJob
    r = admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '錢櫃收據機', 'host': '10.0.0.77',
                                                    'port': '9100', 'role': 'receipt', 'paper_width': '80',
                                                    'encoding': 'big5', 'copies': '1', 'is_active': '1', 'drawer': '1'})
    assert r.status_code == 302
    with app.app_context():
        p = Printer.query.filter_by(host='10.0.0.77').execution_options(skip_tenant_filter=True).one()
        assert p.drawer
        pid = p.id
    yield pid
    with app.app_context():
        PrintJob.query.filter_by(printer_id=pid).execution_options(skip_tenant_filter=True).delete()
        db.session.delete(db.session.get(Printer, pid))
        db.session.commit()


def test_escpos_drawer_command():
    from app.services.escpos import Ticket
    assert Ticket().drawer().bytes().endswith(DRAWER_KICK)
    assert Ticket().drawer(pin=1).bytes().endswith(b'\x1bp\x01\x19\xfa')


def test_cash_checkout_kicks_drawer_but_card_does_not(app, staff_client, drawer_printer, sent, store_settings):
    item = make_item(app, '錢櫃測試餐', price=90)
    r = staff_client.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash',
                                                       'order_type': '內用', 'cash_received': 100})
    assert r.status_code == 200, r.get_json()
    kicks = [d for h, d in sent if h == '10.0.0.77' and d.endswith(DRAWER_KICK)]
    assert len(kicks) == 1, '現金結帳開錢櫃一次'
    sent.clear()
    r = staff_client.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Credit Card', 'payment_info': CARD,
                                                       'order_type': '內用'})
    assert r.status_code == 200, r.get_json()
    assert not [d for h, d in sent if d.endswith(DRAWER_KICK)], '刷卡不開錢櫃'
    store_settings('pos', open_drawer_on_cash=False)
    staff_client.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': '內用',
                                                 'cash_received': 100})
    assert not [d for h, d in sent if d.endswith(DRAWER_KICK)], '關閉自動開錢櫃'


def test_kiosk_cash_order_does_not_kick_drawer(app, drawer_printer, sent):
    from tests.support import checkout, guest_client
    item = make_item(app, '自助機錢櫃測試', price=60)
    r = checkout(guest_client(app), [line(item)])
    assert r.status_code == 200, r.get_json()
    assert not [d for h, d in sent if d.endswith(DRAWER_KICK)]


def test_manual_drawer_open_is_audited(app, staff_client, drawer_printer, sent):
    from app.models.user import StaffAuditLog
    r = staff_client.post('/api/pos/drawer', json={'reason': '換零錢'})
    assert r.status_code == 200 and r.get_json()['count'] == 1
    assert [d for h, d in sent if d.endswith(DRAWER_KICK)]
    with app.app_context():
        log = StaffAuditLog.query.filter_by(action_type='CASH_DRAWER_OPEN').order_by(StaffAuditLog.id.desc()).first()
        assert log is not None and '換零錢' in log.details
    kitchen = login_staff(app, 'drawer_kitchen', 'kitchen')
    assert kitchen.post('/api/pos/drawer', json={}).status_code in (302, 403)


def test_manual_drawer_without_printer_explains_setup(app, staff_client):
    r = staff_client.post('/api/pos/drawer', json={})
    assert r.status_code == 400 and '接錢櫃' in r.get_json()['message']


def test_pos_shows_drawer_and_display_buttons(app, staff_client, drawer_printer):
    page = staff_client.get('/pos').get_data(as_text=True)
    assert 'id="posDrawerBtn"' in page and 'id="posDisplayBtn"' in page
    r = staff_client.get('/pos/display')
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and 'js/pos-display.js' in body and 'id="cdTotal"' in body
    assert app.test_client().get('/pos/display').status_code == 302, '需登入'


def test_member_qr_code_lookup_at_pos(app, staff_client):
    from app.services import member_qr_service
    make_member(app, 'QR 會員', '0966300009', points=12, pin='2468')
    member = member_client(app, '0966300009', '2468')
    data = member.get('/api/member_qr').get_json()
    token = data['token']
    assert token.startswith('KM1.') and data['qr'].startswith('data:image/svg+xml') and 0 < data['expires_in'] <= 60
    r = staff_client.post('/api/pos/member_lookup', json={'query': token})
    assert r.status_code == 200 and r.get_json()['member']['phone'] == '0966300009' and r.get_json()['via_qr']

    tampered = token[:-1] + ('0' if token[-1] != '0' else '1')
    assert staff_client.post('/api/pos/member_lookup', json={'query': tampered}).status_code == 404
    with app.test_request_context('/'):
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            with pytest.raises(Exception) as exc:
                member_qr_service.resolve(token, at=__import__('time').time() + 600)
            assert getattr(exc.value, 'status_code', None) == 410 or '過期' in str(exc.value)
    # 重新產生：舊條碼立即失效
    rotated = member.post('/api/member_qr', json={'action': 'rotate'}).get_json()['token']
    assert rotated != token
    assert staff_client.post('/api/pos/member_lookup', json={'query': token}).status_code == 404
    assert staff_client.post('/api/pos/member_lookup', json={'query': rotated}).status_code == 200
    assert app.test_client().get('/api/member_qr').status_code == 401
    assert 'id="memberQrBtn"' in member.get('/rewards').get_data(as_text=True)


def test_member_qr_from_other_shop_is_rejected(app):
    from tests.support import other_shop_owner_client
    make_member(app, 'QR 他店', '0966300010', pin='2468')
    token = member_client(app, '0966300010', '2468').get('/api/member_qr').get_json()['token']
    other = other_shop_owner_client(app)
    assert other.post('/api/pos/member_lookup', json={'query': token}).status_code == 404


def test_size_options_change_price_and_are_validated(app, admin_client, staff_client):
    from app.extensions import db
    from app.models.menu import MenuItem, parse_size_options
    assert parse_size_options('中杯:0, 大杯:+15，特大杯:25') == [('中杯', 0), ('大杯', 15), ('特大杯', 25)]
    assert parse_size_options('小:-10\n大:abc') == [('小', -10)]
    item = make_item(app, '規格紅茶', price=40, category='飲料')
    form = {'action': 'item', 'item_id': str(item), 'avail_days': list('1234567'), 'size_options': '中杯:0, 大杯:15'}
    assert admin_client.post('/admin/menu_pro', data=form).status_code == 302
    bad = dict(form, size_options='大杯:abc')
    flashes_before = admin_client.post('/admin/menu_pro', data=bad)
    assert flashes_before.status_code in (302, 400)
    assert_flash(admin_client, '規格格式')
    with app.app_context():
        m = db.session.get(MenuItem, item)
        assert m.size_options == '中杯:0, 大杯:+15'
        assert m.to_dict()['sizes'] == [{'name': '中杯', 'delta': 0}, {'name': '大杯', 'delta': 15}]

    r = staff_client.post('/api/pos/checkout', json={'items': [line(item, 2, size='大杯')], 'payment_method': 'Credit Card', 'payment_info': CARD,
                                                       'order_type': '內用'})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['total_price'] == 110 and '大杯' in body['items'][0]['customization']
    r = staff_client.post('/api/pos/checkout', json={'items': [line(item, 1)], 'payment_method': 'Credit Card', 'payment_info': CARD,
                                                       'order_type': '內用'})
    assert r.get_json()['total_price'] == 40 and '中杯' in r.get_json()['items'][0]['customization'], '未指定時用第一個規格'
    r = staff_client.post('/api/pos/checkout', json={'items': [line(item, 1, size='超大杯')],
                                                       'payment_method': 'Credit Card', 'payment_info': CARD, 'order_type': '內用'})
    assert r.status_code == 400 and '規格' in r.get_json()['message']
