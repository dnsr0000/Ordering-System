"""修正小組 finance-pnl 的回歸測試：發票 / 退款後台、會計傳票、損益表"""
from datetime import date, datetime, timedelta

import pytest

from app.extensions import db
from app.models.enums import OrderSource, OrderStatus, PaymentMethod
from app.models.expense import CostHistory, Expense
from app.models.inventory import Ingredient
from app.models.member import WalletLedger
from app.models.order import Order
from app.models.payment import Invoice, PaymentRecord, Refund
from app.services import accounting_service, einvoice_ecpay as ecpay, pnl_service
from app.sync import runtime as sync_runtime
from app.models.user import User
from tests.support import assert_flash, default_tenant_id, get_or_create_tenant, tenant_request

KEY, IV = ecpay.TEST_MERCHANT[1], ecpay.TEST_MERCHANT[2]


@pytest.fixture
def fake_ecpay(monkeypatch, store_settings):
    store_settings('einvoice', enabled=True, stage=True, fallback_email='shop@example.com')
    calls = []

    def fake_post(url, payload, timeout):
        path = url.replace(ecpay.STAGE_URL, '')
        calls.append(path)
        result = {'RtnCode': 1, 'RtnMsg': 'OK'}
        if path == '/B2CInvoice/Issue':
            result.update(InvoiceNo=f'FP{10000000 + len(calls)}', RandomNumber='1234',
                          InvoiceDate=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        return {'TransCode': 1, 'Data': ecpay.encrypt_data(result, KEY, IV)}

    monkeypatch.setattr(ecpay, '_post', fake_post)
    return calls


def _order(app, **kw):
    with app.app_context():
        values = dict(tenant_id=default_tenant_id(app), total_price=100, payment_method='Cash', payment_status='paid',
                      status=OrderStatus.PICKED_UP, order_source=OrderSource.POS)
        values.update(kw)
        o = Order(**values)
        db.session.add(o)
        db.session.commit()
        return o.id


def _invoice(app, order_id, **kw):
    with app.app_context():
        values = dict(tenant_id=default_tenant_id(app), order_id=order_id, relate_number=ecpay.new_relate_number(),
                      amount=100)
        values.update(kw)
        inv = Invoice(**values)
        db.session.add(inv)
        db.session.commit()
        return inv.id


# ---------------------------------------------------------------------------
# B-390 補開發票的檢查
# ---------------------------------------------------------------------------
def test_issue_for_order_validates_order_and_amount(app, admin_client, fake_ecpay):
    cancelled = _order(app, status=OrderStatus.CANCELLED, payment_status='refunded')
    r = admin_client.post(f'/admin/orders/{cancelled}/invoice', json={})
    assert r.status_code == 409 and '已取消' in r.get_json()['message']
    unpaid = _order(app, payment_status='unpaid')
    assert admin_client.post(f'/admin/orders/{unpaid}/invoice', json={}).status_code == 409
    skipped = _order(app)
    _invoice(app, skipped, status='skipped')
    r = admin_client.post(f'/admin/orders/{skipped}/invoice', json={})
    assert r.status_code == 409 and '已開立' not in r.get_json()['message']
    # 已部分退款：以扣掉退款後的金額開立
    partial = _order(app, total_price=100, refunded_amount=30)
    r = admin_client.post(f'/admin/orders/{partial}/invoice', json={})
    assert r.get_json()['success'] is True and r.get_json()['message'] == '已開立'
    with app.app_context():
        assert Invoice.query.filter_by(order_id=partial).one().amount == 70


# ---------------------------------------------------------------------------
# B-388 作廢後重開、修改載具表單
# ---------------------------------------------------------------------------
def test_reissue_void_invoice_and_carrier_form(app, admin_client, fake_ecpay):
    oid = _order(app)
    void_id = _invoice(app, oid, status='void', invoice_no='FP00000001', invoice_date=datetime.now())
    failed_order = _order(app)
    failed_id = _invoice(app, failed_order, status='failed', last_error='載具號碼不存在')
    page = admin_client.get('/admin/invoices').get_data(as_text=True)
    assert 'value="reissue"' in page and 'value="update_carrier"' in page
    admin_client.post(f'/admin/invoices/{void_id}/action', data={'action': 'reissue', 'type': 'company',
                                                                 'buyer_id': '04595257', 'buyer_name': '測試公司'})
    with app.app_context():
        rows = Invoice.query.filter_by(order_id=oid).order_by(Invoice.id).all()
        assert [i.status for i in rows] == ['void', 'issued'] and rows[1].buyer_identifier == '04595257'
    # 已有有效發票時不可再重開
    admin_client.post(f'/admin/invoices/{void_id}/action', data={'action': 'reissue'})
    assert_flash(admin_client, '此訂單已開立發票')
    assert failed_id


# ---------------------------------------------------------------------------
# R2-147 折讓合計與淨額
# ---------------------------------------------------------------------------
def test_invoice_page_shows_allowance_total(app, admin_client, store_settings):
    oid = _order(app, total_price=1000)
    _invoice(app, oid, status='allowance', amount=1000, allowance_amount=1000, invoice_no='FP00000099',
             invoice_date=datetime.now())
    page = admin_client.get('/admin/invoices').get_data(as_text=True)
    assert '折讓合計' in page and '扣除折讓後淨額' in page


# ---------------------------------------------------------------------------
# B-391 已完成退款：標記全部未完成的退款
# ---------------------------------------------------------------------------
def test_online_refund_done_marks_all_open_refunds(app, admin_client):
    oid = _order(app, payment_status='refund_required', payment_method='Credit Card', order_source=OrderSource.ONLINE)
    with app.app_context():
        tid = default_tenant_id(app)
        db.session.add_all([Refund(tenant_id=tid, order_id=oid, kind='partial', amount=30, method='Credit Card',
                                   status='manual'),
                            Refund(tenant_id=tid, order_id=oid, kind='cancel', amount=70, method='Credit Card',
                                   status='manual')])
        db.session.commit()
    admin_client.post(f'/admin/refunds/order/{oid}/done')
    with app.app_context():
        assert {r.status for r in Refund.query.filter_by(order_id=oid)} == {'done'}
        assert db.session.get(Order, oid).payment_status == 'refunded'


# ---------------------------------------------------------------------------
# B-384 線上端：分店的發票 / 退款唯讀
# ---------------------------------------------------------------------------
def test_hub_cannot_touch_branch_invoices_and_refunds(app, admin_client, monkeypatch, fake_ecpay):
    oid = _order(app, node_uuid='node-fp-1')
    inv_id = _invoice(app, oid, status='failed', last_error='x')
    with app.app_context():
        refund = Refund(tenant_id=default_tenant_id(app), order_id=oid, kind='partial', amount=10, method='Credit Card',
                        status='manual')
        db.session.add(refund)
        db.session.commit()
        refund_id = refund.id
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    page = admin_client.get('/admin/invoices').get_data(as_text=True)
    assert 'invoice-remote' in page
    admin_client.post(f'/admin/invoices/{inv_id}/action', data={'action': 'retry'})
    assert_flash(admin_client, '請在分店的後台處理')
    admin_client.post(f'/admin/refunds/{refund_id}/action', data={'action': 'done'})
    assert_flash(admin_client, '請在分店的後台處理')
    assert 'refund-remote' in admin_client.get('/admin/refunds').get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Invoice, inv_id).status == 'failed'
        assert db.session.get(Refund, refund_id).status == 'manual'
    assert '/B2CInvoice/Issue' not in fake_ecpay


