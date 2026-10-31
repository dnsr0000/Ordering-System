import logging
import os
import posixpath
import re
import sqlite3

from flask import Flask, abort, request
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config, engine_options_for
from app.errors import json_error, register_error_handlers, wants_json
from app.extensions import csrf, db, limiter, login_manager, migrate


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """SQLite 連線設定：WAL (讀寫可同時進行)、等待鎖 30 秒、啟用外鍵檢查 (與 PostgreSQL 行為一致)"""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    for pragma in ("journal_mode=WAL", "busy_timeout=30000", "synchronous=NORMAL", "foreign_keys=ON"):
        cursor.execute(f"PRAGMA {pragma}")
    cursor.close()


# 頁面所需資源全部放在本機 static/ (離線也能使用)，因此內容安全政策只允許本站來源
CSP_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' data: blob:",
    "media-src 'self' data: blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
])


def _move_legacy_uploads(app):
    """
    舊版上傳檔放在 static/member、static/menu；第一次啟動新版時搬到設定的上傳資料夾 (已存在的檔案不覆蓋)。
    只在正式執行、且上傳資料夾位於專案內時搬移：測試或腳本使用暫存資料夾時絕不可搬走真實檔案
    (暫存資料夾結束時會被刪除)。
    """
    import shutil
    if app.testing:
        return
    base = os.path.abspath(app.config['BASE_DIR'])
    for legacy, key in (('member', 'UPLOAD_FOLDER_MEMBER'), ('menu', 'UPLOAD_FOLDER_MENU')):
        src = os.path.join(app.static_folder, legacy)
        dst = app.config[key]
        if not os.path.isdir(src) or os.path.abspath(src) == os.path.abspath(dst):
            continue
        if os.path.commonpath([base, os.path.abspath(dst)]) != base:
            logging.getLogger(__name__).warning('上傳資料夾 %s 不在專案內，未自動搬移 static/%s 的舊檔案', dst, legacy)
            continue
        moved = 0
        for root, _dirs, files in os.walk(src):
            for name in files:
                if name == '.gitkeep':
                    continue
                rel = os.path.relpath(os.path.join(root, name), src)
                target = os.path.join(dst, rel)
                if not os.path.exists(target):
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.move(os.path.join(root, name), target)
                    moved += 1
        if moved:
            logging.getLogger(__name__).info('已將 %d 個舊上傳檔從 static/%s 搬到 %s', moved, legacy, dst)


