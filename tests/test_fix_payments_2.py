"""
修正小組 payments-2 的回歸測試：電子發票 (einvoice_ecpay) 與綠界線上付款 (payment_ecpay)。
以假的綠界 API 取代 HTTP；資料自行建立，不依賴其他測試檔。
"""
from datetime import datetime, timedelta

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus
from app.services import einvoice_ecpay as ecpay
from tests.support import checkout, default_tenant_id, line, make_item, make_member, tenant_request

POS = '/api/pos/checkout'
KEY, IV = ecpay.TEST_MERCHANT[1], ecpay.TEST_MERCHANT[2]


@pytest.fixture
def fake_ecpay(monkeypatch, store_settings):
    """啟用電子發票 (測試環境) 並以假的綠界 API 取代 HTTP。handlers[路徑] 可指定回應 (dict / Exception / 函式)。"""
    store_settings('einvoice', enabled=True, stage=True, fallback_email='shop@example.com')
    calls, handlers = [], {}
    counter = {'n': 0}

    def fake_post(url, payload, timeout):
        path = url.replace(ecpay.STAGE_URL, '')
        data = ecpay.decrypt_data(payload['Data'], KEY, IV)
        calls.append((path, data))
        handler = handlers.get(path)
        if callable(handler) and not isinstance(handler, Exception):
            handler = handler(data)
        if isinstance(handler, Exception):
            raise handler
        if handler is not None:
            result = handler
        elif path == '/B2CInvoice/Issue':
            counter['n'] += 1
            result = {'RtnCode': 1, 'InvoiceNo': f'ZX{20000000 + counter["n"]}',
                      'InvoiceDate': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'RandomNumber': '4321'}
        elif path == '/B2CInvoice/GetIssue':
            result = {'RtnCode': 2000060, 'RtnMsg': '查無發票資料'}
        elif path == '/B2CInvoice/Allowance':
            counter['n'] += 1
            result = {'RtnCode': 1, 'IA_Allow_No': f'A{counter["n"]:015d}'}
        else:
            result = {'RtnCode': 1, 'RtnMsg': 'OK'}
        return {'TransCode': 1, 'Data': ecpay.encrypt_data(result, KEY, IV)}

    monkeypatch.setattr(ecpay, '_post', fake_post)
    return calls, handlers


def _pos_order(app, staff_client, name, price=100, **fields):
    iid = make_item(app, name, price=price, stock=20)
    r = checkout(staff_client, [line(iid)], url=POS, cash_received=price, **fields)
    assert r.status_code == 200, r.get_json()
    return r.get_json()['order_id']


def _invoice(app, order_id):
    with app.app_context():
        inv = Invoice.query.filter_by(order_id=order_id).order_by(Invoice.id.desc()).first()
        return {c.name: getattr(inv, c.name) for c in Invoice.__table__.columns} if inv else None


def _set_invoice(app, invoice_id, **values):
    with app.app_context():
        inv = db.session.get(Invoice, invoice_id)
        for k, v in values.items():
            setattr(inv, k, v)
        db.session.commit()


# ---------------------------------------------------------------------------
# B-022 / R2-012：發票資訊驗證
# ---------------------------------------------------------------------------
def test_default_donate_without_love_code_falls_back_to_print():
    cfg = {'default_carrier': 'donate', 'default_love_code': ''}
    assert ecpay.normalize_invoice_info({}, cfg)['carrier_type'] == 'print'
    assert ecpay.normalize_invoice_info(None, dict(cfg, default_love_code='12'))['carrier_type'] == 'print'
    out = ecpay.normalize_invoice_info({}, dict(cfg, default_love_code='168001'))
    assert (out['carrier_type'], out['love_code']) == ('donate', '168001')
    with pytest.raises(AppError):   # 顧客明確選擇捐贈卻沒有可用的愛心碼：仍要擋下
        ecpay.normalize_invoice_info({'type': 'donate'}, cfg)


