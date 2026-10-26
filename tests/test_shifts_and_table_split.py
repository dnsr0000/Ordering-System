"""交班 / 日結 (零用金、應有現金、面額盤點、帳差) 與桌位進階結帳 (換桌、分開結帳、多種付款、LINE Pay 失敗回沖、整桌發票)"""
import pytest

from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice, PaymentRecord, Settlement
from app.models.shift import CashShift
from app.models.table import DiningTable
from tests.support import checkout, line, login_staff, make_item
from tests.test_einvoice_ecpay import ecpay_api  # noqa: F401  (共用假綠界 API fixture)
from tests.test_payments_refunds_wallet import OTK, linepay_api  # noqa: F401

POS = '/api/pos/checkout'


@pytest.fixture
def no_open_shift(app):
    """確保測試開始與結束時沒有開著的班 (其他測試不受影響)"""
    def close_all():
        with app.app_context():
            for s in CashShift.query.filter_by(status='open').all():
                s.status = 'closed'
            db.session.commit()
    close_all()
    yield
    close_all()


def test_shift_open_movements_and_close(app, staff_client, admin_client, kitchen_client, no_open_shift):
    assert staff_client.get('/api/pos/shift').get_json()['shift'] is None
    assert kitchen_client.post('/api/pos/shift/open', json={'opening_cash': 1000}).status_code == 403
    assert staff_client.post('/api/pos/shift/movement', json={'amount': 100, 'kind': 'pay_out', 'note': 'x'}).status_code == 409
    r = staff_client.post('/api/pos/shift/open', json={'opening_cash': 2000})
    assert r.status_code == 200 and r.get_json()['shift']['opening_cash'] == 2000
    assert staff_client.post('/api/pos/shift/open', json={'opening_cash': 1}).status_code == 409

    iid = make_item(app, '交班測試餐', price=150, stock=50)
    cash_order = checkout(staff_client, [line(iid, 2)], url=POS, cash_received=500).get_json()
    assert cash_order['cash_change'] == 200
    checkout(staff_client, [line(iid)], url=POS, payment_method='Credit Card', payment_info={'auth_code': 'QWER12'})
    assert admin_client.post(f"/api/pos/orders/{cash_order['order_id']}/refund", json={'amount': 50, 'reason': '退湯'}).status_code == 200
    assert staff_client.post('/api/pos/shift/movement', json={'amount': 120, 'kind': 'pay_out', 'note': ''}).status_code == 400
    assert staff_client.post('/api/pos/shift/movement', json={'amount': 120, 'kind': 'pay_out', 'note': '買冰塊'}).status_code == 200
    r = staff_client.post('/api/pos/shift/movement', json={'amount': 500, 'kind': 'pay_in', 'note': '補零錢'})
    shift = r.get_json()['shift']
    # 應有現金 = 2000 + 300 (現金收款) - 50 (退款) - 120 + 500
    assert shift['expected_cash'] == 2630 and shift['cash_sales'] == 300 and shift['cash_refunds'] == 50
    assert shift['by_method']['信用卡']['sales'] == 150 and shift['order_count'] == 2
    assert [m['note'] for m in shift['movements']] == ['買冰塊', '補零錢']

    r = staff_client.post('/api/pos/shift/close', json={'denominations': {'1000': 2, '500': 1, '100': 1, '10': 2}})
    assert r.status_code == 200, r.get_json()
    report = r.get_json()['report']
    assert report['counted_cash'] == 2620 and report['difference'] == -10
    sid = r.get_json()['shift_id']
    with app.app_context():
        s = db.session.get(CashShift, sid)
        assert (s.status, s.expected_cash, s.counted_cash, s.difference) == ('closed', 2630, 2620, -10)
        assert PaymentRecord.query.filter_by(shift_id=sid).count() == 5
        assert Order.query.filter_by(shift_id=sid).count() == 2
    assert staff_client.post('/api/pos/shift/close', json={'counted_cash': 1}).status_code == 409
    # 後台：班別列表、明細、日結
    assert f'#{sid}' in admin_client.get('/admin/shifts').get_data(as_text=True)
    detail = admin_client.get(f'/admin/shifts/{sid}').get_data(as_text=True)
    assert '2,630' in detail or '2630' in detail
    assert kitchen_client.get('/admin/shifts').status_code == 403


