"""員工帳號管理頁 (templates/staff_accounts.html) 的回歸測試：B-301 / B-305 / B-500 / R2-113"""
from app.sync import runtime as sync_runtime
from tests.support import get_or_create_tenant, login_staff


def _owner_page(app, monkeypatch, *, node, totp=False):
    tid = get_or_create_tenant(app, 'fix-staff-tpl-shop')
    client = login_staff(app, 'fix_tpl_owner', role='owner', tenant_id=tid)
    login_staff(app, 'fix_tpl_staff', role='staff', tenant_id=tid)
    from app.extensions import db
    from app.models.tenant import StaffAccount
    with app.app_context():
        owner = StaffAccount.query.filter_by(username='fix_tpl_owner').first()
        owner.totp_enabled = totp
        db.session.commit()
    monkeypatch.setattr(sync_runtime, 'is_node', lambda: node)
    r = client.get('/admin/staff')
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_username_input_has_backend_max_length(app, monkeypatch):
    html = _owner_page(app, monkeypatch, node=False)
    assert 'id="staff-username" name="username" maxlength="50"' in html


def test_own_row_reset_form_asks_current_password_and_totp(app, monkeypatch):
    html = _owner_page(app, monkeypatch, node=False)
    assert html.count('name="current_password"') == 1   # 只有自己這一列
    assert 'name="totp_code"' not in html
    html = _owner_page(app, monkeypatch, node=False, totp=True)
    assert html.count('name="totp_code"') == 1


def test_node_hides_owner_password_and_delete_controls(app, monkeypatch):
    html = _owner_page(app, monkeypatch, node=True)
    assert '負責人帳號請到線上端修改' in html
    assert 'name="current_password"' not in html
    # 只剩員工那一列可重設密碼
    assert html.count('value="reset_password"') == 1
    html = _owner_page(app, monkeypatch, node=False)
    assert '負責人帳號請到線上端修改' not in html
    assert html.count('value="reset_password"') == 2
