"""修正小組 auth-security-tenants 的回歸測試 (帳號安全、裝置鎖定、登入節流、稽核頁、平台店家管理)"""
import time
from datetime import datetime, timedelta

from app.extensions import db, limiter
from app.models.tenant import StaffAccount, StaffSession
from app.services import auth_throttle, security_service
from tests.support import default_tenant_id, get_or_create_tenant, login_staff, make_member

PASSWORD = 'StaffPass!2026'


def _staff(app, username, role='manager', tenant_id=None):
    with app.app_context():
        acc = StaffAccount.query.filter_by(username=username).first()
        if acc is None:
            acc = StaffAccount(tenant_id=tenant_id or default_tenant_id(app), username=username, role=role, is_active=True)
            acc.set_password(PASSWORD)
            db.session.add(acc)
            db.session.commit()
        return acc.id


def _password_login(app, username, ip, password=PASSWORD, client=None):
    c = client or app.test_client()
    c.get('/?shop=default')
    r = c.post('/admin', data={'username': username, 'password': password}, headers={'X-Forwarded-For': ip})
    return c, r


def _register_device(app, tid, name='fix-ast 裝置'):
    from flask import g

    from app.services import device_service
    with app.app_context():
        g.tenant_id = tid
        d = device_service.create(tid, name, 'pos')
        db.session.commit()
        d, raw = device_service.pair(tid, d.pair_code)
        db.session.commit()
        return d.id, raw


def _enable_2fa(app, username):
    with app.app_context():
        acc = StaffAccount.query.filter_by(username=username).first()
        acc.totp_secret, acc.totp_enabled, acc.totp_last_counter = security_service.new_secret(), True, 0
        codes, acc.totp_backup_codes = security_service.new_backup_codes()
        db.session.commit()
        return acc.totp_secret, codes


# ---------------------------------------------------------------------------
# B-362 / B-361：帳號安全頁
# ---------------------------------------------------------------------------
def test_revoke_session_with_non_numeric_id_is_404(app):
    c = login_staff(app, 'fix_ast_revoke', 'manager')
    r = c.post('/admin/security', data={'action': 'revoke_session', 'session_id': 'abc'},
               headers={'Accept': 'application/json'})
    assert r.status_code == 404


def test_disable_2fa_is_throttled_and_requires_enabled(app):
    _staff(app, 'fix_ast_disable')
    c, r = _password_login(app, 'fix_ast_disable', '10.91.0.1')
    assert r.status_code == 302
    # 未啟用 2FA：直接拒絕，不比對密碼
    r = c.post('/admin/security', data={'action': 'disable_2fa', 'password': 'guess'}, headers={'Accept': 'application/json'})
    assert r.status_code == 400 and '尚未啟用' in r.get_json()['message']
    secret, _codes = _enable_2fa(app, 'fix_ast_disable')
    msgs = set()
    for _ in range(auth_throttle.STAFF_DEVICE_MAX_FAILS):
        r = c.post('/admin/security', data={'action': 'disable_2fa', 'password': 'guess', 'code': '000000'},
                   headers={'Accept': 'application/json', 'X-Forwarded-For': '10.91.0.1'})
        assert r.status_code == 400
        msgs.add(r.get_json()['message'])
    # 密碼正確但驗證碼錯：訊息相同 (無法藉此判斷密碼是否正確)
    with app.app_context():
        auth_throttle.clear_keys(*auth_throttle.staff_keys('fix_ast_disable', '10.91.0.1').values())
    r = c.post('/admin/security', data={'action': 'disable_2fa', 'password': PASSWORD, 'code': '000000'},
               headers={'Accept': 'application/json', 'X-Forwarded-For': '10.91.0.1'})
    msgs.add(r.get_json()['message'])
    assert len(msgs) == 1
    for _ in range(auth_throttle.STAFF_DEVICE_MAX_FAILS):
        c.post('/admin/security', data={'action': 'disable_2fa', 'password': 'guess', 'code': '000000'},
               headers={'Accept': 'application/json', 'X-Forwarded-For': '10.91.0.1'})
    # 鎖定期間連正確的密碼 + 驗證碼也不放行
    r = c.post('/admin/security', data={'action': 'disable_2fa', 'password': PASSWORD,
                                        'code': security_service.totp_now(secret)},
               headers={'Accept': 'application/json', 'X-Forwarded-For': '10.91.0.1'})
    assert r.status_code == 429
    with app.app_context():
        assert StaffAccount.query.filter_by(username='fix_ast_disable').first().totp_enabled is True
        auth_throttle.clear_keys(*auth_throttle.staff_keys('fix_ast_disable', '10.91.0.1').values())
    limiter.reset()


