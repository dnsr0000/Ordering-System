"""
記錄檔輪替與請求識別碼 (log_service)、靜態資源版本化 (static_version)、CI 靜態檢查設定。
"""
import logging
import os
import re
from logging.handlers import RotatingFileHandler

from flask import Flask, url_for

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 請求識別碼
# ---------------------------------------------------------------------------
def test_every_response_carries_request_id(app):
    c = app.test_client()
    r = c.get('/healthz')
    rid = r.headers.get('X-Request-ID')
    assert rid and re.match(r'^[0-9a-f]{12}$', rid)
    assert c.get('/healthz').headers['X-Request-ID'] != rid, '每個請求都不同'
    # 反向代理送來的識別碼沿用；不合法的 (太短 / 含特殊字元) 忽略
    assert c.get('/healthz', headers={'X-Request-ID': 'proxy-abc-123'}).headers['X-Request-ID'] == 'proxy-abc-123'
    assert c.get('/healthz', headers={'X-Request-ID': 'x'}).headers['X-Request-ID'] != 'x'
    assert c.get('/healthz', headers={'X-Request-ID': 'a b<script>'}).headers['X-Request-ID'] != 'a b<script>'


def test_unexpected_error_shows_request_id_on_page_and_json(app, monkeypatch, caplog):
    def boom():
        raise RuntimeError('故意出錯')
    monkeypatch.setitem(app.view_functions, 'public.offline', boom)
    c = app.test_client()
    with caplog.at_level(logging.ERROR):
        r = c.get('/offline')
    assert r.status_code == 500
    rid = r.headers['X-Request-ID']
    html = r.get_data(as_text=True)
    assert 'errorRequestId' in html and rid in html and '故意出錯' not in html
    r = c.get('/offline', headers={'Accept': 'application/json'})
    assert r.status_code == 500 and r.get_json()['request_id'] == r.headers['X-Request-ID']
    assert '未預期的錯誤' in caplog.text


def test_log_records_include_request_id_filter(app):
    from app.services.log_service import RequestIdFilter
    record = logging.LogRecord('x', logging.INFO, __file__, 1, 'msg', (), None)
    RequestIdFilter().filter(record)
    assert record.request_id == '-', '請求之外為 -'
    with app.test_request_context('/'):
        from flask import g
        g.request_id = 'rid-test'
        RequestIdFilter().filter(record)
        assert record.request_id == 'rid-test'


# ---------------------------------------------------------------------------
# 記錄檔輪替
# ---------------------------------------------------------------------------
def test_rotating_file_handler_configured_from_log_file(tmp_path):
    from app.services import log_service
    log_file = tmp_path / 'logs' / 'app.log'
    other = Flask('log_test')
    other.config.update(LOG_FILE=str(log_file), LOG_MAX_BYTES=2 * 1024 * 1024, LOG_BACKUP_COUNT=3, TESTING=False)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        log_service.configure_logging(other)
        handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler) and getattr(h, '_kiosk_file', False)]
        assert len(handlers) == 1
        h = handlers[0]
        assert (h.maxBytes, h.backupCount) == (2 * 1024 * 1024, 3) and os.path.abspath(h.baseFilename) == str(log_file)
        logging.getLogger('kiosk.test').info('輪替記錄測試 %s', 'OK')
        h.flush()
        text = log_file.read_text(encoding='utf-8')
        assert '輪替記錄測試 OK' in text and '[-]' in text, '背景 / 無請求時識別碼為 -'
        console = [c for c in root.handlers if getattr(c, '_kiosk_console', False)]
        assert console and console[0].level == logging.WARNING, '有檔案記錄時主控台只留警告以上'
        # 再呼叫一次 (gunicorn 多 worker / 測試重建 app) 不會重複加 handler
        log_service.configure_logging(other)
        assert len([x for x in root.handlers if getattr(x, '_kiosk_file', False)]) == 1
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        for h in before:
            if h not in root.handlers:
                root.addHandler(h)
            if getattr(h, '_kiosk_console', False):
                h.setLevel(logging.INFO)


