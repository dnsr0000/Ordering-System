"""資料庫結構升級 (Alembic / Flask-Migrate) 與預設資料"""
from flask_migrate import upgrade

from app.extensions import db
from app.services import bootstrap


def upgrade_database():
    """升級到最新結構並確保預設資料存在。可重複執行 (已是最新版時不做任何事)。"""
    upgrade()
    db.session.remove()
    bootstrap.ensure_defaults()
