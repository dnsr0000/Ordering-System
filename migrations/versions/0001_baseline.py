"""baseline：重構前 (2026-10) 的資料表結構

已存在的資料表會略過，因此全新資料庫與「導入 Alembic 之前的舊資料庫」都從這個版本開始升級
(舊資料庫會先由 app.services.legacy_upgrade 整理成與此一致的結構)。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-22
"""
from alembic import op

from app.services.baseline_schema import BASELINE

revision = '0001_baseline'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    BASELINE.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    raise NotImplementedError('baseline 不支援降版')
