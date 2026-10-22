"""員工登入登出、帳號管理、角色權限矩陣 (負責人 / 店長 / 櫃檯 / 廚房)、未登入一律擋下"""
import re

from app.extensions import db
from app.models.tenant import StaffAccount
from app.models.user import AdminLog, StaffAuditLog
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import (
    assert_flash,
    extract_csrf,
    flashes,
    reward_setting_values,
)


def test_owner_login_logout(app, csrf_enabled):
    """負責人登入、錯誤密碼 401、登出寫入 AdminLog.logout_at"""
    c = app.test_client()
    html = c.get('/admin').get_data(as_text=True)
    assert 'name="csrf_token"' in html, '登入表單缺少 csrf_token 隱藏欄位，瀏覽器登入會失敗'
    token = extract_csrf(html)
    bad = c.post('/admin', data={'username': ADMIN_USERNAME, 'password': 'wrong', 'csrf_token': token},
                 headers={'X-Forwarded-For': '10.10.1.1'})
    assert bad.status_code == 401 and '帳號或密碼錯誤' in bad.get_data(as_text=True)
    ok = c.post('/admin', data={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD, 'csrf_token': token},
                headers={'X-Forwarded-For': '10.10.1.2'})
    assert ok.status_code == 302
    assert c.get('/admin/logout').status_code == 405, '登出只接受 POST (避免其他網站以連結讓員工被登出)'
    assert c.post('/admin/logout').status_code == 403, '登出需要 CSRF Token'
    token = extract_csrf(c.get('/admin').get_data(as_text=True))  # 登入後換了新的工作階段
    assert c.post('/admin/logout', data={'csrf_token': token}).status_code == 302
    with app.app_context():
        log = AdminLog.query.filter_by(username=ADMIN_USERNAME).order_by(AdminLog.id.desc()).first()
        assert log is not None and log.logout_at is not None
    assert c.get('/api/kitchen_orders').status_code == 401, '登出後不應保有後台權限'


def test_staff_account_management(app, admin_client):
    """員工帳號建立、重複、防自鎖、停用/啟用、重設密碼、軟刪除與稽核"""
    post = lambda **form: admin_client.post('/admin/staff', data=form)  # noqa: E731
    assert post(action='create', username='short_u', password='123', role='staff').status_code == 302
    assert_flash(admin_client, '密碼至少 10 個字元')

    for name, role in [('mgr_user', 'manager'), ('pos_user', 'staff'), ('kds_user', 'kitchen'), ('delete_user', 'staff')]:
        assert post(action='create', username=name, password='StaffPass!2026', role=role).status_code == 302
    assert not [m for m in flashes(admin_client) if '❌' in m]
    post(action='create', username='pos_user', password='StaffPass!2026', role='staff')
    assert_flash(admin_client, '已被使用')

    with app.app_context():
        ids = {a.username: a.id for a in StaffAccount.query.all()}
    owner_id, pos_id, del_id = ids[ADMIN_USERNAME], ids['pos_user'], ids['delete_user']

    post(action='toggle_active', account_id=owner_id)
    assert_flash(admin_client, '無法停用或刪除您目前登入的負責人帳號')
    post(action='delete_account', account_id=owner_id)
    assert_flash(admin_client, '無法停用或刪除您目前登入的負責人帳號')

    def password_login(username, password, ip):
        return app.test_client().post('/admin', data={'username': username, 'password': password},
                                      headers={'X-Forwarded-For': ip}).status_code

    assert post(action='toggle_active', account_id=pos_id).status_code == 302
    assert password_login('pos_user', 'StaffPass!2026', '10.10.1.4') == 401, '已停用員工仍可登入'
    post(action='toggle_active', account_id=pos_id)
    assert post(action='reset_password', account_id=pos_id, new_password='NewStaffPass!2026').status_code == 302
    assert_flash(admin_client, '已成功重設員工【pos_user】的密碼')
    assert password_login('pos_user', 'NewStaffPass!2026', '10.10.1.5') == 302

    assert post(action='delete_account', account_id=del_id).status_code == 302
    with app.app_context():
        soft = db.session.get(StaffAccount, del_id)
        assert soft.deleted_at is not None and soft.is_active is False
        assert soft.username == f'delete_user#deleted-{del_id}' and not soft.check_password('StaffPass!2026')
        audit = StaffAuditLog.query.filter_by(action_type='STAFF_DELETE_ACCOUNT').first()
        assert f'ID={del_id}' in audit.details and 'delete_user' in audit.details
    assert password_login('delete_user', 'StaffPass!2026', '10.10.1.96') == 401
    assert f'#deleted-{del_id}' not in admin_client.get('/admin/staff').get_data(as_text=True)
    assert post(action='toggle_active', account_id=del_id).status_code == 404
    assert post(action='reset_password', account_id=del_id, new_password='AnotherPass!2026').status_code == 404
    assert post(action='create', username='delete_user', password='StaffPass!2026', role='staff').status_code == 302
    with app.app_context():
        assert StaffAccount.query.filter_by(username='delete_user').first().id != del_id
        actions = {a.action_type for a in StaffAuditLog.query.all()}
        assert {'STAFF_CREATE', 'STAFF_TOGGLE_ACTIVE', 'STAFF_RESET_PASSWORD', 'STAFF_DELETE_ACCOUNT'} <= actions