def test_unicode_digits_are_rejected_without_500():
    cfg = {'default_carrier': 'print'}
    for ban in ('1234567²', '０４５９５２５７', '٠٤٥٩٥٢٥٧'):
        assert ecpay.is_valid_ban(ban) is False
        with pytest.raises(AppError):
            ecpay.normalize_invoice_info({'type': 'company', 'buyer_id': ban}, cfg)
    assert ecpay.is_valid_ban('04595257') is True
    for code in ('１６８', '16８001', '²³⁴'):
        with pytest.raises(AppError):
            ecpay.normalize_invoice_info({'type': 'donate', 'love_code': code}, cfg)
    with pytest.raises(AppError):
        ecpay.normalize_invoice_info({'type': 'citizen', 'carrier': 'AB1234567890123²'}, cfg)
    assert ecpay.normalize_invoice_info({'type': 'citizen', 'carrier': 'ab12345678901234'}, cfg)['carrier_num'] \
        == 'AB12345678901234'


def test_checkout_succeeds_when_default_donate_has_no_love_code(app, staff_client, fake_ecpay, store_settings):
    store_settings('einvoice', default_carrier='donate', default_love_code='')
    oid = _pos_order(app, staff_client, 'P2 預設捐贈未填碼', price=80)
    inv = _invoice(app, oid)
    assert inv['carrier_type'] == 'print' and inv['status'] == InvoiceStatus.ISSUED


def test_superscript_ban_returns_400_not_500(app, staff_client, fake_ecpay):
    iid = make_item(app, 'P2 上標統編', price=50, stock=20)
    r = checkout(staff_client, [line(iid)], url=POS, cash_received=50,
                 invoice={'type': 'company', 'buyer_id': '1234567²'})
    assert r.status_code == 400 and '統一編號' in r.get_json()['message']


# ---------------------------------------------------------------------------
# B-031：逾時但綠界已開立 → 重送被 RelateNumber 重複拒絕時查回發票號碼
# ---------------------------------------------------------------------------
def test_duplicate_relate_number_recovers_issued_invoice(app, staff_client, fake_ecpay):
    calls, handlers = fake_ecpay
    handlers['/B2CInvoice/Issue'] = ConnectionError('timeout')
    oid = _pos_order(app, staff_client, 'P2 逾時已開立')
    inv = _invoice(app, oid)
    assert inv['status'] == InvoiceStatus.PENDING and inv['attempts'] == 1
    handlers['/B2CInvoice/Issue'] = {'RtnCode': 4000006, 'RtnMsg': 'RelateNumber 重複'}
    handlers['/B2CInvoice/GetIssue'] = lambda data: {
        'RtnCode': 1, 'IIS_Number': 'QQ12345678', 'IIS_Random_Number': '9876', 'IIS_Relate_Number': data['RelateNumber'],
        'IIS_Create_Date': '2026-11-09 10:11:12', 'IIS_Invalid_Status': '0'}
    with app.app_context():
        assert ecpay.issue_invoice(inv['id']) == InvoiceStatus.ISSUED
    inv = _invoice(app, oid)
    assert (inv['invoice_no'], inv['random_number']) == ('QQ12345678', '9876')
    assert inv['invoice_date'] == datetime(2026, 11, 9, 10, 11, 12)
    assert calls[-1][0] == '/B2CInvoice/GetIssue' and calls[-1][1]['RelateNumber'] == inv['relate_number']


