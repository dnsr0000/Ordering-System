"""
桌邊掃碼點餐的衝突 (同時操作) 檢查：以多執行緒讓操作真的同時發生，重複多輪，檢查最後的資料一致。
- 兩台以上 POS 同時結同一桌：只有一台成功，每張訂單只結一次
- 結帳與取消同時發生：不會出現「已取消卻顯示已結帳」(應標記需退款或維持未結帳)
- 顧客送單與店長刪除桌位同時發生：不會留下 POS 看不到的未結帳訂單
- 兩位店長同時新增同名桌位 / 改成同一個名稱：不會出現 500，桌號仍唯一
- 重新產生 QR code 與顧客送單同時發生：不會出現 500
- 多支手機同一桌同時點餐、櫃檯同時結帳：每張訂單不是已結帳就是仍可在 POS 結帳 (不會遺漏)
- 同步：線上端取消分店的桌邊訂單、分店同時結帳 → 兩邊最後都是「已取消 + 需退款」
任一執行緒拋出例外 (500) 都算失敗。

每個情境預設跑 6 輪；要加強檢查可設定輪數，例如 (PowerShell)：
    $env:TABLE_CONFLICT_ROUNDS=50; pytest tests/test_table_concurrency.py
在 PostgreSQL 上跑 (真正的列鎖與同時交易)：同時設定 TEST_DATABASE_URL。
"""
import os

import pytest

from app.extensions import db, limiter
from app.models.order import Order
from app.models.table import DiningTable
from app.models.user import StaffAuditLog
from tests.support import checkout, default_tenant_id, line, login_staff, make_item, run_together

ROUNDS = max(1, int(os.environ.get('TABLE_CONFLICT_ROUNDS', '6')))


@pytest.fixture(autouse=True)
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture(scope='module')
def item(app):
    return make_item(app, '衝突測試餐', price=100, stock=100000)


def _safe(fn):
    """執行緒內的例外轉成結果 (Flask 測試模式會直接拋出伺服器錯誤)"""
    def run():
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            return exc
    return run


def _status(resp):
    return resp if isinstance(resp, Exception) else resp.status_code


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
        o = db.session.get(Order, order_id)
        return o.status, o.payment_status, o.dining_table_id


def _unpaid_ids(staff):
    tables = staff.get('/api/pos/today_orders').get_json()['unpaid_tables']
    return {o['id'] for t in tables for o in t['orders']}


def test_concurrent_settlement_of_same_table(app, item):
    for rnd in range(ROUNDS):
        table_id, token = _new_table(app, f'C結帳{rnd}')
        phone = _phone(app, token)
        ids = [checkout(phone, [line(item)]).get_json()['order_id'] for _ in range(3)]
        cashiers = [login_staff(app, f'cc_pos_{i}') for i in range(4)]
        results = run_together(*[_safe(lambda c=c: c.post(f'/api/pos/tables/{table_id}/settle',
                                                          json={'payment_method': 'Cash'})) for c in cashiers])
        codes = sorted(_status(r) for r in results)
        assert codes == [200, 409, 409, 409], f'第 {rnd} 輪：{codes}'
        assert all(_order(app, i)[1] == 'settled' for i in ids)
        with app.app_context():
            audits = StaffAuditLog.query.filter(StaffAuditLog.action_type == 'TABLE_SETTLE',
                                                StaffAuditLog.details.like(f'C結帳{rnd} 桌結帳%')).count()
        assert audits == 1, '同一桌只記錄一次結帳'


