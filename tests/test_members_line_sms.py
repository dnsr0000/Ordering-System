"""會員經營：等級後台管理與重算、生日禮、儲值金調整、LINE 登入 / 綁定 / 推播 (模擬 LINE API)、簡訊驗證註冊與忘記 PIN"""
import re
from datetime import date

import pytest

from app.extensions import db
from app.models.member import MemberTier, OtpCode
from app.models.user import User
from app.services import line_service, monitor_service
from tests.support import assert_flash, checkout, flashes, line, login_staff, make_item, make_member, member_client

POS = '/api/pos/checkout'


@pytest.fixture
def line_api(monkeypatch, store_settings):
    """啟用 LINE Login + Messaging 並以假 API 取代 HTTP；posts 記錄 (url, kwargs)"""
    store_settings('line', login_enabled=True, login_channel_id='1650000000', login_channel_secret='login-secret',
                   messaging_enabled=True, messaging_access_token='bot-token')
    posts = []
    state = {'sub': 'U' + 'a' * 32}

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        if url == line_service.TOKEN_URL:
            if kwargs['data']['code'] == 'bad':
                return 400, {'error': 'invalid_grant'}
            return 200, {'id_token': 'jwt.token.here', 'access_token': 'x'}
        if url == line_service.VERIFY_URL:
            return 200, {'sub': state['sub'], 'name': 'LINE 小明', 'nonce': kwargs['data']['nonce']}
        return 200, {}
    monkeypatch.setattr(line_service, '_post', fake_post)
    return posts, state


def _user(app, phone):
    with app.app_context():
        u = User.query.filter_by(phone=phone).first()
        return None if u is None else {'id': u.id, 'line': u.line_user_id, 'tier': u.tier.name if u.tier else None,
                                       'spent': u.total_spent, 'points': u.points, 'wallet': u.wallet_balance,
                                       'gift_year': u.birthday_gift_year, 'verified': u.phone_verified_at is not None}


def _line_login(client, posts, code='ok', register=False):
    r = client.get('/auth/line')
    assert r.status_code == 302 and r.headers['Location'].startswith('https://access.line.me/oauth2/v2.1/authorize?')
    loc = r.headers['Location']
    state = re.search(r'state=([^&]+)', loc).group(1)
    assert 'client_id=1650000000' in loc and 'redirect_uri=http%3A%2F%2Flocalhost%2Fauth%2Fline%2Fcallback' in loc
    return client.get(f'/auth/line/callback?code={code}&state={state}' + ('&register=1' if register else ''))


# ---------------------------------------------------------------------------
# 等級 / 生日 / 儲值金 (後台)
# ---------------------------------------------------------------------------
def test_tier_admin_and_recompute(app, admin_client, staff_client):
    admin_client.post('/admin/members_pro', data={'action': 'add_tier', 'name': '金卡測試', 'min_spend': '800',
                                                  'points_multiplier': '1.5', 'discount_percent': '5', 'color': 'warning'})
    admin_client.post('/admin/members_pro', data={'action': 'add_tier', 'name': '金卡測試'})
    assert_flash(admin_client, '已存在')
    uid = make_member(app, '等級會員', '0966100001')
    iid = make_item(app, '等級測試餐', price=500, stock=20)
    checkout(staff_client, [line(iid)], url=POS, user_id=uid, cash_received=500)
    assert _user(app, '0966100001')['tier'] != '金卡測試'
    checkout(staff_client, [line(iid)], url=POS, user_id=uid, cash_received=500)
    assert _user(app, '0966100001')['tier'] == '金卡測試'
    # 降低門檻後重新計算：只升不降
    with app.app_context():
        tier_id = MemberTier.query.filter_by(name='金卡測試').first().id
    admin_client.post('/admin/members_pro', data={'action': 'edit_tier', 'tier_id': tier_id, 'name': '金卡測試',
                                                  'min_spend': '100000', 'discount_percent': '0', 'points_multiplier': '1'})
    admin_client.post('/admin/members_pro', data={'action': 'recompute_tiers'})
    assert _user(app, '0966100001')['tier'] == '金卡測試'
    page = admin_client.get('/admin/members_pro?q=0966100001').get_data(as_text=True)
    assert '等級會員' in page and '金卡測試' in page
    assert staff_client.get('/admin/members_pro').status_code == 403
    admin_client.post('/admin/members_pro', data={'action': 'delete_tier', 'tier_id': tier_id})
    assert _user(app, '0966100001')['tier'] is None