# ---------------------------------------------------------------------------
# B-385 分店：線上信用卡訂單由總部退刷
# ---------------------------------------------------------------------------
def test_node_cannot_mark_online_card_order_refunded(app, admin_client, monkeypatch):
    oid = _order(app, payment_status='refund_required', payment_method='Credit Card', order_source=OrderSource.ONLINE,
                 payment_trade_no='2611090001')
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
    page = admin_client.get('/admin/refunds').get_data(as_text=True)
    assert '由總部向綠界退刷' in page
    admin_client.post(f'/admin/refunds/order/{oid}/done')
    assert_flash(admin_client, '由總部 (線上端) 向綠界退刷')
    with app.app_context():
        assert db.session.get(Order, oid).payment_status == 'refund_required'


# ---------------------------------------------------------------------------
# B-394 綠界退刷失敗可重試、處理中標籤
# ---------------------------------------------------------------------------
def test_failed_ecpay_refund_shows_retry(app, admin_client):
    oid = _order(app, payment_method='Credit Card', order_source=OrderSource.ONLINE)
    with app.app_context():
        db.session.add(Refund(tenant_id=default_tenant_id(app), order_id=oid, kind='partial', amount=10,
                              method='Credit Card', status='failed', provider_result='逾時'))
        db.session.add(Refund(tenant_id=default_tenant_id(app), order_id=oid, kind='partial', amount=5,
                              method='Credit Card', status='processing'))
        db.session.commit()
    page = admin_client.get('/admin/refunds').get_data(as_text=True)
    assert '重試綠界退刷' in page and '退款中' in page


