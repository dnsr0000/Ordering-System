"""修正小組 sync-hub-node-branches-1 的回歸測試 (分店端同步 app/sync/node.py)

自行建立一個線上端與分店 (各自獨立的 SQLite 資料庫與上傳資料夾)，分店以 Flask test client 呼叫線上端的同步 API。
"""
import os
import shutil
import tempfile
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from tests.conftest import TestConfig
from tests.support import jpeg_bytes, line


def _make_app(root, name, role):
    from app import create_app
    base = os.path.join(root, name)
    for sub in ('database', 'member', 'menu'):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    cfg = type(f'Fix{name}Config', (TestConfig,), dict(
        SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(base, 'database', 'menu.db'),
        DATABASE_DIR=os.path.join(base, 'database'), UPLOAD_FOLDER_MEMBER=os.path.join(base, 'member'),
        UPLOAD_FOLDER_MENU=os.path.join(base, 'menu'), SYNC_ROLE=role, SYNC_AUTOSTART=False,
        SYNC_TENANT_SLUG='default', SYNC_LONG_POLL=0))
    return create_app(cfg)


class ClientTransport:
    def __init__(self, hub_app, token):
        self.client = hub_app.test_client()
        self.headers = {'Authorization': f'Bearer {token}'}
        self.fail_put = 0

    def call(self, method, path, params=None, json_body=None, data=None, timeout=None, raw=False):
        from app.sync.node import SyncError
        if method == 'PUT' and self.fail_put:
            self.fail_put -= 1
            raise SyncError('線上端暫時無法連線 (測試)')
        resp = self.client.open('/api/sync/v1' + path, method=method, query_string=params, json=json_body,
                                data=data, headers=self.headers)
        if raw and resp.status_code == 200:
            return resp.data
        body = resp.get_json(silent=True) or {}
        if resp.status_code >= 400 or body.get('success') is False:
            raise SyncError(body.get('message') or f'HTTP {resp.status_code}')
        return body


@contextmanager
def tenant_ctx(app):
    from app.models.tenant import Tenant
    from app.tenant_context import _use_tenant
    with app.test_request_context('/'):
        tenant = Tenant.query.filter_by(slug='default').first()
        _use_tenant(tenant, remember=False)
        yield tenant


class Env:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix='kiosk_fix_node_')
        self.hub = _make_app(self.root, 'hub', 'hub')
        self.a = _make_app(self.root, 'node_a', 'node')
        from app.sync.nodes import create_node
        with self.hub.app_context():
            from app.models.tenant import Tenant
            tid = Tenant.query.filter_by(slug='default').first().id
            node_a, self.token_a = create_node(tid, '台南店')
            node_x, self.token_x = create_node(tid, '另一家分店')
            self.node_a_uuid, self.node_x_uuid = node_a.uuid, node_x.uuid
        self.transports = {self.a: ClientTransport(self.hub, self.token_a)}
        from app.models.user import User
        from app.extensions import db
        with tenant_ctx(self.hub) as tenant:
            user = User(tenant_id=tenant.id, name='線上會員', phone='0977000001', photo_path='')
            db.session.add(user)
            db.session.commit()
            self.member_id = user.id

    def sync(self, node_app, rounds=1):
        from app.sync.node import NodeSync
        for _ in range(rounds):
            with node_app.app_context():
                NodeSync(self.transports[node_app]).run_once()

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


def _hub_item(env, name, price=100):
    """在線上端建立餐點，同步到分店並讓分店回報庫存 (之後才可線上點餐)"""
    from app.extensions import db
    from app.models.menu import MenuItem
    with tenant_ctx(env.hub):
        item = MenuItem(name=name, category='主餐', price=price, stock=50, total_stock=50)
        db.session.add(item)
        db.session.commit()
        uuid, hub_id = item.uuid, item.id
    env.sync(env.a, rounds=2)
    return uuid, hub_id


def _node_item(app, uuid):
    from app.models.menu import MenuItem
    with app.app_context():
        item = MenuItem.query.filter_by(uuid=uuid).first()
        return None if item is None else dict(id=item.id, stock=item.stock, price=item.price)