def test_rbac_matrix(app, manager_client, staff_client, kitchen_client):
    """訪客 / kitchen / staff / manager 的頁面與 API 權限邊界"""
    anon = app.test_client()
    anon.get('/?shop=default')
    assert anon.get('/pos').status_code == 302
    assert anon.post('/api/pos/member_lookup', json={'query': '09'}).status_code == 401
    assert anon.post('/api/pos/checkout', json={'items': []}).status_code == 401
    assert anon.get('/api/kitchen_orders').status_code == 401

    assert kitchen_client.get('/kitchen').status_code == 200
    assert kitchen_client.get('/pos').status_code == 403
    assert kitchen_client.post('/api/pos/checkout', json={'items': []}).status_code == 403
    assert kitchen_client.post('/admin/add', data={'name': '越權餐', 'price': '100'}).status_code == 403
    assert kitchen_client.get('/admin/staff').status_code == 403

    assert staff_client.get('/pos').status_code == 200
    assert staff_client.get('/pickup').status_code == 200
    assert staff_client.post('/admin/add', data={'name': '越權餐', 'price': '100'}).status_code == 403
    assert staff_client.post('/admin/delete/1').status_code == 403
    assert staff_client.get('/admin/staff').status_code == 403

    assert manager_client.get('/admin').status_code == 200
    assert manager_client.get('/pos').status_code == 200
    assert manager_client.get('/admin/staff').status_code == 403


def test_admin_apis_require_login(app):
    anon = app.test_client()
    assert anon.get('/api/admin_live_orders').status_code == 401
    assert anon.post('/api/generate_item_description', json={'name': '測試'}).status_code == 401


def test_kitchen_and_staff_cannot_use_manager_routes(app, kitchen_client, staff_client, reward_setting_snapshot):
    probes = [('/admin/update_reward_setting', {'data': {'points_per_dollar': '1'}}),
              ('/admin/toggle_user_coupon/987654', {}), ('/admin/import_smart', {}), ('/admin/backup_system', {}),
              ('/admin/export_excel', {}), ('/api/generate_item_description', {'json': {'name': '越權'}})]
    offenders = [f'{role}:{path}={r.status_code}'
                 for role, client in (('kitchen', kitchen_client), ('staff', staff_client))
                 for path, kw in probes for r in [client.post(path, **kw)] if r.status_code != 403]
    assert not offenders, offenders


def test_owner_and_manager_can_use_manager_routes(app, admin_client, manager_client, reward_setting_snapshot):
    tid = reward_setting_snapshot
    for role, client in (('owner', admin_client), ('manager', manager_client)):
        r = client.post('/admin/update_reward_setting', data={'is_enabled': '1', 'points_per_dollar': '2',
                                                              'max_discount_per_order': '15', 'spend_per_point': '80'})
        assert r.status_code == 302
        assert reward_setting_values(app, tid) == (2, 15, 80)
        assert client.post('/admin/toggle_user_coupon/987654').status_code == 404
        assert client.post('/admin/import_smart').status_code == 302
        assert_flash(client, '請選擇 Excel / CSV 檔案')
        assert client.post('/admin/export_excel').status_code == 302
        assert_flash(client, '請至少勾選一個資料表')
        assert client.post('/api/generate_item_description', json={'name': ''}).status_code == 400


def test_anonymous_scan(app):
    """匿名使用者對所有後台 / POS / 廚房路由一律被擋 (302 導向登入、401 或 403)"""
    anon = app.test_client()
    anon.get('/?shop=default')
    gets = ['/pos', '/kitchen', '/pickup', '/admin/staff', '/api/kitchen_orders', '/api/admin_live_orders']
    posts = [r.rule for r in app.url_map.iter_rules()
             if 'POST' in r.methods and r.rule.startswith(('/admin/', '/api/pos', '/api/orders', '/api/kitchen', '/api/generate'))]
    offenders = [f'GET {p}={anon.get(p).status_code}' for p in gets if anon.get(p).status_code not in (302, 401, 403)]
    for rule in posts:
        path = re.sub(r'<[^>]+>', '1', rule)
        code = anon.post(path, json={}).status_code
        if code not in (302, 401, 403):
            offenders.append(f'POST {path}={code}')
    assert not offenders and len(posts) >= 25, (offenders, len(posts))


def test_staff_and_kitchen_go_to_their_own_screens(app, staff_client, kitchen_client):
    """後台含會員個資與稽核紀錄：櫃檯導向 POS、廚房導向 KDS；即時營運 API 只限店長以上"""
    assert staff_client.get('/admin').headers['Location'].endswith('/pos')
    assert kitchen_client.get('/admin').headers['Location'].endswith('/kitchen')
    assert staff_client.get('/api/admin_live_orders').status_code == 403
    assert kitchen_client.get('/api/admin_live_orders').status_code == 403


def test_password_or_role_change_ends_existing_sessions(app):
    from tests.support import login_staff
    c = login_staff(app, 'session_version_user', 'staff')
    assert c.get('/pos').status_code == 200
    with app.app_context():
        staff = StaffAccount.query.filter_by(username='session_version_user').first()
        staff.set_password('NewPass!2026')
        db.session.commit()
    assert c.get('/pos').status_code == 302, '改密碼後舊的登入工作階段失效'
    c = login_staff(app, 'session_version_user', 'staff')
    with app.app_context():
        StaffAccount.query.filter_by(username='session_version_user').first().role = 'kitchen'
        db.session.commit()
    assert c.get('/pos').status_code == 302, '改角色後舊的登入工作階段失效'


def test_login_starts_a_fresh_session(app):
    """登入時清掉之前的顧客 / 會員資料 (避免共用機台把上一位顧客的身分帶進員工工作階段)"""
    c = app.test_client()
    c.get('/?shop=default')
    c.post('/guest_login')
    with c.session_transaction() as sess:
        assert sess.get('is_guest') is True
    c.post('/admin', data={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD}, headers={'X-Forwarded-For': '10.10.9.9'})
    with c.session_transaction() as sess:
        assert '_user_id' in sess and 'is_guest' not in sess and 'customer_log_id' not in sess
