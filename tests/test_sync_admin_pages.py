"""
線上端 (hub) / 分店 (node) 的介面與路由：
- 線上端：分店管理 (負責人限定、權杖只顯示一次)、顧客選擇取餐分店、依分店庫存顯示菜單、線上結帳、
  收據與我的訂單顯示「待分店確認」、不提供人臉註冊/登入
- 分店端：同步狀態頁與「立即同步」
- 單機 (standalone)：新頁面一律 404，原有畫面不變
環境建立方式與 tests/test_sync_hub_node.py 相同 (各自獨立的 SQLite 與上傳資料夾)。
"""
import json
import re
import shutil
import tempfile
import threading
from types import SimpleNamespace

import pytest

from tests.conftest import IS_SQLITE
from tests.support import flashes, line, login_staff
from tests.test_sync_hub_node import ClientTransport, _make_app, tenant_ctx

TOKEN_RE = re.compile(r'kn_[A-Za-z0-9_\-]{20,}')
PAGE_DATA_RE = re.compile(r'<script type="application/json" id="page-data">(.*?)</script>', re.S)


def _page_data(html):
    m = PAGE_DATA_RE.search(html)
    assert m, '頁面缺少 page-data'
    return json.loads(m.group(1))


class Env:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix='kiosk_sync_ui_')
        self.hub = _make_app(self.root, 'hub', 'hub')
        self.node = _make_app(self.root, 'node', 'node')
        self.hub_owner = login_staff(self.hub, 'ui_hub_owner', 'owner')
        self.hub_manager = login_staff(self.hub, 'ui_hub_manager', 'manager')
        self.hub_staff = login_staff(self.hub, 'ui_hub_staff', 'staff')

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture(scope='module')
def env():
    e = Env()
    yield e
    e.close()


def _create_node(env, name, online=True, connect=True):
    """以分店管理頁新增分店，回傳 (uuid, 權杖)"""
    data = {'action': 'create', 'name': name}
    if online:
        data['accepts_online_orders'] = '1'
    r = env.hub_owner.post('/admin/nodes', data=data)
    assert r.status_code == 302
    page = env.hub_owner.get('/admin/nodes').get_data(as_text=True)
    tokens = set(TOKEN_RE.findall(page))
    assert len(tokens) == 1, '新增後應顯示一次權杖'
    token = tokens.pop()
    if connect:  # 模擬分店已連線 (線上點餐只開放給連線中的分店)
        env.hub.test_client().post('/api/sync/v1/hello', headers={'Authorization': f'Bearer {token}'})
    from app.models.sync import SyncNode
    with env.hub.app_context():
        node = SyncNode.query.filter_by(name=name).first()
        return node.uuid, token


def _node_row(env, uuid):
    from app.models.sync import SyncNode
    with env.hub.app_context():
        n = SyncNode.query.filter_by(uuid=uuid).first()
        return dict(id=n.id, name=n.name, is_active=n.is_active, online=n.accepts_online_orders)


def _hello(env, token):
    return env.hub.test_client().post('/api/sync/v1/hello', headers={'Authorization': f'Bearer {token}'})


def _hub_item(env, name, price=100):
    from app.extensions import db
    from app.models.menu import MenuItem
    with tenant_ctx(env.hub):
        item = MenuItem(name=name, category='主餐', price=price, stock=99, total_stock=99)
        db.session.add(item)
        db.session.commit()
        return item.id


def _report_stock(env, node_uuid, item_id, stock, sold_out=False):
    """模擬分店回報的庫存快照"""
    from app.extensions import db
    from app.models.sync import NodeStock, SyncNode
    with env.hub.app_context():
        node_id = SyncNode.query.filter_by(uuid=node_uuid).first().id
        row = db.session.get(NodeStock, (node_id, item_id))
        if row is None:
            row = NodeStock(node_id=node_id, menu_item_id=item_id)
            db.session.add(row)
        row.stock, row.is_sold_out = stock, sold_out
        db.session.commit()


_member_seq = [0]


def _hub_member(env):
    """線上點餐只限會員：建立一位會員並以手機 + PIN 登入"""
    from app.extensions import db
    from app.models.user import User
    _member_seq[0] += 1
    phone = f'09550{_member_seq[0]:05d}'
    with tenant_ctx(env.hub):
        user = User(name=f'線上會員{_member_seq[0]}', phone=phone, photo_path='', points=0)
        user.set_pin('1357')
        db.session.add(user)
        db.session.commit()
    c = env.hub.test_client()
    c.get('/?shop=default')
    r = c.post('/phone_login', json={'phone': phone, 'pin': '1357'}, headers={'X-Forwarded-For': f'10.55.0.{_member_seq[0]}'})
    assert r.status_code == 200, r.get_data(as_text=True)
    return c