def test_void_of_previously_sent_failed_invoice_checks_ecpay_first(app, staff_client, fake_ecpay):
    calls, handlers = fake_ecpay
    handlers['/B2CInvoice/Issue'] = ConnectionError('timeout')
    oid = _pos_order(app, staff_client, 'P2 失敗後作廢')
    inv = _invoice(app, oid)
    _set_invoice(app, inv['id'], status=InvoiceStatus.FAILED)
    handlers['/B2CInvoice/GetIssue'] = {'RtnCode': 1, 'IIS_Number': 'QQ00000001', 'IIS_Random_Number': '1111',
                                        'IIS_Create_Date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    with app.app_context():
        assert ecpay.void_or_allowance(inv['id']) == InvoiceStatus.VOID
    assert calls[-1][0] == '/B2CInvoice/Invalid' and calls[-1][1]['InvoiceNo'] == 'QQ00000001', '綠界上已開立的發票要真的作廢'
    # 查詢失敗：不可只在本機標記作廢
    oid2 = _pos_order(app, staff_client, 'P2 失敗後作廢 2')
    inv2 = _invoice(app, oid2)
    _set_invoice(app, inv2['id'], status=InvoiceStatus.FAILED)
    handlers['/B2CInvoice/GetIssue'] = ConnectionError('down')
    with app.app_context():
        assert ecpay.void_or_allowance(inv2['id']) == InvoiceStatus.FAILED


# ---------------------------------------------------------------------------
# B-032：關閉自動開立 / 已取消訂單不由背景開立
# ---------------------------------------------------------------------------
def test_retry_pending_respects_auto_issue_off_and_cancelled_orders(app, staff_client, admin_client, fake_ecpay,
                                                                    store_settings):
    store_settings('einvoice', auto_issue=False, void_on_cancel=False)
    oid = _pos_order(app, staff_client, 'P2 手動開立')
    oid2 = _pos_order(app, staff_client, 'P2 取消不開立')
    inv, inv2 = _invoice(app, oid), _invoice(app, oid2)
    assert inv['status'] == inv2['status'] == InvoiceStatus.PENDING and inv['attempts'] == 0
    assert admin_client.post(f'/api/orders/{oid2}/status', json={'status': 'Cancelled'}).status_code == 200
    with app.app_context():
        ecpay.retry_pending(older_than_seconds=0, limit=1000)
    assert _invoice(app, oid)['status'] == InvoiceStatus.PENDING, '關閉自動開立：等後台手動開立'
    assert _invoice(app, oid2)['status'] == InvoiceStatus.PENDING
    with app.app_context():
        assert ecpay.issue_invoice(inv2['id']) == InvoiceStatus.PENDING, '已取消的訂單不開立'
    assert '已取消' in _invoice(app, oid2)['last_error']
    # 手動開立失敗 (attempts > 0) 後才由背景重送
    _, handlers = fake_ecpay
    handlers['/B2CInvoice/Issue'] = ConnectionError('timeout')
    with app.app_context():
        assert ecpay.issue_invoice(inv['id']) == InvoiceStatus.PENDING
    del handlers['/B2CInvoice/Issue']
    with app.app_context():
        ecpay.retry_pending(older_than_seconds=0, limit=1000)
    assert _invoice(app, oid)['status'] == InvoiceStatus.ISSUED


# ---------------------------------------------------------------------------
# B-044：停在 issuing / 非預期例外
# ---------------------------------------------------------------------------
def test_stuck_issuing_and_unexpected_errors_are_recoverable(app, staff_client, fake_ecpay, monkeypatch):
    _, handlers = fake_ecpay
    handlers['/B2CInvoice/Issue'] = ValueError('bad json')   # 非預期的例外
    oid = _pos_order(app, staff_client, 'P2 非預期錯誤')
    inv = _invoice(app, oid)
    assert inv['status'] == InvoiceStatus.PENDING and '非預期' in inv['last_error']
    del handlers['/B2CInvoice/Issue']
    # 開立途中重啟：停在 issuing 超過 10 分鐘 → 查無 → 改回待開立並重送
    _set_invoice(app, inv['id'], status=ecpay.ISSUING, updated_at=datetime.now() - timedelta(minutes=30))
    with app.app_context():
        db.session.execute(Invoice.__table__.update().where(Invoice.__table__.c.id == inv['id'])
                           .values(updated_at=datetime.now() - timedelta(minutes=30)))
        db.session.commit()
        ecpay.retry_pending(older_than_seconds=0, limit=1000)
    assert _invoice(app, oid)['status'] == InvoiceStatus.ISSUED
    # 綠界其實已開立：補回號碼
    oid2 = _pos_order(app, staff_client, 'P2 開立中已開立')
    inv2 = _invoice(app, oid2)
    handlers['/B2CInvoice/GetIssue'] = {'RtnCode': 1, 'IIS_Number': 'QQ55555555', 'IIS_Random_Number': '5555',
                                        'IIS_Create_Date': '2026-11-09T09:00:00'}
    with app.app_context():
        db.session.execute(Invoice.__table__.update().where(Invoice.__table__.c.id == inv2['id'])
                           .values(status=ecpay.ISSUING, invoice_no='', updated_at=datetime.now() - timedelta(minutes=30)))
        db.session.commit()
        assert ecpay.recover_stuck_issuing() >= 1
    inv2 = _invoice(app, oid2)
    assert inv2['status'] == InvoiceStatus.ISSUED and inv2['invoice_no'] == 'QQ55555555'


# ---------------------------------------------------------------------------
# B-045：同一張發票兩筆折讓同時完成
# ---------------------------------------------------------------------------
def test_concurrent_allowances_do_not_lose_updates(app, staff_client, fake_ecpay):
    _, handlers = fake_ecpay
    oid = _pos_order(app, staff_client, 'P2 折讓併發', price=300)
    inv_id = _invoice(app, oid)['id']
    state = {'nested': False}

    def allowance_reply(data):
        if not state['nested']:   # 第一筆等綠界回應時，另一個執行緒的折讓先完成並提交
            state['nested'] = True
            with db.engine.begin() as conn:
                conn.execute(Invoice.__table__.update().where(Invoice.__table__.c.id == inv_id).values(
                    status=InvoiceStatus.ALLOWANCE, allowance_amount=Invoice.__table__.c.allowance_amount + 50,
                    allowance_numbers='OTHER0001'))
        return {'RtnCode': 1, 'IA_Allow_No': 'MINE0001'}

    handlers['/B2CInvoice/Allowance'] = allowance_reply
    with app.app_context():
        assert ecpay.allowance(inv_id, 100) == InvoiceStatus.ALLOWANCE
    inv = _invoice(app, oid)
    assert inv['allowance_amount'] == 150 and inv['allowance_numbers'] == 'OTHER0001,MINE0001'


# ---------------------------------------------------------------------------
# R2-013：桌位合併發票不列已取消訂單的品項
# ---------------------------------------------------------------------------
def test_settlement_invoice_lines_skip_cancelled_orders(app, staff_client, fake_ecpay):
    a = _pos_order(app, staff_client, 'P2 合併甲', price=300)
    b = _pos_order(app, staff_client, 'P2 合併乙', price=200)
    with app.app_context():
        sid = 990000 + a
        for oid in (a, b):
            db.session.get(Order, oid).settlement_id = sid
        db.session.get(Order, b).status = 'Cancelled'
        inv = Invoice(tenant_id=default_tenant_id(app), settlement_id=sid, relate_number=ecpay.new_relate_number(),
                      status=InvoiceStatus.PENDING, amount=300)
        db.session.add(inv)
        db.session.flush()
        lines = ecpay._invoice_lines(inv)
        db.session.rollback()
    names = [n for n, _q, _p in lines]
    assert 'P2 合併乙' not in names and '折扣' not in names
    assert sum(q * p for _n, q, p in lines) == 300


# ---------------------------------------------------------------------------
# B-023：線上端不開立分店上傳的發票
# ---------------------------------------------------------------------------
def test_hub_does_not_issue_invoices_uploaded_by_branches(app, staff_client, fake_ecpay, monkeypatch):
    calls, handlers = fake_ecpay
    handlers['/B2CInvoice/Issue'] = ConnectionError('timeout')
    oid = _pos_order(app, staff_client, 'P2 分店發票')
    inv = _invoice(app, oid)
    del handlers['/B2CInvoice/Issue']
    with app.app_context():
        db.session.get(Order, oid).node_uuid = 'branch-node-uuid'
        db.session.commit()
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    n = len(calls)
    with app.app_context():
        assert ecpay.is_local_invoice(db.session.get(Invoice, inv['id'])) is False
        ecpay.retry_pending(older_than_seconds=0, limit=1000)
        assert ecpay.issue_invoice(inv['id']) == InvoiceStatus.PENDING
    assert _invoice(app, oid)['status'] == InvoiceStatus.PENDING
    assert not any(c[1].get('RelateNumber') == inv['relate_number'] for c in calls[n:])
    # 線上點餐 (線上端建立) 的發票照常開立
    with app.app_context():
        db.session.get(Order, oid).order_source = 'Online'
        db.session.commit()
        assert ecpay.issue_invoice(inv['id']) == InvoiceStatus.ISSUED


# ---------------------------------------------------------------------------
# B-046：檢查碼含非 ASCII 字元
# ---------------------------------------------------------------------------
def test_verify_rejects_non_ascii_check_mac_without_error(app):
    from app.services import payment_ecpay
    with app.test_request_context('/'):
        assert payment_ecpay.verify({'MerchantTradeNo': 'K1', 'CheckMacValue': 'é'}) is False
        assert payment_ecpay.verify({'MerchantTradeNo': 'K1', 'CheckMacValue': '中' * 64}) is False


# ---------------------------------------------------------------------------
# 線上付款：重複付款 / 記錄實際付款的特店訂單編號 / 逾時取消
# ---------------------------------------------------------------------------
def _pending_online(app, staff_client, name, minutes_ago=0, **values):
    oid = _pos_order(app, staff_client, name, price=120)
    with app.app_context():
        o = db.session.get(Order, oid)
        o.payment_status = 'pending'
        o.payment_trade_no = None
        o.payment_ref = values.pop('payment_ref', None)
        o.created_at = datetime.now() - timedelta(minutes=minutes_ago)
        for k, v in values.items():
            setattr(o, k, v)
        db.session.commit()
        return oid, o.uuid, o.total_price


def test_second_payment_for_same_order_is_flagged_duplicate(app, staff_client, monkeypatch):
    from app.services import monitor_service, payment_ecpay
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda message, **kw: alerts.append((message, kw)))
    oid, uuid, total = _pending_online(app, staff_client, 'P2 重複付款', payment_ref='KNEWER00000000000001')
    with app.app_context():
        assert payment_ecpay.mark_paid(uuid, 'TRADE-A', total, merchant_trade_no='KOLDER00000000000001') == 'paid'
        o = db.session.get(Order, oid)
        assert o.payment_ref == 'KOLDER00000000000001', '退刷要用實際付款的特店訂單編號'
        assert payment_ecpay.mark_paid(uuid, 'TRADE-A', total) == 'already'
        assert alerts == []
        assert payment_ecpay.mark_paid(uuid, 'TRADE-B', total, merchant_trade_no='KNEWER00000000000001') == 'duplicate'
        assert db.session.get(Order, oid).payment_trade_no == 'TRADE-A'
    assert alerts and 'TRADE-B' in alerts[0][0] and alerts[0][1]['key'] == 'order-dup-pay-TRADE-B'


