"""網頁安全：CSRF (含全路由掃描)、狀態變更不接受 GET、安全標頭、訊息跳脫、錯誤輸入不出 500"""
import io
import re

from app.extensions import db
from app.models.menu import MenuItem
from app.utils import safe_float, safe_int
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import (
    assert_flash,
    extract_csrf,
    make_item,
    make_member,
)


def test_csrf_required_on_every_state_change(app, csrf_enabled):
    """缺少或偽造 CSRF Token 的 POST 一律 403 (JSON 與表單皆同)"""
    anon = app.test_client()
    anon.get('/?shop=default')
    codes = [
        anon.post('/submit_order', json={'items': [{'menu_item_id': 1}]}).status_code,
        anon.post('/admin', data={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD}).status_code,
        anon.post('/phone_login', json={'phone': '0912345678', 'pin': '1234'}).status_code,
        anon.post('/api/pos/checkout', json={'items': []}).status_code,
        anon.post('/submit_order', json={'items': []}, headers={'X-CSRFToken': 'forged_invalid_token_999'}).status_code,
    ]
    assert codes == [403] * 5, codes
    # 正確的 Token 可以通過 (此處以錯誤密碼驗證「進得了登入流程」)
    token = extract_csrf(anon.get('/admin').get_data(as_text=True))
    assert anon.post('/admin', data={'username': 'nobody', 'password': 'x', 'csrf_token': token}).status_code == 401


def test_state_changes_reject_get(app, admin_client):
    """刪除 / 下架路由不允許 GET (避免繞過 CSRF)"""
    item = make_item(app, 'GET刪除測試品13', price=10, stock=5)
    for path in (f'/admin/delete/{item}', '/admin/delete_combo/1', f'/admin/remove_reward/{item}',
                 '/admin/delete_coupon/1', '/admin/delete_user/1'):
        assert admin_client.get(path).status_code == 405, path
    with app.app_context():
        assert db.session.get(MenuItem, item) is not None


def test_csrf_scan_every_post_route(app, csrf_enabled):
    anon = app.test_client()
    anon.get('/?shop=default')
    offenders, total = [], 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static' or 'POST' not in rule.methods:
            continue
        if rule.endpoint.startswith(('sync_api.', 'payment.ecpay_', 'delivery.ubereats_webhook', 'delivery.foodpanda_webhook',
                                     'table_pay.notify', 'table_pay.result', 'reservations.deposit_notify',
                                     'reservations.deposit_result', 'api_v1.')):
            # 分店同步 API 以權杖驗證、綠界通知以檢查碼驗證、外送平台 Webhook 以簽章 / 權杖驗證 (都不使用 cookie)，
            # 見 test_sync_hub_node / test_payment_ecpay / test_delivery_platforms
            continue
        total += 1
        path = re.sub(r'<[^>]+>', '1', rule.rule)
        code = anon.post(path, json={}).status_code
        if code != 403:
            offenders.append(f'{path}={code}')
    assert not offenders and total >= 30, (offenders, total)


def test_messages_are_escaped(app, admin_client):
    """錯誤提示含使用者可控的姓名 (例如 </script>) 時被模板自動跳脫"""
    make_member(app, '</script><b>x', '0933150009')
    ub = make_member(app, '正常會員', '0933150010')
    r = admin_client.post(f'/admin/edit_user/{ub}', data={'name': '正常會員', 'phone': '0933150009', 'points': '0'},
                          follow_redirects=True)
    body = r.get_data(as_text=True)
    assert '已被其他會員' in body
    assert '</script><b>x' not in body and '&lt;/script&gt;&lt;b&gt;x' in body


def test_bad_numbers_never_500(app, admin_client):
    assert admin_client.post('/admin/add', data={'name': '模組15壞價', 'price': 'abc', 'stock': 'x'}).status_code == 302
    assert_flash(admin_client, '單價必須是數字')
    assert admin_client.post('/admin/add_coupon', data={'code': 'M15X', 'title': 'T', 'discount_type': 'fixed',
                                                        'discount_value': '10', 'min_spend': 'abc'}).status_code == 302
    assert admin_client.post('/admin/add_combo', data={'name': '壞套餐'}).status_code == 302
    assert_flash(admin_client, '請選擇主餐')
    assert safe_int(float('inf')) == 0 and safe_int('1e999') == 0 and safe_float('nan', 1.5) == 1.5


def test_security_headers(app):
    h = app.test_client().get('/').headers
    assert h['X-Content-Type-Options'] == 'nosniff' and h['X-Frame-Options'] == 'SAMEORIGIN'
    csp = h['Content-Security-Policy']
    script_src = re.search(r"script-src ([^;]+)", csp).group(1)
    assert "'unsafe-inline'" not in script_src and 'http' not in script_src, '頁面腳本應全部來自本站靜態檔'
    assert "frame-ancestors 'self'" in csp and "object-src 'none'" in csp
    assert 'camera=(self)' in h['Permissions-Policy']
    assert 'Content-Security-Policy' not in app.test_client().get('/api/counter_orders').headers
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True and app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


def test_invalid_json_returns_json_400(app):
    c = app.test_client()
    c.get('/?shop=default')
    for kw in ({'data': 'not json', 'content_type': 'text/plain'}, {'json': [1, 2, 3]}):
        r = c.post('/phone_login', **kw)
        assert r.status_code == 400 and r.get_json()['success'] is False


def test_logout_and_guest_login_require_post(app):
    """登出、訪客登入不接受 GET：其他網站不能用 <img src> 或連結把顧客 / 員工登出"""
    c = app.test_client()
    for url in ('/logout', '/guest_login', '/admin/logout'):
        assert c.get(url).status_code == 405, url


def test_privacy_consent_redirect_stays_on_site(app):
    from tests.support import make_member, member_client
    make_member(app, '同意轉址會員', '0933990001', pin='2580')
    c = member_client(app, '0933990001', '2580')
    for target in ('https://evil.example/', '//evil.example/x', '/\\evil.example'):
        r = c.post('/privacy/consent', data={'next': target})
        assert r.status_code == 302 and 'evil.example' not in r.headers['Location'], target
    assert c.post('/privacy/consent', data={'next': '/rewards'}).headers['Location'].endswith('/rewards')


def test_large_upload_limit_only_for_platform_admin(app, admin_client, monkeypatch, csrf_enabled):
    """整庫還原的大檔案上限只對已登入的平台管理者放寬；匿名的大檔案在讀表單前就 413"""
    monkeypatch.setitem(app.config, 'MAX_CONTENT_LENGTH', 2000)
    monkeypatch.setitem(app.config, 'MAX_RESTORE_UPLOAD_SIZE', 100000)
    body = {'backup_zip': (io.BytesIO(b'x' * 5000), 'b.zip')}
    assert app.test_client().post('/admin/restore_backup', data=body, content_type='multipart/form-data').status_code == 413
    body = {'backup_zip': (io.BytesIO(b'x' * 5000), 'b.zip')}
    assert admin_client.post('/admin/restore_backup', data=body, content_type='multipart/form-data').status_code != 413
