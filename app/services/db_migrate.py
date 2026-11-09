"""資料庫結構升級 (Alembic / Flask-Migrate) 與預設資料"""
from alembic import command
from flask import current_app

from app.extensions import db
from app.services import bootstrap


def upgrade_database():
    """升級到最新結構並確保預設資料存在。可重複執行 (已是最新版時不做任何事)。

    直接呼叫 alembic (不用 flask_migrate.upgrade)：後者遇錯只寫一行 log 就 sys.exit(1)，
    SystemExit 不是 Exception，備份還原後在執行緒中升級失敗會被吞掉而誤報成功。改為讓例外正常拋出。
    """
    command.upgrade(current_app.extensions['migrate'].migrate.get_config(), 'head')
    db.session.remove()
    bootstrap.ensure_defaults()