def _items_by_name(page_data):
    return {i['name']: i for i in page_data['menu']['items']}


# ---------------------------------------------------------------------------
# 單機模式：新頁面不存在，原有入口不變
# ---------------------------------------------------------------------------
def test_standalone_has_no_sync_pages(app, admin_client):
    for path in ('/admin/nodes', '/admin/sync'):
        assert admin_client.get(path).status_code == 404, path
    assert admin_client.post('/admin/nodes', data={'action': 'create', 'name': 'x'}).status_code == 404
    assert admin_client.post('/admin/sync/run').status_code == 404
    guest = app.test_client()
    guest.get('/?shop=default')
    assert guest.post('/pickup_node', data={'node_uuid': 'x'}).status_code == 404
    assert guest.get('/member_login').status_code == 302  # 單機導向原本的人臉 / 手機登入頁
    landing = guest.get('/').get_data(as_text=True)
    assert '/register' in landing and '/face_login' in landing and 'pickup-node-bar' not in landing
    dash = admin_client.get('/admin').get_data(as_text=True)
    assert '分店管理' not in dash and '同步狀態' not in dash


def test_backup_section_depends_on_database(admin_client):
    page = admin_client.get('/admin').get_data(as_text=True)
    if IS_SQLITE:
        assert 'pane-export-zip' in page and 'pane-restore-zip' in page and 'backup-unavailable' not in page
    else:
        assert 'pane-export-zip' not in page and 'pane-restore-zip' not in page
        assert 'backup-unavailable' in page and 'pg_dump' in page


# ---------------------------------------------------------------------------
# 線上端：分店管理
# ---------------------------------------------------------------------------
def test_hub_node_pages_are_owner_only(env):
    assert env.hub_owner.get('/admin/nodes').status_code == 200
    for client in (env.hub_manager, env.hub_staff):
        assert client.get('/admin/nodes').status_code == 403
        assert client.post('/admin/nodes', data={'action': 'create', 'name': '偷建分店'}).status_code == 403
    assert env.hub.test_client().get('/admin/nodes').status_code in (302, 401)
    assert env.hub_owner.get('/admin/sync').status_code == 404, '同步狀態頁只在分店端提供'
    assert '分店管理' in env.hub_owner.get('/admin').get_data(as_text=True)
    assert '分店管理' not in env.hub_manager.get('/admin').get_data(as_text=True)


def test_hub_create_node_shows_token_once_and_token_works(env):
    uuid, token = _create_node(env, '台南店', connect=False)
    page = env.hub_owner.get('/admin/nodes').get_data(as_text=True)
    assert not TOKEN_RE.search(page), '權杖只顯示一次'
    assert '台南店' in page and uuid in page and '尚未連線' in page
    r = _hello(env, token)
    assert r.status_code == 200 and r.get_json()['node']['uuid'] == uuid
    page = env.hub_owner.get('/admin/nodes').get_data(as_text=True)
    assert '尚未連線' not in page, '連線後顯示最後連線時間'
    env.tainan = (uuid, token)


def test_hub_create_node_shows_setup_hint(env):
    env.hub_owner.post('/admin/nodes', data={'action': 'create', 'name': '高雄店', 'accepts_online_orders': '1'})
    page = env.hub_owner.get('/admin/nodes').get_data(as_text=True)
    assert 'SYNC_ROLE=node' in page and 'SYNC_HUB_URL=' in page and 'SYNC_NODE_TOKEN=kn_' in page
    assert _hello(env, TOKEN_RE.search(page).group(0)).status_code == 200  # 分店連線
    from app.models.sync import SyncNode
    with env.hub.app_context():
        env.kaohsiung_uuid = SyncNode.query.filter_by(name='高雄店').first().uuid
    r = env.hub_owner.post('/admin/nodes', data={'action': 'create', 'name': '  '})
    assert r.status_code in (302, 400)
    with env.hub.app_context():
        assert SyncNode.query.count() == 2, '空白名稱不可建立分店'


def test_hub_rotate_token(env):
    uuid, old_token = env.tainan
    node_id = _node_row(env, uuid)['id']
    assert env.hub_owner.post('/admin/nodes', data={'action': 'rotate_token', 'node_id': node_id}).status_code == 302
    new_token = TOKEN_RE.search(env.hub_owner.get('/admin/nodes').get_data(as_text=True)).group(0)
    assert new_token != old_token
    assert _hello(env, old_token).status_code == 401, '舊權杖立即失效'
    assert _hello(env, new_token).status_code == 200
    env.tainan = (uuid, new_token)


