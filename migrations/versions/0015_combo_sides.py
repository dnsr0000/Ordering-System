"""套餐訂單記錄賣出當下的配餐 (order_item.combo_sides)：取消回補與分店扣庫存不受之後修改 / 刪除套餐影響

Revision ID: 0015_combo_sides
Revises: 0014_home_delivery
Create Date: 2026-10-24 17:15:57.318237

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0015_combo_sides'
down_revision = '0014_home_delivery'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('order_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('combo_sides', sa.Text(), server_default='', nullable=False))


def downgrade():
    with op.batch_alter_table('order_item', schema=None) as batch_op:
        batch_op.drop_column('combo_sides')