def _online_order(env, hub_item_id, qty=1, pay=True):
    from app.services.order_service import process_checkout
    with tenant_ctx(env.hub) as tenant:
        order, _ = process_checkout(tenant_id=tenant.id, items=[line(hub_item_id, qty)], user_id=env.member_id,
                                    node_uuid=env.node_a_uuid, order_type='外帶')
        info = dict(id=order.id, uuid=order.uuid, total=order.total_price)
    if pay:
        from app.services.payment_ecpay import mark_paid
        with env.hub.test_request_context('/'):
            assert mark_paid(info['uuid'], 'TEST' + info['uuid'][:8], info['total']) == 'paid'
    return info


def _node_order(app, uuid):
    from app.models.order import Order
    with app.app_context():
        o = Order.query.filter_by(uuid=uuid).first()
        return None if o is None else dict(id=o.id, status=o.status, pickup=o.pickup_number, source=o.order_source)


def _hub_cancel(env, order_id):
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    with tenant_ctx(env.hub) as tenant:
        owner = StaffAccount(tenant_id=tenant.id, username='fix_hub_owner', role='owner', password_hash='x')
        change_order_status(order_id, 'Cancelled', owner, tenant.id)


def _put_setting(app, key, values):
    from app.extensions import db
    from app.services import settings_service
    with tenant_ctx(app) as tenant:
        settings_service.put(key, values, tenant_id=tenant.id)
        db.session.commit()


# ---------------------------------------------------------------------------
def test_online_order_split_across_pages_consumes_stock_and_prints(env, monkeypatch):
    """B-188 / B-187：訂單與明細被切在不同分頁時仍扣庫存；收到後依設定自動出單"""
    from app.services import print_service
    from app.sync import node as node_mod
    uuid, hub_id = _hub_item(env, '分頁測試堡')
    printed = []
    monkeypatch.setattr(print_service, 'print_order', lambda order_id, tenant_id, receipt=None, kitchen=None:
                        printed.append(order_id) or 0)
    monkeypatch.setattr(node_mod, 'PAGE', 1)
    order = _online_order(env, hub_id, qty=3)
    env.sync(env.a)
    at_branch = _node_order(env.a, order['uuid'])
    assert at_branch['pickup'] > 0
    assert _node_item(env.a, uuid)['stock'] == 47, '明細在下一頁也要扣到 3 份'
    assert printed == [at_branch['id']], '線上訂單送到分店時自動出單'
    env.sync(env.a)
    assert printed == [at_branch['id']], '同一張訂單不重複處理'
    assert _node_item(env.a, uuid)['stock'] == 47


def test_fresh_host_resync_does_not_treat_history_as_new_online_orders(env):
    """B-186：換新主機 (空資料庫、同一組權杖) 重新下載時，門市的歷史訂單不扣庫存、不當成新到的線上訂單"""
    from app.services.order_service import process_checkout
    uuid, _hub_id = _hub_item(env, '換機測試堡')
    item_id = _node_item(env.a, uuid)['id']
    with tenant_ctx(env.a) as tenant:
        order, _ = process_checkout(tenant_id=tenant.id, items=[line(item_id, 4)])
        kiosk_uuid = order.uuid
    env.sync(env.a)
    c = _make_app(env.root, 'node_c', 'node')
    env.transports[c] = ClientTransport(env.hub, env.token_a)
    env.sync(c)
    assert _node_order(c, kiosk_uuid) is not None
    assert _node_item(c, uuid)['stock'] == 50, '歷史門市訂單不可再扣一次庫存'


