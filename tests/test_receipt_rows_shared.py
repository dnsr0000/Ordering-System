"""
收據內容：熱感機收據 (print_service.build_receipt) 與純文字電子收據 (receipt_service.text_receipt) 由同一份
receipt_service.receipt_rows 產生；這裡以同一張訂單 (折扣、服務費、外送費、現金找零、禮物卡遮罩、發票、贈點、追蹤連結)
固定兩種格式的輸出。
"""
import os

from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus
from app.models.printing import Printer
from tests.support import checkout, default_tenant_id, guest_client, line, make_item, make_member, member_client


def _dump(name, thermal, text):
    """重構前後比對用：RECEIPT_DUMP=資料夾 時把兩種格式寫到檔案"""
    folder = os.environ.get('RECEIPT_DUMP')
    if folder:
        with open(os.path.join(folder, name), 'w', encoding='utf-8') as fh:
            fh.write(thermal + '\n=====\n' + text)


def _rich_order(app, store_settings):
    """各種欄位都有值的訂單 (會員、折扣、服務費、外送、發票)"""
    store_settings('pos', receipt_footer='POS 頁尾 謝謝')
    store_settings('receipt', footer='電子收據頁尾')
    item = make_item(app, '收據列測試餐', price=100)
    make_member(app, '收據列會員', '0977800170', pin='2468')
    oid = checkout(member_client(app, '0977800170', '2468'), [line(item, 2, customization='少冰')]).get_json()['order_id']
    with app.app_context():
        from app.models.tenant import Tenant
        o = db.session.get(Order, oid)
        o.promotion_discount, o.member_discount, o.manual_discount, o.discount_amount = 10, 5, 3, 25
        o.service_charge, o.delivery_fee = 20, 30
        o.delivery_address, o.delivery_phone, o.delivery_zone = '台南市中西區 1 號', '0911000111', '3 公里內'
        o.total_price = 200 - 25 + 20 + 30
        o.cash_received, o.cash_change, o.cashier_username, o.points_earned = 300, 75, 'cashier_a', 7
        o.table_number = 'A5 (收據列會員)'
        db.session.add(Invoice(tenant_id=o.tenant_id, order_id=o.id, relate_number=f'K-rows-{oid}', status=InvoiceStatus.ISSUED,
                               amount=o.total_price, carrier_type='mobile', carrier_num='/ABC+123', invoice_no='AB12345678',
                               random_number='4321'))
        tenant = db.session.get(Tenant, default_tenant_id(app))
        tenant.table_qr_base_url = 'http://192.168.9.9:5000/'
        db.session.commit()
    return oid


def _render(app, oid, title='交易明細'):
    from app.services import print_service, receipt_service
    with app.app_context():
        o = db.session.get(Order, oid)
        printer = Printer(tenant_id=o.tenant_id, name='p', host='127.0.0.1', paper_width=80, encoding='utf8')
        return print_service.build_receipt(o, printer, title).decode('utf-8', errors='replace'), receipt_service.text_receipt(o)


def test_receipt_rows_feed_both_formats(app, store_settings):
    oid = _rich_order(app, store_settings)
    thermal, text = _render(app, oid)
    _dump('rich.txt', thermal, text)
    for s in ('收據列測試餐 x2', '$200', '少冰', '促銷', '-$10', '會員折扣', '-$5', '整單折扣', '-$3', '-$7', '服務費', '+$20',
              '外送費', '+$30', '應付總額', '$225', '電子發票', 'AB12345678', '4321', '本次贈點', '+7'):
        assert s in thermal and s in text, s
    # 熱感機收據才有的明細 (收銀、顧客/桌號、實收找零、外送地址、載具)；頁尾各自讀自己的設定
    assert 'cashier_a' in thermal and 'cashier_a' not in text
    assert '找零' in thermal and '$75' in thermal and '找零' not in text
    assert '台南市中西區 1 號' in thermal and '0911000111' in thermal and '台南市' not in text
    assert '載具' in thermal and '手機條碼' in thermal and '載具' not in text
    assert 'POS 頁尾 謝謝' in thermal and '電子收據頁尾' in text
    assert 'http://192.168.9.9:5000/track/' in thermal and '出餐進度 http://192.168.9.9:5000/track/' in text
    assert '補印收據' in _render(app, oid, '補印收據')[0]


def test_receipt_rows_gift_card_masked(app):
    from app.services import giftcard_service
    item = make_item(app, '收據禮物卡餐', price=80)
    oid = checkout(guest_client(app), [line(item)]).get_json()['order_id']
    code = '1234567890123456'
    with app.app_context():
        o = db.session.get(Order, oid)
        o.payment_method, o.payment_reference = '禮物卡', code
        db.session.commit()
    thermal, text = _render(app, oid)
    _dump('gift.txt', thermal, text)
    assert '1234****3456' in thermal and code not in thermal and code not in text
    assert giftcard_service.mask_code(code) == '1234****3456'
    assert giftcard_service.mask_code(code, dashed=True) == '1234-****-****-3456'
    assert giftcard_service.mask_code('123') == '123', '太短的不遮罩'
