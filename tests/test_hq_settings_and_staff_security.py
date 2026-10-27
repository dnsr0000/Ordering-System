"""總部統一設定下發 (分店可覆寫 / 改回總部)、後台會員分頁、員工兩步驟驗證 (TOTP)、登入裝置管理與閒置登出"""
import re
import time
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.tenant import StaffAccount, StaffSession
from app.services import security_service, settings_service
from tests.support import assert_flash, login_staff, make_member
from tests.test_sync_hub_node import Env, tenant_ctx

PASSWORD = 'StaffPass!2026'


# ---------------------------------------------------------------------------
# 總部統一設定
# ---------------------------------------------------------------------------
@pytest.fixture(scope='module')
def env():
    e = Env()
    yield e
    e.close()


def _get(app, key):
    with tenant_ctx(app):
        return settings_service.get(key), settings_service.source(key)


def test_hq_settings_reach_branches_and_can_be_overridden(env):
    env.sync_all()
    hub_owner = login_staff(env.hub, 'hq_owner', 'owner')
    r = hub_owner.post('/admin/settings/pos', data={'service_charge_percent': '10', 'service_charge_scope': 'pos_dine_in',
                                                    'allow_price_override': '1', 'auto_print_kitchen': '1',
                                                    'receipt_footer': '總部統一頁尾'})
    assert r.status_code == 302
    assert_flash(hub_owner, '並下發到所有分店')
    # 含金鑰的設定不會下發
    with tenant_ctx(env.hub) as t:
        settings_service.put('linepay', {'channel_id': 'HUB-ONLY', 'channel_secret': 'x'}, tenant_id=t.id)
        assert settings_service.publish_hq('linepay', t.id) is None
        settings_service.put('backup', {'time': '01:00'}, tenant_id=t.id)
        assert settings_service.publish_hq('backup', t.id) is None
        db.session.commit()
    env.sync_all()
    for node in (env.a, env.b):
        cfg, src = _get(node, 'pos')
        assert (cfg['service_charge_percent'], cfg['receipt_footer'], src) == (10, '總部統一頁尾', 'hq')
        assert _get(node, 'linepay')[0]['channel_id'] == '' and _get(node, 'backup')[0]['time'] == '03:30'

    # 分店 A 自訂 → 只影響 A；總部再改，A 不受影響、B 跟著改
    node_owner = login_staff(env.a, 'hq_owner')
    page = node_owner.get('/admin/settings/pos').get_data(as_text=True)
    assert '目前使用' in page and '總部設定' in page
    node_owner.post('/admin/settings/pos', data={'service_charge_percent': '5', 'service_charge_scope': 'pos_dine_in',
                                                 'receipt_footer': 'A 店頁尾'})
    assert _get(env.a, 'pos')[0]['service_charge_percent'] == 5 and _get(env.a, 'pos')[1] == 'local'
    hub_owner.post('/admin/settings/pos', data={'service_charge_percent': '12', 'service_charge_scope': 'pos_dine_in',
                                                'receipt_footer': '總部新頁尾'})
    env.sync_all()
    assert _get(env.a, 'pos')[0]['service_charge_percent'] == 5
    assert _get(env.b, 'pos')[0]['service_charge_percent'] == 12
    assert '本店自訂' in node_owner.get('/admin/settings/pos').get_data(as_text=True)
    # 改回總部設定
    node_owner.post('/admin/settings/pos', data={'action': 'revert_hq'})
    assert _get(env.a, 'pos')[0]['service_charge_percent'] == 12 and _get(env.a, 'pos')[1] == 'hq'
    # 金鑰設定頁標示「每台主機各自設定」
    assert '每台主機各自設定' in node_owner.get('/admin/settings/einvoice').get_data(as_text=True)


def test_branch_cannot_push_hq_settings(env):
    from app.models.settings import HqSetting
    with tenant_ctx(env.a) as t:
        row = HqSetting.query.filter_by(tenant_id=t.id, key='pos').first()
        row.value = '{"service_charge_percent": 99}'
        db.session.commit()
    env.sync(env.a)
    with tenant_ctx(env.hub) as t:
        assert '99' not in HqSetting.query.filter_by(tenant_id=t.id, key='pos').first().value
    # 分店本機被改的內容在下一次總部下發時會被覆蓋
    hub_owner = login_staff(env.hub, 'hq_owner')
    hub_owner.post('/admin/settings/pos', data={'service_charge_percent': '8', 'service_charge_scope': 'pos_dine_in'})
    env.sync_all()
    assert _get(env.a, 'pos')[0]['service_charge_percent'] == 8


