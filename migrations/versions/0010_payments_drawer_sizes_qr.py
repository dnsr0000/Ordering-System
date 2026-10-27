"""線上付款方式 (ATM / 超商代碼繳費期限與資訊)、錢櫃 (印表機接錢櫃)、餐點規格 (大小杯)、會員動態 QR 碼

Revision ID: 0010_payments_drawer_sizes_qr
Revises: 0009_hq_settings_2fa_sessions
Create Date: 2026-10-23 21:10:01.319393

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0010_payments_drawer_sizes_qr'
down_revision = '0009_hq_settings_2fa_sessions'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('size_options', sa.String(length=200), server_default='', nullable=False))

    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('payment_due_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('payment_note', sa.String(length=120), server_default='', nullable=False))

    with op.batch_alter_table('printer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('drawer', sa.Boolean(), server_default=sa.false(), nullable=False))

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('qr_secret', sa.String(length=64), server_default='', nullable=False))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('qr_secret')

    with op.batch_alter_table('printer', schema=None) as batch_op:
        batch_op.drop_column('drawer')

    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_column('payment_note')
        batch_op.drop_column('payment_due_at')

    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.drop_column('size_options')
