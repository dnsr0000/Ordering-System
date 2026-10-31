"""資安審查第二輪：訂單的 client_order_id 改存獨立欄位 (前端去重識別)，uuid 一律由伺服器產生

舊資料：既有訂單的 uuid 就是當時前端送的 client_order_id，補到新欄位讓重送仍能去重。

Revision ID: 0024_order_client_order_id
Revises: 0023_order_pickup_at_index
Create Date: 2026-10-30 21:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0024_order_client_order_id'
down_revision = '0023_order_pickup_at_index'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_order_id', sa.String(length=36), nullable=True))
        batch_op.create_index('ix_order_tenant_client_order_id', ['tenant_id', 'client_order_id'], unique=True)
    op.execute('UPDATE "order" SET client_order_id = uuid WHERE client_order_id IS NULL')


def downgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_index('ix_order_tenant_client_order_id')
        batch_op.drop_column('client_order_id')