def test_expiry_waits_for_recent_ecpay_trade_and_retries_failed_cancel(app, staff_client, monkeypatch):
    from app.services import order_service, payment_ecpay
    ref = 'KP2REF00000000000001'
    oid, _uuid, _total = _pending_online(app, staff_client, 'P2 逾時重新付款', minutes_ago=30, payment_ref=ref)
    recent = (datetime.now() - timedelta(minutes=2)).strftime('%Y/%m/%d %H:%M:%S')
    old = (datetime.now() - timedelta(minutes=20)).strftime('%Y/%m/%d %H:%M:%S')

    def query_for(trade_date):
        return lambda r: {'TradeStatus': '0', 'TradeDate': trade_date} if r == ref else None

    with app.app_context():
        payment_ecpay.expire_unpaid_orders(force=True, query=query_for(recent))
        assert db.session.get(Order, oid).payment_status == 'pending', '剛重新付款 (綠界交易成立未滿時限)：不取消'
    # 取消時發生暫時性錯誤：付款狀態已標記逾時，下一輪要再試取消
    real = order_service.change_order_status

    def boom(*a, **kw):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(order_service, 'change_order_status', boom)
    with app.app_context():
        payment_ecpay.expire_unpaid_orders(force=True, query=query_for(old))
        o = db.session.get(Order, oid)
        assert (o.payment_status, o.status) == ('expired', 'Pending')
    monkeypatch.setattr(order_service, 'change_order_status', real)
    with app.app_context():
        payment_ecpay.expire_unpaid_orders(force=True, query=lambda r: None)
        o = db.session.get(Order, oid)
        assert (o.payment_status, o.status) == ('expired', 'Cancelled')


