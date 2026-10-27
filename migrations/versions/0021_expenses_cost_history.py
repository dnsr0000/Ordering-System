"""第四輪：營業費用 (expense)、原料成本 / 餐點售價變動歷史 (cost_history)

Revision ID: 0021_expenses_cost_history
Revises: 0020_integrations_devices
Create Date: 2026-10-26 14:50:09.629536

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0021_expenses_cost_history'
down_revision = '0020_integrations_devices'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cost_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('ref_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('field', sa.String(length=20), nullable=False),
    sa.Column('old_value', sa.Float(), nullable=False),
    sa.Column('new_value', sa.Float(), nullable=False),
    sa.Column('changed_at', sa.DateTime(), nullable=False),
    sa.Column('changed_by', sa.String(length=120), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cost_history', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cost_history_changed_at'), ['changed_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_cost_history_ref_id'), ['ref_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_cost_history_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_cost_history_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_cost_history_uuid'), ['uuid'], unique=True)

    op.create_table('expense',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('spent_on', sa.Date(), nullable=False),
    sa.Column('category', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('vendor', sa.String(length=60), nullable=False),
    sa.Column('invoice_no', sa.String(length=20), nullable=False),
    sa.Column('is_fixed', sa.Boolean(), nullable=False),
    sa.Column('note', sa.String(length=200), nullable=False),
    sa.Column('node_uuid', sa.String(length=36), nullable=True),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('is_deleted', sa.Boolean(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_expense_node_uuid'), ['node_uuid'], unique=False)
        batch_op.create_index(batch_op.f('ix_expense_spent_on'), ['spent_on'], unique=False)
        batch_op.create_index(batch_op.f('ix_expense_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_expense_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_expense_uuid'), ['uuid'], unique=True)


def downgrade():
    with op.batch_alter_table('expense', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_expense_uuid'))
        batch_op.drop_index(batch_op.f('ix_expense_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_expense_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_expense_spent_on'))
        batch_op.drop_index(batch_op.f('ix_expense_node_uuid'))

    op.drop_table('expense')
    with op.batch_alter_table('cost_history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cost_history_uuid'))
        batch_op.drop_index(batch_op.f('ix_cost_history_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_cost_history_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_cost_history_ref_id'))
        batch_op.drop_index(batch_op.f('ix_cost_history_changed_at'))

    op.drop_table('cost_history')
