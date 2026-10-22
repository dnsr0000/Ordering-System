"""登入頻率限制、PIN 錯誤鎖定 (換 IP 也鎖、同 IP 噴灑也鎖)、X-Forwarded-For 只在代理後信任"""
from app.extensions import limiter
from app.models.user import CustomerLog
from app.services import auth_throttle
from tests.conftest import TestConfig
from tests.support import default_tenant_id, make_member


def test_pin_lockout_after_five_failures(app):
    """同一裝置連續 5 次 PIN 錯誤後鎖定，鎖定期間正確 PIN 也回 429"""
    c = app.test_client()
    c.get('/?shop=default')
    ip = {'X-Forwarded-For': '10.20.3.99'}
    codes = [c.post('/phone_login', json={'phone': '0912345678', 'pin': '0000'}, headers=ip).status_code for _ in range(5)]
    codes.append(c.post('/phone_login', json={'phone': '0912345678', 'pin': '2468'}, headers=ip).status_code)
    assert codes == [401] * 5 + [429]


def test_login_rate_limit(app):
    """後台登入以來源 IP 計數 (每分鐘 10 次)：換店家 (/shop/<代號>) 也繞不過；其他 IP 不受影響"""
    c = app.test_client()
    ip = {'X-Forwarded-For': '203.0.113.199'}
    codes = []
    for i in range(12):
        if i == 5:
            c.get('/shop/default')  # 切換店家不會重設計數
        codes.append(c.post('/admin', data={'username': f'nobody_{i}', 'password': 'wrong'}, headers=ip).status_code)
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    other_ip = c.post('/admin', data={'username': 'nobody_x', 'password': 'wrong'}, headers={'X-Forwarded-For': '203.0.113.200'})
    assert other_ip.status_code == 401, '其他來源 IP 不受影響'


def test_staff_account_lockout(app):
    """員工帳號：同一 IP 錯 5 次鎖定 (正確密碼也擋)；不分 IP 累計錯 10 次鎖定；不存在的帳號同樣計數"""
    from app.extensions import db
    from app.models.tenant import StaffAccount
    with app.app_context():
        staff = StaffAccount(tenant_id=default_tenant_id(app), username='lockout_target', role='staff')
        staff.set_password('RightPass!2026')
        db.session.add(staff)
        db.session.commit()

    def login(password, ip, username='lockout_target'):
        return app.test_client().post('/admin', data={'username': username, 'password': password},
                                      headers={'X-Forwarded-For': ip}).status_code

    assert [login('wrong', '198.51.100.1') for _ in range(5)] == [401] * 5
    assert login('RightPass!2026', '198.51.100.1') == 429, '同一 IP 鎖定期間正確密碼也不放行'
    assert login('RightPass!2026', '198.51.100.2') == 302, '其他 IP 仍可登入 (登入成功清除帳號計數)'
    for i in range(10):
        limiter.reset()
        login('wrong', f'198.51.101.{i}')
    assert login('RightPass!2026', '198.51.102.1') == 429, '換 IP 累計錯 10 次後整個帳號鎖定'
    ghost = [login('wrong', '198.51.103.1', username='ghost_account') for _ in range(6)]
    assert ghost == [401] * 5 + [429], '不存在的帳號鎖定行為相同 (無法藉此列舉帳號)'
    with app.app_context():
        auth_throttle.clear_keys(*auth_throttle.staff_keys('lockout_target', '').values(),
                                 *[k for k in auth_throttle.staff_keys('lockout_target', '198.51.100.1').values()])


def test_member_login_rate_limits(app):
    c = app.test_client()
    c.get('/?shop=default')
    phone = [c.post('/phone_login', json={'phone': f'0900{i:06d}', 'pin': '0000'},
                    headers={'X-Forwarded-For': '203.0.113.213'}).status_code for i in range(17)]
    assert 429 not in phone[:15] and phone[15:] == [429, 429]
    face = [c.post('/face_login', data={}, headers={'X-Forwarded-For': '203.0.113.214'}).status_code for _ in range(17)]
    assert 429 not in face[:15] and face[15:] == [429, 429]


def test_forwarded_for_trusted_only_behind_proxy(app):
    from app import create_app
    app0 = create_app(type('NoProxy', (TestConfig,), {'TRUSTED_PROXY_COUNT': 0}))

    def guest_ip(application):
        c = application.test_client()
        c.get('/?shop=default')
        c.post('/guest_login', headers={'X-Forwarded-For': '203.0.113.77'}, environ_overrides={'REMOTE_ADDR': '198.51.100.9'})
        with application.app_context():
            return CustomerLog.query.order_by(CustomerLog.id.desc()).first().ip_address

    assert guest_ip(app0) == '198.51.100.9'
    assert guest_ip(app) == '203.0.113.77'


def test_account_lock_survives_ip_rotation(app):
    """輪換 10 個來源 IP 猜錯 PIN 後帳號鎖定；空號的鎖定行為一致；櫃檯重設後解除"""
    tid = default_tenant_id(app)
    make_member(app, '模組14鎖定會員', '0933140001', pin='2468')
    c = app.test_client()
    c.get('/?shop=default')

    def attempt(phone, pin, ip):
        return c.post('/phone_login', json={'phone': phone, 'pin': pin}, headers={'X-Forwarded-For': ip}).status_code

    assert [attempt('0933140001', '0000', f'198.18.{i}.1') for i in range(10)] == [401] * 10
    assert attempt('0933140001', '2468', '198.18.99.1') == 429
    assert [attempt('0933149999', '0000', f'198.19.{i}.1') for i in range(10)] == [401] * 10
    assert attempt('0933149999', '0000', '198.19.99.1') == 429
    with app.app_context():
        auth_throttle.clear_pin_lockout(tid, '0933140001')
    assert attempt('0933140001', '2468', '198.18.100.1') == 200


def test_ip_spray_lock(app):
    """同一 IP 對大量不同手機猜 PIN：達上限後該 IP 鎖定，其他 IP 不受影響"""
    make_member(app, '模組14噴灑會員', '0933140002', pin='2468')
    limiter.enabled = False  # 只驗證 PIN 失敗鎖定，避開每分鐘頻率限制
    try:
        c = app.test_client()
        c.get('/?shop=default')
        spray = {'X-Forwarded-For': '198.51.100.200'}
        codes = [c.post('/phone_login', json={'phone': f'0944{i:06d}', 'pin': '0000'}, headers=spray).status_code
                 for i in range(auth_throttle.IP_MAX_FAILS)]
        assert codes == [401] * auth_throttle.IP_MAX_FAILS
        assert c.post('/phone_login', json={'phone': '0933140002', 'pin': '2468'}, headers=spray).status_code == 429
        assert c.post('/phone_login', json={'phone': '0933140002', 'pin': '2468'},
                      headers={'X-Forwarded-For': '198.51.100.201'}).status_code == 200
    finally:
        limiter.enabled = True
