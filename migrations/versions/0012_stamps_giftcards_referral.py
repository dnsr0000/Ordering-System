"""會員忠誠度：集點卡 (stamp_ledger)、禮物卡 (gift_card、gift_card_ledger)、推薦好友 (user.referral_code、
referred_by_uuid)、點數到期提醒日 (user.points_notice_on)

Revision ID: 0012_stamps_giftcards_referral
Revises: 0011_pickup_calls_feedback
Create Date: 2026-10-23 22:13:18.210377

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0012_stamps_giftcards_referral'
down_revision = '0011_pickup_calls_feedback'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('gift_card',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('initial_amount', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('note', sa.String(length=100), nullable=False),
    sa.Column('sold_by', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('voided_at', sa.DateTime(), nullable=True),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'code', name='uq_gift_card_tenant_code')
    )
    with op.batch_alter_table('gift_card', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gift_card_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_gift_card_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_gift_card_uuid'), ['uuid'], unique=True)

    op.create_table('gift_card_ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gift_card_id', sa.Integer(), nullable=False),
    sa.Column('delta', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=20), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('staff_username', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['gift_card_id'], ['gift_card.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['order_id'], ['order.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('gift_card_ledger', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gift_card_ledger_gift_card_id'), ['gift_card_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_gift_card_ledger_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_gift_card_ledger_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_gift_card_ledger_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_gift_card_ledger_uuid'), ['uuid'], unique=True)

    op.create_table('stamp_ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('delta', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=20), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('sync_rev', sa.BigInteger(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['order.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('stamp_ledger', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stamp_ledger_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stamp_ledger_sync_rev'), ['sync_rev'], unique=False)
        batch_op.create_index(batch_op.f('ix_stamp_ledger_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stamp_ledger_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stamp_ledger_uuid'), ['uuid'], unique=True)

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('referral_code', sa.String(length=12), server_default='', nullable=False))
        batch_op.add_column(sa.Column('referred_by_uuid', sa.String(length=36), server_default='', nullable=False))
        batch_op.add_column(sa.Column('points_notice_on', sa.Date(), nullable=True))
        batch_op.create_index(batch_op.f('ix_user_referral_code'), ['referral_code'], unique=False)



def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_referral_code'))
        batch_op.drop_column('points_notice_on')
        batch_op.drop_column('referred_by_uuid')
        batch_op.drop_column('referral_code')

    with op.batch_alter_table('stamp_ledger', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stamp_ledger_uuid'))
        batch_op.drop_index(batch_op.f('ix_stamp_ledger_user_id'))
        batch_op.drop_index(batch_op.f('ix_stamp_ledger_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_stamp_ledger_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_stamp_ledger_order_id'))

    op.drop_table('stamp_ledger')
    with op.batch_alter_table('gift_card_ledger', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_gift_card_ledger_uuid'))
        batch_op.drop_index(batch_op.f('ix_gift_card_ledger_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_gift_card_ledger_sync_rev'))
        batch_op.drop_index(batch_op.f('ix_gift_card_ledger_order_id'))
        batch_op.drop_index(batch_op.f('ix_gift_card_ledger_gift_card_id'))

    op.drop_table('gift_card_ledger')
    with op.batch_alter_table('gift_card', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_gift_card_uuid'))
        batch_op.drop_index(batch_op.f('ix_gift_card_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_gift_card_sync_rev'))

    op.drop_table('gift_card')
