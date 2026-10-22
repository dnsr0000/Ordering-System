"""
線上端 (hub) 與分店 (node) 同步的整合測試。

在同一個行程中建立一個線上端與兩個分店 (各自獨立的 SQLite 資料庫與上傳資料夾)，
分店透過 Flask test client 呼叫線上端的同步 API (與正式環境走 HTTPS 的程式路徑相同)。
設定 TEST_SYNC_HUB_DATABASE_URL 可讓線上端改用 PostgreSQL (該資料庫會被清空)。
"""
import os
import shutil
import tempfile
from contextlib import contextmanager

import pytest

from tests.conftest import TestConfig
from tests.support import jpeg_bytes

HUB_DB_URL = os.environ.get('TEST_SYNC_HUB_DATABASE_URL')


# ---------------------------------------------------------------------------
# 測試環境
# ---------------------------------------------------------------------------
def _make_app(root, name, role, **extra):
    from app import create_app
    from app.config import normalize_database_url
    base = os.path.join(root, name)
    for sub in ('database', 'member', 'menu'):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    db_url = 'sqlite:///' + os.path.join(base, 'database', 'menu.db')
    if role == 'hub' and HUB_DB_URL:
        from tests.conftest import _reset_postgres
        db_url = normalize_database_url(HUB_DB_URL)
        _reset_postgres(db_url)

    cfg = type(f'{name}Config', (TestConfig,), dict(
        SQLALCHEMY_DATABASE_URI=db_url, DATABASE_DIR=os.path.join(base, 'database'),
        UPLOAD_FOLDER_MEMBER=os.path.join(base, 'member'), UPLOAD_FOLDER_MENU=os.path.join(base, 'menu'),
        SYNC_ROLE=role, SYNC_AUTOSTART=False, SYNC_TENANT_SLUG='default', SYNC_LONG_POLL=0, **extra))
    return create_app(cfg)


class ClientTransport:
    """以 Flask test client 呼叫線上端 (取代 HttpTransport)"""

    def __init__(self, hub_app, token):
        self.client = hub_app.test_client()
        self.headers = {'Authorization': f'Bearer {token}'}

    def call(self, method, path, params=None, json_body=None, data=None, timeout=None, raw=False):
        from app.sync.node import SyncError
        resp = self.client.open('/api/sync/v1' + path, method=method, query_string=params, json=json_body,
                                data=data, headers=self.headers)
        if raw and resp.status_code == 200:
            return resp.data
        body = resp.get_json(silent=True) or {}
        if resp.status_code >= 400 or body.get('success') is False:
            raise SyncError(body.get('message') or f'HTTP {resp.status_code}')
        return body


@contextmanager
def tenant_ctx(app, slug='default'):
    """在指定店家的請求情境中執行 (查詢會自動加上店家條件)"""
    from app.models.tenant import Tenant
    from app.tenant_context import _use_tenant
    with app.test_request_context('/'):
        tenant = Tenant.query.filter_by(slug=slug).first()
        _use_tenant(tenant, remember=False)
        yield tenant


class Env:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix='kiosk_sync_')
        self.hub = _make_app(self.root, 'hub', 'hub')
        self.a = _make_app(self.root, 'node_a', 'node')
        self.b = _make_app(self.root, 'node_b', 'node')
        from app.sync.nodes import create_node
        with self.hub.app_context():
            from app.models.tenant import Tenant
            tid = Tenant.query.filter_by(slug='default').first().id
            self.node_a, token_a = create_node(tid, '台南店')
            self.node_b, token_b = create_node(tid, '高雄店')
            self.node_a_uuid, self.node_b_uuid = self.node_a.uuid, self.node_b.uuid
        self.transports = {self.a: ClientTransport(self.hub, token_a), self.b: ClientTransport(self.hub, token_b)}

    def sync(self, node_app, rounds=1):
        from app.sync.node import NodeSync
        totals = []
        for _ in range(rounds):
            with node_app.app_context():
                totals.append(NodeSync(self.transports[node_app]).run_once())
        return totals

    def sync_all(self, rounds=2):
        for _ in range(rounds):
            self.sync(self.a)
            self.sync(self.b)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture(scope='module')
def env():
    e = Env()
    yield e
    e.close()


def _q(app, fn):
    with app.app_context():
        return fn()


def _add_item(app, name, price=100, stock=30, **kw):
    from app.extensions import db
    from app.models.menu import MenuItem
    with tenant_ctx(app):
        item = MenuItem(name=name, category=kw.pop('category', '主餐'), price=price, stock=stock, total_stock=stock, **kw)
        db.session.add(item)
        db.session.commit()
        return item.uuid