# ---------------------------------------------------------------------------
# B-024：付款完成時重新標記建單時寫入的異動帳
# ---------------------------------------------------------------------------
def test_mark_paid_retouches_ledgers_written_at_order_time(app, staff_client):
    from app.models.sync import PointLedger
    from app.services import payment_ecpay
    from app.services.points_service import recompute_points
    uid = make_member(app, 'P2 異動帳會員', '0977200001', points=10)
    oid, uuid, total = _pending_online(app, staff_client, 'P2 異動帳')
    with app.app_context():
        entry = PointLedger(tenant_id=default_tenant_id(app), user_id=uid, delta=-10, reason='兌換', order_id=oid)
        db.session.add(entry)
        db.session.commit()
        recompute_points([uid])   # 直接新增的異動帳：重算點數，維持 點數 = 異動帳合計 (test_sync_invariants)
        db.session.commit()
        entry = db.session.get(PointLedger, entry.id)
        entry_id, rev_before = entry.id, entry.sync_rev
        assert payment_ecpay.mark_paid(uuid, 'TRADE-LEDGER', total) == 'paid'
        assert db.session.get(PointLedger, entry_id).sync_rev > rev_before


# ---------------------------------------------------------------------------
# B-047：記錄歷次產生的特店訂單編號；逾時查詢逐一查詢；通知的編號須屬於本訂單
# ---------------------------------------------------------------------------
def test_b047_payment_refs_recorded_queried_and_validated(app, staff_client, monkeypatch):
    from app.services import payment_ecpay
    oid, uuid, total = _pending_online(app, staff_client, 'P2 歷次編號', minutes_ago=30)
    # monkeypatch 結束時還原成原本的值 (TestConfig 預設 True)；直接 pop 會讓之後所有測試的綠界付款都被停用 (503)
    monkeypatch.setitem(app.config, 'ECPAY_ALLOW_TEST_KEYS', True)
    with tenant_request(app, default_tenant_id(app)):
        o = db.session.get(Order, oid)
        first = payment_ecpay.build_payment_form(o, base_url='https://shop.example')['MerchantTradeNo']
        second = payment_ecpay.build_payment_form(o, base_url='https://shop.example')['MerchantTradeNo']
        o = db.session.get(Order, oid)
        assert o.payment_ref == second
        assert o.payment_refs.split(',') == [first, second]

    old = (datetime.now() - timedelta(minutes=20)).strftime('%Y/%m/%d %H:%M:%S')
    asked = []

    def query(r):
        asked.append(r)
        if r == first:   # 顧客用較早開啟的付款頁付款，通知遺失
            return {'TradeStatus': '1', 'TradeNo': 'TRADE-EARLY', 'TradeAmt': str(total), 'MerchantTradeNo': first,
                    'PaymentType': 'Credit_CreditCard'}
        return {'TradeStatus': '0', 'TradeDate': old}

    with app.app_context():
        assert payment_ecpay.expire_unpaid_orders(force=True, query=query) == 0
        o = db.session.get(Order, oid)
        assert asked == [second, first], '從最新的編號往回查'
        assert (o.payment_status, o.payment_trade_no, o.payment_ref) == ('paid', 'TRADE-EARLY', first)


