"""
異地備份上傳 (WebDAV / S3 簽章 / 自訂指令 / 修剪 / 每日備份後呼叫 / 設定頁測試)、
門市一鍵更新 (版本比較 / GitHub 查詢與快取 / 下載 / 系統頁面板 / Windows 以外不可套用)、
錯誤回報與指標 (/metrics 需 METRICS_TOKEN、Prometheus 格式、請求計數；Sentry 選用初始化)、
N+1 守門 (資料加倍查詢數不變)、Dependabot 設定存在。
"""
import os
import re
import zipfile

import pytest

from app.errors import AppError
from tests.support import checkout, count_queries, guest_client, line, login_staff, make_item

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# 異地備份
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, text=''):
        self.status_code, self.text = status, text


def test_offsite_webdav_upload_and_prune(app, monkeypatch, tmp_path):
    from app.services import offsite_backup_service as svc
    calls = []

    def fake_request(method, url, **kw):
        calls.append((method, url))
        if method == 'PROPFIND':
            return FakeResp(207, ''.join(f'<d:href>/dav/backups/menu-2026010{i}-030000.zip</d:href>' for i in range(1, 6)))
        return FakeResp(204)
    monkeypatch.setattr(svc.requests, 'request', fake_request)
    monkeypatch.setattr(svc.requests, 'put', lambda url, **kw: calls.append(('PUT', url)) or FakeResp(201))
    monkeypatch.setattr(svc.requests, 'delete', lambda url, **kw: calls.append(('DELETE', url)) or FakeResp(204))
    path = tmp_path / 'menu-20260131-030000.zip'
    path.write_bytes(b'zip')
    cfg = {'offsite': 'webdav', 'offsite_url': 'https://nas.example.com/dav/backups/', 'offsite_user': 'u',
           'offsite_password': 'p', 'offsite_keep': 3}
    with app.app_context():
        msg = svc.upload(str(path), cfg=cfg)
    assert '已上傳 menu-20260131-030000.zip 到 WebDAV' in msg and '刪除 2 份' in msg
    assert ('PUT', 'https://nas.example.com/dav/backups/menu-20260131-030000.zip') in calls
    deleted = [u for m, u in calls if m == 'DELETE']
    assert deleted == ['https://nas.example.com/dav/backups/menu-20260101-030000.zip',
                       'https://nas.example.com/dav/backups/menu-20260102-030000.zip']
    # 上傳失敗 → AppError；網址錯誤
    monkeypatch.setattr(svc.requests, 'put', lambda url, **kw: FakeResp(500))
    with app.app_context():
        with pytest.raises(AppError):
            svc.upload(str(path), cfg=cfg)
        with pytest.raises(AppError):
            svc.upload(str(path), cfg=dict(cfg, offsite_url='nas.example.com'))
        assert svc.upload(str(path), cfg={'offsite': 'none'}) == '未設定異地備份'