# ---------------------------------------------------------------------------
# B-387 儲值加值贈送入帳；B-393 發票明細依分店
# ---------------------------------------------------------------------------
def test_journal_records_topup_bonus_and_invoices_by_node(app):
    tid = get_or_create_tenant(app, 'fp-acc-shop')
    today = date.today()
    now = datetime.now()
    with tenant_request(app, tid):
        member = User(tenant_id=tid, name='贈送會員', phone='0977050111', photo_path='0977050111.jpg')
        db.session.add(member)
        db.session.flush()
        uid = member.id
        db.session.add(PaymentRecord(tenant_id=tid, kind='topup', method='Cash', amount=1000, staff_username='fp_s1',
                                     created_at=now))
        db.session.add(WalletLedger(tenant_id=tid, user_id=uid, delta=100, reason='bonus', staff_username='fp_s1',
                                    created_at=now))
        # 其他分店 (其他經手人) 的贈送不算進來
        db.session.add(WalletLedger(tenant_id=tid, user_id=uid, delta=50, reason='bonus', staff_username='other',
                                    created_at=now))
        db.session.commit()
        v = accounting_service.journal(tid, today, today)[0]
        assert v['debit'] == v['credit']
        lines = {ln['name']: (ln['debit'], ln['credit']) for ln in v['lines']}
        assert lines['儲值贈送 (行銷費用)'] == (100, 0)
        assert lines['預收款項-儲值金'] == (0, 1100)
        a = Order(tenant_id=tid, total_price=100, node_uuid='node-fp-a')
        b = Order(tenant_id=tid, total_price=200, node_uuid='node-fp-b')
        db.session.add_all([a, b])
        db.session.flush()
        for o, no in ((a, 'FA00000001'), (b, 'FB00000001')):
            db.session.add(Invoice(tenant_id=tid, order_id=o.id, relate_number=ecpay.new_relate_number(), status='issued',
                                   amount=o.total_price, invoice_no=no, invoice_date=now))
        db.session.commit()
        assert [r[0] for r in accounting_service.invoices(tid, today, today, 'node-fp-a')] == ['FA00000001']
        assert len(accounting_service.invoices(tid, today, today)) == 2


# ---------------------------------------------------------------------------
# R2-030 訂位訂金：收取 / 退回走預收款項-訂金，沒收轉收入；綠界收的訂金計入線上金流手續費
# ---------------------------------------------------------------------------
def _sides(voucher):
    """科目名稱 → (借方合計, 貸方合計)；同一科目借貸各一行"""
    out = {}
    for ln in voucher['lines']:
        d, c = out.get(ln['name'], (0, 0))
        out[ln['name']] = (d + ln['debit'], c + ln['credit'])
    return out


