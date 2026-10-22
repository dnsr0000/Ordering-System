from flask import g, request, session
from flask_limiter import Limiter
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()
login_manager = LoginManager()


def rate_limit_key():
    """頻率限制以「店家 + 來源 IP」計數 (來源 IP 是否採信代理標頭由 TRUSTED_PROXY_COUNT 決定)"""
    tenant_id = getattr(g, 'tenant_id', None) or session.get('tenant_id') or 0
    return f"{tenant_id}:{request.remote_addr or '-'}"


def ip_key():
    """只以來源 IP 計數：員工登入、註冊新店家等不分店家的入口 (避免切換店家繞過限制)"""
    return f"ip:{request.remote_addr or '-'}"


# storage_uri 由 RATELIMIT_STORAGE_URI 設定：本機為 memory://，線上多行程時設為 Redis
limiter = Limiter(key_func=rate_limit_key)
