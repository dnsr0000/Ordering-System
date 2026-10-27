"""進銷存與人事：供應商 (supplier)、採購單 (purchase_order、purchase_order_line)、原料批號效期 (ingredient_lot)、
原料預設供應商 / 目標庫存 / 採購單位、員工時薪 (人事成本率)

Revision ID: 0013_purchasing_lots_labor
Revises: 0012_stamps_giftcards_referral
Create Date: 2026-10-24 00:55:24.291556

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0013_purchasing_lots_labor'
down_revision = '0012_stamps_giftcards_referral'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('supplier',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('contact', sa.String(length=60), nullable=False),
    sa.Column('phone', sa.String(length=30), nullable=False),
    sa.Column('email', sa.String(length=120), nullable=False),
    sa.Column('lead_days', sa.Integer(), nullable=False),
    sa.Column('note', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_supplier_tenant_name')
    )
    with op.batch_alter_table('supplier', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_supplier_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_supplier_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_supplier_uuid'), ['uuid'], unique=True)

    op.create_table('purchase_order',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('expected_on', sa.Date(), nullable=True),
    sa.Column('note', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('received_at', sa.DateTime(), nullable=True),
    sa.Column('received_by', sa.String(length=120), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['supplier_id'], ['supplier.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('purchase_order', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_purchase_order_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_order_supplier_id'), ['supplier_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_order_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_order_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_order_uuid'), ['uuid'], unique=True)

    op.create_table('ingredient_lot',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ingredient_id', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.Column('remaining', sa.Float(), nullable=False),
    sa.Column('expires_on', sa.Date(), nullable=True),
    sa.Column('received_at', sa.DateTime(), nullable=False),
    sa.Column('unit_cost', sa.Float(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('note', sa.String(length=100), nullable=False),
    sa.Column('alerted_at', sa.DateTime(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['ingredient_id'], ['ingredient.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('ingredient_lot', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ingredient_lot_expires_on'), ['expires_on'], unique=False)
        batch_op.create_index(batch_op.f('ix_ingredient_lot_ingredient_id'), ['ingredient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ingredient_lot_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_ingredient_lot_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ingredient_lot_uuid'), ['uuid'], unique=True)

    op.create_table('purchase_order_line',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('purchase_order_id', sa.Integer(), nullable=False),
    sa.Column('ingredient_id', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.Column('unit_cost', sa.Float(), nullable=False),
    sa.Column('received_qty', sa.Float(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['ingredient_id'], ['ingredient.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_order.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('purchase_order_line', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_purchase_order_line_purchase_order_id'), ['purchase_order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_order_line_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_order_line_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_order_line_uuid'), ['uuid'], unique=True)

    with op.batch_alter_table('ingredient', schema=None) as batch_op:
        batch_op.add_column(sa.Column('supplier_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('par_level', sa.Float(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('pack_size', sa.Float(), server_default='1', nullable=False))
        batch_op.create_foreign_key('fk_ingredient_supplier_id', 'supplier', ['supplier_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('staff_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hourly_wage', sa.Integer(), server_default='0', nullable=False))



def downgrade():
    with op.batch_alter_table('staff_account', schema=None) as batch_op:
        batch_op.drop_column('hourly_wage')

    with op.batch_alter_table('ingredient', schema=None) as batch_op:
        batch_op.drop_constraint('fk_ingredient_supplier_id', type_='foreignkey')
        batch_op.drop_column('pack_size')
        batch_op.drop_column('par_level')
        batch_op.drop_column('supplier_id')

    with op.batch_alter_table('purchase_order_line', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_purchase_order_line_uuid'))
        batch_op.drop_index(batch_op.f('ix_purchase_order_line_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_purchase_order_line_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_purchase_order_line_purchase_order_id'))

    op.drop_table('purchase_order_line')
    with op.batch_alter_table('ingredient_lot', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ingredient_lot_uuid'))
        batch_op.drop_index(batch_op.f('ix_ingredient_lot_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_ingredient_lot_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_ingredient_lot_ingredient_id'))
        batch_op.drop_index(batch_op.f('ix_ingredient_lot_expires_on'))

    op.drop_table('ingredient_lot')
    with op.batch_alter_table('purchase_order', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_purchase_order_uuid'))
        batch_op.drop_index(batch_op.f('ix_purchase_order_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_purchase_order_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_purchase_order_supplier_id'))
        batch_op.drop_index(batch_op.f('ix_purchase_order_status'))

    op.drop_table('purchase_order')
    with op.batch_alter_table('supplier', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_supplier_uuid'))
        batch_op.drop_index(batch_op.f('ix_supplier_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_supplier_sync_rev'))

    op.drop_table('supplier')