def test_offsite_s3_signature_and_command(app, monkeypatch, tmp_path):
    from app.services import offsite_backup_service as svc
    seen = []

    def fake_request(method, url, **kw):
        seen.append((method, url, kw['headers']))
        if method == 'GET':
            return FakeResp(200, '<Key>store1/menu-20260101-030000.zip</Key><Key>store1/other.txt</Key>')
        return FakeResp(200)
    monkeypatch.setattr(svc.requests, 'request', fake_request)
    path = tmp_path / 'menu-20260131-030000.zip'
    path.write_bytes(b'zip')
    cfg = {'offsite': 's3', 'offsite_url': 'https://s3.ap-northeast-1.amazonaws.com', 'offsite_user': 'AKIA',
           'offsite_password': 'secret', 'offsite_bucket': 'shop-backups/store1', 'offsite_region': 'ap-northeast-1',
           'offsite_keep': 10}
    with app.app_context():
        msg = svc.upload(str(path), cfg=cfg)
    assert msg.startswith('已上傳 menu-20260131-030000.zip 到 S3')
    put = next(x for x in seen if x[0] == 'PUT')
    assert put[1] == 'https://s3.ap-northeast-1.amazonaws.com/shop-backups/store1/menu-20260131-030000.zip'
    auth = put[2]['Authorization']
    assert auth.startswith('AWS4-HMAC-SHA256 Credential=AKIA/') and '/ap-northeast-1/s3/aws4_request' in auth
    assert re.search(r'Signature=[0-9a-f]{64}$', auth) and 'x-amz-date' in put[2] and 'x-amz-content-sha256' in put[2]
    assert all(x[0] != 'DELETE' for x in seen), '未超過保留份數不刪'
    with app.app_context():
        with pytest.raises(AppError):
            svc.upload(str(path), cfg=dict(cfg, offsite_bucket=''))
    # 自訂指令：{file} 代入；失敗的指令 → AppError
    import sys
    ok = {'offsite': 'command', 'offsite_command': f'"{sys.executable}" -c "import sys; open(sys.argv[1]+\'.ok\', \'w\').close()" {{file}}'}
    with app.app_context():
        assert '自訂指令' in svc.upload(str(path), cfg=ok)
    assert os.path.exists(str(path) + '.ok')
    bad = {'offsite': 'command', 'offsite_command': f'"{sys.executable}" -c "raise SystemExit(3)" {{file}}'}
    with app.app_context():
        with pytest.raises(AppError, match='exit 3'):
            svc.upload(str(path), cfg=bad)
        with pytest.raises(AppError, match='{file}'):
            svc.upload(str(path), cfg={'offsite': 'command', 'offsite_command': 'echo hi'})


def test_daily_backup_uploads_offsite_and_alerts_on_failure(app, store_settings, monkeypatch):
    from datetime import datetime
    from app.services import maintenance_service, monitor_service, offsite_backup_service as svc
    uploaded, alerts = [], []
    monkeypatch.setattr(svc, 'upload', lambda path, tenant_id=None, cfg=None: uploaded.append(path) or 'ok')
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append((msg, kw)))
    store_settings('backup', enabled=True, time='03:30', keep_days=14, offsite='webdav', offsite_url='https://x/',
                   offsite_user='u', offsite_password='p')
    with app.app_context():
        from app.extensions import db
        from app.sync.runtime import set_state
        set_state('job:backup', '')   # 其他測試可能今天已經跑過每日備份
        db.session.commit()
        if db.engine.dialect.name != 'sqlite':
            pytest.skip('每日 SQLite 備份只在門市 (SQLite) 執行')
        maintenance_service.run_daily_jobs(datetime.now().replace(hour=4, minute=0))
    assert uploaded and uploaded[0].endswith('.zip')
    # 上傳失敗：進監控事件，不影響本機備份
    def boom(path, tenant_id=None, cfg=None):
        raise AppError('WebDAV 上傳失敗 (HTTP 401)')
    monkeypatch.setattr(svc, 'upload', boom)
    with app.app_context():
        from tests.support import default_tenant_id
        svc.upload_after_backup('/tmp/menu-x.zip', default_tenant_id(app))
    assert alerts and '異地備份上傳失敗' in alerts[-1][0] and alerts[-1][1]['key'] == 'offsite-backup'
    # 設定頁「測試」
    from app.services import settings_service
    assert 'backup' in settings_service.TESTERS
    monkeypatch.setattr(svc, '_dav_put', lambda cfg, name, data: None)
    monkeypatch.setattr(svc, '_dav_delete', lambda cfg, name: None)
    with app.app_context():
        assert 'WebDAV' in settings_service.TESTERS['backup']({'offsite': 'webdav', 'offsite_url': 'https://x/'})
        assert '未設定' in settings_service.TESTERS['backup']({'offsite': 'none'})
    html = login_staff(app, 'offsite_owner', 'owner').get('/admin/settings/backup').get_data(as_text=True)
    assert '異地備份' in html and 'offsite_command' in html