def test_hub_cancel_respects_restock_on_cancel_and_restores_ingredients(env):
    """B-192 / B-189：線上端取消時依分店的「作廢時回補庫存」設定；線上訂單到分店時扣原料，取消時回補"""
    from app.extensions import db
    from app.models.inventory import Ingredient, RecipeLine
    uuid, hub_id = _hub_item(env, '原料測試拿鐵')
    _put_setting(env.a, 'inventory', {'enabled': True})
    item_id = _node_item(env.a, uuid)['id']
    with tenant_ctx(env.a) as tenant:
        ing = Ingredient(tenant_id=tenant.id, name='測試牛奶', unit='ml', stock=1000)
        db.session.add(ing)
        db.session.flush()
        db.session.add(RecipeLine(tenant_id=tenant.id, menu_item_id=item_id,
                                  ingredient_id=ing.id, quantity=200))
        db.session.commit()
        ing_id = ing.id

    def milk():
        return _q(env.a, lambda: db.session.get(Ingredient, ing_id).stock)

    order = _online_order(env, hub_id, qty=1)
    env.sync(env.a)
    assert _node_item(env.a, uuid)['stock'] == 49
    assert milk() == pytest.approx(800), '線上訂單到分店時依配方扣原料'
    _hub_cancel(env, order['id'])
    env.sync(env.a)
    assert _node_order(env.a, order['uuid'])['status'] == 'Cancelled'
    assert _node_item(env.a, uuid)['stock'] == 50
    assert milk() == pytest.approx(1000), '線上端取消：回補原料'

    _put_setting(env.a, 'refund', {'restock_on_cancel': False})
    try:
        order = _online_order(env, hub_id, qty=1)
        env.sync(env.a)
        assert _node_item(env.a, uuid)['stock'] == 49
        _hub_cancel(env, order['id'])
        env.sync(env.a)
        assert _node_order(env.a, order['uuid'])['status'] == 'Cancelled'
        assert _node_item(env.a, uuid)['stock'] == 49, '關閉「作廢時回補庫存」時線上端取消也不回補'
        assert milk() == pytest.approx(800)
    finally:
        _put_setting(env.a, 'refund', {'restock_on_cancel': True})
        _put_setting(env.a, 'inventory', {'enabled': False})


def test_cancelled_online_order_is_not_counted_as_waiting(env):
    """B-193：分店收到時已取消的線上訂單 (取餐號 0) 不算在「等待編號的線上訂單」"""
    from app.sync.node import sync_status
    _uuid, hub_id = _hub_item(env, '取消測試堡')
    order = _online_order(env, hub_id)
    _hub_cancel(env, order['id'])
    env.sync(env.a)
    got = _node_order(env.a, order['uuid'])
    assert got['status'] == 'Cancelled' and got['pickup'] == 0
    with env.a.test_request_context('/'):
        assert sync_status()['online_orders_waiting'] == 0


def test_coupon_deleted_and_renamed_in_same_batch_is_applied(env):
    """B-185：線上端刪除 SALE、再把 SALE2 改成 SALE，分店同一批收到時不可靜默丟掉改名"""
    from app.extensions import db
    from app.models.coupon import Coupon
    from app.models.sync import SyncConflict
    with tenant_ctx(env.hub) as tenant:
        old = Coupon(tenant_id=tenant.id, code='FIXSALE', title='舊活動')
        new = Coupon(tenant_id=tenant.id, code='FIXSALE2', title='新活動')
        db.session.add_all([old, new])
        db.session.commit()
        new_uuid = new.uuid
    env.sync(env.a)
    with tenant_ctx(env.hub):
        db.session.delete(Coupon.query.filter_by(code='FIXSALE').first())
        db.session.commit()
        Coupon.query.filter_by(code='FIXSALE2').first().code = 'FIXSALE'
        db.session.commit()
    env.sync(env.a)
    with env.a.app_context():
        rows = {(c.code, c.uuid) for c in Coupon.query.filter(Coupon.code.like('FIXSALE%'))}
        assert rows == {('FIXSALE', new_uuid)}
        assert SyncConflict.query.filter_by(row_uuid=new_uuid).count() == 0


def test_pull_conflict_that_still_fails_is_recorded_once(env):
    """B-185 / R2-070：仍無法套用的列記錄衝突 (同一筆不重複寫入)，並留待下次重試"""
    from app.extensions import db
    from app.models.sync import SyncConflict
    from app.sync import runtime
    from app.sync.node import PULL_RETRY_KEY, NodeSync, _json_state
    with env.a.app_context():
        sync = NodeSync(env.transports[env.a])
        tenant_id = sync.tenant().id
        bad = {'uuid': 'fix-bad-item-uuid', 'name': '壞圖片', 'category': '主餐', 'price': 10,
               'image_path': '../evil.jpg', 'updated_at': '2030-01-01T00:00:00'}
        for _ in range(2):
            retry = {}
            result = sync._apply(tenant_id, runtime.get_state('node_uuid'), {'menu_item': [bad]}, set())
            sync._hold_for_retry({'menu_item': [bad]}, result.conflicts, retry)
            sync._retry_rows(tenant_id, runtime.get_state('node_uuid'), retry, set())
        assert SyncConflict.query.filter_by(row_uuid='fix-bad-item-uuid').count() == 1
        assert [i['row']['uuid'] for i in _json_state(PULL_RETRY_KEY, [])] == ['fix-bad-item-uuid']
        runtime.set_state(PULL_RETRY_KEY, '')
        db.session.commit()