# ---------------------------------------------------------------------------
# R2-132：手機遺失 → 以備用碼重新綁定驗證 App
# ---------------------------------------------------------------------------
def test_rebind_authenticator_with_backup_code(app, store_settings):
    store_settings('security', require_2fa_roles=['manager'])
    _staff(app, 'fix_ast_rebind')
    old_secret, codes = _enable_2fa(app, 'fix_ast_rebind')
    c, r = _password_login(app, 'fix_ast_rebind', '10.91.1.1')
    assert r.headers['Location'].endswith('/admin/2fa')
    assert c.post('/admin/2fa', data={'code': codes[0]}).status_code == 302
    # 密碼錯：不開始重新綁定
    c.post('/admin/security', data={'action': 'rebind_2fa', 'password': 'wrong', 'code': codes[1]})
    with c.session_transaction() as s:
        assert 'totp_rebind' not in s
    c.post('/admin/security', data={'action': 'rebind_2fa', 'password': PASSWORD, 'code': codes[1]})
    with c.session_transaction() as s:
        new_secret = s['totp_setup']
        assert s['totp_rebind'] is True
    with app.app_context():   # 新的 App 確認前舊的設定仍然有效
        acc = StaffAccount.query.filter_by(username='fix_ast_rebind').first()
        assert acc.totp_enabled and acc.totp_secret == old_secret
    page = c.post('/admin/security', data={'action': 'confirm_2fa', 'code': security_service.totp_now(new_secret)})
    assert page.status_code == 200
    with app.app_context():
        from app.models.user import StaffAuditLog
        acc = StaffAccount.query.filter_by(username='fix_ast_rebind').first()
        assert acc.totp_enabled and acc.totp_secret == new_secret
        assert security_service.remaining_backup_codes(acc) == security_service.BACKUP_CODE_COUNT
        assert StaffAuditLog.query.filter_by(action_type='STAFF_2FA_REBIND', staff_username='fix_ast_rebind').count() == 1
    store_settings('security', require_2fa_roles=[])


def test_confirm_2fa_cannot_replace_secret_without_rebind(app):
    _staff(app, 'fix_ast_confirm')
    old_secret, _codes = _enable_2fa(app, 'fix_ast_confirm')
    c = login_staff(app, 'fix_ast_confirm')
    forged = security_service.new_secret()
    with c.session_transaction() as s:
        s['totp_setup'] = forged
    c.post('/admin/security', data={'action': 'confirm_2fa', 'code': security_service.totp_now(forged)})
    with app.app_context():
        assert StaffAccount.query.filter_by(username='fix_ast_confirm').first().totp_secret == old_secret


# ---------------------------------------------------------------------------
# R2-131：別家店的裝置 Cookie 不可躲過帳號層級鎖定
# ---------------------------------------------------------------------------
def test_other_store_device_cookie_does_not_bypass_account_lock(app):
    _staff(app, 'fix_ast_victim', 'owner')
    other_tid = get_or_create_tenant(app, 'fix-ast-attacker-shop')
    _did, raw = _register_device(app, other_tid, 'attacker pos')
    attacker = app.test_client()
    attacker.set_cookie('kiosk_device', raw)
    attacker.get('/shop/fix-ast-attacker-shop')   # session 停在攻擊者自己的店 (裝置在這裡算本店裝置)
    for i in range(auth_throttle.STAFF_ACCOUNT_MAX_FAILS):
        limiter.reset()
        attacker.post('/admin', data={'username': 'fix_ast_victim', 'password': 'wrong'},
                      headers={'X-Forwarded-For': f'203.0.114.{i}'})
    limiter.reset()
    r = attacker.post('/admin', data={'username': 'fix_ast_victim', 'password': PASSWORD},
                      headers={'X-Forwarded-For': '203.0.114.200'})
    assert r.status_code == 429, '帳號層級鎖定照樣生效'
    with app.app_context():
        auth_throttle.clear_keys('staff:acct:fix_ast_victim')


