"""綠界 B2C 電子發票 (模擬綠界 API)：加解密、載具 / 統編驗證、結帳開立、失敗重送、作廢 / 折讓、後台操作、線上付款後開立"""
from datetime import datetime, timedelta

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice
from app.services import einvoice_ecpay as ecpay
from tests.support import assert_flash, checkout, guest_client, line, make_item

POS = '/api/pos/checkout'
KEY, IV = ecpay.TEST_MERCHANT[1], ecpay.TEST_MERCHANT[2]


@pytest.fixture
def ecpay_api(monkeypatch, store_settings):
    """啟用電子發票 (測試環境) 並以假的綠界 API 取代 HTTP。calls 為 (路徑, 解密後的 Data)；fail 指定路徑回傳錯誤。"""
    store_settings('einvoice', enabled=True, stage=True, fallback_email='shop@example.com')
    calls, fail = [], {}
    counter = {'n': 0}

    def fake_post(url, payload, timeout):
        path = url.replace(ecpay.STAGE_URL, '')
        data = ecpay.decrypt_data(payload['Data'], KEY, IV)
        assert payload['MerchantID'] == data['MerchantID'] == '2000132'
        calls.append((path, data))
        if path in fail:
            result = fail[path]
            if isinstance(result, Exception):
                raise result
        elif path == '/B2CInvoice/Issue':
            counter['n'] += 1
            result = {'RtnCode': 1, 'RtnMsg': '開立發票成功', 'InvoiceNo': f'AB{10000000 + counter["n"]}',
                      'InvoiceDate': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'RandomNumber': '1234'}
        elif path == '/B2CInvoice/Allowance':
            result = {'RtnCode': 1, 'IA_Allow_No': f'2026{counter["n"]:012d}'}
        elif path == '/B2CInvoice/InvoicePrint':
            result = {'RtnCode': 1, 'InvoiceHtml': 'https://einvoice-stage.ecpay.com.tw/print/xyz'}
        else:
            result = {'RtnCode': 1, 'RtnMsg': 'OK'}
        return {'TransCode': 1, 'Data': ecpay.encrypt_data(result, KEY, IV)}

    monkeypatch.setattr(ecpay, '_post', fake_post)
    return calls, fail


def _invoices(app, order_id):
    with app.app_context():
        return [{c.name: getattr(i, c.name) for c in Invoice.__table__.columns}
                for i in Invoice.query.filter_by(order_id=order_id).order_by(Invoice.id).all()]


# ---------------------------------------------------------------------------
# 純函式
# ---------------------------------------------------------------------------
def test_encrypt_roundtrip_and_formats():
    data = {'RelateNumber': 'K123', 'Items': [{'ItemName': '牛肉麵 (大)', 'ItemPrice': 150}], 'Remark': 'a+b&c=d'}
    text = ecpay.encrypt_data(data, KEY, IV)
    assert ecpay.decrypt_data(text, KEY, IV) == data
    assert ecpay.is_valid_ban('04595257') and ecpay.is_valid_ban('12345675')
    assert not ecpay.is_valid_ban('12345678') and not ecpay.is_valid_ban('1234567')
    cfg = {'default_carrier': 'print'}
    assert ecpay.normalize_invoice_info({}, cfg)['carrier_type'] == 'print'
    assert ecpay.normalize_invoice_info({'type': 'mobile', 'carrier': '/abc+123'}, cfg)['carrier_num'] == '/ABC+123'
    assert ecpay.normalize_invoice_info({'type': 'citizen', 'carrier': 'ab12345678901234'}, cfg)['carrier_num'] == 'AB12345678901234'
    assert ecpay.normalize_invoice_info({'type': 'donate', 'love_code': '168001'}, cfg)['love_code'] == '168001'
    out = ecpay.normalize_invoice_info({'type': 'company', 'buyer_id': '04595257', 'buyer_name': '測試公司'}, cfg)
    assert out['buyer_identifier'] == '04595257' and out['buyer_name'] == '測試公司'
    for bad in ({'type': 'mobile', 'carrier': 'ABC1234'}, {'type': 'citizen', 'carrier': '12'},
                {'type': 'donate', 'love_code': '12'}, {'type': 'company', 'buyer_id': '12345678'}, {'type': 'xx'}):
        with pytest.raises(AppError):
            ecpay.normalize_invoice_info(bad, cfg)
    # 未選擇時捐贈預設愛心碼
    assert ecpay.normalize_invoice_info({}, {'default_carrier': 'donate', 'default_love_code': '919'})['love_code'] == '919'
    assert ecpay.same_period(datetime(2026, 9, 1), datetime(2026, 10, 31)) is True     # 9-10 月同一期
    assert ecpay.same_period(datetime(2026, 8, 31), datetime(2026, 9, 1)) is False
    assert ecpay.same_period(datetime(2026, 11, 1), datetime(2026, 12, 31)) is True


