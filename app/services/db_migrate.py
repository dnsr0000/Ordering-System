"""資料庫結構升級 (Alembic / Flask-Migrate) 與預設資料"""
import logging

import sqlalchemy as sa
from flask_migrate import upgrade

from app.extensions import db
from app.services import bootstrap, legacy_upgrade

logger = logging.getLogger(__name__)

BASELINE_REVISION = '0001_baseline'
# 有這些資料表、卻沒有 alembic_version 的資料庫，是導入 Alembic 之前的舊版資料庫
LEGACY_MARKER_TABLES = {'user', 'menu_item', 'order', 'tenant', 'coupon'}


def upgrade_database():
    """升級到最新結構並確保預設資料存在。可重複執行 (已是最新版時不做任何事)。"""
    tables = set(sa.inspect(db.engine).get_table_names())
    if 'alembic_version' not in tables and tables & LEGACY_MARKER_TABLES:
        logger.info('偵測到舊版資料庫，先補齊到 baseline 結構再進行遷移')
        upgrade(revision=BASELINE_REVISION)
        with db.engine.begin() as conn:
            legacy_upgrade.patch_pre_alembic_schema(conn)
    upgrade()
    db.session.remove()
    bootstrap.ensure_defaults()