# ---------------------------------------------------------------------------
# B-359：遠端鎖定的裝置仍可登出、完成 2FA 登入
# ---------------------------------------------------------------------------
def test_locked_device_allows_logout_and_two_factor(app):
    from app.models.integration import Device
    tid = default_tenant_id(app)
    did, raw = _register_device(app, tid, 'fix-ast locked pos')
    with app.app_context():
        db.session.get(Device, did).is_locked = True
        db.session.commit()
    _staff(app, 'fix_ast_locked')
    secret, _codes = _enable_2fa(app, 'fix_ast_locked')
    c = app.test_client()
    c.set_cookie('kiosk_device', raw)
    c, r = _password_login(app, 'fix_ast_locked', '10.91.2.1', client=c)
    assert r.headers['Location'].endswith('/admin/2fa')
    r = c.post('/admin/2fa', data={'code': security_service.totp_now(secret)})
    assert r.status_code == 302 and c.get('/admin').status_code == 200
    assert c.post('/admin/logout').status_code == 302
    assert 'name="password"' in c.get('/admin').get_data(as_text=True)
    assert c.post('/api/orders/1/status', json={'status': 'Completed'}).status_code == 423, '其他操作仍被擋'


# ---------------------------------------------------------------------------
# B-358：PIN 錯誤次數先預扣、原子計數
# ---------------------------------------------------------------------------
def test_reserve_pin_attempt_blocks_after_budget(app):
    tid = default_tenant_id(app)
    keys = auth_throttle.pin_keys(tid, '0977000358', '10.91.3.1')
    with app.app_context():
        results = [auth_throttle.reserve_pin_attempt(keys) for _ in range(auth_throttle.DEVICE_MAX_FAILS + 2)]
        assert results[:auth_throttle.DEVICE_MAX_FAILS] == [None] * auth_throttle.DEVICE_MAX_FAILS
        assert all(r is not None for r in results[auth_throttle.DEVICE_MAX_FAILS:])
        assert auth_throttle.lock_remaining(keys.values()) is not None
        auth_throttle.clear_keys(*keys.values())


def test_record_failure_counts_atomically(app):
    from app.models.system import AuthThrottle
    with app.app_context():
        key = 'test:fix-ast:atomic'
        auth_throttle.clear_keys(key)
        for _ in range(3):
            auth_throttle.record_failure(key, 10)
        db.session.commit()
        # 模擬另一個請求在同一時間寫入：本交易持有的舊物件不會把計數覆蓋回去
        db.session.execute(db.update(AuthThrottle).where(AuthThrottle.key == key).values(fails=AuthThrottle.fails + 1))
        auth_throttle.record_failure(key, 10)
        db.session.commit()
        assert db.session.get(AuthThrottle, key).fails == 5
        auth_throttle.clear_keys(key)


# ---------------------------------------------------------------------------
# R2-134：沒有 PIN 的會員與空號回應一致
# ---------------------------------------------------------------------------
def test_member_without_pin_indistinguishable(app):
    make_member(app, '無 PIN 會員 fix', '0977013400')
    c = app.test_client()
    c.get('/?shop=default')
    no_pin = c.post('/phone_login', json={'phone': '0977013400', 'pin': '1357'}, headers={'X-Forwarded-For': '10.91.4.1'})
    no_phone = c.post('/phone_login', json={'phone': '0977013499', 'pin': '1357'}, headers={'X-Forwarded-For': '10.91.4.2'})
    assert no_pin.status_code == no_phone.status_code == 401
    assert no_pin.get_json()['message'] == no_phone.get_json()['message']