def test_hub_toggle_online_and_active(env):
    uuid, token = env.tainan
    node_id = _node_row(env, uuid)['id']
    env.hub_owner.post('/admin/nodes', data={'action': 'toggle_online', 'node_id': node_id})
    assert _node_row(env, uuid)['online'] is False
    env.hub_owner.post('/admin/nodes', data={'action': 'toggle_online', 'node_id': node_id})
    assert _node_row(env, uuid)['online'] is True
    env.hub_owner.post('/admin/nodes', data={'action': 'toggle_active', 'node_id': node_id})
    assert _node_row(env, uuid)['is_active'] is False
    assert _hello(env, token).status_code == 401, '停用的分店不能同步'
    env.hub_owner.post('/admin/nodes', data={'action': 'toggle_active', 'node_id': node_id})
    assert _node_row(env, uuid)['is_active'] is True and _hello(env, token).status_code == 200
    assert env.hub_owner.post('/admin/nodes', data={'action': 'toggle_active', 'node_id': 99999}).status_code == 404
    assert env.hub_manager.post('/admin/nodes', data={'action': 'toggle_active', 'node_id': node_id}).status_code == 403


# ---------------------------------------------------------------------------
# 線上端：顧客選擇取餐分店與結帳
# ---------------------------------------------------------------------------
def test_hub_menu_requires_pickup_node_and_checkout_fails_without_it(env):
    env.burger = _hub_item(env, '線上牛肉堡', price=120)
    env.fries = _hub_item(env, '線上薯條', price=50)
    guest = _hub_member(env)
    page = guest.get('/').get_data(as_text=True)
    assert 'id="pickupNodeSelect"' in page and '台南店' in page and '高雄店' in page
    assert 'pickup-node-required' in page and 'menuList' not in page, '未選分店前不顯示菜單'
    assert _page_data(page)['pickupNode'] is None
    r = guest.post('/submit_order', json={'items': [line(env.burger)], 'payment_method': 'Cash', 'order_type': '外帶'})
    assert r.status_code == 400 and '分店' in r.get_json()['message']


def test_hub_menu_uses_selected_node_stock(env):
    tainan_uuid = env.tainan[0]
    _report_stock(env, env.kaohsiung_uuid, env.burger, 5)
    _report_stock(env, tainan_uuid, env.burger, 0, sold_out=True)
    _report_stock(env, tainan_uuid, env.fries, 9)
    guest = _hub_member(env)

    r = guest.post('/pickup_node', data={'node_uuid': env.kaohsiung_uuid})
    assert r.status_code == 302
    page = guest.get('/').get_data(as_text=True)
    items = _items_by_name(_page_data(page))
    assert items['線上牛肉堡']['is_available'] is True and items['線上牛肉堡']['stock'] == 5
    assert items['線上薯條']['is_available'] is False, '分店沒有回報過的品項視為售完'
    assert _page_data(page)['pickupNode'] == '高雄店'

    guest.post('/pickup_node', data={'node_uuid': tainan_uuid})
    items = _items_by_name(_page_data(guest.get('/').get_data(as_text=True)))
    assert items['線上牛肉堡']['is_available'] is False and items['線上薯條']['is_available'] is True

    flashes(guest)
    guest.post('/pickup_node', data={'node_uuid': 'not-a-node'})
    assert any('不開放' in m for m in flashes(guest))
    assert _page_data(guest.get('/').get_data(as_text=True))['pickupNode'] is None


def test_hub_checkout_with_selected_node(env):
    guest = _hub_member(env)
    guest.post('/pickup_node', data={'node_uuid': env.kaohsiung_uuid})
    r = guest.post('/submit_order', json={'items': [line(env.burger, 6)], 'payment_method': 'Cash', 'order_type': '外帶'})
    assert r.status_code == 400 and '庫存' in r.get_json()['message'], '依分店庫存檢查'

    r = guest.post('/submit_order', json={'items': [line(env.burger, 2)], 'payment_method': 'Cash', 'order_type': '外帶'})
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data['pickup_number'] == 0 and data['pickup_pending'] is True and data['pickup_node_name'] == '高雄店'
    assert data['payment_url'].endswith('/pay/' + data['order_uuid']), '線上訂單需先付款'
    from app.models.order import Order
    with env.hub.app_context():
        order = Order.query.filter_by(uuid=data['order_uuid']).first()
        assert order.node_uuid == env.kaohsiung_uuid and order.order_source == 'Online' and order.pickup_number == 0
        assert order.payment_status == 'pending'

    mine = guest.get('/my_orders').get_json()
    assert mine[0]['pickup_pending'] is True and mine[0]['pickup_node_name'] == '高雄店'
    assert mine[0]['payment_status'] == 'pending' and mine[0]['payment_url']

    live = env.hub_owner.get('/api/admin_live_orders').get_json()['orders']
    row = next(o for o in live if o['id'] == data['order_id'])
    assert row['pickup_pending'] is True and row['pickup_node_name'] == '高雄店'
    env.online_order_uuid = data['order_uuid']


