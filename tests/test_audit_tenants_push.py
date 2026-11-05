"""
第五輪 (階段 3)：稽核紀錄完整頁 (分頁 / 篩選 / 匯出)、平台端店家管理、Web Push 推播。
"""
import io
from datetime import datetime

import pytest

from tests.support import checkout, default_tenant_id, line, login_staff, make_item, other_shop_owner_client


# ---------------------------------------------------------------------------
# 稽核紀錄頁
# ---------------------------------------------------------------------------
def _seed_audits(app, n, username='audit_seed', action='EDIT_USER'):
    from app.extensions import db
    from app.models.user import StaffAuditLog
    with app.app_context():
        tid = default_tenant_id(app)
        for i in range(n):
            db.session.add(StaffAuditLog(tenant_id=tid, staff_username=username, staff_role='manager', action_type=action,
                                         target_order_id=900000 + i, details=f'稽核分頁測試 {i}', ip_address='10.9.9.9',
                                         created_at=datetime(2026, 1, 2, 3, 4, i % 60)))
        db.session.commit()


def test_audit_page_paginates_filters_and_exports(app):
    mgr = login_staff(app, 'audit_mgr', 'manager')
    _seed_audits(app, 120)
    r = mgr.get('/admin/audit')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'auditLogTable' in html and 'auditPagination' in html and '每頁 50 筆' in html
    assert html.count('class="audit-row"') == 50
    # 篩選：員工 + 日期 + 關鍵字 (單號)
    r = mgr.get('/admin/audit?staff=audit_seed&start=2026-01-02&end=2026-01-02&q=%23900007')
    html = r.get_data(as_text=True)
    assert html.count('class="audit-row"') == 1 and '稽核分頁測試 7' in html
    r = mgr.get('/admin/audit?staff=audit_seed&action=CANCEL_ORDER')
    assert '沒有符合的紀錄' in r.get_data(as_text=True)
    r = mgr.get('/admin/audit?staff=audit_seed&page=3')
    assert r.get_data(as_text=True).count('class="audit-row"') == 20
    assert mgr.get('/admin/audit?start=2026-13-01').status_code in (302, 400)
    # 匯出 Excel (目前篩選)
    r = mgr.get('/admin/audit/export?staff=audit_seed&q=%E7%A8%BD%E6%A0%B8%E5%88%86%E9%A0%81')
    assert r.status_code == 200 and r.mimetype.endswith('spreadsheetml.sheet')
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(r.data)).active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][0] == 'ID' and len(rows) == 121 and rows[1][4] == '修改會員/點數'
    # 匯出本身也留稽核
    from app.models.user import StaffAuditLog
    with app.app_context():
        assert StaffAuditLog.query.filter_by(action_type='AUDIT_EXPORT').count() >= 1
    # 登入日誌分頁
    r = mgr.get('/admin/audit?tab=logins')
    assert r.status_code == 200 and 'loginLogTable' in r.get_data(as_text=True)
    assert mgr.get('/admin/audit/export?tab=logins').status_code == 200


def test_audit_page_requires_manager_and_dashboard_links(app):
    staff = login_staff(app, 'audit_staff', 'staff')
    assert staff.get('/admin/audit').status_code in (302, 403)
    assert staff.get('/admin/audit/export').status_code in (302, 403)
    mgr = login_staff(app, 'audit_mgr2', 'manager')
    html = mgr.get('/admin?tab=audit').get_data(as_text=True)
    assert '/admin/audit' in html and '最近 200 筆' in html
    assert '稽核紀錄' in mgr.get('/admin/settings').get_data(as_text=True)


def test_audit_export_neutralizes_formulas(app):
    _seed_audits(app, 1, username='audit_formula', action='EDIT_USER')
    from app.extensions import db
    from app.models.user import StaffAuditLog
    with app.app_context():
        row = StaffAuditLog.query.filter_by(staff_username='audit_formula').first()
        row.details = '=HYPERLINK("http://evil")'
        db.session.commit()
    mgr = login_staff(app, 'audit_mgr3', 'manager')
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(mgr.get('/admin/audit/export?staff=audit_formula').data)).active
    cell = [c for c in ws['J'] if c.value and 'evil' in str(c.value)][0]
    assert cell.data_type != 'f'