def _item(app, uuid):
    from app.models.menu import MenuItem
    with app.app_context():
        item = MenuItem.query.filter_by(uuid=uuid).first()
        return None if item is None else dict(id=item.id, name=item.name, price=item.price, stock=item.stock,
                                              is_sold_out=item.is_sold_out, image_path=item.image_path)


def _member(app, phone):
    from app.models.user import User
    with app.app_context():
        user = User.query.filter_by(phone=phone).first()
        return None if user is None else dict(id=user.id, uuid=user.uuid, name=user.name, points=user.points)


def _register_member(app, name, phone, points=20):
    from app.extensions import db
    from app.models.user import User
    with tenant_ctx(app):
        user = User(name=name, phone=phone, photo_path='', points=points)
        user.set_pin('2468')
        db.session.add(user)
        db.session.commit()
        return user.uuid


def _checkout(app, items, pay=True, **kw):
    """結帳。線上端只限會員 (預設用台南老客人)，並模擬綠界付款成功通知 (pay=False 則保持待付款)"""
    from app.services.order_service import process_checkout
    is_hub = app.config['SYNC_ROLE'] == 'hub'
    if is_hub and 'user_id' not in kw:
        kw['user_id'] = _member(app, '0911000001')['id']
    with tenant_ctx(app) as tenant:
        order, _ = process_checkout(tenant_id=tenant.id, items=items, **kw)
        result = dict(id=order.id, uuid=order.uuid, pickup=order.pickup_number, total=order.total_price,
                      node_uuid=order.node_uuid, status=order.status, payment_status=order.payment_status)
    if is_hub and pay:
        _pay(app, result)
    return result


def _pay(app, order):
    from app.services.payment_ecpay import mark_paid
    with app.test_request_context('/'):
        assert mark_paid(order['uuid'], 'TEST' + order['uuid'][:8], order['total']) == 'paid'


def _order(app, uuid):
    from app.models.order import Order
    with app.app_context():
        o = Order.query.filter_by(uuid=uuid).first()
        return None if o is None else dict(id=o.id, status=o.status, pickup=o.pickup_number, node_uuid=o.node_uuid,
                                           source=o.order_source, items=[i.item_name for i in o.items],
                                           payment_status=o.payment_status)


# ---------------------------------------------------------------------------
# 測試 (情境依序進行)
# ---------------------------------------------------------------------------
def test_sync_api_requires_node_token(env, app):
    hub = env.hub.test_client()
    assert hub.post('/api/sync/v1/hello').status_code == 401
    assert hub.post('/api/sync/v1/hello', headers={'Authorization': 'Bearer nope'}).status_code == 401
    # 單機模式 (預設) 沒有同步 API
    assert app.test_client().post('/api/sync/v1/hello', headers={'Authorization': 'Bearer x'}).status_code == 404


def test_first_sync_adopts_hub_tenant_and_uploads_existing_data(env):
    # 分店 A 在接上線上端之前就已經營業：有菜單、會員 (含點數) 與訂單
    burger = _add_item(env.a, '同步牛肉堡', price=120)
    _register_member(env.a, '台南老客人', '0911000001', points=50)
    from tests.support import line
    with env.a.app_context():
        from app.models.menu import MenuItem
        item_id = MenuItem.query.filter_by(uuid=burger).first().id
    order = _checkout(env.a, [line(item_id, 2)])
    assert order['node_uuid'] is None  # 尚未連線時建立的訂單

    env.sync(env.a)

    from app.models.tenant import Tenant
    hub_tenant = _q(env.hub, lambda: Tenant.query.filter_by(slug='default').first().uuid)
    assert _q(env.a, lambda: Tenant.query.filter_by(slug='default').first().uuid) == hub_tenant
    assert _item(env.hub, burger)['price'] == 120
    assert _member(env.hub, '0911000001')['points'] == 50
    hub_order = _order(env.hub, order['uuid'])
    assert hub_order['node_uuid'] == env.node_a_uuid and len(hub_order['items']) == 1