def test_hub_single_open_node_is_selected_automatically(env):
    node_id = _node_row(env, env.tainan[0])['id']
    env.hub_owner.post('/admin/nodes', data={'action': 'toggle_online', 'node_id': node_id})
    try:
        guest = _hub_member(env)
        page = guest.get('/').get_data(as_text=True)
        assert _page_data(page)['pickupNode'] == '高雄店' and 'id="pickupNodeSelect"' not in page
        r = guest.post('/submit_order', json={'items': [line(env.burger)], 'payment_method': 'Cash', 'order_type': '內用'})
        assert r.status_code == 200 and r.get_json()['pickup_node_name'] == '高雄店'
    finally:
        env.hub_owner.post('/admin/nodes', data={'action': 'toggle_online', 'node_id': node_id})


def test_hub_has_no_face_login_but_allows_phone_registration(env):
    """線上端沒有門市相機：不提供人臉登入，但可以手機 + PIN 線上註冊 (須同意隱私權告知)"""
    c = env.hub.test_client()
    landing = c.get('/?shop=default').get_data(as_text=True)
    assert '/member_login' in landing and '/register' in landing and '/face_login' not in landing
    r = c.get('/face_login')
    assert r.status_code == 302 and r.headers['Location'].endswith('/member_login')
    assert c.post('/face_login', data={}).status_code == 404
    page = c.get('/member_login').get_data(as_text=True)
    assert 'phoneLoginForm' in page and 'face-tab' not in page and 'loginVideo' not in page
    assert _page_data(page)['faceEnabled'] is False

    reg = c.get('/register').get_data(as_text=True)
    assert 'agreePrivacy' in reg and 'faceConsent' not in reg and 'photoInput' not in reg
    r = c.post('/register', data={'name': '線上註冊', 'phone': '0955000222', 'pin': '1357', 'agree_privacy': '1',
                                  'face_consent': '1'}, headers={'X-Forwarded-For': '10.56.0.1'})
    assert r.status_code == 302
    from app.models.user import User
    with env.hub.app_context():
        user = User.query.filter_by(phone='0955000222').first()
        assert user is not None and user.feature is None and user.face_consented_at is None, '線上註冊不收人臉'
        assert user.privacy_consented_at is not None

    from app.extensions import db
    from app.models.user import User
    with tenant_ctx(env.hub):
        user = User(name='線上會員', phone='0955000111', photo_path='', points=0)
        user.set_pin('1357')
        db.session.add(user)
        db.session.commit()
    r = c.post('/phone_login', json={'phone': '0955000111', 'pin': '1357'})
    assert r.status_code == 200 and r.get_json()['success'] is True
    assert c.get('/rewards').status_code == 200


# ---------------------------------------------------------------------------
# 分店端：同步狀態頁
# ---------------------------------------------------------------------------
def test_node_sync_status_page_permissions(env):
    owner = login_staff(env.node, 'ui_node_owner', 'owner')
    manager = login_staff(env.node, 'ui_node_manager', 'manager')
    staff = login_staff(env.node, 'ui_node_staff', 'staff')
    assert manager.get('/admin/sync').status_code == 200
    assert staff.get('/admin/sync').status_code == 403
    assert staff.post('/admin/sync/run').status_code == 403
    assert owner.get('/admin/nodes').status_code == 404, '分店管理只在線上端提供'
    assert '同步狀態' in owner.get('/admin').get_data(as_text=True)
    assert '同步狀態' in manager.get('/admin').get_data(as_text=True)
    page = manager.get('/admin/sync').get_data(as_text=True)
    assert '尚未完成第一次連線' in page and '尚未設定' in page
    env.node_manager = manager


