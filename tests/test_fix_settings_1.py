"""修正小組 settings-1：系統設定 (settings_service / settings_admin) 的權限、驗證、匯入匯出與稽核回歸測試"""
import io
import json

import pytest

from app.errors import AppError
from app.extensions import db
from tests.support import (assert_flash, default_tenant_id, flashes, get_or_create_tenant, login_staff,
                           other_shop_owner_client, tenant_request)


def _import(client, payload):
    data = {'file': (io.BytesIO(json.dumps(payload, ensure_ascii=False).encode('utf-8')), 'settings.json')}
    return client.post('/admin/settings/import', data=data, content_type='multipart/form-data')


# ---------------------------------------------------------------------------
# B-324 自動備份是整台主機的設定：只有平台管理者能看 / 改 / 測試；其他店家的設定不會生效
# ---------------------------------------------------------------------------
def test_backup_settings_platform_admin_only_and_host_wide(app, store_settings):
    from app.models.settings import StoreSetting
    from app.services import settings_service
    store_settings('backup', enabled=True, time='03:30', offsite='none')
    other = other_shop_owner_client(app, 'fix-s1-backup-shop')
    assert other.get('/admin/settings/backup').status_code == 403
    r = other.post('/admin/settings/backup', data={'enabled': '1', 'time': '00:00', 'offsite': 'webdav',
                                                   'offsite_url': 'https://attacker.example/dav'})
    assert r.status_code == 403
    assert other.post('/admin/settings/backup/test').status_code == 403
    assert '自動備份' not in other.get('/admin/settings').get_data(as_text=True)
    tid_other = get_or_create_tenant(app, 'fix-s1-backup-shop')
    with app.app_context():
        # 舊版留下的其他店家備份設定 (或直接寫入) 一律不生效：讀到的是預設店家那一份
        row = StoreSetting(tenant_id=tid_other, key='backup',
                           value=json.dumps({'time': '00:00', 'offsite': 'command', 'offsite_command': 'evil {file}'}))
        db.session.add(row)
        db.session.commit()
        try:
            cfg = settings_service.get('backup', tid_other)
            assert cfg['offsite'] == 'none' and cfg['time'] == '03:30'
        finally:
            db.session.delete(row)
            db.session.commit()
    # 平台管理者 (預設店家負責人) 照常可用
    owner = login_staff(app, 'fix_s1_platform_owner', 'owner')
    assert owner.get('/admin/settings/backup').status_code == 200


# ---------------------------------------------------------------------------
# B-325 帳號安全只有負責人能改 (設定頁與匯入)
# ---------------------------------------------------------------------------
def test_security_section_owner_only(app, store_settings):
    from app.services import settings_service
    store_settings('security', require_2fa_roles=['owner'])
    mgr = login_staff(app, 'fix_s1_mgr', 'manager')
    assert mgr.get('/admin/settings/security').status_code == 403
    assert mgr.post('/admin/settings/security', data={'staff_idle_minutes': '0'}).status_code == 403
    assert _import(mgr, {'version': 1, 'sections': {'security': {'require_2fa_roles': []}}}).status_code == 302
    with tenant_request(app, default_tenant_id(app)):
        assert settings_service.get('security')['require_2fa_roles'] == ['owner']


# ---------------------------------------------------------------------------
# B-326 測試連線丟出非 AppError (ConnectionError 等) 時顯示失敗原因，不回 500
# ---------------------------------------------------------------------------
def test_tester_unexpected_exception_is_flashed(app, monkeypatch):
    from app.services import settings_service

    def boom(cfg):
        raise ConnectionError('綠界回應錯誤：HashKey 不正確')
    monkeypatch.setitem(settings_service.TESTERS, 'einvoice', boom)
    owner = login_staff(app, 'fix_s1_test_owner', 'owner')
    r = owner.post('/admin/settings/einvoice/test')
    assert r.status_code == 302
    assert_flash(owner, '測試失敗：綠界回應錯誤')


