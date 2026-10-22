"""
pytest 共用設定。

- 測試資料庫：預設在暫存資料夾建立 SQLite；設定環境變數 TEST_DATABASE_URL 可改用 PostgreSQL，例如
    TEST_DATABASE_URL=postgresql://kiosk:kiosk@127.0.0.1/kiosk_test pytest
  (PostgreSQL 資料庫會在測試開始時清空重建)。
- 測試檔依檔案最下方 RUN_ORDER 的順序執行，檔案之間共用同一個資料庫 (情境式測試：前面建立的菜單、會員會被後面使用)。
- CSRF 預設關閉以減少樣板程式碼；專門驗證 CSRF 的測試以 csrf_enabled fixture 臨時開啟。
"""
import os
import shutil
import tempfile

import numpy as np
import pytest
from werkzeug.security import generate_password_hash

TMP_DIR = tempfile.mkdtemp(prefix='kiosk_test_')
DB_DIR = os.path.join(TMP_DIR, 'database')
MEMBER_DIR = os.path.join(TMP_DIR, 'static', 'member')
MENU_DIR = os.path.join(TMP_DIR, 'static', 'menu')
for d in (DB_DIR, MEMBER_DIR, MENU_DIR):
    os.makedirs(d)

# 不讀專案的 .env：開發者自己的設定 (例如 SYNC_ROLE=node、AUTO_MIGRATE=0) 不可影響測試
os.environ['KIOSK_SKIP_DOTENV'] = '1'
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
from app.config import Config, normalize_database_url  # noqa: E402  (需在設定環境變數之後匯入)

DATABASE_URL = normalize_database_url(os.environ.get('TEST_DATABASE_URL') or 'sqlite:///' + os.path.join(DB_DIR, 'menu.db'))
IS_SQLITE = DATABASE_URL.startswith('sqlite')
ADMIN_USERNAME, ADMIN_PASSWORD = 'admin_test', 'AdminPass!2026'


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    DATABASE_DIR = DB_DIR
    UPLOAD_FOLDER_MEMBER = MEMBER_DIR
    UPLOAD_FOLDER_MENU = MENU_DIR
    ADMIN_USERNAME = ADMIN_USERNAME
    ADMIN_PASSWORD = ADMIN_PASSWORD
    ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)
    ADMIN_CREDENTIALS_GENERATED = False
    RESET_ADMIN_PASSWORD_ON_START = False
    REDIS_URL = ''
    RATELIMIT_STORAGE_URI = 'memory://'
    RATELIMIT_ENABLED = True
    GEMINI_API_KEY = ''
    # 不載入本機語言模型 (載入 1GB 以上的模型會拖慢測試；AI 功能測試走規則引擎)
    LOCAL_MODEL_PATH = ''
    WTF_CSRF_ENABLED = False
    # 測試以 X-Forwarded-For 模擬不同來源 IP (等同前面有一層反向代理)
    TRUSTED_PROXY_COUNT = 1
    # 以下也固定住 (環境變數可能被設定成別的值)
    SECRET_KEY = 'test-secret-key'
    AUTO_MIGRATE = True
    CSP_MODE = 'enforce'
    SESSION_COOKIE_SECURE = False
    SYNC_ROLE = 'standalone'
    SYNC_HUB_URL = ''
    SYNC_NODE_TOKEN = ''
    SYNC_TENANT_SLUG = 'default'
    SYNC_AUTOSTART = False
    SYNC_INTERVAL = 10
    SYNC_LONG_POLL = 0
    SYNC_NODE_OFFLINE_AFTER = 120
    ONLINE_PAYMENT = True
    ECPAY_STAGE = True
    ECPAY_MERCHANT_ID = ''
    ECPAY_HASH_KEY = ''
    ECPAY_HASH_IV = ''
    PUBLIC_BASE_URL = ''
    PAYMENT_TIMEOUT_MINUTES = 15


def _reset_postgres(url):
    import sqlalchemy as sa
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.exec_driver_sql('DROP SCHEMA public CASCADE; CREATE SCHEMA public;')
    eng.dispose()


def deterministic_face(_path, _seed=[1]):
    """測試用人臉特徵：每次呼叫產生不同的單位向量 (不同照片 = 不同的人)"""
    vec = np.zeros(128, dtype=np.float32)
    vec[_seed[0] % 128] = 1.0
    _seed[0] += 1
    return vec