def test_require_shift_blocks_checkout(app, staff_client, store_settings, no_open_shift):
    store_settings('pos', require_shift=True)
    iid = make_item(app, '需開班餐', price=40, stock=10)
    r = checkout(staff_client, [line(iid)], url=POS, cash_received=40)
    assert r.status_code == 409 and r.get_json()['need_shift'] is True
    staff_client.post('/api/pos/shift/open', json={'opening_cash': 0})
    assert checkout(staff_client, [line(iid)], url=POS, cash_received=40).status_code == 200


# ---------------------------------------------------------------------------
# 桌位：換桌 / 分開結帳
# ---------------------------------------------------------------------------
def _setup_tables(app, admin_client, names):
    admin_client.post('/admin/tables', data={'action': 'create', 'names': names})
    with app.app_context():
        return {t.name: (t.id, t.token) for t in DiningTable.query.all()}


def _table_order(app, token, item_id, qty=1):
    c = app.test_client()
    c.get(f'/t/{token}')
    c.post('/guest_login')
    r = checkout(c, [line(item_id, qty)])
    assert r.status_code == 200, r.get_json()
    return r.get_json()['order_id']


def _orders(app, ids):
    with app.app_context():
        return {i: (o.dining_table_id, o.table_number, o.payment_status, o.payment_method, o.settlement_id)
                for i in ids for o in [db.session.get(Order, i)]}


def test_move_and_split_settle(app, admin_client, staff_client):
    tables = _setup_tables(app, admin_client, 'S1-S2')
    s1, s2 = tables['S1'], tables['S2']
    iid = make_item(app, '分帳火鍋', price=300, stock=50)
    a, b, c = (_table_order(app, s1[1], iid) for _ in range(3))
    assert {t['id'] for t in staff_client.get('/api/pos/tables').get_json()['tables']} >= {s1[0], s2[0]}
    # 把 c 移到 S2
    r = staff_client.post(f'/api/pos/tables/{s1[0]}/move', json={'to_table_id': s2[0], 'order_ids': [c]})
    assert r.status_code == 200 and r.get_json()['moved'] == [c]
    assert _orders(app, [c])[c][:2] == (s2[0], 'S2 桌')
    assert staff_client.post(f'/api/pos/tables/{s1[0]}/move', json={'to_table_id': s1[0]}).status_code == 400

    # S1 只結 a (分開結帳)，付款分成現金 + 刷卡
    url = f'/api/pos/tables/{s1[0]}/settle'
    bad = staff_client.post(url, json={'order_ids': [a], 'payments': [{'method': 'Cash', 'amount': 100}]})
    assert bad.status_code == 400 and '不符' in bad.get_json()['message']
    nocard = staff_client.post(url, json={'order_ids': [a], 'payments': [{'method': 'Cash', 'amount': 100},
                                                                          {'method': 'Credit Card', 'amount': 200}]})
    assert nocard.status_code == 400 and nocard.get_json()['need_card_auth'] is True
    r = staff_client.post(url, json={'order_ids': [a], 'payments': [
        {'method': 'Cash', 'amount': 100, 'received': 500},
        {'method': 'Credit Card', 'amount': 200, 'auth_code': 'CARD01', 'card_last4': '9876'}]})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['orders'] == [a] and d['total'] == 300 and d['change'] == 400 and d['payment_method'] == 'Credit Card'
    rows = _orders(app, [a, b])
    assert rows[a][2] == 'settled' and rows[b][2] == 'unpaid'
    with app.app_context():
        st = db.session.get(Settlement, d['settlement_id'])
        assert st.total == 300 and st.order_ids == str(a)
        recs = [(p.method, p.amount, p.reference) for p in PaymentRecord.query.filter_by(settlement_id=st.id).order_by(PaymentRecord.id)]
        assert recs == [('Cash', 100, ''), ('Credit Card', 200, 'CARD01')]
    # 儲值金 / 平台代收不可用於桌位結帳
    r = staff_client.post(url, json={'payments': [{'method': '儲值金', 'amount': 300}]})
    assert r.status_code == 400
    # 剩下的 b 整筆結
    assert staff_client.post(url, json={'payment_method': 'Cash', 'cash_received': 300}).status_code == 200
    assert staff_client.post(f'/api/pos/tables/{s2[0]}/settle', json={'payment_method': 'Cash'}).status_code == 200