# ---------------------------------------------------------------------------
# 平台端店家管理
# ---------------------------------------------------------------------------
def test_tenant_management_only_for_platform_admin(app):
    platform = login_staff(app, 'platform_owner', 'owner')
    other = other_shop_owner_client(app, 'tenant-mgmt-shop')
    assert other.get('/admin/tenants').status_code in (302, 403)
    assert '店家管理' not in other.get('/admin/settings').get_data(as_text=True)
    r = platform.get('/admin/tenants')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'tenantTable' in html and 'data-slug="tenant-mgmt-shop"' in html and 'data-slug="default"' in html
    assert '店家管理' in platform.get('/admin/settings').get_data(as_text=True)


def test_tenant_toggle_disables_login_storefront_and_audits(app):
    from app.extensions import db
    from app.models.tenant import Tenant
    from app.models.user import StaffAuditLog
    platform = login_staff(app, 'platform_owner2', 'owner')
    other = other_shop_owner_client(app, 'tenant-toggle-shop')
    with app.app_context():
        tid = Tenant.query.filter_by(slug='tenant-toggle-shop').first().id
        did = Tenant.query.filter_by(slug='default').first().id
    assert app.test_client().get('/shop/tenant-toggle-shop').status_code != 404
    r = platform.post(f'/admin/tenants/{tid}/toggle')
    assert r.status_code == 302
    with app.app_context():
        assert Tenant.query.filter_by(slug='tenant-toggle-shop').first().is_active is False
        assert StaffAuditLog.query.filter_by(action_type='TENANT_TOGGLE').count() >= 1
    assert app.test_client().get('/shop/tenant-toggle-shop').status_code == 404
    assert '登入' in other.get('/admin').get_data(as_text=True), '已登入的員工因店家停用而失效'
    assert other.get('/admin/menu_pro').status_code in (302, 401, 403)
    assert '已停用' in platform.get('/admin/tenants').get_data(as_text=True)
    # 再啟用
    platform.post(f'/admin/tenants/{tid}/toggle')
    with app.app_context():
        assert Tenant.query.filter_by(slug='tenant-toggle-shop').first().is_active is True
    assert app.test_client().get('/shop/tenant-toggle-shop').status_code != 404
    # 預設店家與不存在的店家
    r = platform.post(f'/admin/tenants/{did}/toggle', headers={'Accept': 'application/json'})
    assert r.status_code == 409
    assert platform.post('/admin/tenants/999999/toggle', headers={'Accept': 'application/json'}).status_code == 404
    with app.app_context():
        assert db.session.get(Tenant, did).is_active is True


def test_tenant_overview_counts(app):
    from app.routes.tenants_admin import tenant_overview
    item = make_item(app, '店家概況餐', price=10)
    pos = login_staff(app, 'overview_staff', 'staff')
    pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': '內用'})
    with app.test_request_context('/'):
        rows = tenant_overview()
    default = next(r for r in rows if r['slug'] == 'default')
    assert default['is_default'] and default['orders'] >= 1 and default['revenue'] >= 10 and default['staff'] >= 1
    assert default['last_order_at'] is not None


# ---------------------------------------------------------------------------
# Web Push
# ---------------------------------------------------------------------------
@pytest.fixture
def push_enabled(store_settings):
    store_settings('push', enabled=True, contact_email='push@example.com', vapid_private_key='', vapid_public_key='')
    yield


def _browser_subscription():
    """瀏覽器端的 PushSubscription.toJSON()：p256dh 需是合法的 P-256 公鑰"""
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()  # noqa: E731
    return {'endpoint': 'https://push.example.com/send/abc123', 'keys': {'p256dh': b64(raw), 'auth': b64(b'0123456789abcdef')}}


def test_vapid_keys_generated_once_and_public_key_served(app, push_enabled):
    c = app.test_client()
    c.get('/?shop=default')
    r = c.get('/api/push/vapid')
    assert r.status_code == 200 and r.get_json()['enabled'] is True
    key = r.get_json()['public_key']
    assert len(key) > 60
    assert c.get('/api/push/vapid').get_json()['public_key'] == key, '金鑰只產生一次'
    # 設定頁看得到公鑰、私鑰為 secret (不回傳)
    owner = login_staff(app, 'push_owner', 'owner')
    html = owner.get('/admin/settings/push').get_data(as_text=True)
    assert key in html and 'BEGIN PRIVATE KEY' not in html


def test_push_disabled_hides_feature(app, store_settings):
    store_settings('push', enabled=False)
    item = make_item(app, '推播關閉餐', price=10)
    c = app.test_client()
    c.get('/?shop=default')
    c.post('/guest_login')
    assert c.get('/api/push/vapid').get_json() == {'success': True, 'enabled': False, 'public_key': ''}
    uuid = checkout(c, [line(item)]).get_json()['order_uuid']
    assert c.get(f'/api/track/{uuid}').get_json()['push_enabled'] is False
    assert c.post(f'/api/track/{uuid}/push', json={'subscription': _browser_subscription()}).status_code == 404