def test_b047_expiry_skips_when_any_ref_query_fails_and_foreign_ref_not_written(app, staff_client):
    from app.services import payment_ecpay
    old = (datetime.now() - timedelta(minutes=20)).strftime('%Y/%m/%d %H:%M:%S')
    oid, uuid, total = _pending_online(app, staff_client, 'P2 查詢失敗', minutes_ago=30, payment_ref='KREFB000000000000002',
                                       payment_refs='KREFA000000000000001,KREFB000000000000002')
    with app.app_context():
        payment_ecpay.expire_unpaid_orders(
            force=True, query=lambda r: None if r == 'KREFA000000000000001' else {'TradeStatus': '0', 'TradeDate': old})
        assert db.session.get(Order, oid).payment_status == 'pending', '任一編號查不到：不冒險取消'
        # 不屬於本訂單的編號不覆寫 payment_ref
        assert payment_ecpay.mark_paid(uuid, 'TRADE-X', total, merchant_trade_no='KFOREIGN000000000009') == 'paid'
        assert db.session.get(Order, oid).payment_ref == 'KREFB000000000000002'
    oid2, _uuid2, _t2 = _pending_online(app, staff_client, 'P2 全未付款', minutes_ago=30, payment_ref='KREFD000000000000004',
                                        payment_refs='KREFC000000000000003,KREFD000000000000004')
    with app.app_context():
        payment_ecpay.expire_unpaid_orders(force=True, query=lambda r: {'TradeStatus': '0', 'TradeDate': old})
        o = db.session.get(Order, oid2)
        assert (o.payment_status, o.status) == ('expired', 'Cancelled')