@pytest.fixture(scope='session')
def app():
    if not IS_SQLITE:
        _reset_postgres(DATABASE_URL)
    from app import create_app
    application = create_app(TestConfig)
    yield application
    shutil.rmtree(TMP_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _per_test_isolation(app, monkeypatch):
    """每個測試前清空頻率限制計數，並以確定性的假人臉特徵取代真實模型"""
    import app.routes.auth as auth_routes
    from app.extensions import limiter
    limiter.reset()
    limiter.enabled = True
    monkeypatch.setattr(auth_routes, 'extract_feature', deterministic_face)
    yield


@pytest.fixture
def csrf_enabled(app):
    app.config['WTF_CSRF_ENABLED'] = True
    yield
    app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture(scope='session')
def ctx():
    """模組之間共用的情境資料 (例如 menu_ids、POS 訂單編號)"""
    return {}


@pytest.fixture(scope='session')
def photo_member(app):
    """有照片、77 點、設定 PIN 的會員 (回傳 user id)"""
    from tests.support import PHOTO_MEMBER_PHONE, PHOTO_MEMBER_PIN, make_member
    return make_member(app, '照片會員', PHOTO_MEMBER_PHONE, points=77, pin=PHOTO_MEMBER_PIN, photo_path='m13.jpg')


@pytest.fixture
def reward_setting_snapshot(app):
    """測試結束後把預設店家的紅利設定還原 (回傳店家 id)"""
    from app.extensions import db
    from app.models.user import RewardSetting
    from tests.support import default_tenant_id
    tid = default_tenant_id(app)
    with app.app_context():
        rs = RewardSetting.query.filter_by(tenant_id=tid).first()
        snap = (rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point)
    yield tid
    with app.app_context():
        rs = RewardSetting.query.filter_by(tenant_id=tid).first()
        rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point = snap
        db.session.commit()


@pytest.fixture
def noon_clock(monkeypatch, request):
    """
    把「現在」固定在今天中午 12:00：營運分析、換日結案等以「今天」計算的測試，
    在午夜前後執行時，「幾分鐘前」的訂單才不會被算成昨天。
    固定 order_service、analytics_service 與測試模組本身的 date.today() / datetime.now()。
    """
    import datetime as dt
    frozen = dt.datetime.combine(dt.date.today(), dt.time(12, 0))

    class FrozenDate(dt.date):
        @classmethod
        def today(cls):
            return frozen.date()

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    import app.services.analytics_service as analytics_service
    import app.services.order_service as order_service
    for module in (order_service, analytics_service, request.module):
        if isinstance(getattr(module, 'date', None), type):
            monkeypatch.setattr(module, 'date', FrozenDate)
        if isinstance(getattr(module, 'datetime', None), type):
            monkeypatch.setattr(module, 'datetime', FrozenDateTime)
    return frozen


def _staff_client(app, username, role):
    from tests.support import login_staff
    return login_staff(app, username, role)


@pytest.fixture(scope='session')
def admin_client(app):
    from tests.support import login_staff
    return login_staff(app, ADMIN_USERNAME)


@pytest.fixture(scope='session')
def manager_client(app):
    return _staff_client(app, 'mgr_user', 'manager')


@pytest.fixture(scope='session')
def staff_client(app):
    return _staff_client(app, 'pos_user', 'staff')


@pytest.fixture(scope='session')
def kitchen_client(app):
    return _staff_client(app, 'kds_user', 'kitchen')


@pytest.fixture(scope='session')
def member_client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# 瀏覽器端對端測試 (pytest -m e2e)：以 Playwright 實際開啟頁面
# ---------------------------------------------------------------------------
@pytest.fixture(scope='session')
def live_url(app):
    import threading

    from werkzeug.serving import WSGIRequestHandler, make_server

    class Quiet(WSGIRequestHandler):
        def log_request(self, *args, **kwargs):
            pass

    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=Quiet)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()