def test_birthday_gift_once_per_year(app, admin_client, store_settings, line_api):
    posts, _ = line_api
    store_settings('member', birthday_enabled=True, birthday_points=50)
    today = date.today()
    uid = make_member(app, '壽星', '0966100002', points=10, birthday=today.replace(year=1990, day=1),
                      line_user_id='U' + 'b' * 32)
    make_member(app, '非壽星', '0966100003', points=10,
                birthday=today.replace(year=1990, month=(today.month % 12) + 1, day=1))
    admin_client.post('/admin/members_pro', data={'action': 'birthday_now'})
    msgs = flashes(admin_client)
    assert any('已送出' in m for m in msgs)
    assert _user(app, '0966100002')['points'] == 60 and _user(app, '0966100002')['gift_year'] == today.year
    assert _user(app, '0966100003')['points'] == 10
    push = [p for p in posts if p[0] == line_service.PUSH_URL]
    assert push and push[-1][1]['json']['to'] == 'U' + 'b' * 32 and '生日快樂' in push[-1][1]['json']['messages'][0]['text']
    from app.services import wallet_service
    from tests.support import default_tenant_id
    with app.app_context():
        assert wallet_service.grant_birthday_gifts(default_tenant_id(app)) == 0, '同一年只送一次'
    assert _user(app, '0966100002')['points'] == 60
    # 後台修改生日
    admin_client.post('/admin/members_pro', data={'action': 'member', 'user_id': uid, 'birthday': '1991-02-03', 'tier_id': ''})
    with app.app_context():
        assert db.session.get(User, uid).birthday == date(1991, 2, 3)


def test_wallet_adjust_owner_only(app, admin_client, store_settings):
    uid = make_member(app, '調整儲值會員', '0966100004')
    mgr = login_staff(app, 'wallet_mgr', 'manager')
    mgr.post('/admin/members_pro', data={'action': 'wallet_adjust', 'user_id': uid, 'delta': '100', 'note': '補償'})
    assert _user(app, '0966100004')['wallet'] == 0
    admin_client.post('/admin/members_pro', data={'action': 'wallet_adjust', 'user_id': uid, 'delta': '100', 'note': '補償'})
    assert _user(app, '0966100004')['wallet'] == 100
    admin_client.post('/admin/members_pro', data={'action': 'wallet_adjust', 'user_id': uid, 'delta': '-150', 'note': '扣回'})
    assert_flash(admin_client, '超過目前餘額')
    assert _user(app, '0966100004')['wallet'] == 100


# ---------------------------------------------------------------------------
# LINE 登入 / 綁定 / 推播
# ---------------------------------------------------------------------------
def test_line_login_binding_flow(app, line_api):
    posts, state = line_api
    make_member(app, 'LINE 綁定會員', '0966100005', pin='1357')
    c = app.test_client()
    c.get('/?shop=default')
    # 第一次：LINE 帳號尚未綁定 → 導向登入，登入後自動綁定
    r = _line_login(c, posts)
    assert r.status_code == 302 and '/member_login' in r.headers['Location']
    assert posts[0][1]['data']['client_secret'] == 'login-secret'
    assert c.post('/phone_login', json={'phone': '0966100005', 'pin': '1357'}).status_code == 200
    assert _user(app, '0966100005')['line'] == state['sub']
    # 之後直接以 LINE 登入
    c2 = app.test_client()
    c2.get('/?shop=default')
    r = _line_login(c2, posts)
    assert r.status_code == 302 and r.headers['Location'].endswith('/')
    assert 'LINE 綁定會員' in c2.get('/').get_data(as_text=True)
    # state 錯誤 / 授權碼無效
    c3 = app.test_client()
    c3.get('/?shop=default')
    c3.get('/auth/line')
    c3.get('/auth/line/callback?code=x&state=forged')
    assert_flash(c3, 'LINE 登入驗證失敗')
    r = _line_login(c3, posts, code='bad')
    assert r.status_code == 401 and '授權碼無效' in r.get_data(as_text=True)
    # 解除綁定
    c2.post('/auth/line/unbind')
    assert _user(app, '0966100005')['line'] == ''


def test_line_login_disabled_redirects(app, store_settings):
    store_settings('line', login_enabled=False)
    c = app.test_client()
    c.get('/?shop=default')
    r = c.get('/auth/line')
    assert r.status_code == 302 and 'access.line.me' not in r.headers['Location']


def test_ready_push_and_broadcast(app, admin_client, staff_client, store_settings, line_api):
    posts, _ = line_api
    store_settings('notify', line_ready=True)
    uid = make_member(app, '推播會員', '0966100006', line_user_id='U' + 'c' * 32)
    iid = make_item(app, '推播測試餐', price=60, stock=10)
    oid = checkout(staff_client, [line(iid)], url=POS, user_id=uid, cash_received=60).get_json()['order_id']
    staff_client.post(f'/api/orders/{oid}/status', json={'status': 'Completed'})
    push = [p for p in posts if p[0] == line_service.PUSH_URL][-1][1]
    assert push['headers']['Authorization'] == 'Bearer bot-token' and push['json']['to'] == 'U' + 'c' * 32
    assert '已完成' in push['json']['messages'][0]['text']
    admin_client.post('/admin/members_pro', data={'action': 'broadcast', 'target': 'all', 'message': '週末買一送一'})
    multi = [p for p in posts if p[0] == line_service.MULTICAST_URL][-1][1]['json']
    assert 'U' + 'c' * 32 in multi['to'] and multi['messages'][0]['text'] == '週末買一送一'
    store_settings('line', messaging_enabled=False)
    admin_client.post('/admin/members_pro', data={'action': 'broadcast', 'target': 'all', 'message': 'x'})
    assert_flash(admin_client, '尚未啟用')