# ---------------------------------------------------------------------------
# B-327 線上端匯入設定會下發到分店 (寫入 HqSetting)
# ---------------------------------------------------------------------------
def test_hub_import_publishes_hq_settings(app, store_settings, monkeypatch):
    from app.models.settings import HqSetting
    from app.sync import runtime as sync_runtime
    store_settings('refund', require_reason=True)
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    owner = login_staff(app, 'fix_s1_hub_owner', 'owner')
    tid = default_tenant_id(app)
    with app.app_context():
        before = HqSetting.query.filter_by(tenant_id=tid, key='refund').first()
        saved = before.value if before else None
    try:
        r = _import(owner, {'version': 1, 'sections': {'refund': {'require_reason': False}}})
        assert r.status_code == 302
        assert any('下發' in m for m in flashes(owner))
        with app.app_context():
            row = HqSetting.query.filter_by(tenant_id=tid, key='refund').first()
            assert row is not None and json.loads(row.value)['require_reason'] is False
    finally:
        with app.app_context():
            row = HqSetting.query.filter_by(tenant_id=tid, key='refund').first()
            if saved is None and row is not None:
                db.session.delete(row)
            elif row is not None:
                row.value = saved
            db.session.commit()


# ---------------------------------------------------------------------------
# B-328 / B-330 接單控制：設定頁不寫回暫停狀態 (由 POS 切換)，顯示實際狀態
# ---------------------------------------------------------------------------
def test_order_intake_form_does_not_override_pos_pause(app, store_settings):
    from app.services import intake_service, settings_service
    store_settings('order_intake', paused=True, resume_at='', window_minutes=15)
    owner = login_staff(app, 'fix_s1_intake_owner', 'owner')
    # 表單沒勾暫停、恢復時間亂填 → 暫停狀態不變，其他規則照常儲存
    r = owner.post('/admin/settings/order_intake', data={'resume_at': '18:30', 'window_minutes': '30',
                                                         'max_orders_per_window': '0'})
    assert r.status_code == 302
    with tenant_request(app, default_tenant_id(app)):
        cfg = settings_service.get('order_intake')
        assert cfg['paused'] is True and cfg['resume_at'] == '' and cfg['window_minutes'] == 30
    # 已過自動恢復時間：設定頁顯示「未暫停」
    store_settings('order_intake', paused=True, resume_at='2000-01-01T00:00')
    with tenant_request(app, default_tenant_id(app)):
        view = settings_service.form_view('order_intake')
        assert view['paused'] is False and view['resume_at'] == ''
        assert intake_service.is_paused() is False
    store_settings('order_intake', paused=False, resume_at='')


# ---------------------------------------------------------------------------
# B-331 / R2-118 多個子功能任一啟用就算啟用
# ---------------------------------------------------------------------------
def test_is_enabled_considers_all_sub_features(app, store_settings):
    from app.services import settings_service
    store_settings('delivery', ubereats_enabled=False, foodpanda_enabled=True)
    store_settings('line', login_enabled=False, messaging_enabled=True)
    store_settings('monitor', webhook_url='', email_recipients='ops@example.com', line_alerts=False)
    store_settings('ai', key1='', key2='sk-test-key-2', key3='')
    with tenant_request(app, default_tenant_id(app)):
        for key in ('delivery', 'line', 'monitor', 'ai'):
            assert settings_service.is_enabled(key), key
    store_settings('delivery', foodpanda_enabled=False)
    store_settings('ai', key2='  ')
    with tenant_request(app, default_tenant_id(app)):
        assert not settings_service.is_enabled('delivery') and not settings_service.is_enabled('ai')


# ---------------------------------------------------------------------------
# B-332 小數欄位套用最小 / 最大值；R2-119 超過長度上限拒絕儲存 (不靜默截斷)
# ---------------------------------------------------------------------------
def test_parse_form_float_clamp_and_length_limit(app):
    from werkzeug.datastructures import MultiDict

    from app.services import settings_service
    with tenant_request(app, default_tenant_id(app)):
        v = settings_service.parse_form('pnl', MultiDict({'card_fee_percent': '-5', 'mobile_fee_percent': '500',
                                                          'online_fee_percent': 'nan', 'platform_fee_percent': '12.5'}))
        assert v['card_fee_percent'] == 0 and v['mobile_fee_percent'] == 50
        assert v['online_fee_percent'] == 2.75 and v['platform_fee_percent'] == 12.5
        faq = '\n'.join(f'關鍵字{i} => 這是第 {i} 則回答，內容稍長一些' for i in range(200))
        assert 2000 < len(faq) <= 10000
        assert settings_service.parse_form('chatbot', MultiDict({'faq': faq}))['faq'] == faq
        with pytest.raises(AppError, match='最多'):
            settings_service.parse_form('chatbot', MultiDict({'faq': 'x' * 10001}))
        with pytest.raises(AppError, match='最多 2000 字'):
            settings_service.parse_form('report_email', MultiDict({'recipients': 'a@example.com,' * 200}))


