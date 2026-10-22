"""線上端部署 (健康檢查 /healthz、SSE 反向代理標頭、部署設定檔)"""
import os
import re
import runpy

import app.routes.health as health_routes
from app.extensions import limiter
from app.services.event_bus import order_event_bus

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def test_healthz_ok_without_login(app):
    c = app.test_client()
    r = c.get('/healthz')
    assert r.status_code == 200 and r.mimetype == 'application/json'
    assert r.get_json() == {'status': 'ok', 'database': 'ok'}
    assert r.headers['Cache-Control'] == 'no-store'
    assert c.head('/healthz').status_code == 200


def test_healthz_not_rate_limited(app):
    """容器與監控會頻繁呼叫，不可被頻率限制擋下 (429)"""
    assert limiter.enabled
    c = app.test_client()
    codes = {c.get('/healthz', headers={'X-Forwarded-For': '10.19.0.1'}).status_code for _ in range(120)}
    assert codes == {200}


def test_healthz_reports_503_when_database_fails(app, monkeypatch):
    """資料庫查詢失敗時回 503 (不是 500)，且交易已回滾、之後的請求不受影響"""
    from sqlalchemy import text
    monkeypatch.setattr(health_routes, 'text', lambda _sql: text('SELECT * FROM __healthz_missing_table__'))
    r = app.test_client().get('/healthz')
    assert r.status_code == 503
    assert r.get_json() == {'status': 'error', 'database': 'unavailable'}
    monkeypatch.undo()
    assert app.test_client().get('/healthz').status_code == 200


def test_healthz_csrf_free(app, csrf_enabled):
    assert app.test_client().get('/healthz').status_code == 200


def test_sse_headers_for_reverse_proxy(app):
    """SSE 回應需告知反向代理不要緩衝 / 快取 (Nginx: X-Accel-Buffering；Caddy 會依 text/event-stream 立即送出)"""
    with order_event_bus._lock:
        order_event_bus._subscribers.clear()
    c = app.test_client()
    c.get('/?shop=default')
    resp = c.get('/api/orders_stream', buffered=False)
    try:
        assert resp.status_code == 200 and resp.mimetype == 'text/event-stream'
        assert resp.headers['X-Accel-Buffering'] == 'no'
        assert 'no-cache' in resp.headers['Cache-Control']
    finally:
        resp.close()


# ---------------------------------------------------------------------------
# 部署設定檔的基本檢查 (容器實際建置與啟動見 DEPLOY.txt)
# ---------------------------------------------------------------------------
def test_gunicorn_conf(monkeypatch):
    for name in ('GUNICORN_WORKERS', 'GUNICORN_THREADS', 'GUNICORN_TIMEOUT', 'GUNICORN_BIND', 'PORT'):
        monkeypatch.delenv(name, raising=False)
    conf = runpy.run_path(os.path.join(ROOT, 'deploy', 'gunicorn.conf.py'))
    assert conf['worker_class'] == 'gthread'
    # 每個行程的 SSE 上限 85 條 (公開 25 + 員工 60)，執行緒必須明顯更多
    assert conf['threads'] >= 100 and conf['workers'] >= 1
    assert conf['bind'] == '0.0.0.0:8000'
    assert conf['accesslog'] == '-' and conf['errorlog'] == '-'
    assert conf['preload_app'] is False

    monkeypatch.setenv('GUNICORN_WORKERS', '4')
    monkeypatch.setenv('GUNICORN_THREADS', '200')
    monkeypatch.setenv('PORT', '9000')
    conf = runpy.run_path(os.path.join(ROOT, 'deploy', 'gunicorn.conf.py'))
    assert (conf['workers'], conf['threads'], conf['bind']) == (4, 200, '0.0.0.0:9000')


def test_deploy_files():
    requirements = _read('requirements.txt')
    assert re.search(r'^opencv-python-headless==', requirements, re.M)
    assert not re.search(r'^opencv[-_]python==', requirements, re.M)
    assert re.search(r'^gunicorn==[\d.]+; sys_platform != "win32"$', requirements, re.M)

    dockerfile = _read('Dockerfile')
    assert 'TZ=Asia/Taipei' in dockerfile and 'tzdata' in dockerfile
    assert re.search(r'^USER (?!root)\w+', dockerfile, re.M)
    assert 'HEALTHCHECK' in dockerfile and '/healthz' in dockerfile

    entrypoint = _read('deploy/entrypoint.sh')
    assert '\r' not in entrypoint
    assert 'flask --app run setup' in entrypoint and 'exec gunicorn' in entrypoint
    assert 'AUTO_MIGRATE=0' in entrypoint
    if os.name != 'nt':
        assert os.access(os.path.join(ROOT, 'deploy', 'entrypoint.sh'), os.X_OK)

    compose = _read('docker-compose.yml')
    for needle in ('AUTO_MIGRATE: "0"', 'TRUSTED_PROXY_COUNT: "1"', 'SESSION_COOKIE_SECURE: "1"', 'SYNC_ROLE: hub',
                   'REDIS_URL: redis://redis:6379/0', '/app/database', '/app/uploads', 'service_healthy'):
        assert needle in compose, needle

    dockerignore = _read('.dockerignore').splitlines()
    for pattern in ('.env', 'venv', '.venv', 'database/*', 'uploads', 'static/member', 'static/menu'):
        assert pattern in dockerignore, pattern