def test_journal_and_pnl_fees_handle_reservation_deposit(app):
    tid = get_or_create_tenant(app, 'fp-deposit-shop')
    today = date.today()
    now = datetime.now()
    with tenant_request(app, tid):
        db.session.add_all([
            PaymentRecord(tenant_id=tid, kind='deposit', method='Cash', amount=500, staff_username='fp_d1',
                          created_at=now),
            PaymentRecord(tenant_id=tid, kind='deposit', method='Cash', amount=-200, staff_username='fp_d1',
                          created_at=now),
            PaymentRecord(tenant_id=tid, kind='deposit_forfeit', method='Cash', amount=210, staff_username='fp_d1',
                          created_at=now)])
        db.session.commit()
        v = accounting_service.journal(tid, today, today)[0]
        assert v['debit'] == v['credit']
        lines = _sides(v)
        assert lines['現金'] == (500, 200)
        assert lines['預收款項-訂金'] == (200 + 210, 500)
        assert lines['銷貨收入'][1] + lines['銷項稅額'][1] == 210
        # 沒收撤銷：轉回預收、列為銷貨退回
        db.session.add(PaymentRecord(tenant_id=tid, kind='deposit_forfeit', method='Cash', amount=-210,
                                     staff_username='fp_d1', created_at=now))
        db.session.commit()
        v = accounting_service.journal(tid, today, today)[0]
        assert v['debit'] == v['credit']
        lines = _sides(v)
        assert lines['預收款項-訂金'] == (410, 710) and lines['現金'] == (500, 200)
    tid2 = get_or_create_tenant(app, 'fp-deposit-fee')
    with tenant_request(app, tid2):
        db.session.add_all([
            PaymentRecord(tenant_id=tid2, kind='deposit', method='Credit Card', amount=1000, staff_username='ecpay',
                          created_at=now),
            PaymentRecord(tenant_id=tid2, kind='deposit_forfeit', method='Credit Card', amount=1000,
                          staff_username='ecpay', created_at=now)])
        db.session.commit()
        assert pnl_service.statement(tid2, today, today)['fees']['online_fee_percent'] == 28


# ---------------------------------------------------------------------------
# B-392 綠界線上刷卡套用線上金流費率；R2-146 線上端銷貨成本提醒
# ---------------------------------------------------------------------------
def test_pnl_fees_ecpay_online_rate_and_cogs_flag(app, monkeypatch):
    tid = get_or_create_tenant(app, 'fp-pnl-shop')
    today = date.today()
    with tenant_request(app, tid):
        db.session.add_all([
            PaymentRecord(tenant_id=tid, kind='sale', method='Credit Card', amount=1000, staff_username='ecpay',
                          created_at=datetime.now()),
            PaymentRecord(tenant_id=tid, kind='sale', method='Credit Card', amount=1000, staff_username='pos1',
                          created_at=datetime.now())])
        db.session.commit()
        s = pnl_service.statement(tid, today, today)
        assert s['fees']['card_fee_percent'] == 20 and s['fees']['online_fee_percent'] == 28
        assert s['cogs_available'] is True
        monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
        hub = pnl_service.statement(tid, today, today)
        assert hub['cogs_available'] is False
        from openpyxl import load_workbook
        wb = load_workbook(pnl_service.to_excel(hub, hub, [], 'x'))
        assert any('銷貨成本只依線上端' in str(c) for row in wb['損益表'].iter_rows(values_only=True) for c in row)


# ---------------------------------------------------------------------------
# R2-148 分店首次連線前登錄的固定費用
# ---------------------------------------------------------------------------
def test_node_copies_fixed_expenses_registered_before_first_hello(app, monkeypatch):
    tid = get_or_create_tenant(app, 'fp-node-exp')
    month = '2026-03'
    with tenant_request(app, tid):
        db.session.add(Expense(tenant_id=tid, spent_on=date(2026, 2, 5), category='rent', name='單機時期房租',
                               amount=30000, is_fixed=True, node_uuid=None))
        db.session.commit()
        monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
        monkeypatch.setattr(sync_runtime, 'local_node_uuid', lambda: 'node-fp-new')
        assert pnl_service.copy_fixed(tid, month) == 1
        db.session.commit()
        assert pnl_service.copy_fixed(tid, month) == 0   # 已複製過不重複


