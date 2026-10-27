"""第四輪：一鍵展示資料 (flask demo seed / remove)：獨立的示範店家、各報表都有內容、不影響其他店家"""
from contextlib import contextmanager
from datetime import date, timedelta

from app.extensions import db
from app.models.expense import CostHistory, Expense
from app.models.group_order import GroupOrder
from app.models.order import Order, OrderFeedback
from app.models.settings import StoreSetting
from app.models.tenant import StaffAccount, Tenant
from app.models.user import User
from app.services import demo_service

PASSWORD = 'DemoSeed!2026'


@contextmanager
def _ctx(app, tenant_id):
    from app.tenant_context import _use_tenant
    with app.test_request_context('/'):
        _use_tenant(db.session.get(Tenant, tenant_id), remember=False)
        yield


def _counts_outside(app, tenant_id):
    """示範店家以外的資料量 (訂單、會員、員工、設定)"""
    with app.app_context():
        return tuple(m.query.execution_options(skip_tenant_filter=True).filter(m.tenant_id != tenant_id).count()
                     for m in (Order, User, StaffAccount, StoreSetting, Expense))


def _rows_of(app, tenant_id):
    with app.app_context():
        return sum(len(db.session.execute(table.select().where(table.c.tenant_id == tenant_id)).all())
                   for table in db.metadata.sorted_tables if 'tenant_id' in table.c)


def test_demo_seed_reports_reset_and_remove(app):
    runner = app.test_cli_runner()
    before = _counts_outside(app, -1)
    r = runner.invoke(args=['demo', 'seed', '--days', '4', '--orders-per-day', '12', '--password', PASSWORD])
    assert r.exit_code == 0, r.output
    assert '示範店家「示範店家」已建立' in r.output and 'demo_owner' in r.output and PASSWORD in r.output
    assert 'PIN 2468' in r.output
    with app.app_context():
        tenant = Tenant.query.filter_by(slug='demo').one()
        tid, tenant_uuid = tenant.id, tenant.uuid
        old_orders = [o.uuid for o in Order.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=tid)]
        owner = StaffAccount.query.filter_by(username='demo_owner').one()
        assert owner.tenant_id == tid and owner.role == 'owner' and owner.check_password(PASSWORD)
        member = User.query.filter_by(tenant_id=tid, phone='0900000001').one()
        assert member.check_pin('2468')
    assert _counts_outside(app, tid) == before   # 其他店家完全沒變

    today = date.today()
    start = today - timedelta(days=4)
    from app.services import (accounting_service, marketing_service, menu_engineering_service, pnl_service,
                              report_service, staff_anomaly_service, staffing_service)
    with _ctx(app, tid):
        orders = Order.query.filter_by(tenant_id=tid).all()
        assert len(orders) >= 30
        assert {o.order_source for o in orders} >= {'Kiosk', 'POS'} and any(o.user_id for o in orders)
        assert any(o.status == 'Cancelled' for o in orders) and any(o.status == 'PickedUp' for o in orders)
        rep = report_service.build_report(tid, start, today)
        assert rep['order_count'] > 0 and rep['cogs'] > 0 and rep['labor_cost'] > 0 and len(rep['by_method']) >= 3
        pnl = pnl_service.statement(tid, today.replace(day=1) if today.day > 4 else start, today)
        assert pnl['expense_total'] > 0 and pnl['fee_total'] > 0
        journal = accounting_service.journal(tid, start, today)
        assert journal and all(v['debit'] == v['credit'] for v in journal)
        me = menu_engineering_service.analyze(tid, start, today)
        assert sum(me['counts'].values()) >= 8 and not me['missing_cost']
        anomaly = staff_anomaly_service.analyze(tid, start, today)
        assert {r['name'] for r in anomaly['staff']} >= {'demo_pos', 'demo_pos2'}
        assert staffing_service.plan(tid, today + timedelta(days=1))['have_hours'] > 0
        assert marketing_service.member_stats(tid)
        assert CostHistory.query.filter_by(tenant_id=tid).count() == 4 and pnl_service.cost_impact(tid, 30)
        assert OrderFeedback.query.filter_by(tenant_id=tid).count() >= 5
        grp = GroupOrder.query.filter_by(tenant_id=tid).one()
        assert grp.status == 'open' and len(grp.entries) == 3
        # 收款紀錄的時間跟著訂單 (不是建立資料的當下)
        from app.models.payment import PaymentRecord
        assert PaymentRecord.query.filter(PaymentRecord.tenant_id == tid,
                                          PaymentRecord.created_at < start + timedelta(days=1)).count() > 0

    # 管理頁面可以用示範帳號登入檢視
    from tests.support import login_staff
    owner_client = login_staff(app, 'demo_owner')
    for path in ('/admin/pnl', '/admin/reports', '/admin/reports/menu-engineering', '/admin/staff-anomalies',
                 '/admin/accounting', '/admin/staffing', '/admin/group-orders', '/admin/live', '/admin/forecast',
                 '/admin/cost-history', '/admin/expenses'):
        resp = owner_client.get(path)
        assert resp.status_code == 200, path

    # 已存在：不加 --reset 拒絕；--reset 重建 (舊資料全部刪除)
    again = runner.invoke(args=['demo', 'seed', '--days', '1'])
    assert again.exit_code != 0 and '已存在' in again.output
    r2 = runner.invoke(args=['demo', 'seed', '--days', '2', '--orders-per-day', '5', '--reset', '--password', PASSWORD])
    assert r2.exit_code == 0, r2.output
    with app.app_context():
        new = Tenant.query.filter_by(slug='demo').one()
        new_tid = new.id
        assert new.uuid != tenant_uuid   # SQLite 可能重用相同的 id，以 uuid 判斷是新的店家
        assert Order.query.execution_options(skip_tenant_filter=True).filter(Order.uuid.in_(old_orders)).count() == 0
        assert Order.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=new_tid).count() < len(old_orders)
    assert _counts_outside(app, new_tid) == before

    # 刪除
    assert runner.invoke(args=['demo', 'remove', '--yes']).output.startswith('已刪除')
    with app.app_context():
        assert Tenant.query.filter_by(slug='demo').first() is None
        assert StaffAccount.query.filter(StaffAccount.username.like('demo\\_%', escape='\\')).count() == 0
    assert _rows_of(app, new_tid) == 0 and _counts_outside(app, -1) == before
    assert '找不到示範店家' in runner.invoke(args=['demo', 'remove', '--yes']).output