def test_other_branch_receives_menu_and_members_but_not_orders(env):
    env.sync_all()
    from app.models.menu import ModifierOption
    from app.models.order import Order
    from app.models.user import RewardSetting
    burger = _q(env.a, lambda: __import__('app.models.menu', fromlist=['MenuItem']).MenuItem.query
                .filter_by(name='同步牛肉堡').first().uuid)
    assert _item(env.b, burger)['name'] == '同步牛肉堡'
    assert _member(env.b, '0911000001')['points'] == 50
    assert _q(env.b, lambda: Order.query.count()) == 0, '其他分店的訂單不應下發'
    for node_app in (env.hub, env.a, env.b):
        # 各自建立的預設加料與紅利設定合併為同一份 (不會重複)
        names = _q(node_app, lambda: sorted(m.name for m in ModifierOption.query.all()))
        assert names == ['加荷包蛋', '加起司片'], names
        assert _q(node_app, lambda: RewardSetting.query.count()) == 1


def test_same_phone_registered_at_two_branches_is_merged(env):
    # 斷線期間兩家分店各自註冊同一支手機
    _register_member(env.a, '王小明', '0922000002', points=20)
    _register_member(env.b, '王小明 (高雄)', '0922000002', points=20)
    env.sync_all(rounds=3)
    uuids = {_member(x, '0922000002')['uuid'] for x in (env.hub, env.a, env.b)}
    assert len(uuids) == 1, '三邊應為同一位會員'
    for x in (env.hub, env.a, env.b):
        assert _member(x, '0922000002')['points'] == 40, '兩邊的註冊禮點數都保留'


def test_points_spent_offline_at_both_branches_are_merged(env):
    from app.services.points_service import change_points
    for node_app, delta in ((env.a, -15), (env.b, -10)):
        with tenant_ctx(node_app) as tenant:
            from app.extensions import db
            from app.models.user import User
            uid = User.query.filter_by(phone='0911000001').first().id
            assert change_points(tenant.id, uid, delta, 'test') == delta
            db.session.commit()
    env.sync_all(rounds=2)
    for x in (env.hub, env.a, env.b):
        assert _member(x, '0911000001')['points'] == 25


def test_hub_menu_edit_reaches_branches_without_touching_branch_stock(env):
    from app.extensions import db
    from app.models.menu import MenuItem
    uuid = _q(env.a, lambda: MenuItem.query.filter_by(name='同步牛肉堡').first().uuid)
    with tenant_ctx(env.b):
        MenuItem.query.filter_by(uuid=uuid).first().stock = 7  # 高雄店自己的庫存
        db.session.commit()
    with tenant_ctx(env.hub):
        MenuItem.query.filter_by(uuid=uuid).first().price = 135
        db.session.commit()
    env.sync_all()
    assert _item(env.a, uuid)['price'] == 135 and _item(env.b, uuid)['price'] == 135
    assert _item(env.b, uuid)['stock'] == 7, '線上端的菜單修改不可覆蓋分店庫存'


def test_online_order_flows_to_branch_and_back(env):
    from app.models.menu import MenuItem
    from tests.support import line
    burger_uuid = _q(env.a, lambda: MenuItem.query.filter_by(name='同步牛肉堡').first().uuid)
    env.sync(env.b)  # 高雄店回報庫存 (7 份)
    hub_item_id = _item(env.hub, burger_uuid)['id']

    from app.errors import AppError
    with pytest.raises(AppError, match='庫存'):
        _checkout(env.hub, [line(hub_item_id, 8)], node_uuid=env.node_b_uuid)  # 超過高雄店庫存
    with pytest.raises(AppError, match='分店'):
        _checkout(env.hub, [line(hub_item_id, 1)])  # 沒選取餐分店
    with pytest.raises(AppError, match='會員'):
        _checkout(env.hub, [line(hub_item_id, 1)], node_uuid=env.node_b_uuid, user_id=None)  # 線上只限會員

    order = _checkout(env.hub, [line(hub_item_id, 2)], node_uuid=env.node_b_uuid, order_type='外帶', pay=False)
    assert order['pickup'] == 0 and order['node_uuid'] == env.node_b_uuid and order['payment_status'] == 'pending'
    env.sync(env.b)
    assert _order(env.b, order['uuid']) is None, '尚未付款的線上訂單不可送到分店'

    _pay(env.hub, order)
    env.sync(env.b)
    at_branch = _order(env.b, order['uuid'])
    assert at_branch['source'] == 'Online' and at_branch['pickup'] > 0, '分店收到後編取餐號'
    assert _item(env.b, burger_uuid)['stock'] == 5, '分店扣庫存'
    assert _order(env.a, order['uuid']) is None, '只下發給取餐分店'

    env.sync(env.b)  # 取餐號上傳
    assert _order(env.hub, order['uuid'])['pickup'] == at_branch['pickup']

    # 分店出餐 → 線上看得到狀態
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    with tenant_ctx(env.b) as tenant:
        staff = StaffAccount(tenant_id=tenant.id, username='b_kitchen', role='kitchen', password_hash='x')
        change_order_status(at_branch['id'], 'Completed', staff, tenant.id)
    env.sync(env.b)
    assert _order(env.hub, order['uuid'])['status'] == 'Completed'


