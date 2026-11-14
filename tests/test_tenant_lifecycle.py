"""免費 SaaS：租戶資料匯出與可恢復的 owner 關閉申請。"""
import io
import json
import uuid
import zipfile

from openpyxl import load_workbook

from app.extensions import db
from app.models.menu import MenuItem
from app.models.tenant import TenantClosureRequest
from app.models.user import StaffAuditLog
from app.services import settings_service
from tests.support import default_tenant_id, get_or_create_tenant, login_staff


def test_owner_export_bundle_is_tenant_scoped_and_excludes_secrets(app):
    own_slug = f'lifecycle-export-{uuid.uuid4().hex[:8]}'
    other_slug = f'lifecycle-other-{uuid.uuid4().hex[:8]}'
    own_id = get_or_create_tenant(app, own_slug)
    other_id = get_or_create_tenant(app, other_slug)
    owner = login_staff(app, f'owner-{own_slug}', role='owner', tenant_id=own_id)
    account_page = owner.get('/admin/tenant/account')
    assert account_page.status_code == 200 and '下載本店資料包' in account_page.get_data(as_text=True)
    settings_page = owner.get('/admin/settings')
    assert settings_page.status_code == 200 and '店家資料匯出與關閉' in settings_page.get_data(as_text=True)
    manager = login_staff(app, f'manager-{own_slug}', role='manager', tenant_id=own_id)
    assert manager.get('/admin/tenant/account/export').status_code == 403
    with app.app_context():
        db.session.add_all([
            MenuItem(tenant_id=own_id, name='TENANT-EXPORT-OWN-MARKER', category='主餐', price=100),
            MenuItem(tenant_id=other_id, name='TENANT-EXPORT-OTHER-MARKER', category='主餐', price=200),
        ])
        settings_service.put('line', {'messaging_enabled': True, 'messaging_access_token': 'EXPORT-SECRET-MARKER'},
                             tenant_id=own_id)
        db.session.commit()

    response = owner.get('/admin/tenant/account/export')
    assert response.status_code == 200 and response.mimetype == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        settings = json.loads(archive.read('settings.json'))
        workbook = load_workbook(io.BytesIO(archive.read('business_data.xlsx')), read_only=True, data_only=True)
        values = [str(cell.value) for row in workbook['菜單品項(MenuItem)'].iter_rows() for cell in row]
        assert manifest['tenant']['slug'] == own_slug
        assert 'TENANT-EXPORT-OWN-MARKER' in values
        assert 'TENANT-EXPORT-OTHER-MARKER' not in values
        assert 'EXPORT-SECRET-MARKER' not in json.dumps(settings, ensure_ascii=False)
        assert 'branches.json' in archive.namelist()
    with app.app_context():
        assert StaffAuditLog.query.filter_by(tenant_id=own_id, action_type='TENANT_DATA_EXPORT').count() == 1


def test_store_login_page_links_to_tenant_reactivation(app):
    response = app.test_client().get('/admin')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="tenant-reactivate-link"' in html
    assert 'href="/tenant/reactivate"' in html
    assert '店家已停用？恢復店家' in html


