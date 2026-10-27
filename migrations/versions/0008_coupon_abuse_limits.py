"""優惠券防濫用：公開代碼限會員使用、每位會員使用次數、單筆最高折抵金額

Revision ID: 0008_coupon_abuse_limits
Revises: 0007_coupon_expiry_batch
Create Date: 2026-10-23 20:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0008_coupon_abuse_limits'
down_revision = '0007_coupon_expiry_batch'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('coupon', schema=None) as batch_op:
        batch_op.add_column(sa.Column('login_required', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('uses_per_member', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('max_discount', sa.Integer(), server_default='0', nullable=False))


def downgrade():
    with op.batch_alter_table('coupon', schema=None) as batch_op:
        batch_op.drop_column('max_discount')
        batch_op.drop_column('uses_per_member')
        batch_op.drop_column('login_required')
