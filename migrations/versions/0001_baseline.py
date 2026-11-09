"""baseline：重構前 (2026-10) 的資料表結構

全新資料庫從這個版本開始升級 (已存在的資料表不重建)。
重構前較早版本的資料庫可能少了後來才加的欄位 (例如 staff_account.deleted_at)：可空欄位在這裡補上，
必填欄位缺少時明確中止，不讓後續遷移在半途失敗或升級「成功」後執行時才出錯。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op

from app.services.baseline_schema import BASELINE

revision = '0001_baseline'
down_revision = None
branch_labels = None
depends_on = None


def _missing_columns(bind):
    """已存在的 baseline 資料表缺少的欄位：[(資料表, 欄位)]"""
    inspector = sa.inspect(bind)
    missing = []
    for table in BASELINE.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c['name'] for c in inspector.get_columns(table.name)}
        missing.extend((table, col) for col in table.columns if col.name not in existing)
    return missing


def upgrade():
    bind = op.get_bind()
    missing = _missing_columns(bind)
    required = [f'{t.name}.{c.name}' for t, c in missing if not c.nullable]
    if required:
        raise RuntimeError(
            '資料庫版本太舊，無法自動升級：缺少必要欄位 ' + '、'.join(required)
            + '。請先用重構前 (2026-10-22 以前) 的版本啟動一次讓它補齊欄位，或改用較新的資料庫備份還原')
    BASELINE.create_all(bind, checkfirst=True)
    # 可空欄位直接補上 (不帶外鍵；後續遷移需要的外鍵會在各自的步驟重建)
    for table, col in missing:
        op.add_column(table.name, sa.Column(col.name, col.type, nullable=True))


def downgrade():
    raise NotImplementedError('baseline 不支援降版')