def test_push_conflicts_are_not_duplicated(env):
    """R2-070：上傳重試時線上端回傳同樣的衝突，不重複新增 SyncConflict"""
    from app.models.sync import SyncConflict
    from app.sync.node import NodeSync
    body = {'conflicts': [{'table': 'staff_account', 'uuid': 'fix-dup-uuid', 'reason': '帳號名稱已被其他店家使用'}]}
    with env.a.app_context():
        sync = NodeSync(env.transports[env.a])
        tenant_id = sync.tenant().id
        sync._handle_push_response(tenant_id, body)
        sync._handle_push_response(tenant_id, body)
        assert SyncConflict.query.filter_by(row_uuid='fix-dup-uuid').count() == 1


def test_push_and_pull_errors_are_kept_separately(env):
    """B-194：下載成功不會清掉上傳仍在失敗的錯誤"""
    from app.sync import runtime
    from app.sync.node import record_error, record_ok
    with env.a.app_context():
        record_error(RuntimeError('上傳被拒'), 'push')
        record_ok('pull')
        assert runtime.get_state('last_error') == '上傳被拒'
        record_error(RuntimeError('下載逾時'), 'pull')
        record_ok('pull')
        assert runtime.get_state('last_error') == '上傳被拒'
        record_ok('push')
        assert runtime.get_state('last_error') == ''
        record_error(RuntimeError('手動同步失敗'))
        record_ok()
        assert runtime.get_state('last_error') == ''


def test_token_swapped_to_other_branch_stops_sync(env):
    """B-184：同步過的分店把權杖換成另一筆分店，行程重新啟動後第一次同步就停止 (不沿用舊分店 uuid 與游標)"""
    from app.sync import runtime
    from app.sync.node import NodeSync, SyncError, _HELLO_OK
    with env.a.app_context():
        node_uuid = runtime.get_state('node_uuid')
        env.a.extensions.pop(_HELLO_OK, None)   # 模擬重新啟動
        with pytest.raises(SyncError, match='另一筆分店'):
            NodeSync(ClientTransport(env.hub, env.token_x)).run_once()
        assert runtime.get_state('node_uuid') == node_uuid
        NodeSync(env.transports[env.a]).run_once()   # 換回原權杖即恢復
        assert env.a.extensions.get(_HELLO_OK)


def test_upgrade_with_new_synced_column_backfills_existing_rows(env):
    """R2-067：舊版分店略過了線上端新欄位的值，升級 (同步結構多了欄位) 後重新下載並補上"""
    import json
    from app.extensions import db
    from app.models.menu import MenuItem
    from app.sync import runtime
    from app.sync.node import _HELLO_OK, SCHEMA_KEY
    uuid, _hub_id = _hub_item(env, '補值測試堡', price=120)
    with env.a.app_context():
        table = MenuItem.__table__
        db.session.execute(sa.update(table).where(table.c.uuid == uuid).values(price=1, updated_at=table.c.updated_at))
        schema = json.loads(runtime.get_state(SCHEMA_KEY))
        schema['menu_item'] = [c for c in schema['menu_item'] if c != 'price']
        runtime.set_state(SCHEMA_KEY, json.dumps(schema))
        db.session.commit()
        env.a.extensions.pop(_HELLO_OK, None)
    env.sync(env.a)
    assert _node_item(env.a, uuid)['price'] == 120
    with env.a.app_context():
        assert runtime.get_state('sync_backfill') == ''