def create_app(config_class=Config):
    app = Flask(__name__,
                template_folder=os.path.join(config_class.BASE_DIR, 'templates'),
                static_folder=os.path.join(config_class.BASE_DIR, 'static'))
    app.config.from_object(config_class)
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = engine_options_for(app.config['SQLALCHEMY_DATABASE_URI'])
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')

    # 僅在明確設定代理層數時才信任 X-Forwarded-*，否則來源 IP 可被偽造而繞過頻率限制
    if app.config['TRUSTED_PROXY_COUNT'] > 0:
        hops = app.config['TRUSTED_PROXY_COUNT']
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    for folder in ('DATABASE_DIR', 'UPLOAD_FOLDER_MEMBER', 'UPLOAD_FOLDER_MENU', 'MODELS_DIR'):
        os.makedirs(app.config[folder], exist_ok=True)
    _move_legacy_uploads(app)

    @app.before_request
    def allow_large_backup_restore():
        """整庫還原的備份檔可能大於一般上傳上限 (需在 CSRF 檢查讀取表單之前設定)"""
        # 只對已登入的平台管理者放寬 (未登入者的大檔案在讀取表單前就被 413 擋下)
        from app.utils import is_platform_admin
        if request.endpoint == 'admin.restore_backup' and is_platform_admin():
            request.max_content_length = app.config['MAX_RESTORE_UPLOAD_SIZE']

    db.init_app(app)
    from app.services import points_service  # noqa: F401  (註冊同步變更追蹤與點數異動帳的 SQLAlchemy 事件)
    from app.services import history_service  # noqa: F401  (原料成本 / 售價變動歷史)
    migrate.init_app(app, db, directory=os.path.join(config_class.BASE_DIR, 'migrations'))
    csrf.init_app(app)
    limiter.init_app(app)
    _init_login(app)

    from app.services.event_bus import order_event_bus
    order_event_bus.init_app(app)
    from app.services import deferred, monitor_service
    deferred.init_app(app)
    monitor_service.install_log_handler(app)

    register_error_handlers(app)
    _register_security_hooks(app)

    @app.template_filter('flash_text')
    def flash_text(message):
        """提示訊息的圖示由 _flashes.html 依類別加上；AppError 等仍帶 ✅ ❌ 開頭的訊息先去掉，避免重複"""
        return re.sub(r'^(?:✅|❌|⚠️|ℹ️)\s*', '', str(message or ''))

    from app.tenant_context import install_tenant_context
    install_tenant_context(app)

    from app.routes import register_blueprints
    register_blueprints(app)

    from app.cli import register_cli
    register_cli(app)

    @app.before_request
    def daily_auto_close_stale_orders():
        if request.endpoint not in ('static', 'health.healthz', 'payment.ecpay_notify', 'payment.ecpay_result',
                                    'payment.ecpay_info') \
                and request.blueprint != 'sync_api':
            from app.services.order_service import ensure_daily_auto_close
            ensure_daily_auto_close()

    if app.config['AUTO_MIGRATE']:
        with app.app_context():
            from app.services.db_migrate import upgrade_database
            upgrade_database()
            from app.services.order_service import ensure_daily_auto_close
            ensure_daily_auto_close()

    if app.config['SYNC_ROLE'] not in ('standalone', 'hub', 'node'):
        raise RuntimeError(f"SYNC_ROLE 設定錯誤：{app.config['SYNC_ROLE']} (可用 standalone / hub / node)")
    if app.config['SYNC_ROLE'] == 'hub' and app.config.get('ONLINE_PAYMENT') and not app.config.get('ECPAY_STAGE') \
            and not all(app.config.get(k) for k in ('ECPAY_MERCHANT_ID', 'ECPAY_HASH_KEY', 'ECPAY_HASH_IV')):
        raise RuntimeError('ECPAY_STAGE=0 (正式收款) 必須同時設定 ECPAY_MERCHANT_ID、ECPAY_HASH_KEY、ECPAY_HASH_IV')
    if app.config['SYNC_ROLE'] == 'hub' and app.config.get('ONLINE_PAYMENT') and not app.testing:
        from app.services.payment_ecpay import test_keys_blocked
        with app.app_context():
            if test_keys_blocked():
                logging.getLogger(__name__).warning(
                    '綠界使用公開測試金鑰且未設定 ECPAY_ALLOW_TEST_KEYS=1：線上付款、桌邊線上結帳、訂位訂金的付款頁與通知一律拒絕 '
                    '(測試環境請設 ECPAY_ALLOW_TEST_KEYS=1；正式收款請設 ECPAY_STAGE=0 並填入正式特店資料)')
    if app.config['SYNC_ROLE'] == 'hub' and not app.testing and not app.config.get('SESSION_COOKIE_SECURE'):
        logging.getLogger(__name__).warning('線上端未設定 SESSION_COOKIE_SECURE=1：登入 Cookie 可能經由 http 明文傳送')
    hub_url = app.config.get('SYNC_HUB_URL', '')
    if app.config['SYNC_ROLE'] == 'node' and hub_url.startswith('http://') \
            and not hub_url.startswith(('http://127.0.0.1', 'http://localhost')):
        logging.getLogger(__name__).warning('SYNC_HUB_URL 不是 https：分店權杖與會員資料會以明文傳送，正式環境請使用 https')

    # 線上端：背景每分鐘取消逾時未付款的線上訂單 (CLI 指令與測試不啟動；多個 worker 同時執行也安全)
    if app.config['SYNC_ROLE'] == 'hub' and app.config.get('ONLINE_PAYMENT') and not app.testing and not _running_cli():
        from app.services.payment_ecpay import start_expiry_worker
        start_expiry_worker(app)

    # 背景排程：列印佇列、發票重送、每日備份 / 生日禮 / 報表、監控通知 (CLI 指令與測試不啟動)
    if not app.testing and not _running_cli():
        from app.services.maintenance_service import start_scheduler
        start_scheduler(app)

    # 分店：伺服器啟動後在背景與線上端同步 (CLI 指令與測試不啟動)
    if (app.config['SYNC_ROLE'] == 'node' and app.config['SYNC_AUTOSTART'] and not app.testing
            and app.config['SYNC_HUB_URL'] and app.config['SYNC_NODE_TOKEN'] and not _running_cli()):
        from app.sync.node import start_background_sync
        start_background_sync(app)
    return app


def _running_cli():
    """flask CLI 指令 (flask ... / python -m flask ...) 執行中：不啟動背景執行緒"""
    import click
    return click.get_current_context(silent=True) is not None


def _init_login(app):
    from app.models.tenant import StaffAccount

    login_manager.init_app(app)
    login_manager.login_view = 'admin.admin_dashboard'
    login_manager.login_message = None

    @login_manager.user_loader
    def load_staff(session_id):
        # get_id 為 "<id>:<版本>"：密碼或角色變更後版本不同，舊的工作階段自動登出
        staff_id, _, version = str(session_id).partition(':')
        if not staff_id.isdigit():
            return None
        staff = db.session.get(StaffAccount, int(staff_id))
        if staff is None or not staff.can_login or version != staff.session_version:
            return None
        from app.services import security_service
        if not security_service.session_is_valid(staff):   # 已被登出 (其他裝置操作) 或閒置逾時
            return None
        return staff

    @login_manager.unauthorized_handler
    def unauthorized():
        from flask import redirect, url_for
        if wants_json():
            return json_error('未授權操作，請先登入員工帳號！', 401)
        return redirect(url_for('admin.admin_dashboard'))


def _register_security_hooks(app):
    csp_mode = app.config['CSP_MODE']

    @app.after_request
    def add_security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        # 人臉註冊/登入需使用相機 (限本站)，其餘感測器一律禁用
        resp.headers.setdefault('Permissions-Policy', 'camera=(self), microphone=(), geolocation=()')
        if request.is_secure:
            resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
        if csp_mode != 'off' and resp.mimetype == 'text/html':
            header = 'Content-Security-Policy-Report-Only' if csp_mode == 'report-only' else 'Content-Security-Policy'
            resp.headers.setdefault(header, CSP_POLICY)
        return resp

    @app.before_request
    def block_legacy_member_folder():
        """舊版把會員照片放在 static/member；改由 /media/member 控管後，靜態路徑一律不提供 (含 /static/./Member 等變形)"""
        path = posixpath.normpath(request.path.replace('\\', '/')).lower()
        if path == '/static/member' or path.startswith('/static/member/'):
            abort(404)