# ---------------------------------------------------------------------------
# 一鍵更新
# ---------------------------------------------------------------------------
def test_update_service_version_compare_fetch_download_and_apply_guard(app, monkeypatch, tmp_path):
    from app.services import update_service as svc
    assert svc.is_newer('2026.11.01', '2026.10.31') and not svc.is_newer('2026.10.31', '2026.10.31')
    assert svc.is_newer('v2026.11.1', '2026.10.31') and not svc.is_newer('2026.9.1', '2026.10.31')
    assert svc.is_newer('v1', '未知') and not svc.is_newer('', '2026.10.31')
    with app.app_context():
        cur = svc.current_version()
        assert re.match(r'^\d{4}\.\d{1,2}\.\d{1,2}$', cur['version'])
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith('/releases/latest'):
            return type('R', (), {'status_code': 200, 'json': lambda self: {
                'tag_name': 'v2099.1.1', 'name': 'Big update', 'published_at': '2099-01-01T00:00:00Z', 'body': '- 新功能',
                'zipball_url': 'https://api.github.com/repos/x/y/zipball/v2099.1.1', 'html_url': 'https://github.com/x/y/releases/tag/v2099.1.1'}})()
        raise AssertionError(url)
    monkeypatch.setattr(svc.requests, 'get', fake_get)
    monkeypatch.setattr(svc, 'updates_dir', lambda: str(tmp_path))
    with app.app_context():
        info = svc.check(force=True)
        assert info['update_available'] is True and info['latest']['tag'] == 'v2099.1.1' and info['downloaded'] == ''
        svc.check()
        assert len(calls) == 1, '一小時內快取'
        # 下載：串流寫檔並驗證是 zip
        zip_path = tmp_path / 'src.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('repo-abc/VERSION', '2099.1.1\n')
        data = zip_path.read_bytes()

        class Stream:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, n):
                yield data
        monkeypatch.setattr(svc.requests, 'get', lambda url, **kw: Stream())
        target = svc.download()
        assert target == str(tmp_path / 'v2099.1.1.zip') and zipfile.is_zipfile(target)
        assert svc.check()['downloaded'] == target
        with pytest.raises(AppError):
            svc.download('v1.0')   # 只能下載最新版
        with pytest.raises(AppError):
            svc.download('../etc')
        if os.name != 'nt':
            with pytest.raises(AppError, match='Windows'):
                svc.apply(target)
    # 系統頁面板 (平台管理者才有)
    owner = login_staff(app, 'update_owner', 'owner')
    monkeypatch.setattr(svc, 'latest_release', lambda force=False: {'tag': 'v2099.1.1', 'name': 'Big', 'published_at': '2099-01-01',
                                                                     'notes': '- 新功能', 'zip_url': 'https://x', 'url': 'https://y'})
    html = owner.get('/admin/system').get_data(as_text=True)
    assert 'id="updatePanel"' in html and '最新 v2099.1.1' in html and '已下載 v2099.1.1.zip' in html
    assert ('套用並重啟' in html) == (os.name == 'nt')
    manager = login_staff(app, 'update_manager', 'manager')
    assert 'id="updatePanel"' not in manager.get('/admin/system').get_data(as_text=True)
    assert manager.post('/admin/system/update/download', data={'tag': 'v2099.1.1'}).status_code == 403
    assert os.path.exists(os.path.join(ROOT, 'deploy', 'windows', 'update_store.cmd'))
    with open(os.path.join(ROOT, 'deploy', 'windows', 'update_store.cmd'), encoding='utf-8') as fh:
        script = fh.read()
    assert 'db upgrade' in script and ':rollback' in script and 'robocopy' in script and 'taskkill' in script