def test_menu_image_download_and_upload_are_retried(env, monkeypatch):
    """R2-068 / R2-071：圖片下載 / 上傳失敗後記入待處理清單，之後重試成功"""
    from app.extensions import db
    from app.models.menu import MenuItem
    from app.sync import node as node_mod
    monkeypatch.setattr(node_mod, 'MEDIA_BACKOFF', 0)
    # 下載：線上端的餐點已有圖片路徑，但圖片檔還沒上傳到線上端
    rel = '8/fix_pending_photo.jpg'
    with tenant_ctx(env.hub):
        db.session.add(MenuItem(name='等圖片的餐點', category='主餐', price=90, image_path=rel))
        db.session.commit()
    env.sync(env.a)
    local = os.path.join(env.a.config['UPLOAD_FOLDER_MENU'], rel)
    assert not os.path.isfile(local)
    with env.a.app_context():
        assert rel in node_mod.NodeSync._media_queue(node_mod.MEDIA_DOWN_KEY)
    hub_file = os.path.join(env.hub.config['UPLOAD_FOLDER_MENU'], rel)
    os.makedirs(os.path.dirname(hub_file), exist_ok=True)
    with open(hub_file, 'wb') as fh:
        fh.write(jpeg_bytes().read())
    env.sync(env.a)
    assert os.path.isfile(local), '之後的同步補下載'
    with env.a.app_context():
        assert rel not in node_mod.NodeSync._media_queue(node_mod.MEDIA_DOWN_KEY)

    # 上傳：PUT 圖片失敗，重送時線上端不會再回報 missing_media
    rel_up = '8/fix_upload_photo.jpg'
    up_local = os.path.join(env.a.config['UPLOAD_FOLDER_MENU'], rel_up)
    with open(up_local, 'wb') as fh:
        fh.write(jpeg_bytes().read())
    with tenant_ctx(env.a):
        db.session.add(MenuItem(name='分店新圖片餐點', category='主餐', price=80, image_path=rel_up))
        db.session.commit()
    env.transports[env.a].fail_put = 1
    env.sync(env.a)
    hub_up = os.path.join(env.hub.config['UPLOAD_FOLDER_MENU'], rel_up)
    assert not os.path.isfile(hub_up)
    env.sync(env.a)
    assert os.path.isfile(hub_up), '下次上傳時補傳圖片'
    with env.a.app_context():
        assert node_mod.NodeSync._media_queue(node_mod.MEDIA_UP_KEY) == {}


def test_hub_cancel_restocks_only_what_was_consumed(env):
    """B-018：線上訂單送達分店時只剩 1 份 (扣到 0)，線上端取消後分店只回補實際扣到的 1 份"""
    from app.extensions import db
    from app.models.menu import MenuItem
    uuid, hub_id = _hub_item(env, '不足回補測試堡')
    order = _online_order(env, hub_id, qty=3)   # 線上端以舊的庫存快照 (50) 接單
    with env.a.app_context():   # 分店現場先賣掉，只剩 1 份 (尚未回報)
        table = MenuItem.__table__
        db.session.execute(sa.update(table).where(table.c.uuid == uuid).values(stock=1, updated_at=table.c.updated_at))
        db.session.commit()
    env.sync(env.a)
    assert _node_order(env.a, order['uuid'])['pickup'] > 0
    assert _node_item(env.a, uuid)['stock'] == 0
    _hub_cancel(env, order['id'])
    env.sync(env.a)
    assert _node_order(env.a, order['uuid'])['status'] == 'Cancelled'
    assert _node_item(env.a, uuid)['stock'] == 1, '不可回補沒扣到的 2 份'


def test_order_changes_from_hub_bump_local_feed_rev_only(env):
    """R2-182：從線上端套用進來的訂單異動更新本機 feed_rev (API cursor 收得到)，但不標記 sync_rev、不改 updated_at"""
    from types import SimpleNamespace
    from app.extensions import db
    from app.models.order import Order
    from app.sync.node import NodeSync

    def row():
        with env.a.app_context():
            o = Order.query.filter_by(uuid=order['uuid']).first()
            return o.id, o.feed_rev, o.sync_rev, o.updated_at

    _uuid, hub_id = _hub_item(env, '異動序號測試堡')
    order = _online_order(env, hub_id)
    env.sync(env.a)
    order_id, feed_before, sync_before, updated_before = row()
    assert feed_before > 0, '新收到的線上訂單也有 feed_rev'
    with env.a.app_context():
        NodeSync._bump_feed_rev(SimpleNamespace(changed_order_ids={order_id}, new_orders=[]))
        db.session.commit()
    _id, feed_after, sync_after, updated_after = row()
    assert feed_after > feed_before
    assert (sync_after, updated_after) == (sync_before, updated_before), '不可標記 sync_rev (否則會傳回線上端)'