# ---------------------------------------------------------------------------
# 簡訊驗證碼
# ---------------------------------------------------------------------------
def _last_code(app, phone):
    with app.app_context():
        for e in monitor_service.recent_events(30):
            if phone in e['message']:
                return re.search(r'驗證碼 (\d{6})', e['message']).group(1)
    raise AssertionError('找不到驗證碼')


def test_register_with_sms_otp(app, store_settings):
    store_settings('sms', provider='log', otp_on_register=True, allow_pin_reset=True)
    c = app.test_client()
    c.get('/?shop=default')
    assert 'otp' in c.get('/register').get_data(as_text=True).lower()
    form = {'name': '簡訊會員', 'phone': '0966100007', 'pin': '2580', 'agree_privacy': '1', 'birthday': '1995-05-05'}
    r = c.post('/register', data=form)
    assert r.status_code in (302, 400)
    assert _user(app, '0966100007') is None, '未驗證手機不可註冊'
    assert c.post('/api/otp/send', json={'phone': '0966100007', 'purpose': 'register'}).status_code == 200
    assert c.post('/api/otp/send', json={'phone': '0966100007', 'purpose': 'register'}).status_code == 429   # 太快重送
    code = _last_code(app, '0966100007')
    c.post('/register', data=dict(form, otp_code='000000' if code != '000000' else '111111'))
    assert _user(app, '0966100007') is None
    c.post('/register', data=dict(form, otp_code=code))
    u = _user(app, '0966100007')
    assert u is not None and u['verified'] is True
    with app.app_context():
        assert db.session.get(User, u['id']).birthday == date(1995, 5, 5)
        assert OtpCode.query.filter_by(phone='0966100007').first().consumed_at is not None
    assert c.post('/api/otp/send', json={'phone': '0966100007', 'purpose': 'register'}).status_code == 409


def test_forgot_pin_with_sms(app, store_settings):
    make_member(app, '忘記 PIN 會員', '0966100008', pin='1470')
    store_settings('sms', provider='log', allow_pin_reset=False)
    c = app.test_client()
    c.get('/?shop=default')
    assert c.get('/forgot_pin').status_code == 302
    assert c.post('/api/otp/send', json={'phone': '0966100008', 'purpose': 'reset_pin'}).status_code == 403
    store_settings('sms', allow_pin_reset=True)
    assert c.get('/forgot_pin').status_code == 200
    assert c.post('/api/otp/send', json={'phone': '0966100008', 'purpose': 'reset_pin'}).status_code == 200
    code = _last_code(app, '0966100008')
    c.post('/forgot_pin', data={'phone': '0966100008', 'pin': '1234', 'otp_code': code})   # 弱 PIN
    c.post('/forgot_pin', data={'phone': '0966100008', 'pin': '8526', 'otp_code': code})
    member_client(app, '0966100008', '8526', ip='10.99.7.1')
    # 驗證碼只能用一次
    r = c.post('/forgot_pin', data={'phone': '0966100008', 'pin': '9637', 'otp_code': code})
    member_client(app, '0966100008', '8526', ip='10.99.7.2')


def test_sms_provider_mitake_parsing(app, monkeypatch, store_settings):
    from app.errors import AppError
    from app.services import sms_service
    from tests.support import default_tenant_id
    store_settings('sms', provider='mitake', username='acct', password='pw')
    sent = []

    def fake_post(url, **kwargs):
        sent.append((url, kwargs))
        return 200, '[1]\nmsgid=1234567890\nstatuscode=1\nAccountPoint=99\n'
    monkeypatch.setattr(sms_service, '_post', fake_post)
    with app.app_context():
        assert sms_service.send_sms(default_tenant_id(app), '0966100009', '測試') == 'sent'
        assert sent[0][1]['data']['dstaddr'] == '0966100009' and sent[0][1]['data']['username'] == 'acct'
        monkeypatch.setattr(sms_service, '_post', lambda url, **kw: (200, '[1]\nstatuscode=e\nError=帳號錯誤'))
        with pytest.raises(AppError):
            sms_service.send_sms(default_tenant_id(app), '0966100009', '測試')