@pytest.fixture(scope='session')
def browser():
    import glob

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:  # 環境內建的 Chromium 版本與 Playwright 不同時，改用找得到的執行檔
            root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/pw-browsers')
            candidates = sorted(glob.glob(os.path.join(root, 'chromium-*', 'chrome-linux*', 'chrome')))
            if not candidates:
                raise
            b = p.chromium.launch(executable_path=candidates[-1])
        yield b
        b.close()


@pytest.fixture
def page(browser, live_url):
    """每個測試一個乾淨的瀏覽器分頁；頁面上的 JS 錯誤與 CSP 違規會讓測試失敗"""
    context = browser.new_context(base_url=live_url, viewport={'width': 1366, 'height': 900})
    pg = context.new_page()
    problems = []
    pg.on('pageerror', lambda err: problems.append(f'JS 錯誤: {err}'))
    pg.on('console', lambda msg: problems.append(f'console.{msg.type}: {msg.text}')
          if msg.type == 'error' or 'Content Security Policy' in msg.text else None)
    pg.on('dialog', lambda dialog: dialog.accept())
    pg.problems = problems
    yield pg
    context.close()
    assert not problems, '\n'.join(problems)


# ---------------------------------------------------------------------------
# 執行順序
# 共用同一個測試資料庫的情境測試有先後依賴 (例如菜單測試建立的餐點、會員測試建立的會員、
# 優惠券測試建立的 OPEN20，會被 POS 與訂單狀態測試使用)，因此依下列順序執行，不依檔名排序。
# 不在清單中的新檔案排在 '*' 的位置；sync_invariants 檢查前面所有測試留下的資料，
# backup_restore 會整庫還原 (換掉資料庫)，所以兩者放最後。
# ---------------------------------------------------------------------------
RUN_ORDER = [
    'test_startup_defaults', 'test_legacy_database_upgrade', 'test_staff_login_and_roles', 'test_web_security',
    'test_menu_admin', 'test_member_accounts', 'test_member_face_and_photos', 'test_kiosk_checkout',
    'test_coupons_and_rewards', 'test_pos_checkout', 'test_order_status_and_cancel', 'test_reports_and_excel',
    'test_rate_limits_and_lockout', 'test_concurrency_stock_points', 'test_live_updates_sse', 'test_tenants_and_isolation',
    'test_pos_today_orders_api', 'test_table_qr_ordering', 'test_table_concurrency', 'test_deploy_config',
    'test_frontend_static_files',
    '*',
    'test_sync_invariants', 'test_backup_restore',
]


def pytest_collection_modifyitems(session, config, items):
    default = RUN_ORDER.index('*')
    rank = {name: i for i, name in enumerate(RUN_ORDER)}
    items.sort(key=lambda item: rank.get(item.module.__name__.rsplit('.', 1)[-1], default))


# 情境依賴：單獨執行某個測試檔 (例如 pytest tests/test_pos_checkout.py) 時，自動把它需要的前置測試檔一起加入，
# 前置檔會依 RUN_ORDER 先執行 (菜單測試建立餐點與套餐、會員測試建立陳小美、優惠券測試建立 OPEN20、POS 測試建立訂單)。
# 新增有依賴的測試檔時記得補在這裡。
NEEDS = {
    'test_kiosk_checkout': ['test_menu_admin'],
    'test_reports_and_excel': ['test_menu_admin'],
    'test_member_face_and_photos': ['test_member_accounts'],
    'test_coupons_and_rewards': ['test_menu_admin', 'test_member_accounts'],
    'test_pos_checkout': ['test_coupons_and_rewards'],
    'test_order_status_and_cancel': ['test_pos_checkout'],
}


def pytest_configure(config):
    here = os.path.dirname(os.path.abspath(__file__))
    given = {os.path.splitext(os.path.basename(arg.split('::')[0]))[0] for arg in config.args}
    added = []

    def require(name):
        for dep in NEEDS.get(name, ()):
            if dep not in given and dep not in added:
                require(dep)
                added.append(dep)

    for name in sorted(given):
        require(name)
    if added:
        config.args.extend(os.path.join(here, f'{name}.py') for name in added)
        config.kiosk_added_prerequisites = added


def pytest_report_header(config):
    added = getattr(config, 'kiosk_added_prerequisites', None)
    if added:
        return '已自動加入前置測試檔 (情境依賴)：' + '、'.join(f'{name}.py' for name in added)