# ---------------------------------------------------------------------------
# 會員分頁
# ---------------------------------------------------------------------------
def test_member_list_paginated_and_searchable(app, admin_client):
    for i in range(55):
        make_member(app, f'分頁會員{i:02d}', f'0955{i:06d}')
    page = admin_client.get('/admin?tab=users').get_data(as_text=True)
    assert len(re.findall(r'<tr data-user-id=', page)) == 50 and 'id="memberPager"' in page
    assert '分頁會員54' in page, '最新加入的在前'
    page2 = admin_client.get('/admin?tab=users&up=2').get_data(as_text=True)
    assert 'id="memberPager"' in page2 and len(re.findall(r'<tr data-user-id=', page2)) >= 5
    found = admin_client.get('/admin?tab=users&uq=0955000003').get_data(as_text=True)
    assert len(re.findall(r'<tr data-user-id=', found)) == 1 and '分頁會員03' in found
    assert '找不到符合的會員' in admin_client.get('/admin?tab=users&uq=不存在的人').get_data(as_text=True)
    uid = int(re.search(r'<tr data-user-id="(\d+)"', found).group(1))
    data = admin_client.get(f'/admin/users/{uid}/coupons').get_json()
    assert data['success'] is True and data['coupons'] == [] and data['unused'] == 0
    assert '"userCoupons"' not in page, '頁面不再內嵌所有會員的持券資料'


# ---------------------------------------------------------------------------
# 兩步驟驗證
# ---------------------------------------------------------------------------
def test_totp_rfc6238_vectors():
    secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'   # "12345678901234567890"
    assert security_service.totp_now(secret, at=59) == '287082'
    assert security_service.totp_now(secret, at=1111111109) == '081804'
    assert security_service.match_totp(secret, '287082', 0, at=59) == 1
    assert security_service.match_totp(secret, '287082', 1, at=59) is None, '同一組不可重複使用'
    assert security_service.match_totp(secret, '287082', 0, at=59 + 30) == 1, '容許前一個時間窗'
    assert security_service.match_totp(secret, '287082', 0, at=59 + 90) is None
    assert security_service.match_totp(secret, 'abc', 0) is None


def _login(app, username, ip, password=PASSWORD):
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/admin', data={'username': username, 'password': password}, headers={'X-Forwarded-For': ip})
    return c, r


def _secret(app, username):
    with app.app_context():
        return StaffAccount.query.filter_by(username=username).first().totp_secret


def test_enable_2fa_and_login_with_code_and_backup(app):
    login_staff(app, 'tfa_mgr', 'manager')
    c, r = _login(app, 'tfa_mgr', '10.88.0.1')
    assert r.status_code == 302
    page = c.get('/admin/security').get_data(as_text=True)
    assert '開始設定' in page
    c.post('/admin/security', data={'action': 'start_2fa'})
    page = c.get('/admin/security').get_data(as_text=True)
    secret = re.search(r'id="totpSecret">([A-Z2-7]+)<', page).group(1)
    assert 'data:image/svg+xml;base64' in page
    c.post('/admin/security', data={'action': 'confirm_2fa', 'code': '000000'})
    assert_flash(c, '驗證碼不正確')
    page = c.post('/admin/security', data={'action': 'confirm_2fa', 'code': security_service.totp_now(secret)}).get_data(as_text=True)
    codes = re.findall(r'<div class="col">(\d{4}-\d{4})</div>', page)
    assert len(codes) == 10
    assert _secret(app, 'tfa_mgr') == secret

    # 登入：密碼正確 → 第二步
    c2, r = _login(app, 'tfa_mgr', '10.88.0.2')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa')
    assert c2.get('/admin/staff').status_code in (302, 401, 403), '還沒通過第二步不算登入'
    assert c2.post('/admin/2fa', data={'code': '123456'}).status_code == 401
    # 同一時間窗的驗證碼已在啟用時用過 → 等下一個時間窗或用備用碼
    r = c2.post('/admin/2fa', data={'code': codes[0]})
    assert r.status_code == 302
    assert c2.get('/admin').status_code == 200 and '帳號安全' in c2.get('/admin').get_data(as_text=True)
    # 備用碼只能用一次
    c3, _ = _login(app, 'tfa_mgr', '10.88.0.3')
    assert c3.post('/admin/2fa', data={'code': codes[0]}).status_code == 401
    # 下一個時間窗的 TOTP 可以登入
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='tfa_mgr').first()
        acc.totp_last_counter = 0
        db.session.commit()
    assert c3.post('/admin/2fa', data={'code': security_service.totp_now(secret)}).status_code == 302

    # 停用需要密碼 + 驗證碼
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='tfa_mgr').first()
        acc.totp_last_counter = 0
        db.session.commit()
    c3.post('/admin/security', data={'action': 'disable_2fa', 'password': 'wrong', 'code': security_service.totp_now(secret)})
    assert_flash(c3, '密碼不正確')
    c3.post('/admin/security', data={'action': 'disable_2fa', 'password': PASSWORD, 'code': codes[1]})
    with app.app_context():
        assert StaffAccount.query.filter_by(username='tfa_mgr').first().totp_enabled is False