# ---------------------------------------------------------------------------
# R2-133 / B-365：閒置登出與登入中的裝置
# ---------------------------------------------------------------------------
def test_background_polling_does_not_keep_session_alive(app, store_settings):
    _staff(app, 'fix_ast_idle')
    c, r = _password_login(app, 'fix_ast_idle', '10.91.5.1')
    assert r.status_code == 302
    store_settings('security', staff_idle_minutes=5)
    try:
        with c.session_transaction() as s:
            s[security_service.ACTIVE_KEY] = time.time() - 600   # 10 分鐘前最後一次操作
            s['staff_sid_seen'] = time.time()                    # 但背景輪詢一直有送請求
        r = c.get('/api/admin_live_orders', headers={'Accept': 'application/json'})
        assert r.status_code in (302, 401)
        with app.app_context():
            acc_id = StaffAccount.query.filter_by(username='fix_ast_idle').first().id
            row = StaffSession.query.filter_by(staff_account_id=acc_id).order_by(StaffSession.id.desc()).first()
            assert '閒置' in (row.revoked_reason or '')
    finally:
        store_settings('security', staff_idle_minutes=0)


def test_background_request_is_not_user_activity(app):
    with app.test_request_context('/api/admin_live_orders', headers={'Accept': 'application/json'}):
        assert security_service.is_user_activity() is False
    with app.test_request_context('/admin', headers={'Accept': 'text/html,application/xhtml+xml'}):
        assert security_service.is_user_activity() is True
    with app.test_request_context('/api/orders/1/status', method='POST'):
        assert security_service.is_user_activity() is True


def test_active_sessions_hide_expired_logins(app):
    acc_id = _staff(app, 'fix_ast_sessions')
    with app.test_request_context('/'):
        now = datetime.now()
        db.session.add_all([
            StaffSession(staff_account_id=acc_id, token_hash='fix-ast-old', created_at=now - timedelta(days=2),
                         last_seen=now - timedelta(days=2), ip_address='10.0.0.1', user_agent='', device='old'),
            StaffSession(staff_account_id=acc_id, token_hash='fix-ast-new', created_at=now, last_seen=now,
                         ip_address='10.0.0.2', user_agent='', device='new'),
        ])
        db.session.commit()
        acc = db.session.get(StaffAccount, acc_id)
        assert [s.device for s in security_service.active_sessions(acc)] == ['new']


# ---------------------------------------------------------------------------
# B-360 / B-364：稽核頁
# ---------------------------------------------------------------------------
def test_audit_search_with_odd_digits(app, admin_client):
    for q in ('²', '99999999999999999999', '#12'):
        assert admin_client.get('/admin/audit', query_string={'q': q}).status_code == 200
    assert admin_client.get('/admin/audit/export', query_string={'q': '²'}).status_code == 200


def test_action_labels_cover_refunds_and_security():
    from app.services.audit import ACTION_LABELS
    assert 'REFUND' not in ACTION_LABELS
    for key in ('REFUND_DONE', 'PARTIAL_REFUND', 'STAFF_2FA_DISABLE', 'STAFF_2FA_RESET', 'STAFF_LOGOUT_ALL',
                'STAFF_PERMISSIONS', 'WALLET_ADJUST', 'DEVICE'):
        assert key in ACTION_LABELS


# ---------------------------------------------------------------------------
# B-363 / R2-136：平台店家管理
# ---------------------------------------------------------------------------
def test_tenant_overview_counts_revenue_and_live_staff(app):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    from app.routes.tenants_admin import tenant_overview
    tid = get_or_create_tenant(app, 'fix-ast-overview')
    with app.app_context():
        db.session.add_all([
            Order(tenant_id=tid, total_price=100, status=OrderStatus.COMPLETED, payment_status=''),
            Order(tenant_id=tid, total_price=200, status=OrderStatus.PENDING, payment_status='paid'),
            Order(tenant_id=tid, total_price=5000, status=OrderStatus.PENDING, payment_status='unpaid'),
            Order(tenant_id=tid, total_price=7000, status=OrderStatus.PENDING, payment_status='pending'),
            StaffAccount(tenant_id=tid, username='fix_ast_ov_live', role='staff', is_active=True, password_hash='x'),
            StaffAccount(tenant_id=tid, username='fix_ast_ov_gone', role='staff', is_active=False, password_hash='x',
                         deleted_at=datetime.now()),
        ])
        db.session.commit()
    with app.test_request_context('/'):
        row = next(r for r in tenant_overview() if r['id'] == tid)
    assert row['revenue'] == 300 and row['orders'] == 2
    assert row['staff'] == 1


