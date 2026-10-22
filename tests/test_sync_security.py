"""
同步 API 的安全限制：分店權杖外流或分店電腦被入侵時，能做的事只限該店家的一般資料。
- 不可建立、修改、刪除負責人帳號 (平台管理者 = 預設店家負責人)
- 餐點圖片、會員照片路徑不可含 ../ (否則之後刪除餐點 / 撤回人臉時會刪到任意檔案)
- 不可讀寫其他店家的餐點圖片、不可使用其他店家資料的識別碼
- 單次上傳筆數有上限
"""
import os
import uuid as uuidlib
from datetime import datetime, timedelta

import pytest

from tests.support import jpeg_bytes
from tests.test_sync_hub_node import ClientTransport, Env, _q, tenant_ctx


@pytest.fixture(scope='module')
def env():
    e = Env()
    e.sync(e.a)  # 第一次連線 (分店改用線上店家)
    from app.extensions import db
    from app.models.tenant import Tenant
    from app.sync.nodes import create_node
    with e.hub.app_context():  # 另一家店 (店家 2) 與它的分店
        other = Tenant(name='別的店', slug='other-shop')
        db.session.add(other)
        db.session.commit()
        _node, token = create_node(other.id, '別的店分店')
    e.other = ClientTransport(e.hub, token)
    yield e
    e.close()


def _push(transport, changes):
    return transport.client.post('/api/sync/v1/push', json={'changes': changes}, headers=transport.headers)


def _owner(env):
    from app.models.tenant import StaffAccount
    with env.hub.app_context():
        o = StaffAccount.query.filter_by(role='owner').order_by(StaffAccount.id).first()
        return dict(uuid=o.uuid, username=o.username, password_hash=o.password_hash, tenant_id=o.tenant_id)


def _staff_row(**kw):
    now = datetime.now()
    row = dict(uuid=str(uuidlib.uuid4()), username=f'u_{uuidlib.uuid4().hex[:8]}', password_hash='scrypt:fake',
               role='staff', is_active=True, created_at=now.isoformat(), deleted_at=None,
               updated_at=(now + timedelta(days=1)).isoformat())
    row.update(kw)
    return row


def _reasons(resp):
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return [c['reason'] for c in resp.get_json()['conflicts']]


def test_branch_cannot_create_owner_account(env):
    row = _staff_row(role='owner')
    reasons = _reasons(_push(env.transports[env.a], {'staff_account': [row]}))
    assert any('負責人' in r for r in reasons)
    from app.models.tenant import StaffAccount
    assert _q(env.hub, lambda: StaffAccount.query.filter_by(uuid=row['uuid']).first()) is None


def test_branch_cannot_change_owner_password_role_or_delete_owner(env):
    owner = _owner(env)
    hijack = _staff_row(uuid=owner['uuid'], username=owner['username'], role='owner', password_hash='scrypt:attacker')
    assert any('負責人' in r for r in _reasons(_push(env.transports[env.a], {'staff_account': [hijack]})))
    tomb = dict(uuid=str(uuidlib.uuid4()), table_name='staff_account', row_uuid=owner['uuid'],
                deleted_at=datetime.now().isoformat())
    assert any('負責人' in r for r in _reasons(_push(env.transports[env.a], {'sync_tombstone': [tomb]})))
    assert _owner(env)['password_hash'] == owner['password_hash'], '負責人密碼不可被分店改掉'


def test_branch_cannot_promote_staff_to_owner(env):
    row = _staff_row()
    assert _reasons(_push(env.transports[env.a], {'staff_account': [row]})) == [], '一般員工帳號照常同步'
    promoted = dict(row, role='owner', updated_at=(datetime.now() + timedelta(days=2)).isoformat())
    assert any('負責人' in r for r in _reasons(_push(env.transports[env.a], {'staff_account': [promoted]})))
    from app.models.tenant import StaffAccount
    assert _q(env.hub, lambda: StaffAccount.query.filter_by(uuid=row['uuid']).first().role) == 'staff'