def test_2fa_pending_expires_and_locks(app):
    login_staff(app, 'tfa_lock', 'manager')
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='tfa_lock').first()
        acc.totp_secret, acc.totp_enabled = security_service.new_secret(), True
        db.session.commit()
    c, _ = _login(app, 'tfa_lock', '10.88.1.1')
    for _ in range(5):
        c.post('/admin/2fa', data={'code': '000000'})
    r = c.post('/admin/2fa', data={'code': security_service.totp_now(_secret(app, 'tfa_lock'))})
    assert r.status_code == 302 and c.get('/admin/staff').status_code != 200, '錯誤太多次鎖定，正確驗證碼也不放行'
    c2 = app.test_client()
    with c2.session_transaction() as s:
        s[security_service.PENDING_KEY] = {'id': 1, 'v': 'x', 'at': time.time() - 3600}
    assert c2.get('/admin/2fa').status_code == 302


def test_required_2fa_redirects_until_enabled(app, store_settings):
    store_settings('security', require_2fa_roles=['manager'])
    mgr = login_staff(app, 'tfa_required', 'manager')
    r = mgr.get('/admin')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/security')
    r = mgr.get('/api/pos/today_orders')
    assert r.status_code == 403 and r.get_json()['need_2fa_setup'] is True
    assert mgr.get('/admin/security').status_code == 200
    # 員工 (staff) 不在要求名單
    assert login_staff(app, 'tfa_not_required', 'staff').get('/pos').status_code == 200


def test_owner_resets_staff_2fa(app, admin_client):
    login_staff(app, 'tfa_lost_phone', 'staff')
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='tfa_lost_phone').first()
        acc.totp_secret, acc.totp_enabled = security_service.new_secret(), True
        db.session.commit()
        acc_id = acc.id
    assert '2FA' in admin_client.get('/admin/staff').get_data(as_text=True)
    admin_client.post('/admin/staff', data={'action': 'reset_2fa', 'account_id': acc_id})
    with app.app_context():
        acc = db.session.get(StaffAccount, acc_id)
        assert acc.totp_enabled is False and acc.totp_secret == ''
    c, r = _login(app, 'tfa_lost_phone', '10.88.2.1')
    assert r.status_code == 302 and not r.headers['Location'].endswith('/admin/2fa')


# ---------------------------------------------------------------------------
# 登入裝置
# ---------------------------------------------------------------------------
def test_sessions_revoke_logout_all_and_idle(app, admin_client, store_settings):
    login_staff(app, 'sess_mgr', 'manager')
    a, _ = _login(app, 'sess_mgr', '10.88.3.1')
    b, _ = _login(app, 'sess_mgr', '10.88.3.2')
    assert a.get('/admin').status_code == 200
    assert b.get('/admin').status_code == 200
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='sess_mgr').first()
        rows = StaffSession.query.filter_by(staff_account_id=acc.id, revoked_at=None).all()
        assert len(rows) == 2 and {r.ip_address for r in rows} == {'10.88.3.1', '10.88.3.2'}
        b_row_id = next(r.id for r in rows if r.ip_address == '10.88.3.2')
        acc_id = acc.id
    page = a.get('/admin/security').get_data(as_text=True)
    assert '10.88.3.2' in page and '這台' in page
    a.post('/admin/security', data={'action': 'revoke_session', 'session_id': b_row_id})
    assert b.get('/admin').get_data(as_text=True).count('name="password"') >= 1, 'B 被登出，回到登入頁'
    assert a.get('/admin').status_code == 200

    # 閒置逾時
    store_settings('security', staff_idle_minutes=5)
    with app.app_context():
        row = StaffSession.query.filter_by(staff_account_id=acc_id, revoked_at=None).one()
        row.last_seen = datetime.now() - timedelta(minutes=30)
        db.session.commit()
        row_id = row.id
    assert 'name="password"' in a.get('/admin').get_data(as_text=True)
    with app.app_context():
        assert '閒置' in db.session.get(StaffSession, row_id).revoked_reason
    store_settings('security', staff_idle_minutes=0)

    # 負責人強制登出 (包含沒有工作階段紀錄的舊登入)
    c, _ = _login(app, 'sess_mgr', '10.88.3.3')
    legacy = login_staff(app, 'sess_mgr')
    assert c.get('/admin').status_code == 200 and legacy.get('/admin').status_code == 200
    admin_client.post('/admin/staff', data={'action': 'logout_all', 'account_id': acc_id})
    assert 'name="password"' in c.get('/admin').get_data(as_text=True)
    assert 'name="password"' in legacy.get('/admin').get_data(as_text=True)
    # 重新登入正常
    d, r = _login(app, 'sess_mgr', '10.88.3.4')
    assert d.get('/admin').status_code == 200
    # 登出會註銷這一筆
    d.post('/admin/logout')
    with app.app_context():
        assert StaffSession.query.filter_by(staff_account_id=acc_id, revoked_at=None).count() == 0
