"""分店價格 / 分店上下架 (branch_menu_override)、分店叫貨 / 調撥單 (stock_transfer)

Revision ID: 0018_branch_menu_transfers
Revises: 0017_floor_tablepay_passes
Create Date: 2026-10-24 21:51:13.234182

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0018_branch_menu_transfers'
down_revision = '0017_floor_tablepay_passes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('stock_transfer',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('from_site', sa.String(length=36), nullable=False),
    sa.Column('to_site', sa.String(length=36), nullable=False),
    sa.Column('from_name', sa.String(length=100), nullable=False),
    sa.Column('to_name', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('lines_json', sa.Text(), nullable=False),
    sa.Column('note', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('shipped_by', sa.String(length=120), nullable=False),
    sa.Column('shipped_at', sa.DateTime(), nullable=True),
    sa.Column('received_by', sa.String(length=120), nullable=False),
    sa.Column('received_at', sa.DateTime(), nullable=True),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('stock_transfer', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stock_transfer_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_transfer_from_site'), ['from_site'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_transfer_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_transfer_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_transfer_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_transfer_to_site'), ['to_site'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_transfer_uuid'), ['uuid'], unique=True)

    op.create_table('branch_menu_override',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_uuid', sa.String(length=36), nullable=False),
    sa.Column('menu_item_id', sa.Integer(), nullable=False),
    sa.Column('price', sa.Integer(), nullable=True),
    sa.Column('is_hidden', sa.Boolean(), nullable=False),
    sa.Column('changed_by', sa.String(length=120), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['menu_item_id'], ['menu_item.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'node_uuid', 'menu_item_id', name='uq_branch_menu_override')
    )
    with op.batch_alter_table('branch_menu_override', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_branch_menu_override_menu_item_id'), ['menu_item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_branch_menu_override_node_uuid'), ['node_uuid'], unique=False)
        batch_op.create_index(batch_op.f('ix_branch_menu_override_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_branch_menu_override_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_branch_menu_override_uuid'), ['uuid'], unique=True)



def downgrade():
    with op.batch_alter_table('branch_menu_override', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_branch_menu_override_uuid'))
        batch_op.drop_index(batch_op.f('ix_branch_menu_override_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_branch_menu_override_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_branch_menu_override_node_uuid'))
        batch_op.drop_index(batch_op.f('ix_branch_menu_override_menu_item_id'))

    op.drop_table('branch_menu_override')
    with op.batch_alter_table('stock_transfer', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stock_transfer_uuid'))
        batch_op.drop_index(batch_op.f('ix_stock_transfer_to_site'))
        batch_op.drop_index(batch_op.f('ix_stock_transfer_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_stock_transfer_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_stock_transfer_status'))
        batch_op.drop_index(batch_op.f('ix_stock_transfer_from_site'))
        batch_op.drop_index(batch_op.f('ix_stock_transfer_created_at'))

    op.drop_table('stock_transfer')
