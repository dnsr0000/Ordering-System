"""跨檔請求修正 (unowned:app/routes) 的回歸測試：R2-016 取餐 / 叫號限門市、R2-011 桌邊線上結帳待退款清單、B-393 發票依分店"""
from datetime import datetime

from app.extensions import db
from app.models.table import DiningTable, TablePayment
from tests.support import assert_flash, default_tenant_id, other_shop_owner_client


def test_pickup_and_counter_are_store_only(app, admin_client, monkeypatch):
    """R2-016：線上端 (hub) 的取餐核銷與叫號看板一律 404 (不能在線上端把分店訂單核銷成已取餐)"""
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    for url in ('/pickup', '/api/pickup_orders', '/counter', '/api/counter_orders'):
        assert admin_client.get(url).status_code == 404, url
    assert app.test_client().get('/api/counter_orders').status_code == 404
    # 即時通知串流不限：線上端的點餐頁 / 後台也透過 common.js 訂閱
    resp = admin_client.get('/api/orders_stream', buffered=False)
    try:
        assert resp.status_code == 200
    finally:
        resp.close()
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'standalone')
    assert admin_client.get('/pickup').status_code == 200
    assert app.test_client().get('/counter').status_code == 200


def _conflict_payment(app, table_name, trade_no, provider='2411090101', amount=360):
    with app.app_context():
        tid = default_tenant_id(app)
        table = DiningTable.query.filter_by(name=table_name).first()
        tp = TablePayment(tenant_id=tid, dining_table_id=table.id if table else None, amount=amount, trade_no=trade_no,
                          status='conflict', provider_trade_no=provider, note='沒有可結帳的訂單',
                          paid_at=datetime(2026, 11, 9, 12, 30))
        db.session.add(tp)
        db.session.commit()
        return tp.id


def test_conflict_table_payments_listed_and_marked_refunded(app, admin_client, staff_client):
    """R2-011：後台桌位頁列出 conflict 的桌邊線上結帳，按「已到綠界退款」條件式改成 refunded 並記下操作人員"""
    admin_client.post('/admin/tables', data={'action': 'create', 'names': 'RFX1'})
    pid = _conflict_payment(app, 'RFX1', 'TPRFX0001')
    html = admin_client.get('/admin/tables').get_data(as_text=True)
    assert 'tablePayConflicts' in html and '2411090101' in html and '沒有可結帳的訂單' in html and '$360' in html

    # 一般員工不能操作；其他店家看不到這筆
    assert staff_client.post('/admin/tables', data={'action': 'table_pay_refunded', 'payment_id': pid}).status_code == 403
    other = other_shop_owner_client(app)
    assert other.post('/admin/tables', data={'action': 'table_pay_refunded', 'payment_id': pid}).status_code == 404
    with app.app_context():
        assert db.session.get(TablePayment, pid).status == 'conflict'

    resp = admin_client.post('/admin/tables', data={'action': 'table_pay_refunded', 'payment_id': pid})
    assert resp.status_code == 302
    with app.app_context():
        tp = db.session.get(TablePayment, pid)
        assert tp.status == 'refunded'
        assert '已到綠界退款' in tp.note and '沒有可結帳的訂單' in tp.note and len(tp.note) <= 200
        note = tp.note
    assert '2411090101' not in admin_client.get('/admin/tables').get_data(as_text=True), '已退款的不再列出'
    # 重複按 (或兩人同時按)：只會成功一次，第二次提示已不是待退款狀態、紀錄不再改動
    admin_client.post('/admin/tables', data={'action': 'table_pay_refunded', 'payment_id': pid})
    assert_flash(admin_client, '不是待退款狀態')
    with app.app_context():
        assert db.session.get(TablePayment, pid).note == note


def test_refund_note_keeps_operator_when_note_is_long(app, admin_client):
    """原因很長時仍保留操作紀錄 (note 欄位 200 字)"""
    admin_client.post('/admin/tables', data={'action': 'create', 'names': 'RFX2'})
    pid = _conflict_payment(app, 'RFX2', 'TPRFX0002', provider='2411090102')
    with app.app_context():
        tp = db.session.get(TablePayment, pid)
        tp.note = '原' * 200
        db.session.commit()
    assert admin_client.post('/admin/tables', data={'action': 'table_pay_refunded', 'payment_id': pid}).status_code == 302
    with app.app_context():
        tp = db.session.get(TablePayment, pid)
        assert tp.status == 'refunded' and len(tp.note) <= 200 and '已到綠界退款' in tp.note


def test_accounting_invoices_filtered_by_node(app, admin_client, monkeypatch):
    """B-393：會計頁的發票筆數與發票匯出都要依選取的分店過濾"""
    from app.services import accounting_service
    calls = []
    real = accounting_service.invoices

    def spy(*args, **kwargs):
        calls.append(args[3:] + tuple(kwargs.values()))
        return real(*args, **kwargs)

    monkeypatch.setattr(accounting_service, 'invoices', spy)
    monkeypatch.setattr(accounting_service, 'journal', lambda *a, **kw: [])  # 只驗證發票的分店參數
    assert admin_client.get('/admin/accounting?node=node-abc').status_code == 200
    assert admin_client.get('/admin/accounting?node=node-abc&export=invoices').status_code == 200
    assert calls == [('node-abc',), ('node-abc',)], calls


def test_counter_board_lists_waitlist_called_after_midnight(app, monkeypatch):
    """B-066：BUSINESS_DAY_START_HOUR 換日前 (午夜後) 叫號的候位，營業日是前一天，叫號看板仍要列出"""
    from app.services import order_lifecycle
    from app.services.reservation_service import join_waitlist

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 9, 2, 0)

    monkeypatch.setitem(app.config, 'BUSINESS_DAY_START_HOUR', 4)
    monkeypatch.setattr(order_lifecycle, 'datetime', FrozenDatetime)
    with app.app_context():
        entry = join_waitlist(default_tenant_id(app), '深夜客', '', 2)
        entry.status, entry.called_at = 'called', datetime(2026, 11, 9, 2, 0)
        db.session.commit()
        label, business_date = entry.label, entry.business_date
    assert str(business_date) == '2026-11-08'
    board = app.test_client().get('/api/counter_orders').get_json()
    assert label in [w['label'] for w in board['called_waitlist']]
