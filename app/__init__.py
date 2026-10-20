import os
import posixpath
import secrets
from flask import Flask, request, jsonify, session, abort
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix
from app.config import Config
from app.extensions import db
from app.utils import js_alert_redirect


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """每條 SQLite 連線建立時套用 WAL 等 PRAGMA (模組載入時只註冊一次，create_app 重複呼叫也不會重複註冊)"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout=30000;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA cache_size=-64000;")
    cursor.close()


# 內容安全政策：本系統頁面使用內嵌 script/style，且只向 jsDelivr (Bootstrap/Icons/Cropper)、Google Fonts、Unsplash (背景圖) 取資源
_CSP_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
    "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com",
    "img-src 'self' data: blob: https://images.unsplash.com",
    "media-src 'self' data: blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
])


def create_app(config_class=Config):
    app = Flask(
        __name__,
        template_folder=os.path.join(config_class.BASE_DIR, 'templates'),
        static_folder=os.path.join(config_class.BASE_DIR, 'static')
    )
    app.config.from_object(config_class)

    # 僅在明確設定 TRUSTED_PROXY_COUNT (>0) 時，才信任反向代理 (Nginx / Cloudflare) 傳入的
    # X-Forwarded-* 標頭；預設 0 = 以實際連線位址作為客戶端 IP，避免來源 IP 被偽造而繞過限流。
    proxy_hops = int(getattr(config_class, 'TRUSTED_PROXY_COUNT', 0) or 0)
    if proxy_hops > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops, x_host=proxy_hops)

    os.makedirs(config_class.DATABASE_DIR, exist_ok=True)
    os.makedirs(config_class.UPLOAD_FOLDER_MEMBER, exist_ok=True)
    os.makedirs(config_class.UPLOAD_FOLDER_MENU, exist_ok=True)
    os.makedirs(config_class.MODELS_DIR, exist_ok=True)

    db.init_app(app)

    csp_mode = str(getattr(config_class, 'CSP_MODE', 'enforce') or 'enforce').lower()

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
            resp.headers.setdefault(header, _CSP_POLICY)
        return resp

    # CSRF Token 生成與驗證
    def generate_csrf_token():
        tok = session.get('_csrf_token') or session.get('csrf_token')
        if not tok:
            tok = secrets.token_hex(24)
        session['_csrf_token'] = tok
        session['csrf_token'] = tok
        return tok

    app.jinja_env.globals['csrf_token'] = generate_csrf_token

    @app.before_request
    def security_guard_before_request():
        # 1. 保護會員人臉照片 (/static/member/*)，僅允許已登入的「同一家店」員工讀取。
        #    路徑先正規化再比對：/static/./member/x、/static/Member/x (Windows 檔案系統不分大小寫)
        #    都會被 Flask 的靜態檔路由當成同一個檔案，不能只用原始字串 startswith 判斷。
        norm_path = posixpath.normpath(request.path.replace('\\', '/')).lower()
        if norm_path == '/static/member' or norm_path.startswith('/static/member/'):
            if not session.get('admin_logged_in') or not session.get('staff_account_id'):
                abort(403)
            photo_name = norm_path[len('/static/member/'):]
            if photo_name:
                from app.models.user import User
                owner = User.query.execution_options(skip_tenant_filter=True).filter(
                    db.func.lower(User.photo_path) == photo_name
                ).first()
                # 照片屬於別家店的會員：即使是已登入的員工也不可讀取
                if owner is not None and owner.tenant_id != session.get('tenant_id'):
                    abort(403)
            return None

        # 2. 全域狀態變更請求 (POST/PUT/DELETE/PATCH) 強制驗證 CSRF Token
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            expected = session.get('_csrf_token') or session.get('csrf_token')
            provided = (
                request.headers.get('X-CSRFToken')
                or request.headers.get('X-CSRF-Token')
                or request.form.get('csrf_token')
            )
            if not provided and request.is_json:
                body = request.get_json(silent=True) or {}
                if isinstance(body, dict):
                    provided = body.get('csrf_token')

            if not expected or not provided or not secrets.compare_digest(str(expected), str(provided)):
                msg = '安全驗證失敗 (CSRF Token 無效或已過期)，請重新整理頁面後再試！'
                if request.is_json or request.path.startswith('/api/') or request.path in ('/phone_login', '/submit_order'):
                    return jsonify({'success': False, 'error': msg, 'message': msg}), 403
                return js_alert_redirect(f'❌ {msg}', status=403)
        return None

    from .routes.customer import customer_bp
    from .routes.auth import auth_bp
    from .routes.rewards import rewards_bp
    from .routes.kitchen import kitchen_bp
    from .routes.pickup import pickup_bp
    from .routes.admin import admin_bp
    from .routes.pos import pos_bp
    from .routes.orders import orders_bp

    app.register_blueprint(customer_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(rewards_bp)
    app.register_blueprint(kitchen_bp)
    app.register_blueprint(pickup_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(pos_bp)
    app.register_blueprint(orders_bp)

    from app.tenant_context import install_tenant_context
    install_tenant_context(app)

    # 換日自動結案：跨日後的第一個請求會把前一日(含更早)還在「製作中」的訂單改為「已出餐」
    @app.before_request
    def daily_auto_close_stale_orders():
        if request.endpoint == 'static':
            return None
        from app.services.order_service import ensure_daily_auto_close
        ensure_daily_auto_close()
        return None

    @app.errorhandler(413)
    def request_entity_too_large(error):
        if request.is_json or request.path.startswith('/api/'):
            return jsonify({'success': False, 'message': '上傳內容超出系統限制 (上限 50MB)！'}), 413
        return js_alert_redirect('❌ 上傳檔案過大，超出伺服器處理限制 (最大 50MB)！', status=413)

    with app.app_context():
        from app.models.tenant import Tenant, StaffAccount
        from app.models.user import User, AdminLog, StaffAuditLog, CustomerLog, RewardSetting, RewardRedemption
        db.create_all()
        from app.services.migration_service import run_database_migrations
        run_database_migrations()
        # 啟動時先結一次 (停機期間跨日的訂單)；失敗只記錄，不影響啟動
        from app.services.order_service import ensure_daily_auto_close
        ensure_daily_auto_close()

    return app