def test_settlement_and_cancellation_at_the_same_time(app, item):
    login_staff(app, 'cc_mgr', 'manager')
    for rnd in range(ROUNDS):
        table_id, token = _new_table(app, f'C取消{rnd}')
        phone = _phone(app, token)
        order_id = checkout(phone, [line(item)]).get_json()['order_id']
        cashier, manager = login_staff(app, 'cc_pos_0'), login_staff(app, 'cc_mgr', 'manager')
        settle, cancel = run_together(
            _safe(lambda: cashier.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash'})),
            _safe(lambda: manager.post(f'/api/orders/{order_id}/status', json={'status': 'Cancelled'})))
        assert _status(settle) in (200, 409) and _status(cancel) == 200, (settle, cancel)
        status, payment, _ = _order(app, order_id)
        assert status == 'Cancelled'
        assert payment in ('unpaid', 'refund_required'), f'第 {rnd} 輪：已取消的訂單不可維持已結帳 ({payment})'
        assert (payment == 'refund_required') == (_status(settle) == 200), '有收款才需要退款'


def test_order_submission_while_table_is_deleted(app, item):
    admin = login_staff(app, 'cc_owner_del', 'owner')
    cashier = login_staff(app, 'cc_pos_0')
    for rnd in range(ROUNDS):
        table_id, token = _new_table(app, f'C刪除{rnd}')
        phone = _phone(app, token)
        order, delete = run_together(
            _safe(lambda: checkout(phone, [line(item)])),
            _safe(lambda: admin.post('/admin/tables', data={'action': 'delete', 'table_id': table_id})))
        assert not isinstance(order, Exception) and not isinstance(delete, Exception), (order, delete)
        assert _status(order) in (200, 409), order.get_data(as_text=True)
        with app.app_context():
            table_left = db.session.get(DiningTable, table_id) is not None
        if _status(order) == 200:
            order_id = order.get_json()['order_id']
            assert table_left, f'第 {rnd} 輪：桌位有未結帳訂單時不可被刪除'
            assert order_id in _unpaid_ids(cashier), 'POS 必須看得到這張未結帳訂單'
            cashier.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash'})


def test_duplicate_table_names_from_two_managers(app):
    managers = [login_staff(app, f'cc_mgr_{i}', 'manager') for i in range(3)]
    for rnd in range(ROUNDS):
        names = f'DUP{rnd}A, DUP{rnd}B'
        results = run_together(*[_safe(lambda m=m: m.post('/admin/tables', data={'action': 'create', 'names': names}))
                                 for m in managers])
        assert all(_status(r) == 302 for r in results), [_status(r) for r in results]
        with app.app_context():
            assert DiningTable.query.filter(DiningTable.name.in_([f'DUP{rnd}A', f'DUP{rnd}B'])).count() == 2

    a, _ = _new_table(app, 'R甲')
    b, _ = _new_table(app, 'R乙')
    results = run_together(
        _safe(lambda: managers[0].post('/admin/tables', data={'action': 'rename', 'table_id': a, 'name': 'R同名'})),
        _safe(lambda: managers[1].post('/admin/tables', data={'action': 'rename', 'table_id': b, 'name': 'R同名'})))
    assert all(_status(r) == 302 for r in results), [_status(r) for r in results]
    with app.app_context():
        assert DiningTable.query.filter_by(name='R同名').count() == 1


def test_qr_rotation_while_ordering(app, item):
    manager = login_staff(app, 'cc_mgr', 'manager')
    for rnd in range(ROUNDS):
        table_id, token = _new_table(app, f'C換碼{rnd}')
        phone = _phone(app, token)
        order, rotate = run_together(
            _safe(lambda: checkout(phone, [line(item)])),
            _safe(lambda: manager.post('/admin/tables', data={'action': 'rotate', 'table_id': table_id})))
        assert _status(order) in (200, 409) and _status(rotate) == 302, (order, rotate)
        assert checkout(phone, [line(item)]).status_code == 409, '換碼後舊的綁定一定失效'


def test_many_phones_and_settlement_never_lose_an_order(app, item):
    cashier = login_staff(app, 'cc_pos_0')
    for rnd in range(ROUNDS):
        table_id, token = _new_table(app, f'C多人{rnd}')
        phones = [_phone(app, token) for _ in range(4)]
        jobs = [_safe(lambda p=p: checkout(p, [line(item)])) for p in phones]
        jobs.append(_safe(lambda: cashier.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash'})))
        results = run_together(*jobs)
        assert not any(isinstance(r, Exception) for r in results), results
        order_ids = [r.get_json()['order_id'] for r in results[:4] if r.status_code == 200]
        assert len(order_ids) == 4
        unpaid = _unpaid_ids(cashier)
        for oid in order_ids:
            payment = _order(app, oid)[1]
            assert payment == 'settled' or (payment == 'unpaid' and oid in unpaid), f'第 {rnd} 輪：訂單 #{oid} 遺漏'
        cashier.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash'})


def test_hub_cancel_and_branch_settlement_conflict():
    """線上端取消分店的桌邊訂單、分店同時已在櫃檯結帳：同步後兩邊都是「已取消 + 需退款」"""
    from app.models.tenant import StaffAccount
    from app.services import table_service
    from app.services.order_service import change_order_status, process_checkout
    from tests.test_sync_hub_node import Env, _add_item, tenant_ctx
    env = Env()
    try:
        env.sync(env.a)
        item_uuid = _add_item(env.a, '同步衝突餐', price=80, stock=50)
        with tenant_ctx(env.a) as tenant:
            from app.models.menu import MenuItem
            table = DiningTable(tenant_id=tenant.id, name='S衝突')
            db.session.add(table)
            db.session.commit()
            item_id = MenuItem.query.filter_by(uuid=item_uuid).first().id
            order, _ = process_checkout(tenant_id=tenant.id, items=[line(item_id)], dining_table=table)
            order_uuid, table_id = order.uuid, table.id
        env.sync(env.a)
        # 兩邊在下一次同步前各自操作
        with tenant_ctx(env.hub) as tenant:
            hub_order = Order.query.filter_by(uuid=order_uuid).first()
            owner = StaffAccount(tenant_id=tenant.id, username='hub_cc_owner', role='owner', password_hash='x')
            change_order_status(hub_order.id, 'Cancelled', owner, tenant.id)
        with tenant_ctx(env.a) as tenant:
            cashier = StaffAccount(tenant_id=tenant.id, username='a_cc_cashier', role='staff', password_hash='x')
            table_service.settle_table(tenant.id, table_id, 'Cash', 0, cashier)
        env.sync(env.a, rounds=2)
        for side in (env.hub, env.a):
            with side.app_context():
                o = Order.query.filter_by(uuid=order_uuid).first()
                assert (o.status, o.payment_status) == ('Cancelled', 'refund_required'), (side.name, o.payment_status)
    finally:
        env.close()


def test_conflict_check_command_finds_and_fixes(app, item):
    """flask --app run tables check：找出沒有桌位的未結帳訂單、已取消卻已收款、久未結帳；--fix 修正可自動修正的項目"""
    from datetime import datetime, timedelta
    table_id, token = _new_table(app, 'C檢查')
    phone = _phone(app, token)
    ids = [checkout(phone, [line(item)]).get_json()['order_id'] for _ in range(3)]
    orphan, cancelled_paid, stale = ids
    with app.app_context():
        db.session.get(Order, orphan).dining_table_id = None
        o = db.session.get(Order, cancelled_paid)
        o.status, o.payment_status = 'Cancelled', 'settled'
        db.session.get(Order, stale).created_at = datetime.now() - timedelta(days=2)
        db.session.commit()
    runner = app.test_cli_runner()
    result = runner.invoke(args=['tables', 'check'])
    assert result.exit_code == 1, result.output
    for kind, oid in (('orphan', orphan), ('cancelled_paid', cancelled_paid), ('stale_unpaid', stale)):
        assert f'[{kind}]' in result.output and f'#{oid} ' in result.output, (kind, result.output)
    result = runner.invoke(args=['tables', 'check', '--fix'])
    assert result.exit_code == 0 and '已自動修正' in result.output
    assert _order(app, cancelled_paid)[1] == 'refund_required'
    with app.app_context():  # 清理：讓之後的檢查不受這幾張測試訂單影響
        for oid in ids:
            o = db.session.get(Order, oid)
            o.status = 'Cancelled'
            o.payment_status = 'refund_required' if oid == cancelled_paid else 'unpaid'
        db.session.commit()
    assert '[orphan]' not in runner.invoke(args=['tables', 'check']).output
