"""套餐任選群組 (combo_option_item.choice_group / extra_price) 與單品作廢 (order_item.voided_qty / void_note)

Revision ID: 0016_combo_choices_void
Revises: 0015_combo_sides
Create Date: 2026-10-24 20:08:41.356750

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0016_combo_choices_void'
down_revision = '0015_combo_sides'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('combo_option_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('choice_group', sa.String(length=30), server_default='', nullable=False))
        batch_op.add_column(sa.Column('extra_price', sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('order_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('voided_qty', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('void_note', sa.String(length=120), server_default='', nullable=False))



def downgrade():
    with op.batch_alter_table('order_item', schema=None) as batch_op:
        batch_op.drop_column('void_note')
        batch_op.drop_column('voided_qty')

    with op.batch_alter_table('combo_option_item', schema=None) as batch_op:
        batch_op.drop_column('extra_price')
        batch_op.drop_column('choice_group')