# ---------------------------------------------------------------------------
# 指標與 Sentry
# ---------------------------------------------------------------------------
def test_metrics_endpoint_requires_token_and_reports_prometheus_text(app, monkeypatch):
    from app.services import observability
    c = app.test_client()
    monkeypatch.setitem(app.config, 'METRICS_TOKEN', '')
    assert c.get('/metrics').status_code == 404, '未設定 METRICS_TOKEN'
    monkeypatch.setitem(app.config, 'METRICS_TOKEN', 'secret-token')
    assert c.get('/metrics').status_code == 404 and c.get('/metrics?token=wrong').status_code == 404
    observability.reset()
    item = make_item(app, '指標餐', price=10)
    g = guest_client(app)
    checkout(g, [line(item)])
    g.get('/no-such-page-metrics')
    r = c.get('/metrics?token=secret-token')
    assert r.status_code == 200 and r.mimetype == 'text/plain'
    text = r.get_data(as_text=True)
    assert '# TYPE kiosk_http_requests_total counter' in text
    assert re.search(r'kiosk_http_requests_total\{class="2xx"\} [1-9]\d*', text)
    assert re.search(r'kiosk_http_requests_total\{class="4xx"\} [1-9]\d*', text)
    assert 'kiosk_http_request_seconds_bucket{le="+Inf"}' in text and 'kiosk_uptime_seconds' in text
    assert re.search(r'kiosk_orders_today [1-9]\d*', text) and 'kiosk_revenue_today' in text and 'kiosk_orders_pending' in text
    r = c.get('/metrics', headers={'Authorization': 'Bearer secret-token'})
    assert r.status_code == 200 and r.headers['Cache-Control'] == 'no-store'


def test_sentry_initialised_only_with_dsn_and_sdk(app, monkeypatch):
    import sys
    import types
    from app.services import observability
    inits = []
    fake = types.ModuleType('sentry_sdk')
    fake.init = lambda **kw: inits.append(kw)

    class _Scope:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_tag(self, *a):
            pass
    fake.configure_scope = lambda: _Scope()
    integ = types.ModuleType('sentry_sdk.integrations')
    flask_mod = types.ModuleType('sentry_sdk.integrations.flask')
    flask_mod.FlaskIntegration = lambda: 'flask-integration'
    monkeypatch.setitem(sys.modules, 'sentry_sdk', fake)
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations', integ)
    monkeypatch.setitem(sys.modules, 'sentry_sdk.integrations.flask', flask_mod)
    from flask import Flask
    dummy = Flask('dummy')
    dummy.config['SENTRY_DSN'] = ''
    assert observability._init_sentry(dummy) is False and inits == []
    dummy.config['SENTRY_DSN'] = 'https://key@o1.ingest.sentry.io/1'
    dummy.config['SYNC_ROLE'] = 'hub'
    assert observability._init_sentry(dummy) is True
    assert inits[0]['dsn'] == 'https://key@o1.ingest.sentry.io/1' and inits[0]['environment'] == 'hub'
    assert inits[0]['release'].startswith('kiosk@') and inits[0]['send_default_pii'] is False
    # 沒裝 sdk：略過不報錯
    monkeypatch.delitem(sys.modules, 'sentry_sdk')
    monkeypatch.setitem(sys.modules, 'sentry_sdk', None)
    assert observability._init_sentry(dummy) is False


# ---------------------------------------------------------------------------
# N+1 守門：資料量加倍，查詢數不變
# ---------------------------------------------------------------------------
def _hot_paths(app):
    g = guest_client(app)
    pos = login_staff(app, 'n1_pos', 'staff')
    kds = login_staff(app, 'n1_kds', 'kitchen')
    adm = login_staff(app, 'n1_adm', 'manager')
    g.get('/')
    pos.get('/pos')
    adm.get('/admin')
    out = {}
    for name, client, path in (('kiosk', g, '/'), ('availability', g, '/api/menu/availability'), ('my_orders', g, '/my_orders'),
                               ('pos', pos, '/pos'), ('today_orders', pos, '/api/pos/today_orders'),
                               ('kitchen', kds, '/api/kitchen_orders'), ('admin', adm, '/admin')):
        with count_queries(app) as q:
            assert client.get(path).status_code == 200, path
        out[name] = q['n']
    return out, g