def test_owner_close_request_suspends_without_deleting_and_platform_can_restore(app):
    slug = f'lifecycle-close-{uuid.uuid4().hex[:8]}'
    tenant_id = get_or_create_tenant(app, slug)
    owner = login_staff(app, f'owner-{slug}', role='owner', tenant_id=tenant_id)
    with app.app_context():
        item = MenuItem(tenant_id=tenant_id, name='TENANT-CLOSE-RETAIN-MARKER', category='主餐', price=100)
        db.session.add(item)
        db.session.commit()
        item_id = item.id

    invalid = owner.post('/admin/tenant/account/close-request', data={
        'current_password': 'wrong-password', 'reason': '停止營業',
    })
    assert invalid.status_code == 403
    with app.app_context():
        from app.models.tenant import Tenant
        assert db.session.get(Tenant, tenant_id).is_active is True

    closed = owner.post('/admin/tenant/account/close-request', data={
        'current_password': 'StaffPass!2026', 'reason': '暫停營運，請保留資料', 'confirm': '1',
    })
    assert closed.status_code == 200 and '資料仍會保留' in closed.get_data(as_text=True)
    with app.app_context():
        from app.models.tenant import Tenant
        tenant = db.session.get(Tenant, tenant_id)
        request = TenantClosureRequest.query.filter_by(tenant_id=tenant_id, status='pending').one()
        assert tenant.is_active is False
        assert request.requested_by_username == f'owner-{slug}' and request.reason == '暫停營運，請保留資料'
        assert db.session.get(MenuItem, item_id) is not None
        assert StaffAuditLog.query.filter_by(tenant_id=tenant_id, action_type='TENANT_CLOSE_REQUEST').count() == 1

    platform = login_staff(app, f'platform-{slug}', role='owner', tenant_id=default_tenant_id(app))
    tenants_page = platform.get('/admin/tenants')
    assert slug in tenants_page.get_data(as_text=True) and '關閉申請' in tenants_page.get_data(as_text=True)
    assert platform.post(f'/admin/tenants/{tenant_id}/toggle').status_code == 302
    with app.app_context():
        tenant = db.session.get(Tenant, tenant_id)
        request = TenantClosureRequest.query.filter_by(tenant_id=tenant_id).one()
        assert tenant.is_active is True and request.status == 'resolved'
        assert db.session.get(MenuItem, item_id) is not None


def test_owner_can_self_reactivate_pending_closure(app):
    slug = f'lifecycle-self-reactivate-{uuid.uuid4().hex[:8]}'
    tenant_id = get_or_create_tenant(app, slug)
    username = f'owner-{slug}'
    owner = login_staff(app, username, role='owner', tenant_id=tenant_id)
    closed = owner.post('/admin/tenant/account/close-request', data={
        'current_password': 'StaffPass!2026', 'reason': '自行恢復測試', 'confirm': '1',
    })
    assert closed.status_code == 200 and 'tenant/reactivate' in closed.get_data(as_text=True)

    page = owner.get('/tenant/reactivate', query_string={'slug': slug})
    assert page.status_code == 200 and slug in page.get_data(as_text=True)
    recovery_data = {'slug': slug, 'username': username, 'current_password': 'StaffPass!2026'}
    rejected = owner.post('/tenant/reactivate', data={**recovery_data, 'current_password': 'wrong-password'})
    assert rejected.status_code == 401
    with app.app_context():
        from app.models.tenant import Tenant
        assert db.session.get(Tenant, tenant_id).is_active is False

    restored = owner.post('/tenant/reactivate', data=recovery_data)
    assert restored.status_code == 302
    assert owner.get('/admin').status_code == 200
    with app.app_context():
        from app.models.tenant import Tenant
        tenant = db.session.get(Tenant, tenant_id)
        request = TenantClosureRequest.query.filter_by(tenant_id=tenant_id).one()
        assert tenant.is_active is True and request.status == 'owner_reactivated'
        assert request.resolved_by_username == username


def test_owner_cannot_self_reactivate_platform_disabled_tenant(app):
    slug = f'lifecycle-platform-disabled-{uuid.uuid4().hex[:8]}'
    tenant_id = get_or_create_tenant(app, slug)
    username = f'owner-{slug}'
    owner = login_staff(app, username, role='owner', tenant_id=tenant_id)
    with app.app_context():
        from app.models.tenant import Tenant
        db.session.get(Tenant, tenant_id).is_active = False
        db.session.commit()

    response = owner.post('/tenant/reactivate', data={
        'slug': slug, 'username': username, 'current_password': 'StaffPass!2026',
    })
    assert response.status_code == 401
    with app.app_context():
        from app.models.tenant import Tenant
        assert db.session.get(Tenant, tenant_id).is_active is False
        assert TenantClosureRequest.query.filter_by(tenant_id=tenant_id).count() == 0