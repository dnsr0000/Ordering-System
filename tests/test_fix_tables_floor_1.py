"""
修正小組 tables-floor-1 的回歸測試 (app/services/table_service.py)：
桌號範圍解析、$0 整桌結帳、order_ids 格式、結帳與金額異動競態、多種付款方式的分攤、換桌後的綁定與線上結帳、
整桌結清後的綁定、刪除付款中的桌位、hub 上的 tables check、結帳後的「想結帳」呼叫、改名同步訂位。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db, limiter
from app.models.order import Order, OrderItem, ServiceCall
from app.models.payment import PaymentRecord
from app.models.table import DiningTable, TablePayment
from tests.support import checkout, default_tenant_id, line, make_item, tenant_request


@pytest.fixture(autouse=True)
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture(scope='module')
def item(app):
    return make_item(app, 'FT1 測試餐', price=100, stock=100000)


def _new_table(app, name):
    with app.app_context():
        t = DiningTable.query.filter_by(name=name).first()
        if t is None:
            t = DiningTable(tenant_id=default_tenant_id(app), name=name)
            db.session.add(t)
            db.session.commit()
        return t.id, t.token


def _phone(app, token):
    c = app.test_client()
    assert c.get(f'/t/{token}').status_code == 302
    c.post('/guest_login')
    return c


def _order(app, order_id):
    with app.app_context():
        return db.session.get(Order, order_id)


# ---------------------------------------------------------------------------
# B-146 / R2-056：桌號範圍
# ---------------------------------------------------------------------------
def test_parse_table_names_ranges(app):
    from app.services.table_service import parse_table_names
    with app.app_context():
        assert parse_table_names('A1 - A3') == ['A1', 'A2', 'A3']
        assert parse_table_names('A1 -A3, B1') == ['A1', 'A2', 'A3', 'B1']
        assert parse_table_names('2F1-2F3') == ['2F1', '2F2', '2F3']
        assert parse_table_names('1A-3A') == ['1A', '2A', '3A']
        assert parse_table_names('A1-10')[-1] == 'A10'
        assert parse_table_names('A01-A03') == ['A01', 'A02', 'A03']
        assert parse_table_names('A-1, 2F-1, 吧台1') == ['A-1', '2F-1', '吧台1']
        for bad in ('A1-B3', '-', 'A1 -', '-A3', '2F1-3F5'):
            with pytest.raises(AppError):
                parse_table_names(bad)


# ---------------------------------------------------------------------------
# B-138：$0 整桌結帳
# ---------------------------------------------------------------------------
def test_zero_total_table_can_be_settled(app, item, staff_client):
    table_id, token = _new_table(app, 'FT1零元')
    oid = checkout(_phone(app, token), [line(item)]).get_json()['order_id']
    with app.app_context():
        db.session.get(Order, oid).total_price = 0
        db.session.commit()
    r = staff_client.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash', 'cash_received': 0})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['total'] == 0 and r.get_json()['payments'] == []
    o = _order(app, oid)
    assert o.payment_status == 'settled'
    with app.app_context():
        assert PaymentRecord.query.filter_by(settlement_id=o.settlement_id).count() == 0


# ---------------------------------------------------------------------------
# B-148：order_ids 格式
# ---------------------------------------------------------------------------
def test_order_ids_must_be_a_list(app, item, staff_client):
    table_id, token = _new_table(app, 'FT1格式')
    other_id, _ = _new_table(app, 'FT1格式2')
    checkout(_phone(app, token), [line(item)])
    r = staff_client.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash', 'order_ids': 5})
    assert r.status_code == 400 and 'order_ids' in r.get_json()['message']
    r = staff_client.post(f'/api/pos/tables/{table_id}/move', json={'to_table_id': other_id, 'order_ids': '123'})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# B-142：讀取後金額被改 (同時作廢品項) 不以舊金額結帳
# ---------------------------------------------------------------------------
def test_settle_rejects_when_total_changed_after_read(app, item, monkeypatch):
    from app.services import table_service
    table_id, token = _new_table(app, 'FT1競態')
    oid = checkout(_phone(app, token), [line(item, 2)]).get_json()['order_id']
    real = table_service._parse_payments

    def parse_then_void(*args, **kwargs):
        out = real(*args, **kwargs)
        db.session.execute(update(Order).where(Order.id == oid).values(total_price=100)
                           .execution_options(synchronize_session=False))
        return out

    monkeypatch.setattr(table_service, '_parse_payments', parse_then_void)
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        with pytest.raises(AppError) as exc:
            table_service.settle_table(tid, table_id, 'Cash', 200, SimpleNamespace(username='ft1'))
        assert exc.value.status == 409
    assert _order(app, oid).payment_status == 'unpaid'


# ---------------------------------------------------------------------------
# R2-053：多種付款方式 → 混合付款，收款紀錄分攤到各訂單
# ---------------------------------------------------------------------------
def test_multi_method_settle_records_per_order(app, item, staff_client):
    from app.services import payment_service
    table_id, token = _new_table(app, 'FT1混合')
    phone = _phone(app, token)
    first = checkout(phone, [line(item)]).get_json()['order_id']
    second = checkout(phone, [line(item, 2)]).get_json()['order_id']
    r = staff_client.post(f'/api/pos/tables/{table_id}/settle', json={'payments': [
        {'method': 'Cash', 'amount': 150, 'received': 200}, {'method': 'Credit Card', 'amount': 150, 'auth_code': 'AB1234', 'card_last4': '4242'}]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['change'] == 50
    with app.app_context():
        o1, o2 = db.session.get(Order, first), db.session.get(Order, second)
        assert o1.payment_method == o2.payment_method == '混合付款'
        assert payment_service.paid_by_method(o1) == {'Cash': 100}
        assert payment_service.paid_by_method(o2) == {'Cash': 50, 'Credit Card': 150}
        recs = PaymentRecord.query.filter_by(settlement_id=o1.settlement_id).all()
        assert sum(p.amount for p in recs) == 300 and sum(p.change for p in recs) == 50


def test_single_method_settle_keeps_settlement_record(app, item, staff_client):
    table_id, token = _new_table(app, 'FT1單一')
    oid = checkout(_phone(app, token), [line(item)]).get_json()['order_id']
    r = staff_client.post(f'/api/pos/tables/{table_id}/settle', json={'payments': [
        {'method': 'Cash', 'amount': 50}, {'method': 'Cash', 'amount': 50}]})
    assert r.status_code == 200, r.get_json()
    o = _order(app, oid)
    assert o.payment_method == 'Cash'
    with app.app_context():
        assert {(p.order_id, p.amount) for p in PaymentRecord.query.filter_by(settlement_id=o.settlement_id)} \
            == {(None, 50)}


# ---------------------------------------------------------------------------
# B-140 / R2-054：換桌後的綁定與線上結帳
# ---------------------------------------------------------------------------
def test_binding_follows_moved_orders(app, item, staff_client):
    src, token = _new_table(app, 'FT1換桌A')
    dst, _ = _new_table(app, 'FT1換桌B')
    phone = _phone(app, token)
    first = checkout(phone, [line(item)]).get_json()['order_id']
    assert staff_client.post(f'/api/pos/tables/{src}/move', json={'to_table_id': dst}).status_code == 200
    second = checkout(phone, [line(item)]).get_json()['order_id']
    assert _order(app, first).dining_table_id == dst
    assert _order(app, second).dining_table_id == dst, '換桌後加點掛到新的桌位'


def test_online_settle_after_move(app, item, staff_client):
    from app.services import table_service
    src, token = _new_table(app, 'FT1線上A')
    dst, _ = _new_table(app, 'FT1線上B')
    oid = checkout(_phone(app, token), [line(item)]).get_json()['order_id']
    assert staff_client.post(f'/api/pos/tables/{src}/move', json={'to_table_id': dst}).status_code == 200
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        result = table_service.settle_table(tid, src, 'Credit Card', 0, SimpleNamespace(username='ecpay'),
                                            order_ids=[oid], online={'method': 'Credit Card', 'amount': 100,
                                                                     'reference': 'FT1TEST'})
        assert result['table'] == 'FT1線上B'
    assert _order(app, oid).payment_status == 'settled'


def test_binding_expires_after_table_cleared_and_next_group_orders(app, item, staff_client):
    table_id, token = _new_table(app, 'FT1換組')
    first_group = _phone(app, token)
    checkout(first_group, [line(item)])
    assert staff_client.post(f'/api/pos/tables/{table_id}/settle',
                             json={'payment_method': 'Cash', 'cash_received': 100}).status_code == 200
    # 結清後同一支手機繼續加點不受影響
    assert checkout(first_group, [line(item)]).status_code == 200
    assert staff_client.post(f'/api/pos/tables/{table_id}/settle',
                             json={'payment_method': 'Cash', 'cash_received': 100}).status_code == 200
    second_group = _phone(app, token)
    assert checkout(second_group, [line(item)]).status_code == 200
    r = checkout(first_group, [line(item)])
    assert r.status_code == 409, '前一組客人的手機不可再把餐點掛到這一桌'
    assert checkout(second_group, [line(item)]).status_code == 200


# ---------------------------------------------------------------------------
# B-139：有付款中的線上結帳時不可刪除桌位
# ---------------------------------------------------------------------------
def test_delete_table_with_pending_online_payment_is_refused(app):
    from app.services import table_service
    table_id, _ = _new_table(app, 'FT1刪除')
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        db.session.add(TablePayment(tenant_id=tid, dining_table_id=table_id, amount=100, trade_no='FT1DEL0001',
                                    order_ids='1'))
        db.session.commit()
        with pytest.raises(AppError) as exc:
            table_service.delete_table(db.session.get(DiningTable, table_id))
        assert exc.value.status == 409
    with app.app_context():
        assert db.session.get(DiningTable, table_id) is not None


# ---------------------------------------------------------------------------
# B-147：作廢 (數量 0) 的品項不顯示
# ---------------------------------------------------------------------------
def test_unpaid_tables_hide_voided_items(app, item):
    from app.services import table_service
    _table_id, token = _new_table(app, 'FT1作廢')
    other = make_item(app, 'FT1 作廢品', price=50, stock=100)
    oid = checkout(_phone(app, token), [line(item), line(other)]).get_json()['order_id']
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        db.session.execute(update(OrderItem).where(OrderItem.order_id == oid, OrderItem.menu_item_id == other)
                           .values(quantity=0).execution_options(synchronize_session=False))
        db.session.commit()
        t = next(t for t in table_service.unpaid_tables() if t['name'] == 'FT1作廢')
        assert [i['name'] for o in t['orders'] for i in o['items']] == ['FT1 測試餐']


# ---------------------------------------------------------------------------
# B-150：線上端不檢查 orphan / stale_unpaid
# ---------------------------------------------------------------------------
def test_find_conflicts_on_hub_skips_local_table_checks(app, item, monkeypatch):
    from app.services import table_service
    from app.sync import runtime as sync_runtime
    _table_id, token = _new_table(app, 'FT1檢查')
    oid = checkout(_phone(app, token), [line(item)]).get_json()['order_id']
    with app.app_context():
        o = db.session.get(Order, oid)
        o.dining_table_id, o.created_at = None, datetime.now() - timedelta(days=3)
        db.session.commit()
        assert any(f['order_id'] == oid and f['kind'] == 'orphan' for f in table_service.find_conflicts())
        monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
        assert not [f for f in table_service.find_conflicts() if f['order_id'] == oid]
        o = db.session.get(Order, oid)
        o.status = 'Cancelled'   # 不影響其他測試的 tables check
        db.session.commit()


# ---------------------------------------------------------------------------
# B-151 / B-152：呼叫服務
# ---------------------------------------------------------------------------
def test_settle_closes_bill_call(app, item, staff_client):
    table_id, token = _new_table(app, 'FT1呼叫')
    phone = _phone(app, token)
    checkout(phone, [line(item)])
    assert phone.post('/api/table/call', json={'kind': 'bill'}).get_json()['created'] is True
    assert phone.post('/api/table/call', json={'kind': 'bill'}).get_json()['created'] is False
    assert phone.post('/api/table/call', json={'kind': 'water'}).get_json()['created'] is True
    assert staff_client.post(f'/api/pos/tables/{table_id}/settle',
                             json={'payment_method': 'Cash', 'cash_received': 100}).status_code == 200
    with app.app_context():
        calls = {c.kind: c for c in ServiceCall.query.filter_by(dining_table_id=table_id).all()}
        assert calls['bill'].handled_at is not None and calls['water'].handled_at is None


# ---------------------------------------------------------------------------
# R2-057：改名時一併更新訂位 / 候位的桌名
# ---------------------------------------------------------------------------
def test_rename_table_updates_reservations(app):
    from app.models.reservation import Reservation
    from app.services import table_service
    table_id, _ = _new_table(app, 'FT1改名')
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        booked = Reservation(tenant_id=tid, name='訂位甲', reserved_at=datetime.now() + timedelta(hours=1),
                             table_name='FT1改名')
        seated = Reservation(tenant_id=tid, name='訂位乙', reserved_at=datetime.now() - timedelta(hours=1),
                             table_name='FT1改名', status='seated')
        db.session.add_all([booked, seated])
        db.session.commit()
        table_service.rename_table(db.session.get(DiningTable, table_id), 'FT1新名')
        db.session.commit()
        assert db.session.get(Reservation, booked.id).table_name == 'FT1新名'
        assert db.session.get(Reservation, seated.id).table_name == 'FT1改名', '已入座的歷史紀錄不改'


# ---------------------------------------------------------------------------
# B-112 / B-013：桌位結帳實收現金的上限
# ---------------------------------------------------------------------------
def test_table_cash_received_bounds(app, item, staff_client):
    table_id, token = _new_table(app, 'FT1實收')
    oid = checkout(_phone(app, token), [line(item)]).get_json()['order_id']
    for bad in (3_000_000_000, 200_101):
        r = staff_client.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash', 'cash_received': bad})
        assert r.status_code == 400, (bad, r.get_json())
    assert _order(app, oid).payment_status == 'unpaid'
    r = staff_client.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash', 'cash_received': 1000})
    assert r.status_code == 200 and r.get_json()['change'] == 900
    assert 'invoice_id' in r.get_json()


# ---------------------------------------------------------------------------
# B-234：畫面上的應收金額已異動 → 409；R2-011：顧客正在線上結帳 → 需確認
# ---------------------------------------------------------------------------
def test_settle_checks_expected_total_and_pending_online_payment(app, item):
    from app.services import table_service
    table_id, token = _new_table(app, 'FT1預期')
    oid = checkout(_phone(app, token), [line(item)]).get_json()['order_id']
    tid = default_tenant_id(app)
    staff = SimpleNamespace(username='ft1')
    with tenant_request(app, tid):
        with pytest.raises(AppError) as exc:
            table_service.settle_table(tid, table_id, 'Cash', 0, staff, expected_total=90)
        assert exc.value.status == 409
        db.session.add(TablePayment(tenant_id=tid, dining_table_id=table_id, amount=100, trade_no='FT1PEND0001',
                                    order_ids=str(oid)))
        db.session.commit()
        with pytest.raises(AppError) as exc:
            table_service.settle_table(tid, table_id, 'Cash', 0, staff, expected_total=100)
        assert exc.value.status == 409 and exc.value.extra.get('online_pay_pending') is True
        result = table_service.settle_table(tid, table_id, 'Cash', 0, staff, expected_total=100,
                                            force_online_pending=True)
        assert result['total'] == 100
    assert _order(app, oid).payment_status == 'settled'


# ---------------------------------------------------------------------------
# R2-055：停用但仍有未結帳訂單的桌位仍出現在平面圖
# ---------------------------------------------------------------------------
def test_floor_plan_keeps_inactive_table_with_unpaid_orders(app, item):
    from app.services import table_service
    table_id, token = _new_table(app, 'FT1停用')
    idle_id, _ = _new_table(app, 'FT1停用空')
    oid = checkout(_phone(app, token), [line(item)]).get_json()['order_id']
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        for t in (table_id, idle_id):
            db.session.get(DiningTable, t).is_active = False
        db.session.commit()
        tables = {t['id']: t for t in table_service.floor_plan()['tables']}
        assert tables[table_id]['inactive'] is True and tables[table_id]['status'] == 'occupied'
        assert idle_id not in tables
        assert all(t['inactive'] is False for k, t in tables.items() if k != table_id)
        o = db.session.get(Order, oid)
        o.status = 'Cancelled'   # 不影響其他測試
        db.session.commit()


# ---------------------------------------------------------------------------
# B-005 / B-015：到店付款訂單 (沒有桌位的 unpaid 訂單) 收款
# ---------------------------------------------------------------------------
def test_settle_order_for_pay_at_store_order(app, item):
    from app.services import table_service
    from app.services.order_service import process_checkout
    tid = default_tenant_id(app)
    staff = SimpleNamespace(username='ft1')
    with tenant_request(app, tid):
        order, _ = process_checkout(tenant_id=tid, items=[line(item, 2)])
        oid = order.id
        db.session.execute(update(Order).where(Order.id == oid).values(payment_status='unpaid')
                           .execution_options(synchronize_session=False))
        db.session.commit()
        with pytest.raises(AppError):
            table_service.settle_order(tid, oid, 'Cash', 999_999_999, staff)
        result = table_service.settle_order(tid, oid, 'Cash', 500, staff)
        assert (result['total'], result['cash_received'], result['change']) == (200, 500, 300)
        with pytest.raises(AppError) as exc:
            table_service.settle_order(tid, oid, 'Cash', 500, staff)
        assert exc.value.status == 409
    with app.app_context():
        o = db.session.get(Order, oid)
        assert (o.payment_status, o.cash_received, o.cash_change) == ('settled', 500, 300)
        recs = PaymentRecord.query.filter_by(order_id=oid, note='到店付款').all()
        assert [(r.kind, r.amount, r.change) for r in recs] == [('sale', 200, 300)]