def test_hot_pages_query_count_does_not_grow_with_data(app):
    for i in range(20):
        make_item(app, f'N+1 餐 {i}', price=10 + i, category=f'N+1 分類 {i % 4}')
    first, g = _hot_paths(app)
    for i in range(10):
        checkout(g, [line(make_item(app, f'N+1 訂單餐 {i}', price=5))])
    for i in range(20, 40):
        make_item(app, f'N+1 餐 {i}', price=10 + i, category=f'N+1 分類 {i % 4}')
    second, _g = _hot_paths(app)
    for name in first:
        assert second[name] <= first[name] + 2, f'{name}：資料加倍後查詢數 {first[name]} → {second[name]} (疑似 N+1)'
    limits = {'kiosk': 40, 'availability': 12, 'my_orders': 15, 'pos': 45, 'today_orders': 25, 'kitchen': 25, 'admin': 90}
    for name, n in second.items():
        assert n <= limits[name], f'{name} 一次請求用了 {n} 句 SQL (上限 {limits[name]})'


def test_dependabot_and_ci_lint_configured():
    with open(os.path.join(ROOT, '.github', 'dependabot.yml'), encoding='utf-8') as fh:
        cfg = fh.read()
    assert 'package-ecosystem: pip' in cfg and 'package-ecosystem: github-actions' in cfg and 'weekly' in cfg


# ---------------------------------------------------------------------------
# 小項：設定匯出 / 匯入、POS 常用備註、淺色模式與 PWA 安裝提示
# ---------------------------------------------------------------------------
def test_settings_export_and_import_round_trip(app, store_settings, monkeypatch):
    import json
    from app.services import settings_service
    store_settings('pos', service_charge_percent=12, quick_notes='少油, 不要蔥')
    store_settings('smtp', host='smtp.example.com', password='top-secret')
    owner = login_staff(app, 'settings_io_owner', 'owner')
    r = owner.get('/admin/settings/export.json')
    assert r.status_code == 200 and r.mimetype == 'application/json' and 'attachment' in r.headers['Content-Disposition']
    payload = r.get_json()
    assert payload['version'] == 1 and payload['sections']['pos']['service_charge_percent'] == 12
    assert 'password' not in payload['sections']['smtp'] and 'top-secret' not in r.get_data(as_text=True), '金鑰不匯出'
    assert 'backup' not in payload['sections'] and 'order_intake' not in payload['sections'], '本機專屬區塊不匯出'
    # 平台管理者可要求含金鑰
    assert owner.get('/admin/settings/export.json?secrets=1').get_json()['sections']['smtp']['password'] == 'top-secret'
    # 改掉再匯入：恢復；不認得的區塊 / 欄位 / 型別錯誤被略過；數值被夾在範圍內
    store_settings('pos', service_charge_percent=0)
    payload['sections']['pos']['service_charge_percent'] = 99
    payload['sections']['pos']['bogus_field'] = 1
    payload['sections']['nope'] = {'x': 1}
    payload['sections']['smtp'] = {'port': 'abc'}
    data = {'file': (__import__('io').BytesIO(json.dumps(payload, ensure_ascii=False).encode('utf-8')), 'settings.json')}
    r = owner.post('/admin/settings/import', data=data, content_type='multipart/form-data')
    assert r.status_code == 302
    with app.app_context():
        with app.test_request_context('/'):
            from flask import g
            from tests.support import default_tenant_id
            g.tenant_id = default_tenant_id(app)
            assert settings_service.get('pos')['service_charge_percent'] == 30, '夾在上限 30'
            assert settings_service.get('smtp')['password'] == 'top-secret', '匯入檔沒有的欄位不動'
    bad = {'file': (__import__('io').BytesIO(b'not json'), 'x.json')}
    assert owner.post('/admin/settings/import', data=bad, content_type='multipart/form-data').status_code == 302
    with app.app_context():
        with pytest.raises(AppError):
            settings_service.import_all({'sections': []})
        with pytest.raises(AppError):
            settings_service.import_all({'version': 99, 'sections': {}})
    html = owner.get('/admin/settings').get_data(as_text=True)
    assert 'id="settingsTransfer"' in html
    staff = login_staff(app, 'settings_io_staff', 'staff')
    assert staff.get('/admin/settings/export.json').status_code == 403