def test_intake_snapshot_reports_branch_own_delivery(env):
    """R2-002：分店生效中的自有外送設定摘要 (啟用、是否開放線上、配送區域、免運門檻) 隨同步回報線上端"""
    from app.sync.node import NodeSync
    _put_setting(env.a, 'own_delivery', {'enabled': True, 'channels': ['pos'], 'zones': '3 公里內:50:300',
                                         'free_over': 800})
    try:
        with tenant_ctx(env.a) as tenant:
            intake, digest = NodeSync(env.transports[env.a])._intake_snapshot(tenant.id)
        assert intake['delivery'] == {'enabled': True, 'online': False, 'free_over': 800,
                                      'zones': [{'name': '3 公里內', 'fee': 50, 'min_spend': 300}]}
        env.sync(env.a)   # 線上端收到多出的欄位不影響上傳
        _put_setting(env.a, 'own_delivery', {'enabled': True, 'channels': ['online', 'pos'],
                                             'zones': '3 公里內:50:300', 'free_over': 800})
        with tenant_ctx(env.a) as tenant:
            intake2, digest2 = NodeSync(env.transports[env.a])._intake_snapshot(tenant.id)
        assert intake2['delivery']['online'] is True and digest2 != digest, '設定變動時重新回報'
    finally:
        _put_setting(env.a, 'own_delivery', {'enabled': False, 'channels': ['online', 'pos'],
                                             'zones': '3 公里內:30:200\n3~6 公里:60:400', 'free_over': 0})


class RecordingTransport(ClientTransport):
    """記錄每次 /push 的內容"""

    def __init__(self, hub_app, token):
        super().__init__(hub_app, token)
        self.pushes = []

    def call(self, method, path, params=None, json_body=None, data=None, timeout=None, raw=False):
        if path == '/push':
            self.pushes.append(json_body)
        return super().call(method, path, params=params, json_body=json_body, data=data, timeout=timeout, raw=raw)


def _hub_own_delivery(env):
    import json
    from app.models.sync import SyncNode
    with env.hub.app_context():
        raw = SyncNode.query.filter_by(uuid=env.node_a_uuid).first().own_delivery_json
        return json.loads(raw) if raw else None


def test_push_reports_branch_own_delivery_config(env):
    """R2-002：分店生效的自有外送設定 (含本店自訂) 有變動時隨 push 的 own_delivery 上傳，線上端存到 SyncNode"""
    from app.sync.node import NodeSync
    expected = {'enabled': True, 'channels': ['pos'], 'zones': '近:40:150', 'free_over': 600, 'eta_minutes': 30}
    _put_setting(env.a, 'own_delivery', dict(expected))
    try:
        transport = RecordingTransport(env.hub, env.token_a)
        with env.a.app_context():
            NodeSync(transport).push()
        sent = [p['own_delivery'] for p in transport.pushes if p.get('own_delivery')]
        assert sent and sent[-1] == expected
        assert _hub_own_delivery(env) == expected
        transport.pushes.clear()
        with env.a.app_context():
            NodeSync(transport).push()
        assert not any(p.get('own_delivery') for p in transport.pushes), '沒有變動時不重送'
    finally:
        _put_setting(env.a, 'own_delivery', {'enabled': False, 'channels': ['online', 'pos'],
                                             'zones': '3 公里內:30:200\n3~6 公里:60:400', 'free_over': 0,
                                             'eta_minutes': 45})


def _drop_hub_column(env, table, column, stale_hash=False):
    """模擬線上端舊版時記錄的結構 (沒有 table.column)"""
    import json
    from app.extensions import db
    from app.sync import runtime
    from app.sync.node import HUB_SCHEMA_KEY
    with env.a.app_context():
        schema = json.loads(runtime.get_state(HUB_SCHEMA_KEY))
        schema[table] = [c for c in schema[table] if c != column]
        runtime.set_state(HUB_SCHEMA_KEY, json.dumps(schema, sort_keys=True))
        if stale_hash:
            runtime.set_state('hub_schema_hash', 'old-hub-schema')
        db.session.commit()


def _desc(app, uuid):
    from app.models.menu import MenuItem
    with app.app_context():
        return MenuItem.query.filter_by(uuid=uuid).first().description


def _set_desc_keep_time(app, uuid, value):
    """直接改欄位值、不改 updated_at (模擬線上端舊版時略過了這個欄位)"""
    from app.extensions import db
    from app.models.menu import MenuItem
    with app.app_context():
        table = MenuItem.__table__
        db.session.execute(sa.update(table).where(table.c.uuid == uuid)
                           .values(description=value, updated_at=table.c.updated_at))
        db.session.commit()


