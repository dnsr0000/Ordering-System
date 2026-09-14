import os
from flask import Flask, request, jsonify
from sqlalchemy import event
from sqlalchemy.engine import Engine
from app.config import Config
from app.extensions import db

def create_app(config_class=Config):
    app = Flask(
        __name__, 
        template_folder=os.path.join(config_class.BASE_DIR, 'templates'),
        static_folder=os.path.join(config_class.BASE_DIR, 'static')
    )
    app.config.from_object(config_class)

    # 確保資料夾存在
    os.makedirs(config_class.DATABASE_DIR, exist_ok=True)
    os.makedirs(config_class.UPLOAD_FOLDER_MEMBER, exist_ok=True)
    os.makedirs(config_class.UPLOAD_FOLDER_MENU, exist_ok=True)

    # 初始化資料庫
    db.init_app(app)

    # 監聽底層連線建立事件，啟用 WAL 模式與 PRAGMA
    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA cache_size=-64000;")
        cursor.close()

    # 註冊 Blueprints
    from .routes.customer import customer_bp
    from .routes.auth import auth_bp
    from .routes.rewards import rewards_bp
    from .routes.kitchen import kitchen_bp
    from .routes.pickup import pickup_bp
    from .routes.admin import admin_bp

    app.register_blueprint(customer_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(rewards_bp)
    app.register_blueprint(kitchen_bp)
    app.register_blueprint(pickup_bp)
    app.register_blueprint(admin_bp)

    # 全域 413 上傳過大例外攔截
    @app.errorhandler(413)
    def request_entity_too_large(error):
        if request.is_json or request.path.startswith('/api/'):
            return jsonify({'success': False, 'message': '上傳內容超出系統限制 (上限 50MB)！'}), 413
        return "<script>alert('❌ 上傳檔案過大，超出伺服器處理限制 (最大 50MB)！'); window.history.back();</script>", 413
    
    # 初始化資料庫資料表
    with app.app_context():
        db.create_all()

    return app