def test_pos_quick_notes_and_customer_theme_toggle(app, store_settings):
    store_settings('pos', quick_notes='少油, 少鹽, 分開裝')
    pos = login_staff(app, 'quick_note_pos', 'staff')
    html = pos.get('/pos').get_data(as_text=True)
    assert 'id="posQuickNotes"' in html and 'data-note="少鹽"' in html
    with open(os.path.join(ROOT, 'static', 'js', 'pos.js'), encoding='utf-8') as fh:
        assert 'quickNote' in fh.read()
    html = guest_client(app).get('/').get_data(as_text=True)
    assert 'id="themeToggle"' in html and 'data-action="toggle-theme"' in html and 'id="pwaInstallBtn"' in html
    with open(os.path.join(ROOT, 'static', 'js', 'common.js'), encoding='utf-8') as fh:
        js = fh.read()
    assert 'beforeinstallprompt' in js and "'kiosk-theme'" in js and 'data-bs-theme' in js
    with open(os.path.join(ROOT, 'static', 'css', 'customer.css'), encoding='utf-8') as fh:
        assert '[data-bs-theme="light"] body.customer-page' in fh.read()


# ---------------------------------------------------------------------------
# 安全審視 (收尾)：權限邊界與對外連線
# ---------------------------------------------------------------------------
def test_menu_permission_cannot_export_or_import_member_data(app):
    import io
    from app.models.tenant import StaffAccount
    staff = login_staff(app, 'sec_menu_staff', 'staff')
    with app.app_context():
        StaffAccount.query.filter_by(username='sec_menu_staff').first().permissions = 'menu'
        from app.extensions import db
        db.session.commit()
    # 匯出：勾了會員 / 訂單也只會得到菜單 (全部被濾掉 → 400)
    from tests.support import assert_flash
    assert staff.post('/admin/export_excel', data={'tables': ['User', 'Order']}).status_code == 302
    assert_flash(staff, '頁面權限')
    assert staff.post('/admin/export_excel', data={'tables': ['MenuItem']}).status_code == 200
    # 匯入：含會員工作表的 Excel 被拒
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'User'
    ws.append(['姓名(name)', '電話(phone)'])
    ws.append(['偷渡會員', '0977800199'])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    r = staff.post('/admin/import_smart', data={'file': (buf, 'members.xlsx')}, content_type='multipart/form-data')
    assert r.status_code == 302
    with app.app_context():
        from app.models.user import User
        assert User.query.filter_by(phone='0977800199').first() is None


def test_offsite_command_blocked_on_hub_and_private_urls(app, monkeypatch):
    from app.services import offsite_backup_service as svc
    from app.sync import runtime as sync_runtime
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    with app.app_context():
        with pytest.raises(AppError, match='線上端'):
            svc._run_command({'offsite_command': 'echo {file}'}, '/tmp/x.zip')
        with pytest.raises(AppError, match='內網'):
            svc._dav_base({'offsite_url': 'http://127.0.0.1:8080/dav'})
        with pytest.raises(AppError, match='內網'):
            svc._s3_parts({'offsite_url': 'http://10.0.0.5:9000', 'offsite_bucket': 'b'})


def test_settings_import_never_writes_secrets_and_non_owner_skips_secret_sections(app):
    from app.services import settings_service
    with app.app_context():
        with app.test_request_context('/'):
            from flask import g
            from tests.support import default_tenant_id
            g.tenant_id = default_tenant_id(app)
            before = settings_service.get('smtp')['password']
            applied, skipped = settings_service.import_all(
                {'version': 1, 'sections': {'smtp': {'password': 'evil', 'host': 'x.example.com'},
                                            'backup': {'offsite_command': 'rm -rf /'}, 'pos': {'service_charge_percent': 3}}},
                allow_secrets=False)
            assert 'smtp' in skipped and 'backup' in skipped and applied == 1
            assert settings_service.get('smtp')['password'] == before
            applied, _skipped = settings_service.import_all({'version': 1, 'sections': {'smtp': {'password': 'evil', 'host': 'y.example.com'}}},
                                                            allow_secrets=True)
            assert applied == 1 and settings_service.get('smtp')['password'] == before, '金鑰欄位一律不從匯入檔寫入'
            assert settings_service.get('smtp')['host'] == 'y.example.com'