def test_branch_cancel_restores_branch_stock_and_hub_status(env):
    from app.models.menu import MenuItem
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    from tests.support import line
    burger_uuid = _q(env.a, lambda: MenuItem.query.filter_by(name='同步牛肉堡').first().uuid)
    env.sync(env.b)
    order = _checkout(env.hub, [line(_item(env.hub, burger_uuid)['id'], 1)], node_uuid=env.node_b_uuid)
    env.sync(env.b)
    stock_before = _item(env.b, burger_uuid)['stock']
    order_id = _order(env.b, order['uuid'])['id']
    with tenant_ctx(env.b) as tenant:
        manager = StaffAccount(tenant_id=tenant.id, username='b_mgr', role='manager', password_hash='x')
        change_order_status(order_id, 'Cancelled', manager, tenant.id)
    assert _item(env.b, burger_uuid)['stock'] == stock_before + 1
    env.sync(env.b)
    assert _order(env.hub, order['uuid'])['status'] == 'Cancelled'
    assert _order(env.hub, order['uuid'])['payment_status'] == 'refund_required', '已付款的訂單被分店取消：線上端標記需退款'


def test_hub_cancel_reaches_branch_and_restores_branch_stock(env):
    from app.models.menu import MenuItem
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    from tests.support import line
    burger_uuid = _q(env.a, lambda: MenuItem.query.filter_by(name='同步牛肉堡').first().uuid)
    env.sync(env.b)
    order = _checkout(env.hub, [line(_item(env.hub, burger_uuid)['id'], 1)], node_uuid=env.node_b_uuid)
    env.sync(env.b)
    stock_after_order = _item(env.b, burger_uuid)['stock']
    order_id = _order(env.hub, order['uuid'])['id']
    with tenant_ctx(env.hub) as tenant:
        owner = StaffAccount(tenant_id=tenant.id, username='hub_owner_x', role='owner', password_hash='x')
        change_order_status(order_id, 'Cancelled', owner, tenant.id)
    assert _order(env.hub, order['uuid'])['payment_status'] == 'refund_required', '已付款的訂單被取消：標記需退款'
    env.sync(env.b)
    assert _order(env.b, order['uuid'])['status'] == 'Cancelled'
    assert _item(env.b, burger_uuid)['stock'] == stock_after_order + 1


def test_member_points_from_online_order_reach_branches(env):
    from app.models.menu import MenuItem
    from tests.support import line
    burger_uuid = _q(env.a, lambda: MenuItem.query.filter_by(name='同步牛肉堡').first().uuid)
    env.sync(env.b)
    user_id = _member(env.hub, '0922000002')['id']
    before = _member(env.hub, '0922000002')['points']
    order = _checkout(env.hub, [line(_item(env.hub, burger_uuid)['id'], 1)], node_uuid=env.node_b_uuid,
                      user_id=user_id)
    earned = order['total'] // 100
    assert _member(env.hub, '0922000002')['points'] == before + earned
    env.sync_all()
    assert _member(env.a, '0922000002')['points'] == before + earned


def test_delete_on_hub_propagates(env):
    from app.extensions import db
    from app.models.coupon import Coupon
    with tenant_ctx(env.hub):
        db.session.add(Coupon(code='SYNCDEL', title='即將刪除', discount_type='fixed', discount_value=10))
        db.session.commit()
    env.sync_all(rounds=1)
    assert _q(env.a, lambda: Coupon.query.filter_by(code='SYNCDEL').count()) == 1
    with tenant_ctx(env.hub):
        db.session.delete(Coupon.query.filter_by(code='SYNCDEL').first())
        db.session.commit()
    env.sync_all(rounds=1)
    for x in (env.a, env.b):
        assert _q(x, lambda: Coupon.query.filter_by(code='SYNCDEL').count()) == 0