def test_demo_seed_refuses_real_tenant_node_and_bad_options(app, monkeypatch):
    runner = app.test_cli_runner()
    with app.app_context():
        db.session.add(Tenant(name='真的店', slug='realdemo', is_active=True))
        db.session.commit()
    r = runner.invoke(args=['demo', 'seed', '--slug', 'realdemo', '--days', '1', '--reset'])
    assert r.exit_code != 0 and '真實店家' in r.output
    r = runner.invoke(args=['demo', 'remove', '--slug', 'realdemo', '--yes'])
    assert r.exit_code != 0 and '真實店家' in r.output
    with app.app_context():
        assert Tenant.query.filter_by(slug='realdemo').one().is_active
    assert '至少 10 個字元' in runner.invoke(args=['demo', 'seed', '--password', 'short']).output
    assert '1 ~ 400' in runner.invoke(args=['demo', 'seed', '--days', '0']).output
    from app.sync import runtime
    monkeypatch.setattr(runtime, 'is_node', lambda: True)
    assert '分店主機與線上端不可執行' in runner.invoke(args=['demo', 'seed', '--days', '1']).output
    monkeypatch.undo()
    monkeypatch.setattr(runtime, 'is_hub', lambda: True)
    assert '分店主機與線上端不可執行' in runner.invoke(args=['demo', 'seed', '--days', '1']).output
    monkeypatch.undo()
    # 建到一半失敗：整個示範店家刪除，不留下標記 (之後可直接重建)
    def boom(*a, **kw):
        raise RuntimeError('模擬失敗')
    monkeypatch.setattr(demo_service, '_expenses', boom)
    r = runner.invoke(args=['demo', 'seed', '--slug', 'broken', '--days', '1', '--orders-per-day', '2'])
    assert r.exit_code != 0 and isinstance(r.exception, RuntimeError)
    monkeypatch.undo()
    with app.app_context():
        assert Tenant.query.filter_by(slug='broken').first() is None
        assert StaffAccount.query.filter_by(username='broken_owner').first() is None
    # 帳號名稱撞到其他店家的員工：整個不建立
    from tests.support import login_staff
    login_staff(app, 'clash_owner', role='owner')
    r = runner.invoke(args=['demo', 'seed', '--slug', 'clash', '--days', '1', '--orders-per-day', '2'])
    assert r.exit_code != 0 and 'clash_owner 已被其他店家使用' in r.output
    with app.app_context():
        assert Tenant.query.filter_by(slug='clash').first() is None
    assert demo_service.DEFAULT_SLUG == 'demo'
