"""門市桌位與手機掃碼點餐

- dining_table：每家門市的桌位，token 為桌上 QR code 網址 (/t/<token>) 的代碼 (本機資料，不同步)
- order.dining_table_id：掃碼點餐的桌位 (餐後至櫃檯結帳：payment_status unpaid → settled)
- tenant.table_qr_base_url：QR code 使用的門市網址 (顧客手機在店內 Wi-Fi 能開啟的網址)

Revision ID: 0005_dining_tables
Revises: 0004_payment_privacy
Create Date: 2026-10-22
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0005_dining_tables'
down_revision = '0004_payment_privacy'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('dining_table',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=30), nullable=False),
    sa.Column('token', sa.String(length=32), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_dining_table_tenant_name')
    )
    with op.batch_alter_table('dining_table', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_dining_table_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_dining_table_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_dining_table_token'), ['token'], unique=True)
        batch_op.create_index(batch_op.f('ix_dining_table_uuid'), ['uuid'], unique=True)

    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dining_table_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_order_dining_table_id'), ['dining_table_id'], unique=False)
        batch_op.create_foreign_key('fk_order_dining_table_id', 'dining_table', ['dining_table_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('tenant', schema=None) as batch_op:
        batch_op.add_column(sa.Column('table_qr_base_url', sa.String(length=200), server_default='', nullable=False))



def downgrade():
    with op.batch_alter_table('tenant', schema=None) as batch_op:
        batch_op.drop_column('table_qr_base_url')

    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_constraint('fk_order_dining_table_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_order_dining_table_id'))
        batch_op.drop_column('dining_table_id')

    with op.batch_alter_table('dining_table', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_dining_table_uuid'))
        batch_op.drop_index(batch_op.f('ix_dining_table_token'))
        batch_op.drop_index(batch_op.f('ix_dining_table_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_dining_table_sync_rev'))

    op.drop_table('dining_table')
