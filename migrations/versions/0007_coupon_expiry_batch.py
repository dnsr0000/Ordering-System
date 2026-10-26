"""優惠券：活動期間、領取後到期天數、會員專屬券；會員券的到期時間、來源與發放批次

Revision ID: 0007_coupon_expiry_batch
Revises: 0006_pro_features
Create Date: 2026-10-23 12:30:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0007_coupon_expiry_batch'
down_revision = '0006_pro_features'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('coupon', schema=None) as batch_op:
        batch_op.add_column(sa.Column('member_only', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('valid_from', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('valid_until', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('valid_days', sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('user_coupon', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expires_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('source', sa.String(length=20), server_default='reward', nullable=False))
        batch_op.add_column(sa.Column('batch_id', sa.String(length=36), server_default='', nullable=False))
        batch_op.create_index(batch_op.f('ix_user_coupon_batch_id'), ['batch_id'], unique=False)


def downgrade():
    with op.batch_alter_table('user_coupon', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_coupon_batch_id'))
        batch_op.drop_column('batch_id')
        batch_op.drop_column('source')
        batch_op.drop_column('expires_at')

    with op.batch_alter_table('coupon', schema=None) as batch_op:
        batch_op.drop_column('valid_days')
        batch_op.drop_column('valid_until')
        batch_op.drop_column('valid_from')
        batch_op.drop_column('member_only')