def test_node_sync_now_runs_once(env, monkeypatch):
    import app.routes.sync_admin as sync_admin
    uuid, token = env.tainan
    env.node.config.update(SYNC_HUB_URL='http://hub.test', SYNC_NODE_TOKEN=token)
    monkeypatch.setattr(sync_admin, 'make_transport', lambda _app: ClientTransport(env.hub, token))
    manager = env.node_manager
    flashes(manager)
    assert manager.post('/admin/sync/run').status_code == 302
    assert any('同步完成' in m for m in flashes(manager))
    page = manager.get('/admin/sync').get_data(as_text=True)
    assert '台南店' in page and uuid in page and '尚未上傳' not in page and '尚未設定' not in page

    # 線上端的菜單已下載到分店
    from app.models.menu import MenuItem
    with env.node.app_context():
        assert MenuItem.query.filter_by(name='線上牛肉堡').count() == 1


def test_node_sync_now_shows_error(env, monkeypatch):
    import app.routes.sync_admin as sync_admin
    from app.sync.node import SyncError

    def broken(_app):
        raise SyncError('無法連線到線上端：測試斷線')
    monkeypatch.setattr(sync_admin, 'make_transport', broken)
    manager = env.node_manager
    flashes(manager)
    manager.post('/admin/sync/run')
    assert any('同步失敗' in m and '測試斷線' in m for m in flashes(manager))
    page = manager.get('/admin/sync').get_data(as_text=True)
    assert 'sync-last-error' in page and '測試斷線' in page

    # 恢復連線後再同步一次會清除錯誤
    monkeypatch.setattr(sync_admin, 'make_transport', lambda _app: ClientTransport(env.hub, env.tainan[1]))
    manager.post('/admin/sync/run')
    assert 'sync-last-error' not in manager.get('/admin/sync').get_data(as_text=True)


def test_node_sync_now_wakes_background_worker(env):
    worker = SimpleNamespace(local_change=threading.Event())
    env.node.extensions['sync_worker'] = worker
    try:
        manager = env.node_manager
        flashes(manager)
        assert manager.post('/admin/sync/run').status_code == 302
        assert worker.local_change.is_set()
        assert any('背景同步' in m for m in flashes(manager))
        assert '背景同步執行中' in manager.get('/admin/sync').get_data(as_text=True)
    finally:
        env.node.extensions.pop('sync_worker', None)


def test_hub_node_list_shows_stock_report(env):
    page = env.hub_owner.get('/admin/nodes').get_data(as_text=True)
    assert '項餐點' in page


def test_offline_node_is_not_offered_and_rejects_online_orders(env):
    """分店太久沒連線：不出現在取餐分店清單，結帳也會被拒絕"""
    from datetime import datetime, timedelta

    from app.errors import AppError
    from app.extensions import db
    from app.models.sync import SyncNode
    from app.services.order_service import online_pickup_node
    uuid, _token = _create_node(env, '離線分店')
    with env.hub.app_context():
        node = SyncNode.query.filter_by(uuid=uuid).first()
        node.last_seen_at = datetime.now() - timedelta(minutes=10)
        tenant_id = node.tenant_id
        db.session.commit()
    with env.hub.test_request_context('/'):
        from app.models.tenant import Tenant
        from app.tenant_context import _use_tenant
        _use_tenant(db.session.get(Tenant, tenant_id), remember=False)
        with pytest.raises(AppError, match='離線'):
            online_pickup_node(tenant_id, uuid)
    page = env.hub.test_client().get('/?shop=default').get_data(as_text=True)
    assert '離線分店' not in page


def test_hub_is_members_only_but_branch_allows_guests(env):
    """線上點餐只限會員；門市 (含顧客用手機連門市網站) 仍可用訪客點餐"""
    c = env.hub.test_client()
    c.get('/?shop=default')
    r = c.post('/guest_login')
    assert r.status_code == 302 and r.headers['Location'].endswith('/member_login')
    c.post('/pickup_node', data={'node_uuid': env.kaohsiung_uuid})
    r = c.post('/submit_order', json={'items': [line(env.burger)], 'payment_method': 'Cash', 'order_type': '外帶'})
    assert r.status_code == 401 and '會員' in r.get_json()['message']
    assert 'guest_login' not in c.get('/?shop=default').get_data(as_text=True), '線上首頁不顯示訪客點餐'

    node = env.node.test_client()
    assert 'guest_login' in node.get('/?shop=default').get_data(as_text=True), '門市首頁保留訪客點餐'
    r = node.post('/guest_login')
    assert r.status_code == 302 and r.headers['Location'].endswith('/')