# ---------------------------------------------------------------------------
# B-333 店長匯出不含只有負責人能看的區塊
# ---------------------------------------------------------------------------
def test_manager_export_skips_owner_only_sections(app, store_settings):
    store_settings('smtp', host='smtp.example.com', username='mailer')
    mgr = login_staff(app, 'fix_s1_export_mgr', 'manager')
    sections = mgr.get('/admin/settings/export.json').get_json()['sections']
    assert 'pos' in sections
    for key in ('smtp', 'line', 'sms', 'einvoice', 'monitor', 'security'):
        assert key not in sections, key
    owner = login_staff(app, 'fix_s1_export_owner', 'owner')
    assert owner.get('/admin/settings/export.json').get_json()['sections']['smtp']['host'] == 'smtp.example.com'


# ---------------------------------------------------------------------------
# B-334 匯入檔型別錯誤不回 500；bool 字串 "false" 不會變成 True
# ---------------------------------------------------------------------------
def test_import_rejects_bad_types_without_500(app, store_settings):
    from app.services import settings_service
    store_settings('refund', allow_partial=True)
    with tenant_request(app, default_tenant_id(app)):
        with pytest.raises(AppError):
            settings_service.import_all({'version': 'abc', 'sections': {}})
        applied, skipped = settings_service.import_all({'version': '1.0', 'sections': {
            'payments': {'pos_methods': [['x'], 'Cash'], 'atm_expire_days': float('inf')},
            'sms': {}, 'einvoice': {},
            'refund': {'allow_partial': 'false', 'require_reason': {'x': 1}},
            'stamps': {'mode': ['item']},
        }}, allow_secrets=True)
        assert 'payments.atm_expire_days' in skipped and 'refund.require_reason' in skipped and 'stamps.mode' in skipped
        assert settings_service.get('payments')['pos_methods'] == ['Cash']
        assert settings_service.get('refund')['allow_partial'] is False
        db.session.rollback()
    owner = login_staff(app, 'fix_s1_import_owner', 'owner')
    assert _import(owner, {'version': 1, 'sections': {'stamps': {'mode': {'a': 1}}}}).status_code == 302


# ---------------------------------------------------------------------------
# B-335 匯入後已啟用但缺金鑰的服務要提醒
# ---------------------------------------------------------------------------
def test_import_warns_missing_secrets(app, store_settings):
    store_settings('sms', provider='none', password='')
    owner = login_staff(app, 'fix_s1_missing_owner', 'owner')
    r = _import(owner, {'version': 1, 'sections': {'sms': {'provider': 'mitake', 'username': 'u'}}})
    assert r.status_code == 302
    assert_flash(owner, '尚未填寫金鑰')


# ---------------------------------------------------------------------------
# R2-116 功能入口依權限顯示
# ---------------------------------------------------------------------------
def test_feature_pages_filtered_by_permission(app):
    from app.models.tenant import StaffAccount
    staff = login_staff(app, 'fix_s1_settings_staff', 'staff')
    with app.app_context():
        StaffAccount.query.filter_by(username='fix_s1_settings_staff').first().permissions = 'settings'
        db.session.commit()
    html = staff.get('/admin/settings').get_data(as_text=True)
    for desc in ('毛利、時段熱度、分店比較', '日結傳票、發票明細', '作廢 / 退款、折扣 / 改價、交班帳差異常排行',
                 '時段供應、通路價、過敏原', '開立紀錄、重送、作廢'):
        assert desc not in html, desc
    assert '自有外送：指派外送員' in html   # POS 角色可進的頁面照常顯示
    mgr = login_staff(app, 'fix_s1_pages_mgr', 'manager')
    assert '毛利、時段熱度、分店比較' in mgr.get('/admin/settings').get_data(as_text=True)