def test_table_settle_linepay_split_failure_refunds_first_charge(app, admin_client, staff_client, linepay_api):
    calls, responses = linepay_api
    tables = _setup_tables(app, admin_client, 'L1')
    iid = make_item(app, 'LINE Pay 分帳餐', price=200, stock=20)
    _table_order(app, tables['L1'][1], iid, 2)
    seen = {'n': 0}

    def second_fails(body):
        seen['n'] += 1
        if seen['n'] == 2:
            return {'returnCode': '1155', 'returnMessage': 'The transaction can not be used.'}
        return {'returnCode': '0000', 'info': {'transactionId': f'TX{seen["n"]}'}}
    responses['/oneTimeKeys/pay'] = second_fails
    url = f"/api/pos/tables/{tables['L1'][0]}/settle"
    r = staff_client.post(url, json={'payments': [{'method': 'Line Pay', 'amount': 200, 'one_time_key': OTK},
                                                  {'method': 'Line Pay', 'amount': 200, 'one_time_key': OTK}]})
    assert r.status_code == 402 and r.get_json()['payment_failed'] is True
    refunds = [c for c in calls if c[1].endswith('/refund')]
    assert len(refunds) == 1 and refunds[0][2] == {'refundAmount': 200}
    with app.app_context():
        assert Order.query.filter_by(dining_table_id=tables['L1'][0], payment_status='unpaid').count() == 1
    del responses['/oneTimeKeys/pay']
    r = staff_client.post(url, json={'payments': [{'method': 'Line Pay', 'amount': 400, 'one_time_key': OTK}]})
    assert r.status_code == 200, r.get_json()


def test_table_settle_issues_one_invoice(app, admin_client, staff_client, ecpay_api):
    calls, _ = ecpay_api
    tables = _setup_tables(app, admin_client, 'V1')
    iid = make_item(app, '整桌發票餐', price=120, stock=20)
    o1 = _table_order(app, tables['V1'][1], iid)
    o2 = _table_order(app, tables['V1'][1], iid, 2)
    with app.app_context():
        assert Invoice.query.filter(Invoice.order_id.in_([o1, o2])).count() == 0, '桌邊點餐下單時不開發票'
    r = staff_client.post(f"/api/pos/tables/{tables['V1'][0]}/settle",
                          json={'payment_method': 'Cash', 'cash_received': 360, 'invoice': {'type': 'company', 'buyer_id': '04595257'}})
    assert r.status_code == 200
    with app.app_context():
        inv = Invoice.query.filter_by(settlement_id=r.get_json()['settlement_id']).one()
        assert inv.status == 'issued' and inv.amount == 360 and inv.buyer_identifier == '04595257'
    data = calls[-1][1]
    assert data['SalesAmount'] == 360 and sum(i['ItemAmount'] for i in data['Items']) == 360


def test_staff_extra_permissions_via_admin(app, admin_client, staff_client):
    """員工額外權限：負責人在員工帳號頁勾選後，該員工可直接作廢 / 退款 (不需主管核准)"""
    login_staff(app, 'perm_staff', 'staff')
    from app.models.tenant import StaffAccount
    with app.app_context():
        acc_id = StaffAccount.query.filter_by(username='perm_staff').first().id
    admin_client.post('/admin/staff', data={'action': 'permissions', 'account_id': acc_id, 'perm': ['refund', 'bogus']})
    with app.app_context():
        staff = db.session.get(StaffAccount, acc_id)
        assert staff.permissions == 'refund' and staff.has_permission('refund') and staff.has_permission('cash_drawer')
    client = login_staff(app, 'perm_staff')
    iid = make_item(app, '權限退款餐', price=90, stock=10)
    oid = checkout(client, [line(iid)], url=POS, cash_received=90).get_json()['order_id']
    r = client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 10, 'reason': '權限測試'})
    assert r.status_code == 200, r.get_json()
