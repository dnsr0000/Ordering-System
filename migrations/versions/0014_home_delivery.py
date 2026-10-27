"""自有外送 (外送到府)：訂單的外送地址 / 電話 / 配送區域 / 外送費 / 外送員 / 配送狀態與時間

Revision ID: 0014_home_delivery
Revises: 0013_purchasing_lots_labor
Create Date: 2026-10-24 01:32:54.452241

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0014_home_delivery'
down_revision = '0013_purchasing_lots_labor'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('delivery_fee', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('delivery_address', sa.String(length=200), server_default='', nullable=False))
        batch_op.add_column(sa.Column('delivery_phone', sa.String(length=30), server_default='', nullable=False))
        batch_op.add_column(sa.Column('delivery_zone', sa.String(length=30), server_default='', nullable=False))
        batch_op.add_column(sa.Column('driver_name', sa.String(length=60), server_default='', nullable=False))
        batch_op.add_column(sa.Column('delivery_status', sa.String(length=12), server_default='', nullable=False))
        batch_op.add_column(sa.Column('dispatched_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('delivered_at', sa.DateTime(), nullable=True))



def downgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_column('delivered_at')
        batch_op.drop_column('dispatched_at')
        batch_op.drop_column('delivery_status')
        batch_op.drop_column('driver_name')
        batch_op.drop_column('delivery_zone')
        batch_op.drop_column('delivery_phone')
        batch_op.drop_column('delivery_address')
        batch_op.drop_column('delivery_fee')

