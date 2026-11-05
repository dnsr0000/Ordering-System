"""接單控制 (分店暫停接單狀態)、預估等候時間 (品項製作時間、訂單預計完成時間)、Web Push 訂閱

Revision ID: 0025_intake_eta_push
Revises: 0024_order_client_order_id
Create Date: 2026-10-31 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0025_intake_eta_push'
down_revision = '0024_order_client_order_id'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('prep_minutes', sa.Integer(), nullable=False, server_default='0'))
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('estimated_ready_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('sync_node', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paused_until', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('paused_message', sa.String(length=200), nullable=False, server_default=''))
    op.create_table(
        'push_subscription',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.String(length=500), nullable=False),
        sa.Column('p256dh', sa.String(length=200), nullable=False),
        sa.Column('auth', sa.String(length=100), nullable=False),
        sa.Column('order_uuid', sa.String(length=36), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_error', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('uuid', sa.String(length=36), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], name='fk_push_subscription_tenant_id'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_push_subscription_user_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('push_subscription', schema=None) as batch_op:
        batch_op.create_index('ix_push_subscription_tenant_id', ['tenant_id'], unique=False)
        batch_op.create_index('ix_push_subscription_order_uuid', ['order_uuid'], unique=False)
        batch_op.create_index('ix_push_subscription_endpoint', ['endpoint'], unique=False)
        batch_op.create_index('ix_push_subscription_sync_rev', ['sync_rev'], unique=False)
        batch_op.create_index('ix_push_subscription_uuid', ['uuid'], unique=True)


def downgrade():
    op.drop_table('push_subscription')
    with op.batch_alter_table('sync_node', schema=None) as batch_op:
        batch_op.drop_column('paused_message')
        batch_op.drop_column('paused_until')
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_column('estimated_ready_at')
    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.drop_column('prep_minutes')