# ---------------------------------------------------------------------------
# R2-117 稽核只記錄實際變動的欄位，金鑰記錄更新 / 清除
# ---------------------------------------------------------------------------
def test_settings_change_audit_lists_only_changed_fields(app, store_settings):
    from app.models.user import StaffAuditLog
    store_settings('refund', void_requires_approval=True, require_reason=True, allow_partial=True, restock_on_cancel=True)
    store_settings('linepay', channel_secret='old-secret')
    owner = login_staff(app, 'fix_s1_audit_owner', 'owner')
    owner.post('/admin/settings/refund', data={'require_reason': '1', 'allow_partial': '1', 'restock_on_cancel': '1'})
    owner.post('/admin/settings/linepay', data={'channel_secret__clear': '1', 'sandbox': '1', 'product_name': '餐點'})
    with app.app_context():
        logs = (StaffAuditLog.query.filter_by(action_type='SETTINGS_CHANGE', staff_username='fix_s1_audit_owner')
                .execution_options(skip_tenant_filter=True).order_by(StaffAuditLog.id).all())
        refund, linepay = logs[-2].details, logs[-1].details
    assert '員工作廢已收款訂單需主管核准: True→False' in refund and '部分退款必須填寫原因' not in refund
    assert 'Channel Secret Key 已清除' in linepay


# ---------------------------------------------------------------------------
# 跨檔請求：B-022 / B-153 / R2-123 / B-459 / R2-179 設定頁儲存時的驗證
# ---------------------------------------------------------------------------
def test_parse_form_cross_field_validation(app):
    from werkzeug.datastructures import MultiDict

    from app.services import settings_service
    with tenant_request(app, default_tenant_id(app)):
        # B-022 預設捐贈但愛心碼空白 / 格式錯誤 → 拒絕；填 3~7 碼數字才可存
        with pytest.raises(AppError, match='愛心碼'):
            settings_service.parse_form('einvoice', MultiDict({'default_carrier': 'donate', 'default_love_code': ''}))
        with pytest.raises(AppError, match='愛心碼'):
            settings_service.parse_form('einvoice', MultiDict({'default_carrier': 'donate', 'default_love_code': '12ab'}))
        v = settings_service.parse_form('einvoice', MultiDict({'default_carrier': 'donate', 'default_love_code': ' 168001 '}))
        assert v['default_love_code'] == '168001'
        assert settings_service.parse_form('einvoice', MultiDict({'default_carrier': 'print'}))['default_carrier'] == 'print'
        # B-153 AI 金鑰：去掉零寬字元；全形 / 中文 / 中間有空白拒絕
        v = settings_service.parse_form('ai', MultiDict({'key1': '​AIzaSyTEST123﻿ '}))
        assert v['key1'] == 'AIzaSyTEST123'
        for bad in ('AIzaSyＡBC', 'sk-ant-test(備用)', 'sk-ant a b'):
            with pytest.raises(AppError, match='無效字元'):
                settings_service.parse_form('ai', MultiDict({'key1': bad}))
        # 其他區塊的金鑰不受影響
        assert settings_service.parse_form('linepay', MultiDict({'channel_secret': ' abc '}))['channel_secret'] == 'abc'
        # R2-123 url 欄位：主機含反斜線 / 空白拒絕
        for bad in ('http://127.0.0.1:8080\\@example.com/bucket', 'https://exa mple.com/x', 'http://[::1/x'):
            with pytest.raises(AppError):
                settings_service.parse_form('backup', MultiDict({'offsite_url': bad, 'time': '03:30'}))
        ok = settings_service.parse_form('backup', MultiDict({'offsite_url': 'https://s3.example.com', 'time': '03:30'}))
        assert ok['offsite_url'] == 'https://s3.example.com'
        # B-459 監控 Webhook：內網 / 本機與格式錯誤拒絕；留空 = 沿用原值不檢查
        with pytest.raises(AppError, match='內網或本機'):
            settings_service.parse_form('monitor', MultiDict({'webhook_url': 'https://127.0.0.1/hook'}))
        with pytest.raises(AppError):
            settings_service.parse_form('monitor', MultiDict({'webhook_url': 'https://127.0.0.1:8080\\@example.com/'}))
        with pytest.raises(AppError):
            settings_service.parse_form('monitor', MultiDict({'webhook_url': 'https://a..b/x'}))
        settings_service.parse_form('monitor', MultiDict({'webhook_url': '', 'clock_server': 'time.stdtime.gov.tw'}))
        # R2-179 校時伺服器格式
        for bad in ('time..stdtime.gov.tw', '.time.google.com', 'a' * 64 + '.com', 'time.google.com:123',
                    'http://time.google.com', 'time google.com'):
            with pytest.raises(AppError, match='校時伺服器'):
                settings_service.parse_form('monitor', MultiDict({'clock_server': bad}))
        for good in ('pool.ntp.org', '192.168.1.10', '2001:db8::1', ''):
            assert settings_service.parse_form('monitor', MultiDict({'clock_server': good}))['clock_server'] == good