def test_traversal_paths_are_rejected(env):
    now = datetime.now().isoformat()
    item = dict(uuid=str(uuidlib.uuid4()), name='壞圖片', category='主餐', price=10, image_path='../../database/menu.db',
                updated_at=now, created_at=now)
    member = dict(uuid=str(uuidlib.uuid4()), name='壞照片', phone='0955000001', photo_path='../../database/menu.db',
                  updated_at=now, created_at=now)
    reasons = _reasons(_push(env.transports[env.a], {'menu_item': [item], 'user': [member]}))
    assert any('圖片路徑' in r for r in reasons) and any('照片路徑' in r for r in reasons)
    from app.models.menu import MenuItem
    assert _q(env.hub, lambda: MenuItem.query.filter_by(uuid=item['uuid']).first()) is None


def test_media_is_scoped_to_own_tenant_and_never_overwritten(env):
    from app.extensions import db
    from app.models.menu import MenuItem
    rel = '1/menu_victim0000000001.jpg'
    folder = os.path.join(env.hub.config['UPLOAD_FOLDER_MENU'], '1')
    os.makedirs(folder, exist_ok=True)
    original = jpeg_bytes(color=(10, 200, 10)).getvalue()
    with open(os.path.join(folder, 'menu_victim0000000001.jpg'), 'wb') as fh:
        fh.write(original)
    with tenant_ctx(env.hub):
        db.session.add(MenuItem(name='受害店家的餐點', category='主餐', price=50, image_path=rel))
        db.session.commit()
    attacker = env.other
    evil = jpeg_bytes(color=(200, 10, 10)).getvalue()
    assert attacker.client.put(f'/api/sync/v1/media/menu/{rel}', data=evil, headers=attacker.headers).status_code == 404
    assert attacker.client.get(f'/api/sync/v1/media/menu/{rel}', headers=attacker.headers).status_code == 404
    same_tenant = env.transports[env.a]
    r = same_tenant.client.put(f'/api/sync/v1/media/menu/{rel}', data=evil, headers=same_tenant.headers)
    assert r.status_code == 200 and r.get_json().get('existed'), '已存在的圖片不覆蓋'
    with open(os.path.join(folder, 'menu_victim0000000001.jpg'), 'rb') as fh:
        assert fh.read() == original


def test_uuid_of_other_tenant_is_rejected(env):
    from app.extensions import db
    from app.models.user import User
    with tenant_ctx(env.hub):
        victim = User(name='別店會員', phone='0955000002', photo_path='', points=5)
        db.session.add(victim)
        db.session.commit()
        victim_uuid = victim.uuid
    now = datetime.now().isoformat()
    row = dict(uuid=victim_uuid, name='冒用', phone='0955000003', photo_path='', updated_at=now, created_at=now)
    assert any('其他店家' in r for r in _reasons(_push(env.other, {'user': [row]})))
    assert _q(env.hub, lambda: User.query.filter_by(uuid=victim_uuid).first().name) == '別店會員'


def test_push_size_is_limited(env):
    rows = [dict(uuid=str(uuidlib.uuid4())) for _ in range(5001)]
    assert _push(env.transports[env.a], {'customer_log': rows}).status_code == 413


def test_face_withdrawal_removes_photo_at_branch(env):
    """會員在線上端撤回人臉同意：同步後分店本機的照片檔也刪除"""
    from app.extensions import db
    from app.models.user import User
    from app.services.privacy_service import withdraw_face
    photo = 'member_withdraw_test01.jpg'
    path = os.path.join(env.a.config['UPLOAD_FOLDER_MEMBER'], photo)
    with open(path, 'wb') as fh:
        fh.write(jpeg_bytes().read())
    with tenant_ctx(env.a):
        db.session.add(User(name='撤回會員', phone='0955000009', photo_path=photo, points=0))
        db.session.commit()
    env.sync(env.a)
    with tenant_ctx(env.hub):
        withdraw_face(User.query.filter_by(phone='0955000009').first())
        db.session.commit()
    env.sync(env.a)
    assert not os.path.exists(path)
