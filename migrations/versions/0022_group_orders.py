"""第四輪：團體預購 (group_order / group_order_entry，門市本機資料)

Revision ID: 0022_group_orders
Revises: 0021_expenses_cost_history
Create Date: 2026-10-26 15:10:06.189227

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0022_group_orders'
down_revision = '0021_expenses_cost_history'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('group_order',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token', sa.String(length=32), nullable=False),
    sa.Column('manage_key_hash', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=60), nullable=False),
    sa.Column('organizer_name', sa.String(length=30), nullable=False),
    sa.Column('organizer_phone', sa.String(length=20), nullable=False),
    sa.Column('order_type', sa.String(length=10), nullable=False),
    sa.Column('pickup_at', sa.DateTime(), nullable=False),
    sa.Column('deadline', sa.DateTime(), nullable=False),
    sa.Column('auto_submit', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('note', sa.String(length=100), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('error', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('submitted_at', sa.DateTime(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['order.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('group_order', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_group_order_deadline'), ['deadline'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_order_pickup_at'), ['pickup_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_order_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_order_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_order_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_order_token'), ['token'], unique=True)
        batch_op.create_index(batch_op.f('ix_group_order_uuid'), ['uuid'], unique=True)

    op.create_table('group_order_entry',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('group_order_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=30), nullable=False),
    sa.Column('items', sa.Text(), nullable=False),
    sa.Column('note', sa.String(length=100), nullable=False),
    sa.Column('entry_key_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['group_order_id'], ['group_order.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('group_order_entry', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_group_order_entry_group_order_id'), ['group_order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_order_entry_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_order_entry_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_order_entry_uuid'), ['uuid'], unique=True)


def downgrade():
    with op.batch_alter_table('group_order_entry', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_group_order_entry_uuid'))
        batch_op.drop_index(batch_op.f('ix_group_order_entry_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_group_order_entry_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_group_order_entry_group_order_id'))

    op.drop_table('group_order_entry')
    with op.batch_alter_table('group_order', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_group_order_uuid'))
        batch_op.drop_index(batch_op.f('ix_group_order_token'))
        batch_op.drop_index(batch_op.f('ix_group_order_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_group_order_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_group_order_status'))
        batch_op.drop_index(batch_op.f('ix_group_order_pickup_at'))
        batch_op.drop_index(batch_op.f('ix_group_order_deadline'))

    op.drop_table('group_order')
