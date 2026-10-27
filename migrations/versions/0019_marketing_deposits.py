"""會員分群與自動行銷 (marketing_campaign / campaign_send、user.marketing_opt_out)、訂位訂金 / 提醒 (reservation.deposit_* / reminded_at)

Revision ID: 0019_marketing_deposits
Revises: 0018_branch_menu_transfers
Create Date: 2026-10-24 22:21:30.252908

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0019_marketing_deposits'
down_revision = '0018_branch_menu_transfers'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('marketing_campaign',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('segment', sa.String(length=20), nullable=False),
    sa.Column('inactive_days', sa.Integer(), nullable=False),
    sa.Column('cooldown_days', sa.Integer(), nullable=False),
    sa.Column('coupon_id', sa.Integer(), nullable=True),
    sa.Column('coupon_valid_days', sa.Integer(), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=False),
    sa.Column('send_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_run_at', sa.DateTime(), nullable=True),
    sa.Column('sent_count', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['coupon_id'], ['coupon.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('marketing_campaign', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_marketing_campaign_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_marketing_campaign_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_marketing_campaign_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_marketing_campaign_uuid'), ['uuid'], unique=True)

    op.create_table('campaign_send',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('campaign_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=False),
    sa.Column('channel', sa.String(length=10), nullable=False),
    sa.Column('batch_id', sa.String(length=32), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['marketing_campaign.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('campaign_send', schema=None) as batch_op:
        batch_op.create_index('ix_campaign_send_campaign_user', ['campaign_id', 'user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_campaign_send_sent_at'), ['sent_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_campaign_send_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_campaign_send_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_campaign_send_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_campaign_send_uuid'), ['uuid'], unique=True)

    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deposit_amount', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('deposit_status', sa.String(length=14), server_default='', nullable=False))
        batch_op.add_column(sa.Column('deposit_trade_no', sa.String(length=20), server_default='', nullable=False))
        batch_op.add_column(sa.Column('deposit_provider_no', sa.String(length=30), server_default='', nullable=False))
        batch_op.add_column(sa.Column('deposit_paid_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('deposit_note', sa.String(length=200), server_default='', nullable=False))
        batch_op.add_column(sa.Column('reminded_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('status_changed_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_reservation_deposit_status'), ['deposit_status'], unique=False)
        batch_op.create_index(batch_op.f('ix_reservation_deposit_trade_no'), ['deposit_trade_no'], unique=False)

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('marketing_opt_out', sa.Boolean(), server_default='0', nullable=False))



def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('marketing_opt_out')

    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reservation_deposit_trade_no'))
        batch_op.drop_index(batch_op.f('ix_reservation_deposit_status'))
        batch_op.drop_column('status_changed_at')
        batch_op.drop_column('reminded_at')
        batch_op.drop_column('deposit_note')
        batch_op.drop_column('deposit_paid_at')
        batch_op.drop_column('deposit_provider_no')
        batch_op.drop_column('deposit_trade_no')
        batch_op.drop_column('deposit_status')
        batch_op.drop_column('deposit_amount')

    with op.batch_alter_table('campaign_send', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_campaign_send_uuid'))
        batch_op.drop_index(batch_op.f('ix_campaign_send_user_id'))
        batch_op.drop_index(batch_op.f('ix_campaign_send_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_campaign_send_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_campaign_send_sent_at'))
        batch_op.drop_index('ix_campaign_send_campaign_user')

    op.drop_table('campaign_send')
    with op.batch_alter_table('marketing_campaign', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_marketing_campaign_uuid'))
        batch_op.drop_index(batch_op.f('ix_marketing_campaign_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_marketing_campaign_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_marketing_campaign_status'))

    op.drop_table('marketing_campaign')