# ---------------------------------------------------------------------------
# B-336 總部從未下發過的區塊：不顯示「改回總部設定」，改回時也不刪本店設定
# ---------------------------------------------------------------------------
def test_revert_to_hq_requires_hq_setting(app, store_settings, monkeypatch):
    from app.models.settings import HqSetting, StoreSetting
    from app.services import settings_service
    from app.sync import runtime as sync_runtime
    tid = default_tenant_id(app)
    with app.app_context():
        assert HqSetting.query.filter_by(tenant_id=tid, key='kds').first() is None
    store_settings('kds', warn_minutes=9)
    monkeypatch.setattr(sync_runtime, 'is_node', lambda: True)
    owner = login_staff(app, 'fix_s1_revert_owner', 'owner')
    assert '改回總部設定</button>' not in owner.get('/admin/settings/kds').get_data(as_text=True)
    owner.post('/admin/settings/kds', data={'action': 'revert_hq'})
    with app.app_context():
        assert StoreSetting.query.filter_by(tenant_id=tid, key='kds').first() is not None
    with tenant_request(app, tid):
        assert settings_service.has_hq('kds') is False
        with pytest.raises(AppError, match='總部尚未下發'):
            settings_service.revert_to_hq('kds')
    # 總部已下發：照常顯示並可改回
    with app.app_context():
        db.session.add(HqSetting(tenant_id=tid, key='kds', value=json.dumps({'warn_minutes': 7})))
        db.session.commit()
    try:
        assert '改回總部設定</button>' in owner.get('/admin/settings/kds').get_data(as_text=True)
        assert owner.post('/admin/settings/kds', data={'action': 'revert_hq'}).status_code == 302
        with tenant_request(app, tid):
            assert settings_service.get('kds')['warn_minutes'] == 7
    finally:
        with app.app_context():
            HqSetting.query.filter_by(tenant_id=tid, key='kds').delete()
            db.session.commit()


# ---------------------------------------------------------------------------
# R2-164 其他店家也啟用同一平台且沒填代碼時，本店必須填平台店家代碼
# ---------------------------------------------------------------------------
def test_delivery_platform_code_required_when_ambiguous(app, store_settings):
    from werkzeug.datastructures import MultiDict

    from app.models.settings import StoreSetting
    from app.services import settings_service
    other = get_or_create_tenant(app, 'fix-s1-delivery-shop')
    with tenant_request(app, default_tenant_id(app)):
        # 沒有其他店家啟用：可不填
        settings_service.parse_form('delivery', MultiDict({'foodpanda_enabled': '1'}))
    with tenant_request(app, other):
        settings_service.put('delivery', {'foodpanda_enabled': True, 'foodpanda_vendor_code': ''})
        db.session.commit()
    try:
        with tenant_request(app, default_tenant_id(app)):
            with pytest.raises(AppError, match='foodpanda 店家代碼'):
                settings_service.parse_form('delivery', MultiDict({'foodpanda_enabled': '1'}))
            v = settings_service.parse_form('delivery', MultiDict({'foodpanda_enabled': '1', 'foodpanda_vendor_code': 'V1'}))
            assert v['foodpanda_vendor_code'] == 'V1'
            settings_service.parse_form('delivery', MultiDict({'ubereats_enabled': '1'}))   # 其他平台不受影響
    finally:
        with app.app_context():
            (StoreSetting.query.filter_by(tenant_id=other, key='delivery')
             .execution_options(skip_tenant_filter=True).delete())
            db.session.commit()


def test_new_setting_fields_exist(app):
    from app.services import settings_service
    names = {k: {f.name: f for f in s.fields} for k, s in settings_service.SECTIONS.items()}
    assert names['refund']['restock_picked_up'].default is False
    assert names['accounting']['acct_bonus'].default == '6150 儲值贈送 (行銷費用)'
    assert '平板' in names['delivery']['ubereats_auto_accept'].help
    assert '必填' in names['delivery']['ubereats_store_id'].help and '必填' in names['delivery']['foodpanda_vendor_code'].help