def test_tenant_page_does_not_link_storefront(app):
    platform = login_staff(app, 'fix_ast_platform', 'owner')
    html = platform.get('/admin/tenants').get_data(as_text=True)
    table = html[html.index('id="tenantTable"'):]
    assert 'href="/shop/' not in table and '/shop/default' in table and '無痕視窗' in html


# ---------------------------------------------------------------------------
# R2-135：裝置管理的重新配對 / 刪除需要確認
# ---------------------------------------------------------------------------
def test_device_delete_and_pair_need_confirm(app, admin_client):
    _register_device(app, default_tenant_id(app), 'fix-ast confirm pos')
    html = admin_client.get('/admin/devices').get_data(as_text=True)
    assert 'data-confirm="確定要刪除裝置「fix-ast confirm pos」' in html
    assert 'data-confirm="確定要重新配對「fix-ast confirm pos」' in html


# ---------------------------------------------------------------------------
# 跨檔請求：B-351 配對網址帶店家代號
# ---------------------------------------------------------------------------
def test_device_page_pair_url_has_shop(app):
    tid = get_or_create_tenant(app, 'fix-ast-pairurl')
    c = login_staff(app, 'fix_ast_pairurl_mgr', 'manager', tenant_id=tid)
    html = c.get('/admin/devices').get_data(as_text=True)
    assert '/device/pair?shop=fix-ast-pairurl' in html


# ---------------------------------------------------------------------------
# 跨檔請求：R2-113 首次啟用 2FA 要輸入目前密碼
# ---------------------------------------------------------------------------
def _start_setup(c):
    c.post('/admin/security', data={'action': 'start_2fa'})
    with c.session_transaction() as s:
        return s['totp_setup']


def test_confirm_2fa_requires_current_password(app):
    _staff(app, 'fix_ast_enable_pw')
    c = login_staff(app, 'fix_ast_enable_pw')
    secret = _start_setup(c)
    for pw in (None, 'wrong-password'):
        data = {'action': 'confirm_2fa', 'code': security_service.totp_now(secret)}
        if pw:
            data['current_password'] = pw
        r = c.post('/admin/security', data=data, headers={'Accept': 'application/json'})
        assert r.status_code == 400 and '需輸入目前密碼' in r.get_json()['message']
    with app.app_context():
        assert StaffAccount.query.filter_by(username='fix_ast_enable_pw').first().totp_enabled is False
    r = c.post('/admin/security', data={'action': 'confirm_2fa', 'current_password': PASSWORD,
                                        'code': security_service.totp_now(secret)})
    assert r.status_code == 200
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='fix_ast_enable_pw').first()
        assert acc.totp_enabled and acc.totp_secret == secret
        auth_throttle.clear_keys(*auth_throttle.staff_keys('fix_ast_enable_pw', '127.0.0.1').values())


# ---------------------------------------------------------------------------
# 跨檔請求：B-301 / B-175 分店上的負責人不可變更 2FA，也不被強制導去設定
# ---------------------------------------------------------------------------
def test_node_owner_cannot_change_2fa(app, monkeypatch, store_settings):
    owner = login_staff(app, 'fix_ast_node_owner', 'owner')
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
    for action in ('start_2fa', 'confirm_2fa', 'regen_backup', 'disable_2fa', 'rebind_2fa'):
        r = owner.post('/admin/security', data={'action': action, 'code': '000000', 'password': PASSWORD},
                       headers={'Accept': 'application/json'})
        assert r.status_code == 403 and '線上端' in r.get_json()['message'], action
    with owner.session_transaction() as s:
        assert 'totp_setup' not in s
    # 本店要求負責人啟用 2FA：分店上不強制導去帳號安全頁 (否則負責人會卡住)
    store_settings('security', require_2fa_roles=['owner'])
    r = owner.get('/admin/staff')
    assert not (r.status_code == 302 and '/admin/security' in r.headers.get('Location', ''))
    # 單機 / 線上端照常強制
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'standalone')
    r = owner.get('/admin/staff')
    assert r.status_code == 302 and '/admin/security' in r.headers['Location']