def test_subscribe_and_notify_on_ready_and_delivery(app, push_enabled, monkeypatch):
    from app.models.integration import PushSubscription
    from app.services import push_service
    item = make_item(app, '推播通知餐', price=30)
    c = app.test_client()
    c.get('/?shop=default')
    c.post('/guest_login')
    receipt = checkout(c, [line(item)]).get_json()
    uuid, order_id = receipt['order_uuid'], receipt['order_id']
    track = c.get(f'/api/track/{uuid}').get_json()
    assert track['push_enabled'] is True
    # 訂閱：格式檢查
    assert c.post(f'/api/track/{uuid}/push', json={'subscription': {'endpoint': 'http://insecure'}}).status_code == 400
    sub = _browser_subscription()
    assert c.post(f'/api/track/{uuid}/push', json={'subscription': sub}).status_code == 200
    assert c.post(f'/api/track/{uuid}/push', json={'subscription': sub}).status_code == 200, '重複訂閱只更新'
    with app.app_context():
        assert PushSubscription.query.filter_by(order_uuid=uuid).count() == 1

    sent = []

    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    def fake_post(url, data=None, headers=None, timeout=None):
        sent.append((url, data, headers))
        return FakeResp(201)
    monkeypatch.setattr(push_service.requests, 'post', fake_post)
    kitchen = login_staff(app, 'push_kitchen', 'kitchen')
    r = kitchen.post(f'/api/orders/{order_id}/status', json={'status': 'Completed'})
    assert r.status_code == 200
    assert len(sent) == 1
    url, body, headers = sent[0]
    assert url == sub['endpoint'] and headers['Content-Encoding'] == 'aes128gcm' and headers['TTL'] == '3600'
    assert headers['Authorization'].startswith('vapid t=') and 'k=' in headers['Authorization']
    assert isinstance(body, bytes) and len(body) > 100 and '餐點'.encode() not in body, '內容已加密'
    with app.app_context():
        assert PushSubscription.query.filter_by(order_uuid=uuid).count() == 0, '送出後刪除訂閱'
    # 已完成的訂單不再接受訂閱
    assert c.post(f'/api/track/{uuid}/push', json={'subscription': sub}).status_code == 409


def test_push_gone_removes_subscription_and_errors_are_kept(app, push_enabled, monkeypatch):
    from app.extensions import db
    from app.models.integration import PushSubscription
    from app.models.order import Order
    from app.services import push_service
    item = make_item(app, '推播失效餐', price=30)
    c = app.test_client()
    c.get('/?shop=default')
    c.post('/guest_login')
    uuid = checkout(c, [line(item)]).get_json()['order_uuid']
    s1, s2 = _browser_subscription(), _browser_subscription()
    s2['endpoint'] = 'https://push.example.com/send/other'
    for s in (s1, s2):
        assert c.post(f'/api/track/{uuid}/push', json={'subscription': s}).status_code == 200

    class FakeResp:
        def __init__(self, code):
            self.status_code = code
    codes = {s1['endpoint']: 410, s2['endpoint']: 500}
    monkeypatch.setattr(push_service.requests, 'post', lambda url, **kw: FakeResp(codes[url]))
    with app.app_context():   # 背景執行緒：沒有請求情境
        tid = default_tenant_id(app)
        assert push_service.send_to_order(uuid, {'title': 't', 'body': 'b'}, tid) == 0
    with app.app_context():
        left = PushSubscription.query.filter_by(order_uuid=uuid).all()
        assert [s.endpoint for s in left] == [s2['endpoint']] and left[0].last_error == 'HTTP 500'
        # 每日清理：超過 7 天的訂閱刪除
        left[0].created_at = datetime(2020, 1, 1)
        db.session.commit()
        assert push_service.cleanup() == 1
        db.session.commit()
        assert PushSubscription.query.filter_by(order_uuid=uuid).count() == 0
        assert db.session.get(Order, Order.query.filter_by(uuid=uuid).first().id) is not None


def test_service_worker_and_track_page_have_push_code(app):
    c = app.test_client()
    sw = c.get('/sw.js').get_data(as_text=True)
    assert "addEventListener('push'" in sw and "addEventListener('notificationclick'" in sw
    with open('static/js/track.js', encoding='utf-8') as fh:
        js = fh.read()
    assert 'pushManager.subscribe' in js and '/api/push/vapid' in js
