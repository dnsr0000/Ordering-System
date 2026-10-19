import os
import secrets
from flask import Flask, request, jsonify, session, abort
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix
from app.config import Config
from app.extensions import db
from app.utils import js_alert_redirect


def create_app(config_class=Config):
    app = Flask(
        __name__,
        template_folder=os.path.join(config_class.BASE_DIR, 'templates'),
        static_folder=os.path.join(config_class.BASE_DIR, 'static')
    )
    app.config.from_object(config_class)

    # 支援反向代理 (Nginx / Cloudflare) 後方的真實客戶端 IP 解析
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    os.makedirs(config_class.DATABASE_DIR, exist_ok=True)
    os.makedirs(config_class.UPLOAD_FOLDER_MEMBER, exist_ok=True)
    os.makedirs(config_class.UPLOAD_FOLDER_MENU, exist_ok=True)
    os.makedirs(config_class.MODELS_DIR, exist_ok=True)

    db.init_app(app)

    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA cache_size=-64000;")
        cursor.close()

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
        # 1. 保護會員人臉照片 (/static/member/*)，僅允許已登入的店家員工讀取
        if request.path.startswith('/static/member/'):
            if not session.get('admin_logged_in') or not session.get('staff_account_id'):
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

    return app