# ---------------------------------------------------------------------------
# 結帳開立
# ---------------------------------------------------------------------------
def test_checkout_issues_invoice_with_carrier(app, staff_client, ecpay_api, store_settings):
    calls, _ = ecpay_api
    store_settings('pos', service_charge_percent=10)
    iid = make_item(app, '發票牛肉麵', price=200, stock=20)
    r = checkout(staff_client, [line(iid, 2)], url=POS, cash_received=500,
                 order_discount={'type': 'amount', 'value': 0}, invoice={'type': 'mobile', 'carrier': '/ABC+123'})
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    inv = _invoices(app, oid)[0]
    assert inv['status'] == 'issued' and inv['invoice_no'].startswith('AB') and inv['amount'] == 440
    path, data = calls[-1]
    assert path == '/B2CInvoice/Issue'
    assert data['CarrierType'] == '3' and data['CarrierNum'] == '/ABC+123' and data['Print'] == '0'
    assert data['SalesAmount'] == 440 and sum(i['ItemAmount'] for i in data['Items']) == 440
    assert [i['ItemName'] for i in data['Items']][-1] == '服務費'
    assert data['CustomerEmail'] == 'shop@example.com'
    # 今日訂單 API 帶出發票摘要
    rows = staff_client.get('/api/pos/today_orders').get_json()['orders']
    row = next(o for o in rows if o['id'] == oid)
    assert row['invoice']['invoice_no'] == inv['invoice_no']


def test_company_invoice_prints_and_bad_carrier_rejected_before_order(app, staff_client, ecpay_api):
    calls, _ = ecpay_api
    iid = make_item(app, '統編便當', price=100, stock=20)
    r = checkout(staff_client, [line(iid)], url=POS, cash_received=100,
                 invoice={'type': 'company', 'buyer_id': '04595257', 'buyer_name': '好吃股份有限公司'})
    data = calls[-1][1]
    assert data['CustomerIdentifier'] == '04595257' and data['Print'] == '1' and data['CustomerAddr']
    n_orders = None
    with app.app_context():
        n_orders = Order.query.count()
    r = checkout(staff_client, [line(iid)], url=POS, invoice={'type': 'company', 'buyer_id': '11111111'})
    assert r.status_code == 400 and '統一編號' in r.get_json()['message']
    with app.app_context():
        assert Order.query.count() == n_orders


def test_issue_failure_retry_and_business_error(app, staff_client, admin_client, ecpay_api):
    _, fail = ecpay_api
    iid = make_item(app, '發票重送餐', price=90, stock=20)
    fail['/B2CInvoice/Issue'] = ConnectionError('timeout')
    oid = checkout(staff_client, [line(iid)], url=POS, cash_received=100).get_json()['order_id']
    inv = _invoices(app, oid)[0]
    assert inv['status'] == 'pending' and inv['attempts'] == 1 and 'timeout' in inv['last_error']
    # 背景重送：建立未滿一分鐘不重送；之後成功
    del fail['/B2CInvoice/Issue']
    with app.app_context():
        assert ecpay.retry_pending() == 0
        assert ecpay.retry_pending(older_than_seconds=0) >= 1
    assert _invoices(app, oid)[0]['status'] == 'issued'

    # 資料錯誤 (例如載具不存在) → failed，後台修改載具後重送
    fail['/B2CInvoice/Issue'] = {'RtnCode': 5000006, 'RtnMsg': '載具號碼不存在'}
    oid2 = checkout(staff_client, [line(iid)], url=POS, cash_received=100,
                    invoice={'type': 'mobile', 'carrier': '/ZZZ+999'}).get_json()['order_id']
    inv2 = _invoices(app, oid2)[0]
    assert inv2['status'] == 'failed' and '載具號碼不存在' in inv2['last_error']
    del fail['/B2CInvoice/Issue']
    admin_client.post(f'/admin/invoices/{inv2["id"]}/action', data={'action': 'update_carrier', 'type': 'print'})
    assert _invoices(app, oid2)[0]['carrier_type'] == 'print' and _invoices(app, oid2)[0]['status'] == 'pending'
    admin_client.post(f'/admin/invoices/{inv2["id"]}/action', data={'action': 'retry'})
    assert _invoices(app, oid2)[0]['status'] == 'issued'
    page = admin_client.get('/admin/invoices').get_data(as_text=True)
    assert _invoices(app, oid2)[0]['invoice_no'] in page


