"""第四輪 (負載測試)：order.pickup_at 加索引 —— POS / 廚房 / 取餐看板的「今天的訂單」條件
(今天建立 OR 預約今天取餐) 才能用索引，不必掃描整個訂單表

Revision ID: 0023_order_pickup_at_index
Revises: 0022_group_orders
Create Date: 2026-10-26 15:55:42.888430

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0023_order_pickup_at_index'
down_revision = '0022_group_orders'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_order_pickup_at'), ['pickup_at'], unique=False)


def downgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_order_pickup_at'))
