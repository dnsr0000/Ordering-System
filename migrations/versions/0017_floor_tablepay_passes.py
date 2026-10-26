"""桌位平面圖 (dining_table.seats / area / shape / pos_x / pos_y)、桌邊線上結帳 (table_payment)、寄杯 (pass_product / member_pass / pass_ledger)

Revision ID: 0017_floor_tablepay_passes
Revises: 0016_combo_choices_void
Create Date: 2026-10-24 20:47:59.695494

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0017_floor_tablepay_passes'
down_revision = '0016_combo_choices_void'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('table_payment',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('dining_table_id', sa.Integer(), nullable=False),
    sa.Column('order_ids', sa.String(length=300), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('trade_no', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('provider_trade_no', sa.String(length=30), nullable=False),
    sa.Column('note', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('paid_at', sa.DateTime(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['dining_table_id'], ['dining_table.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('trade_no')
    )
    with op.batch_alter_table('table_payment', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_table_payment_dining_table_id'), ['dining_table_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_table_payment_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_table_payment_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_table_payment_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_table_payment_uuid'), ['uuid'], unique=True)

    op.create_table('pass_product',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('menu_item_id', sa.Integer(), nullable=False),
    sa.Column('uses', sa.Integer(), nullable=False),
    sa.Column('price', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['menu_item_id'], ['menu_item.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pass_product', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pass_product_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_pass_product_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_pass_product_uuid'), ['uuid'], unique=True)

    op.create_table('member_pass',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('menu_item_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('total_uses', sa.Integer(), nullable=False),
    sa.Column('price_paid', sa.Integer(), nullable=False),
    sa.Column('sale_order_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('remaining', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['menu_item_id'], ['menu_item.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['product_id'], ['pass_product.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['sale_order_id'], ['order.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('member_pass', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_member_pass_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_member_pass_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_member_pass_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_member_pass_uuid'), ['uuid'], unique=True)

    op.create_table('pass_ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('member_pass_id', sa.Integer(), nullable=False),
    sa.Column('delta', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=20), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('staff_username', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['member_pass_id'], ['member_pass.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['order_id'], ['order.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pass_ledger', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pass_ledger_member_pass_id'), ['member_pass_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_pass_ledger_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_pass_ledger_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_pass_ledger_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_pass_ledger_uuid'), ['uuid'], unique=True)

    with op.batch_alter_table('dining_table', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seats', sa.Integer(), server_default='4', nullable=False))
        batch_op.add_column(sa.Column('area', sa.String(length=20), server_default='', nullable=False))
        batch_op.add_column(sa.Column('shape', sa.String(length=10), server_default='square', nullable=False))
        batch_op.add_column(sa.Column('pos_x', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('pos_y', sa.Integer(), nullable=True))



def downgrade():
    with op.batch_alter_table('dining_table', schema=None) as batch_op:
        batch_op.drop_column('pos_y')
        batch_op.drop_column('pos_x')
        batch_op.drop_column('shape')
        batch_op.drop_column('area')
        batch_op.drop_column('seats')

    with op.batch_alter_table('pass_ledger', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pass_ledger_uuid'))
        batch_op.drop_index(batch_op.f('ix_pass_ledger_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_pass_ledger_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_pass_ledger_order_id'))
        batch_op.drop_index(batch_op.f('ix_pass_ledger_member_pass_id'))

    op.drop_table('pass_ledger')
    with op.batch_alter_table('member_pass', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_member_pass_uuid'))
        batch_op.drop_index(batch_op.f('ix_member_pass_user_id'))
        batch_op.drop_index(batch_op.f('ix_member_pass_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_member_pass_sync_rev'))

    op.drop_table('member_pass')
    with op.batch_alter_table('pass_product', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pass_product_uuid'))
        batch_op.drop_index(batch_op.f('ix_pass_product_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_pass_product_sync_rev'))

    op.drop_table('pass_product')
    with op.batch_alter_table('table_payment', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_table_payment_uuid'))
        batch_op.drop_index(batch_op.f('ix_table_payment_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_table_payment_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_table_payment_status'))
        batch_op.drop_index(batch_op.f('ix_table_payment_dining_table_id'))

    op.drop_table('table_payment')
