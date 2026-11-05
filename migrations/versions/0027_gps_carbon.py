"""外送員 GPS 追蹤 (order.driver_token、delivery_position 資料表) 與碳足跡標示
(ingredient.origin / carbon_factor、menu_item.carbon_grams / carbon_auto)

Revision ID: 0027_gps_carbon
Revises: 0026_round6
Create Date: 2026-11-05 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0027_gps_carbon'
down_revision = '0026_round6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('driver_token', sa.String(length=36), nullable=False, server_default=''))
    op.create_table(
        'delivery_position',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=36), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('sync_rev', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('order_uuid', sa.String(length=36), nullable=False),
        sa.Column('lat', sa.Float(), nullable=False),
        sa.Column('lng', sa.Float(), nullable=False),
        sa.Column('accuracy', sa.Float(), nullable=False, server_default='0'),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('delivery_position', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_delivery_position_uuid'), ['uuid'], unique=True)
        batch_op.create_index(batch_op.f('ix_delivery_position_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_position_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_position_order_uuid'), ['order_uuid'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_position_recorded_at'), ['recorded_at'], unique=False)
    with op.batch_alter_table('ingredient', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origin', sa.String(length=60), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('carbon_factor', sa.Float(), nullable=False, server_default='0'))
    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('carbon_grams', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('carbon_auto', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.drop_column('carbon_auto')
        batch_op.drop_column('carbon_grams')
    with op.batch_alter_table('ingredient', schema=None) as batch_op:
        batch_op.drop_column('carbon_factor')
        batch_op.drop_column('origin')
    op.drop_table('delivery_position')
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_column('driver_token')
