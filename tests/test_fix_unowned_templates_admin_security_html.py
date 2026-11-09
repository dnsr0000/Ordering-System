"""帳號安全頁 (templates/admin_security.html) 的回歸測試：R2-132 重新設定驗證 App 的畫面入口、R2-113 首次啟用要輸入目前密碼"""
import re

from app.services import security_service
from tests.support import login_staff

PASSWORD = 'StaffPass!2026'


def _page(client):
    r = client.get('/admin/security')
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _secret(html):
    return re.search(r'id="totpSecret">([A-Z2-7]+)<', html).group(1)


def test_first_setup_form_asks_current_password(app):
    c = login_staff(app, 'fix_tpl_sec_first', 'manager')
    html = _page(c)
    assert 'value="rebind_2fa"' not in html and 'value="cancel_rebind"' not in html
    c.post('/admin/security', data={'action': 'start_2fa'})
    html = _page(c)
    assert 'id="confirmTotpForm"' in html and 'name="current_password"' in html
    assert 'value="cancel_rebind"' not in html


def test_rebind_entry_and_cancel(app):
    c = login_staff(app, 'fix_tpl_sec_rebind', 'manager')
    c.post('/admin/security', data={'action': 'start_2fa'})
    secret = _secret(_page(c))
    html = c.post('/admin/security', data={'action': 'confirm_2fa', 'current_password': PASSWORD,
                                           'code': security_service.totp_now(secret)}).get_data(as_text=True)
    codes = re.findall(r'<div class="col">(\d{4}-\d{4})</div>', html)
    assert len(codes) == 10

    # 已啟用：顯示重新設定驗證 App 表單；重新產生備用碼可輸入備用碼 (9 碼)
    html = _page(c)
    assert 'value="rebind_2fa"' in html and 'id="rebindPassword"' in html
    assert re.search(r'id="regenCode"[^>]*maxlength="9"', html)
    assert 'id="totpSecret"' not in html

    # 用密碼 + 備用碼開始重新綁定 → 顯示新的 QR / 金鑰與取消按鈕，確認表單不再要求密碼
    c.post('/admin/security', data={'action': 'rebind_2fa', 'password': PASSWORD, 'code': codes[0]})
    html = _page(c)
    new_secret = _secret(html)
    assert new_secret != secret
    assert 'value="cancel_rebind"' in html and '正在重新設定驗證 App' in html
    assert 'name="current_password"' not in html
    assert 'value="rebind_2fa"' not in html

    # 取消 → 回到已啟用畫面
    c.post('/admin/security', data={'action': 'cancel_rebind'})
    html = _page(c)
    assert 'id="totpSecret"' not in html and 'value="rebind_2fa"' in html


TOTP_FORMS = ('start_2fa', 'confirm_2fa', 'regen_backup', 'disable_2fa', 'rebind_2fa')


def test_node_owner_page_hides_2fa_forms(app, monkeypatch):
    """B-301：分店上的負責人帳號顯示「請到線上端設定」，不提供會被後端 403 的 2FA 表單"""
    c = login_staff(app, 'fix_tpl_sec_node_owner', 'owner')
    c.post('/admin/security', data={'action': 'start_2fa'})
    secret = _secret(_page(c))
    c.post('/admin/security', data={'action': 'confirm_2fa', 'current_password': PASSWORD,
                                    'code': security_service.totp_now(secret)})
    html = _page(c)
    assert 'id="ownerOnNodeNotice"' not in html and 'value="regen_backup"' in html

    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
    html = _page(c)
    assert 'id="ownerOnNodeNotice"' in html and '剩餘備用碼' in html
    for action in TOTP_FORMS:
        assert f'value="{action}"' not in html, action
    assert 'value="logout_others"' in html   # 登入裝置管理照常

    # 未啟用的負責人：也不提供「開始設定」
    c2 = login_staff(app, 'fix_tpl_sec_node_owner2', 'owner')
    html = _page(c2)
    assert 'id="ownerOnNodeNotice"' in html and 'id="start2faBtn"' not in html

    # 分店上的店長不受影響
    c3 = login_staff(app, 'fix_tpl_sec_node_mgr', 'manager')
    html = _page(c3)
    assert 'id="ownerOnNodeNotice"' not in html and 'id="start2faBtn"' in html