def test_cancel_voids_and_partial_refund_allowance(app, admin_client, ecpay_api):
    calls, _ = ecpay_api
    iid = make_item(app, '作廢發票餐', price=300, stock=20)
    oid = checkout(admin_client, [line(iid)], url=POS, cash_received=300).get_json()['order_id']
    assert admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 100, 'reason': '餐點少一樣'}).status_code == 200
    inv = _invoices(app, oid)[0]
    assert inv['status'] == 'allowance' and inv['allowance_amount'] == 100
    assert calls[-1][0] == '/B2CInvoice/Allowance' and calls[-1][1]['AllowanceAmount'] == 100
    # 取消：已有折讓的發票再折讓剩餘金額 (同期)；或作廢
    oid2 = checkout(admin_client, [line(iid)], url=POS, cash_received=300).get_json()['order_id']
    assert admin_client.post(f'/api/orders/{oid2}/status', json={'status': 'Cancelled'}).status_code == 200
    assert _invoices(app, oid2)[0]['status'] == 'void' and calls[-1][0] == '/B2CInvoice/Invalid'
    # 跨期：改開全額折讓
    oid3 = checkout(admin_client, [line(iid)], url=POS, cash_received=300).get_json()['order_id']
    with app.app_context():
        inv3 = Invoice.query.filter_by(order_id=oid3).first()
        inv3.invoice_date = datetime.now() - timedelta(days=70)
        db.session.commit()
        assert ecpay.void_or_allowance(inv3.id) == 'allowance'
        assert db.session.get(Invoice, inv3.id).allowance_amount == 300


def test_cancel_before_issue_marks_void_without_api(app, admin_client, ecpay_api, store_settings):
    calls, _ = ecpay_api
    store_settings('einvoice', auto_issue=False)
    iid = make_item(app, '未開立就取消', price=60, stock=20)
    oid = checkout(admin_client, [line(iid)], url=POS, cash_received=60).get_json()['order_id']
    assert _invoices(app, oid)[0]['status'] == 'pending'
    n = len(calls)
    admin_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled'})
    assert _invoices(app, oid)[0]['status'] == 'void' and len(calls) == n


def test_admin_issue_for_order_and_print(app, admin_client, ecpay_api, store_settings):
    iid = make_item(app, '補開發票餐', price=75, stock=20)
    oid = checkout(admin_client, [line(iid)], url=POS, cash_received=75).get_json()['order_id']
    # 已開立 → 不可重複補開
    assert admin_client.post(f'/admin/orders/{oid}/invoice', json={}).status_code == 409
    store_settings('einvoice', enabled=False)
    oid2 = checkout(admin_client, [line(iid)], url=POS, cash_received=75).get_json()['order_id']
    assert _invoices(app, oid2) == []
    store_settings('einvoice', enabled=True)
    r = admin_client.post(f'/admin/orders/{oid2}/invoice', json={'type': 'donate', 'love_code': '8585'})
    assert r.get_json()['success'] is True and _invoices(app, oid2)[0]['love_code'] == '8585'
    inv_id = _invoices(app, oid2)[0]['id']
    r = admin_client.get(f'/admin/invoices/{inv_id}/print')
    assert r.status_code == 302 and r.headers['Location'].startswith('https://einvoice-stage.ecpay.com.tw/')
    # 後台作廢
    admin_client.post(f'/admin/invoices/{inv_id}/action', data={'action': 'void', 'reason': '重開'})
    assert _invoices(app, oid2)[0]['status'] == 'void'


def test_kiosk_invoice_and_online_waiting_flow(app, ecpay_api):
    calls, _ = ecpay_api
    iid = make_item(app, '點餐機發票餐', price=55, stock=20)
    r = checkout(guest_client(app), [line(iid)], invoice={'type': 'donate', 'love_code': '168001'})
    assert r.status_code == 200
    assert _invoices(app, r.get_json()['order_id'])[0]['status'] == 'issued'
    # 線上付款：付款前 waiting，付款完成 → 開立
    from app.models.payment import InvoiceStatus
    from app.services import payment_ecpay, payment_service
    with app.app_context():
        order = db.session.get(Order, r.get_json()['order_id'])
        Invoice.query.filter_by(order_id=order.id).delete()
        inv = payment_service.maybe_create_invoice(order, {'type': 'print'}, status=InvoiceStatus.WAITING)
        db.session.commit()
        assert inv.status == 'waiting'
        n = len(calls)
        payment_ecpay._after_online_paid(order, 'TRADE123')
        db.session.commit()
        db.session.refresh(inv)
        assert inv.status == 'pending' and len(calls) == n       # 非請求情境：交給背景重送
        assert ecpay.issue_invoice(inv.id) == 'issued' and len(calls) == n + 1


def test_settings_test_button_uses_ecpay(app, admin_client, ecpay_api):
    calls, fail = ecpay_api
    admin_client.post('/admin/settings/einvoice/test')
    assert_flash(admin_client, '2000132')
    assert calls[-1] == ('/B2CInvoice/CheckLoveCode', {'LoveCode': '168001', 'MerchantID': '2000132'})
    fail['/B2CInvoice/CheckLoveCode'] = {'RtnCode': 9000001, 'RtnMsg': '驗證失敗'}
    admin_client.post('/admin/settings/einvoice/test')
    assert_flash(admin_client, '驗證失敗')
