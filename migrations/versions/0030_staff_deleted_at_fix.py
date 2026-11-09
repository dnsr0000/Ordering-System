"""補救：已升級到最新版、卻缺少 staff_account.deleted_at 的資料庫

0001_baseline 修正前，重構前較早版本的資料庫 (沒有 staff_account.deleted_at) 升級時不會補上這個欄位，
升到最新版後員工查詢就會失敗、系統無法啟動。修正後的 0001_baseline 已會替新升級的舊資料庫補上，
這裡只處理在修正前就已經升到最新版的資料庫：欄位不存在才新增 (可空，與模型一致)。

Revision ID: 0030_staff_deleted_at_fix
Revises: 0029_support_chat
Create Date: 2026-11-09 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0030_staff_deleted_at_fix'
down_revision = '0029_support_chat'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    cols = {c['name'] for c in sa.inspect(bind).get_columns('staff_account')}
    if 'deleted_at' not in cols:
        op.add_column('staff_account', sa.Column('deleted_at', sa.DateTime(), nullable=True))


def downgrade():
    # 正常的資料庫本來就有這個欄位 (baseline 建立)，不能刪
    pass
