"""API 金鑰與 Webhook (api_key / webhook_endpoint / webhook_delivery / webhook_order_state)、裝置管理 (device)、
封存訂單的累積消費 (user.archived_spent)

Revision ID: 0020_integrations_devices
Revises: 0019_marketing_deposits
Create Date: 2026-10-25 00:01:13.030234

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0020_integrations_devices'
down_revision = '0019_marketing_deposits'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('api_key',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('prefix', sa.String(length=12), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('scopes', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.Column('last_ip', sa.String(length=50), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    with op.batch_alter_table('api_key', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_key_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_api_key_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_api_key_uuid'), ['uuid'], unique=True)

    op.create_table('device',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=True),
    sa.Column('pair_code', sa.String(length=8), nullable=True),
    sa.Column('pair_expires_at', sa.DateTime(), nullable=True),
    sa.Column('paired_at', sa.DateTime(), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(), nullable=True),
    sa.Column('last_ip', sa.String(length=50), nullable=False),
    sa.Column('user_agent', sa.String(length=200), nullable=False),
    sa.Column('app_version', sa.String(length=20), nullable=False),
    sa.Column('last_path', sa.String(length=120), nullable=False),
    sa.Column('is_locked', sa.Boolean(), nullable=False),
    sa.Column('lock_message', sa.String(length=120), nullable=False),
    sa.Column('reload_requested_at', sa.DateTime(), nullable=True),
    sa.Column('note', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('device', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_device_pair_code'), ['pair_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_device_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_device_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_device_uuid'), ['uuid'], unique=True)

    op.create_table('webhook_endpoint',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('url', sa.String(length=300), nullable=False),
    sa.Column('secret', sa.String(length=64), nullable=False),
    sa.Column('events', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('fail_streak', sa.Integer(), nullable=False),
    sa.Column('last_status', sa.String(length=120), nullable=False),
    sa.Column('last_sent_at', sa.DateTime(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('webhook_endpoint', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_webhook_endpoint_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_endpoint_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_endpoint_uuid'), ['uuid'], unique=True)

    op.create_table('webhook_delivery',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('endpoint_id', sa.Integer(), nullable=False),
    sa.Column('event', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
    sa.Column('response_code', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.String(length=300), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['endpoint_id'], ['webhook_endpoint.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('webhook_delivery', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_webhook_delivery_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_delivery_endpoint_id'), ['endpoint_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_delivery_next_attempt_at'), ['next_attempt_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_delivery_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_delivery_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_delivery_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_delivery_uuid'), ['uuid'], unique=True)

    op.create_table('webhook_order_state',
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('payment_status', sa.String(length=20), nullable=False),
    sa.Column('seen_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['order.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('order_id')
    )
    with op.batch_alter_table('webhook_order_state', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_webhook_order_state_tenant_id'), ['tenant_id'], unique=False)

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archived_spent', sa.Integer(), server_default='0', nullable=False))



def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('archived_spent')

    with op.batch_alter_table('webhook_order_state', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webhook_order_state_tenant_id'))

    op.drop_table('webhook_order_state')
    with op.batch_alter_table('webhook_delivery', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webhook_delivery_uuid'))
        batch_op.drop_index(batch_op.f('ix_webhook_delivery_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_webhook_delivery_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_webhook_delivery_status'))
        batch_op.drop_index(batch_op.f('ix_webhook_delivery_next_attempt_at'))
        batch_op.drop_index(batch_op.f('ix_webhook_delivery_endpoint_id'))
        batch_op.drop_index(batch_op.f('ix_webhook_delivery_created_at'))

    op.drop_table('webhook_delivery')
    with op.batch_alter_table('webhook_endpoint', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webhook_endpoint_uuid'))
        batch_op.drop_index(batch_op.f('ix_webhook_endpoint_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_webhook_endpoint_sync_rev'))

    op.drop_table('webhook_endpoint')
    with op.batch_alter_table('device', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_device_uuid'))
        batch_op.drop_index(batch_op.f('ix_device_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_device_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_device_pair_code'))

    op.drop_table('device')
    with op.batch_alter_table('api_key', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_key_uuid'))
        batch_op.drop_index(batch_op.f('ix_api_key_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_api_key_sync_rev'))

    op.drop_table('api_key')
