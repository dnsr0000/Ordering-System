"""真人客服對話：support_chat (顧客轉接的對話) 與 support_message (對話內容)

Revision ID: 0029_support_chat
Revises: 0028_staff_availability
Create Date: 2026-11-06 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0029_support_chat'
down_revision = '0028_staff_availability'
branch_labels = None
depends_on = None


def _sync_columns():
    return [
        sa.Column('uuid', sa.String(length=36), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('sync_rev', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
    ]


def upgrade():
    op.create_table(
        'support_chat',
        sa.Column('id', sa.Integer(), nullable=False),
        *_sync_columns(),
        sa.Column('token', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False, server_default='waiting'),
        sa.Column('customer_label', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('page', sa.String(length=20), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_customer_at', sa.DateTime(), nullable=True),
        sa.Column('last_staff_at', sa.DateTime(), nullable=True),
        sa.Column('staff_seen_id', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('closed_by', sa.String(length=120), nullable=False, server_default=''),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id']),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['order_id'], ['order.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('support_chat', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_support_chat_uuid'), ['uuid'], unique=True)
        batch_op.create_index(batch_op.f('ix_support_chat_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_support_chat_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_support_chat_token'), ['token'], unique=True)
        batch_op.create_index(batch_op.f('ix_support_chat_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_support_chat_created_at'), ['created_at'], unique=False)

    op.create_table(
        'support_message',
        sa.Column('id', sa.Integer(), nullable=False),
        *_sync_columns(),
        sa.Column('chat_id', sa.Integer(), nullable=False),
        sa.Column('sender', sa.String(length=10), nullable=False),
        sa.Column('staff_name', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('text', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id']),
        sa.ForeignKeyConstraint(['chat_id'], ['support_chat.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('support_message', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_support_message_uuid'), ['uuid'], unique=True)
        batch_op.create_index(batch_op.f('ix_support_message_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_support_message_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_support_message_chat_id'), ['chat_id'], unique=False)


def downgrade():
    op.drop_table('support_message')
    op.drop_table('support_chat')