def test_hub_upgraded_later_resends_with_backfill(env):
    """R2-067：分店先升級、線上端較晚升級 (/hello 的結構多了欄位)：分店從頭重新上傳一次並帶 backfill 補上新欄位"""
    from app.sync import runtime
    from app.sync.node import _HELLO_OK, NodeSync
    uuid, _hub_id = _hub_item(env, '上傳補值測試堡')
    _set_desc_keep_time(env.a, uuid, '分店寫的說明')
    _set_desc_keep_time(env.hub, uuid, '')   # 線上端舊版時略過了這個欄位
    _drop_hub_column(env, 'menu_item', 'description')
    transport = RecordingTransport(env.hub, env.token_a)
    with env.a.app_context():
        env.a.extensions.pop(_HELLO_OK, None)   # 重新啟動
        sync = NodeSync(transport)
        sync.ensure_hello()
        assert int(runtime.get_state('push_cursor')) == -1
        sync.push()
        assert int(runtime.get_state('push_cursor')) > 0
        assert runtime.get_state('sync_push_backfill') == ''
    assert transport.pushes and all(p.get('backfill') == {'menu_item': ['description']} for p in transport.pushes)
    assert _desc(env.hub, uuid) == '分店寫的說明', '線上端補上新欄位的值'
    transport.pushes.clear()
    with env.a.app_context():
        NodeSync(transport).push()
    assert not any(p.get('backfill') for p in transport.pushes), '補值完成後不再帶 backfill'


def test_hub_upgraded_while_node_running_is_detected_by_push(env):
    """R2-067：線上端在分店執行中升級：push 回應的 schema_hash 不同時重新比對結構，下一次上傳從頭重送並補值"""
    from app.sync import runtime
    from app.sync.node import _HELLO_OK, NodeSync
    uuid, _hub_id = _hub_item(env, '執行中升級測試堡')
    _set_desc_keep_time(env.a, uuid, '分店說明二')
    _set_desc_keep_time(env.hub, uuid, '')
    _drop_hub_column(env, 'menu_item', 'description', stale_hash=True)
    transport = RecordingTransport(env.hub, env.token_a)
    with env.a.app_context():
        from app.extensions import db
        from app.models.menu import MenuItem
        table = MenuItem.__table__
        db.session.execute(sa.update(table).where(table.c.uuid == uuid).values(stock=table.c.stock - 1))
        db.session.commit()   # 有東西要上傳 (庫存回報)
        assert env.a.extensions.get(_HELLO_OK), '本行程已確認過身分 (ensure_hello 不會再呼叫 /hello)'
        NodeSync(transport).push()
        assert transport.pushes
        assert int(runtime.get_state('push_cursor')) == -1, 'push 回應的 schema_hash 改變：觸發完整重新上傳'
        assert runtime.get_state('hub_schema_hash') != 'old-hub-schema'
        transport.pushes.clear()
        NodeSync(transport).push()
    assert transport.pushes and all(p.get('backfill') == {'menu_item': ['description']} for p in transport.pushes)
    assert _desc(env.hub, uuid) == '分店說明二'


def test_reserved_order_pickup_number_uses_pickup_business_day(env):
    """B-190：預約之後營業日取餐的線上訂單依取餐日的計數編號；今天 (或過去) 取餐的沿用今天"""
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from app.services.order_stock import business_day
    from app.sync.node import NodeSync
    calls = []

    def fake_next(tenant_id, business_date=None):
        calls.append(business_date)
        return 7

    later = datetime.now() + timedelta(days=2)
    with env.a.app_context():
        for pickup_at in (later, None, datetime.now() - timedelta(days=3)):
            assert NodeSync._assign_pickup_number(1, SimpleNamespace(id=-1, pickup_at=pickup_at), fake_next) is None
    assert calls == [business_day(later), None, None]


def test_online_order_consumes_ingredients_even_when_short(monkeypatch):
    """B-189：線上訂單已成立，分店收到時原料不足也照扣 (block=False)，不再回報原料未扣"""
    from types import SimpleNamespace

    from app.services import inventory_service
    from app.sync.node import NodeSync
    seen = []
    monkeypatch.setattr(inventory_service, 'consume',
                        lambda tenant_id, order_id, demands, block=True: seen.append((order_id, block)))
    assert NodeSync._consume_ingredients(1, SimpleNamespace(id=5), {}) == ''
    assert seen == [(5, False)]