def test_update_download_only_from_github_and_size_capped(app, monkeypatch, tmp_path):
    from app.services import update_service as svc
    monkeypatch.setattr(svc, 'updates_dir', lambda: str(tmp_path))
    monkeypatch.setattr(svc, 'latest_release', lambda force=False: {'tag': 'v2099.2.2', 'zip_url': 'https://evil.example.com/x.zip'})
    with app.app_context():
        with pytest.raises(AppError, match='GitHub'):
            svc.download()
        for bad in ('..', '.hidden', 'a/../b'):
            with pytest.raises(AppError):
                svc._safe_tag(bad)
        monkeypatch.setattr(svc, 'latest_release', lambda force=False: {'tag': 'v2099.2.2', 'zip_url': 'https://api.github.com/repos/x/y/zipball/v2099.2.2'})

        class Big:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, n):
                for _ in range(2):
                    yield b'0' * (200 * 1024 * 1024)
        monkeypatch.setattr(svc.requests, 'get', lambda url, **kw: Big())
        with pytest.raises(AppError, match='300 MB'):
            svc.download()
        assert not (tmp_path / 'v2099.2.2.zip').exists()


def test_receipt_email_per_order_cap(app, store_settings, monkeypatch):
    from app.services import mail_service
    store_settings('smtp', host='smtp.example.com', port=587, sender='shop@example.com')
    store_settings('receipt', email_enabled=True)
    monkeypatch.setattr(mail_service, 'send_mail', lambda *a, **k: 1)
    item = make_item(app, '收據上限餐', price=10)
    c = guest_client(app)
    oid = checkout(c, [line(item)]).get_json()['order_id']
    codes = [c.post(f'/api/my_orders/{oid}/receipt/email', json={'email': f'u{i}@example.com'}).status_code for i in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


# ---------------------------------------------------------------------------
# 修正：百分比優惠券留下小數 (例如 90 × 85% = 76.5) 時，點數上限是四捨五入後的應付金額，不會永遠剩 $1 折不掉
# ---------------------------------------------------------------------------
def test_points_can_cover_full_amount_after_percent_coupon(app):
    from app.extensions import db
    from app.models.coupon import Coupon
    from tests.support import default_tenant_id, make_member, member_client
    with app.app_context():
        if Coupon.query.filter_by(code='PCT85PTS').first() is None:
            db.session.add(Coupon(tenant_id=default_tenant_id(app), code='PCT85PTS', title='85 折點數測試', discount_type='percent',
                                  discount_value=85, min_spend=0, is_reward=False, member_only=False))
            db.session.commit()
    from app.services.reward_service import get_or_create_reward_setting
    with app.app_context():
        rs = get_or_create_reward_setting(default_tenant_id(app))
        before = (rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order)
        rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order = True, 1, 0
        db.session.commit()
    make_member(app, '點數折抵會員', '0977800171', pin='2468', points=5000)
    c = member_client(app, '0977800171', '2468')
    for price, expect_points in ((90, 77), (95, 81), (99, 84), (100, 85)):   # 76.5 → 77、80.75 → 81、84.15 → 84、85 → 85
        item = make_item(app, f'點數折抵餐 {price}', price=price)
        body = checkout(c, [line(item)], promo_code='PCT85PTS', use_points=5000).get_json()
        assert body['total_price'] == 0, (price, body['total_price'])
        assert body['points_used'] == expect_points, (price, body['points_used'])
        assert body['discount_amount'] == price
    # 只折一部分時剩下的照常四捨五入
    item = make_item(app, '點數折抵餐 部分', price=90)
    body = checkout(c, [line(item)], promo_code='PCT85PTS', use_points=70).get_json()
    assert body['points_used'] == 70 and body['total_price'] == 7   # 76.5 - 70 = 6.5 → 7
    for rel in ('customer/totals.js', 'pos/totals.js'):
        with open(os.path.join(ROOT, 'static', 'js', rel), encoding='utf-8') as fh:
            assert 'Math.floor(remaining + 0.5)' in fh.read(), rel
    with app.app_context():
        rs = get_or_create_reward_setting(default_tenant_id(app))
        rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order = before
        db.session.commit()
