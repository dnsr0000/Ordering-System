"""第六輪：會員價 (menu_item.member_price)、KDS 製作卡 (menu_item.prep_note)、顧客評價回覆 / 公開 (order_feedback.reply / replied_at / is_public)、
員工額外權限欄位加長 (staff_account.permissions 300 → 500，頁面權限碼變多)、會員 Email (user.email，電子收據)

Revision ID: 0026_round6
Revises: 0025_intake_eta_push
Create Date: 2026-10-31 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0026_round6'
down_revision = '0025_intake_eta_push'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('member_price', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('prep_note', sa.String(length=500), nullable=False, server_default=''))
    with op.batch_alter_table('order_feedback', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reply', sa.String(length=300), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('replied_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('is_public', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('staff_account', schema=None) as batch_op:
        batch_op.alter_column('permissions', existing_type=sa.String(length=300), type_=sa.String(length=500),
                              existing_nullable=False, existing_server_default='')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email', sa.String(length=120), nullable=False, server_default=''))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('email')
    with op.batch_alter_table('staff_account', schema=None) as batch_op:
        batch_op.alter_column('permissions', existing_type=sa.String(length=500), type_=sa.String(length=300),
                              existing_nullable=False, existing_server_default='')
    with op.batch_alter_table('order_feedback', schema=None) as batch_op:
        batch_op.drop_column('is_public')
        batch_op.drop_column('replied_at')
        batch_op.drop_column('reply')
    with op.batch_alter_table('menu_item', schema=None) as batch_op:
        batch_op.drop_column('prep_note')
        batch_op.drop_column('member_price')