def test_staff_username_taken_by_other_store_is_reported_as_conflict(env):
    from app.extensions import db
    from app.models.sync import SyncConflict
    from app.models.tenant import StaffAccount, Tenant
    with env.hub.app_context():
        other = Tenant(name='別家店', slug='other-store')
        db.session.add(other)
        db.session.flush()
        db.session.add(StaffAccount(tenant_id=other.id, username='dup_name', role='staff', password_hash='x'))
        db.session.commit()
    with tenant_ctx(env.a) as tenant:
        db.session.add(StaffAccount(tenant_id=tenant.id, username='dup_name', role='staff', password_hash='x'))
        db.session.commit()
    env.sync(env.a)
    conflicts = _q(env.a, lambda: [c.reason for c in SyncConflict.query.filter_by(table_name='staff_account')])
    assert conflicts and '其他店家' in conflicts[0]


def test_menu_images_are_synced_both_ways(env):
    from app.extensions import db
    from app.models.menu import MenuItem
    rel = '7/menu_sync_photo_a.jpg'
    path = os.path.join(env.a.config['UPLOAD_FOLDER_MENU'], '7')
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'menu_sync_photo_a.jpg'), 'wb') as fh:
        fh.write(jpeg_bytes().read())
    uuid = _add_item(env.a, '有圖片的餐點', image_path=rel)
    env.sync(env.a)
    assert os.path.isfile(os.path.join(env.hub.config['UPLOAD_FOLDER_MENU'], rel)), '分店上傳圖片到線上端'
    env.sync(env.b)
    assert os.path.isfile(os.path.join(env.b.config['UPLOAD_FOLDER_MENU'], rel)), '其他分店下載圖片'
    assert _item(env.b, uuid)['image_path'] == rel
    with tenant_ctx(env.hub):
        assert MenuItem.query.filter_by(uuid=uuid).first() is not None
        db.session.rollback()


def test_repeated_sync_is_quiet(env):
    env.sync_all(rounds=3)
    (pushed_a, pulled_a), = env.sync(env.a)
    (pushed_b, pulled_b), = env.sync(env.b)
    assert pushed_a == pushed_b == 0
    assert pulled_a == pulled_b == 0


def test_status_report(env):
    from app.sync.node import sync_status
    with env.a.app_context():
        status = sync_status()
    assert status['role'] == 'node' and status['node_name'] == '台南店'
    assert status['pending_changes'] == 0 and status['last_push_at']


def test_invariants_hold_on_every_database(env):
    import sqlalchemy as sa

    from app.extensions import db
    from app.models.sync import PointLedger
    from app.models.user import User
    from app.sync.registry import SPECS
    for node_app in (env.hub, env.a, env.b):
        with node_app.app_context():
            for spec in SPECS:
                t = spec.model.__table__
                assert db.session.execute(sa.select(sa.func.count()).select_from(t).where(t.c.sync_rev < 0)).scalar() == 0
            total = sa.select(sa.func.coalesce(sa.func.sum(PointLedger.delta), 0)).where(
                PointLedger.user_id == User.id).scalar_subquery()
            assert not db.session.execute(sa.select(User.id).where(User.points != total)).all()


def test_table_order_syncs_and_counter_settlement_reaches_hub(env):
    """門市桌邊掃碼點餐：訂單照常上傳 (未結帳)，桌位本身不同步；櫃檯結帳後線上端也顯示已結帳"""
    from app.extensions import db
    from app.models.menu import MenuItem
    from app.models.order import Order
    from app.models.table import DiningTable
    from app.models.tenant import StaffAccount
    from app.services import table_service
    from app.services.order_service import process_checkout
    from tests.support import line
    with tenant_ctx(env.a) as tenant:
        table = DiningTable(tenant_id=tenant.id, name='S1')
        db.session.add(table)
        db.session.commit()
        item_id = MenuItem.query.filter_by(name='同步牛肉堡').first().id
        order, _ = process_checkout(tenant_id=tenant.id, items=[line(item_id)], dining_table=table)
        order_uuid, table_id = order.uuid, table.id
    env.sync(env.a)
    with env.hub.app_context():
        o = Order.query.filter_by(uuid=order_uuid).first()
        assert (o.payment_status, o.order_source, o.dining_table_id) == ('unpaid', 'Table', None)
        assert DiningTable.query.count() == 0, '桌位是門市自己的資料，不同步'
    with tenant_ctx(env.a) as tenant:
        cashier = StaffAccount(tenant_id=tenant.id, username='a_cashier', role='staff', password_hash='x')
        table_service.settle_table(tenant.id, table_id, 'Line Pay', 0, cashier)
    env.sync(env.a)
    with env.hub.app_context():
        o = Order.query.filter_by(uuid=order_uuid).first()
        assert (o.payment_status, o.payment_method) == ('settled', 'Line Pay')
