"""後台「系統設定」：表單產生、金鑰不外流、負責人限定、測試連線、功能入口"""
from app.extensions import db
from app.models.settings import StoreSetting
from app.services import settings_service
from tests.support import assert_flash, default_tenant_id, login_staff


def test_settings_hub_lists_all_sections(app, admin_client):
    body = admin_client.get('/admin/settings').get_data(as_text=True)
    for key in settings_service.SECTIONS:
        assert f'data-setting-key="{key}"' in body, key
    for name in ('電子發票', '交班與日結', '原物料與配方', '促銷活動', '營運報表', '系統監控'):
        assert name in body


def test_every_setting_page_renders(app, admin_client):
    for key in settings_service.SECTIONS:
        r = admin_client.get(f'/admin/settings/{key}')
        assert r.status_code == 200, key
    assert admin_client.get('/admin/settings/not-a-section').status_code == 404


def test_secret_is_saved_but_never_rendered(app, admin_client, store_settings):
    store_settings('linepay', enabled=False)   # 測試結束後還原
    r = admin_client.post('/admin/settings/linepay', data={
        'enabled': '1', 'sandbox': '1', 'channel_id': '1650000000', 'channel_secret': 'super-secret-value-123',
        'product_name': '餐點'})
    assert r.status_code == 302
    page = admin_client.get('/admin/settings/linepay').get_data(as_text=True)
    assert 'super-secret-value-123' not in page and '已設定 (留空保持不變)' in page
    # 留空 = 保持原值；勾選清除才刪除
    admin_client.post('/admin/settings/linepay', data={'enabled': '1', 'channel_id': '1650000000', 'channel_secret': ''})
    with app.test_request_context():
        assert settings_service.get('linepay', default_tenant_id(app))['channel_secret'] == 'super-secret-value-123'
    admin_client.post('/admin/settings/linepay', data={'enabled': '1', 'channel_secret__clear': '1'})
    with app.app_context():
        assert settings_service.get('linepay', default_tenant_id(app))['channel_secret'] == ''


def test_secret_sections_are_owner_only(app, manager_client):
    assert manager_client.get('/admin/settings/einvoice').status_code == 403
    assert manager_client.get('/admin/settings/pos').status_code == 200   # 沒有金鑰的設定店長可修改
    assert login_staff(app, 'settings_staff', 'staff').get('/admin/settings').status_code == 403


def test_form_validation_and_ranges(app, admin_client, store_settings):
    store_settings('pos', service_charge_percent=0)
    admin_client.post('/admin/settings/pos', data={'service_charge_percent': '999', 'service_charge_scope': ['pos_dine_in', 'evil']})
    with app.app_context():
        cfg = settings_service.get('pos', default_tenant_id(app))
        assert cfg['service_charge_percent'] == 30 and cfg['service_charge_scope'] == ['pos_dine_in']
    admin_client.post('/admin/settings/reservation', data={'open_time': '25:99'})
    assert_flash(admin_client, '時間格式')


def test_tester_failure_is_reported(app, admin_client, store_settings, monkeypatch):
    from app.errors import AppError
    store_settings('sms', provider='mitake')
    monkeypatch.setitem(settings_service.TESTERS, 'sms', lambda cfg: (_ for _ in ()).throw(AppError('帳號錯誤')))
    admin_client.post('/admin/settings/sms/test')
    assert_flash(admin_client, '測試失敗：帳號錯誤')
    monkeypatch.setitem(settings_service.TESTERS, 'sms', lambda cfg: '連線正常')
    admin_client.post('/admin/settings/sms/test')
    assert_flash(admin_client, '測試成功：連線正常')


def test_settings_are_per_tenant(app, store_settings):
    from tests.support import other_shop_owner_client
    store_settings('pos', service_charge_percent=10)
    other = other_shop_owner_client(app)
    other.post('/admin/settings/pos', data={'service_charge_percent': '5'})
    with app.app_context():
        assert settings_service.get('pos', default_tenant_id(app))['service_charge_percent'] == 10
        rows = StoreSetting.query.filter_by(key='pos').all()
        assert len({r.tenant_id for r in rows}) == 2
        db.session.rollback()