def test_testing_app_does_not_write_log_file(app):
    """測試設定 (TESTING) 不寫記錄檔；LOG_FILE 空字串也不寫 (Docker 交給 json-file)"""
    root = logging.getLogger()
    assert not any(getattr(h, '_kiosk_file', False) for h in root.handlers)
    from app.config import Config
    assert Config.LOG_FILE.endswith(os.path.join('logs', 'app.log'))
    compose = _read('docker-compose.yml')
    assert 'LOG_FILE: ""' in compose and 'max-size' in compose
    cmd = _read('deploy/windows/start_store.cmd')
    assert 'server.log.1' in cmd, '門市主控台輸出超過 5 MB 時換檔'
    assert 'basicConfig' not in _read('serve_windows.py')


# ---------------------------------------------------------------------------
# 靜態資源版本化
# ---------------------------------------------------------------------------
def test_static_urls_are_versioned_and_served(app):
    with app.test_request_context('/'):
        url = url_for('static', filename='js/common.js')
        assert re.search(r'/static/js/common\.js\?v=[0-9a-f]{8}$', url), url
        assert url_for('static', filename='js/common.js') == url, '同一檔案同一版本'
        assert url_for('static', filename='js/common.js', v='x') .endswith('?v=x'), '明確指定時不覆蓋'
        missing = url_for('static', filename='js/no-such-file.js')
        assert '?v=' not in missing, '檔案不存在時不加參數'
    c = app.test_client()
    assert c.get(url).status_code == 200
    page = c.get('/offline').get_data(as_text=True)
    assert re.search(r'/static/js/common\.js\?v=[0-9a-f]{8}', page), '頁面裡的 script / link 都帶版本'


def test_version_changes_when_file_changes(app, tmp_path, monkeypatch):
    from app.services import static_version
    static_dir = tmp_path / 'static'
    static_dir.mkdir()
    target = static_dir / 'v.css'
    target.write_text('a{}', encoding='utf-8')
    monkeypatch.setattr(app, 'static_folder', str(static_dir))
    with app.test_request_context('/'):
        v1 = static_version.version_of('v.css')
        target.write_text('b{}', encoding='utf-8')
        os.utime(target, (1, 1))   # 強制不同的修改時間
        v2 = static_version.version_of('v.css')
    assert v1 and v2 and v1 != v2


def test_service_worker_cache_name_follows_static_digest(app):
    from app.services import static_version
    c = app.test_client()
    body = c.get('/sw.js').get_data(as_text=True)
    with app.test_request_context('/'):
        digest = static_version.bundle_digest()
    assert f"'kiosk-static-{digest}'" in body and "'kiosk-static-v2'" not in body
    assert c.get('/sw.js').headers['Cache-Control'] == 'no-cache'


# ---------------------------------------------------------------------------
# CI：靜態檢查 / 依賴稽核 / 涵蓋率
# ---------------------------------------------------------------------------
def test_ci_workflow_has_lint_audit_and_coverage():
    wf = _read('.github/workflows/tests.yml')
    assert 'ruff check .' in wf and 'pip-audit -r requirements.txt' in wf
    assert '--cov=app' in wf and '--cov-fail-under=' in wf
    pyproject = _read('pyproject.toml')
    assert '[tool.ruff]' in pyproject and '[tool.mypy]' in pyproject and '[tool.pytest.ini_options]' in pyproject
    assert '- run: mypy' in wf
    assert 'pytest-cov' in _read('requirements-dev.txt') and 'pip install -r requirements-dev.txt' in wf


def test_ruff_passes_on_repository():
    import shutil
    import subprocess
    import sys
    ruff = shutil.which('ruff') or os.path.join(os.path.dirname(sys.executable), 'ruff')
    if not os.path.exists(ruff):
        import pytest
        pytest.skip('未安裝 ruff (pip install ruff)')
    proc = subprocess.run([ruff, 'check', '.', '--output-format', 'concise'], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