# ---------------------------------------------------------------------------
# 跨檔請求：R2-050 人臉辨識不可用時不提供人臉功能
# ---------------------------------------------------------------------------
def test_face_unavailable_hides_face_features(app, monkeypatch):
    import app.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes, 'face_available', lambda: False)
    c = app.test_client()
    c.get('/?shop=default')
    r = c.get('/face_login')
    assert r.status_code == 200
    assert '"faceEnabled": false' in r.get_data(as_text=True)
    r = c.post('/face_login', data={}, headers={'Accept': 'application/json'})
    assert r.status_code == 503 and '人臉辨識暫不可用' in r.get_json()['message']
    assert '"faceEnabled": false' in c.get('/register').get_data(as_text=True)
    r = c.post('/register', data={'name': '人臉不可用', 'phone': '0937505050', 'pin': '2580', 'agree_privacy': '1',
                                  'face_consent': '1'}, headers={'Accept': 'application/json'})
    assert r.status_code == 400 and '人臉辨識暫不可用' in r.get_json()['message']


# ---------------------------------------------------------------------------
# 跨檔請求：R2-045 註冊需勾選才綁定 LINE；R2-139 姓名去掉控制字元
# ---------------------------------------------------------------------------
def _line_pending(tid, line_id, token):
    return {'user_id': line_id, 'name': '待綁定', 'tenant_id': tid, 'at': datetime.now().isoformat(), 'token': token}


def test_register_binds_line_only_with_token_and_strips_control_chars(app):
    from app.models.user import User
    from app.tenant_context import tenant_bypass
    tid = default_tenant_id(app)
    base = {'pin': '2580', 'agree_privacy': '1'}
    c = app.test_client()
    c.get('/?shop=default')
    with c.session_transaction() as s:
        s['line_pending'] = _line_pending(tid, 'U-fix-ast-nobind', 'tok-a')
    r = c.post('/register', data={**base, 'name': '不綁\x0b定', 'phone': '0937515151'})
    assert r.status_code == 302
    c2 = app.test_client()
    c2.get('/?shop=default')
    with c2.session_transaction() as s:
        s['line_pending'] = _line_pending(tid, 'U-fix-ast-bind', 'tok-b')
    r = c2.post('/register', data={**base, 'name': '要綁定', 'phone': '0937525252', 'bind_line_token': 'tok-b'})
    assert r.status_code == 302
    with app.app_context():
        with tenant_bypass():
            a = User.query.filter_by(phone='0937515151').first()
            b = User.query.filter_by(phone='0937525252').first()
            assert a.name == '不綁定' and not a.line_user_id
            assert b.line_user_id == 'U-fix-ast-bind'


# ---------------------------------------------------------------------------
# 跨檔請求：R2-139 稽核匯出去掉 Excel 不接受的控制字元；B-305 稽核帳號截斷
# ---------------------------------------------------------------------------
def test_audit_export_strips_illegal_chars(app, admin_client):
    from flask import g

    from app.models.user import StaffAuditLog
    with app.test_request_context('/'):
        g.tenant_id = default_tenant_id(app)
        from app.services.audit import record_staff_audit
        record_staff_audit('EDIT_USER', details='會員 王\x0b小明', as_system=True)
        db.session.commit()
    r = admin_client.get('/admin/audit/export')
    assert r.status_code == 200 and r.data[:2] == b'PK'
    with app.app_context():
        assert StaffAuditLog.query.filter(StaffAuditLog.details.like('%小明%')).count() >= 1


def test_record_staff_audit_truncates_username(app, monkeypatch):
    from types import SimpleNamespace

    from flask import g

    import app.services.audit as audit
    fake = SimpleNamespace(is_authenticated=True, id=None, username='u' * 200, role='staff')
    monkeypatch.setattr(audit, 'current_user', fake)
    with app.test_request_context('/'):
        g.tenant_id = default_tenant_id(app)
        row = audit.record_staff_audit('EDIT_USER', details='x')
        assert len(row.staff_username) == 120
        db.session.rollback()
