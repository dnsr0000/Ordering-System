"""預約取餐時間 (order.pickup_at)、桌邊呼叫服務 (service_call，本機)、餐後評分 (order_feedback，上傳線上端)、
餐點熱量與營養標示

Revision ID: 0011_pickup_calls_feedback
Revises: 0010_payments_drawer_sizes_qr
Create Date: 2026-10-23 21:41:38.374851

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0011_pickup_calls_feedback'
down_revision = '0010_payments_drawer_sizes_qr'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('service_call',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('dining_table_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('note', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('handled_at', sa.DateTime(), nullable=True),
    sa.Column('handled_by', sa.String(length=120), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['dining_table_id'], ['dining_table.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('service_call', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_service_call_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_service_call_dining_table_id'), ['dining_table_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_service_call_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_service_call_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_service_call_uuid'), ['uuid'], unique=True)

    op.create_table('order_feedback',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('comment', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['order.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('order_feedback', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_order_feedback_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_order_feedback_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_order_feedback_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_order_feedback_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_order_feedback_uuid'), ['uuid'], unique=True)

    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('calories', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('nutrition', sa.String(length=200), server_default='', nullable=False))

    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pickup_at', sa.DateTime(), nullable=True))



def downgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_column('pickup_at')

    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.drop_column('nutrition')
        batch_op.drop_column('calories')

    with op.batch_alter_table('order_feedback', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_order_feedback_uuid'))
        batch_op.drop_index(batch_op.f('ix_order_feedback_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_order_feedback_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_order_feedback_order_id'))
        batch_op.drop_index(batch_op.f('ix_order_feedback_created_at'))

    op.drop_table('order_feedback')
    with op.batch_alter_table('service_call', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_service_call_uuid'))
        batch_op.drop_index(batch_op.f('ix_service_call_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_service_call_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_service_call_dining_table_id'))
        batch_op.drop_index(batch_op.f('ix_service_call_created_at'))

    op.drop_table('service_call')