# ---------------------------------------------------------------------------
# R2-149 SQLite 重用原料 id：不套用已刪除原料的成本歷史
# ---------------------------------------------------------------------------
def test_ingredient_cost_at_ignores_history_of_deleted_ingredient(app):
    tid = get_or_create_tenant(app, 'fp-cost-shop')
    now = datetime.now()
    with tenant_request(app, tid):
        ing = Ingredient(tenant_id=tid, name='重用 id 的新原料', unit_cost=100)
        db.session.add(ing)
        db.session.flush()
        # 已刪除原料 (同 id) 留下的紀錄：10 → 12，接不上新原料目前的成本 100
        db.session.add(CostHistory(tenant_id=tid, kind='ingredient', ref_id=ing.id, name='已刪除原料', field='unit_cost',
                                   old_value=10, new_value=12, changed_at=now - timedelta(days=5)))
        db.session.commit()
        assert pnl_service.ingredient_cost_at(tid, now - timedelta(days=30))[ing.id] == 100
        # 新原料自己的變動照常套用 (90 → 100)
        db.session.add(CostHistory(tenant_id=tid, kind='ingredient', ref_id=ing.id, name=ing.name, field='unit_cost',
                                   old_value=90, new_value=100, changed_at=now - timedelta(days=1)))
        db.session.commit()
        assert pnl_service.ingredient_cost_at(tid, now - timedelta(days=30))[ing.id] == 90


# ---------------------------------------------------------------------------
# B-036 已取消訂單的開立失敗發票不可重送 / 改載具後改回待開立
# ---------------------------------------------------------------------------
def test_failed_invoice_of_cancelled_order_cannot_be_retried(app, admin_client, fake_ecpay):
    oid = _order(app, status=OrderStatus.CANCELLED, payment_status='refunded')
    inv_id = _invoice(app, oid, status='failed', last_error='載具號碼不存在')
    admin_client.post(f'/admin/invoices/{inv_id}/action', data={'action': 'retry'})
    assert_flash(admin_client, '訂單已取消，不可再開立此發票')
    admin_client.post(f'/admin/invoices/{inv_id}/action', data={'action': 'update_carrier', 'type': 'print'})
    assert_flash(admin_client, '訂單已取消，不可再開立此發票')
    with app.app_context():
        assert db.session.get(Invoice, inv_id).status == 'failed'
    assert '/B2CInvoice/Issue' not in fake_ecpay


# ---------------------------------------------------------------------------
# B-311 退款列表可用逗號篩選多個狀態；B-497 禮物卡卡號遮罩
# ---------------------------------------------------------------------------
def test_refund_list_filters_multiple_statuses_and_masks_gift_card(app, admin_client):
    oid = _order(app, payment_method='Credit Card')
    with app.app_context():
        tid = default_tenant_id(app)
        for status, note in (('manual', 'RF-MANUAL'), ('failed', 'RF-FAILED'), ('done', 'RF-DONE')):
            db.session.add(Refund(tenant_id=tid, order_id=oid, kind='partial', amount=10, method='Credit Card',
                                  status=status, provider_result=note))
        db.session.commit()
    page = admin_client.get('/admin/refunds?status=manual,failed&start=2000-01-01').get_data(as_text=True)
    assert 'RF-MANUAL' in page and 'RF-FAILED' in page and 'RF-DONE' not in page
    assert 'value="manual,failed" selected' in page
    page = admin_client.get('/admin/refunds?status=done').get_data(as_text=True)
    assert 'RF-DONE' in page and 'RF-MANUAL' not in page
    code = '1234567890123456'
    _order(app, payment_method=PaymentMethod.GIFT_CARD, payment_status='refund_required', payment_reference=code,
           status=OrderStatus.CANCELLED)
    page = admin_client.get('/admin/refunds').get_data(as_text=True)
    assert code not in page
