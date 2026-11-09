"""帳號欄位加長、訂單編號清單改為不限長度、刪除桌位時保留桌邊線上結帳紀錄

- admin_log.username (50)、staff_audit_log.staff_username (100)、order.cashier_username (100) 改成 120，
  與 staff_account.username 同長 (較短時 PostgreSQL 上長帳號登入 / 結帳會因字串過長失敗)
- settlement.order_ids (500)、table_payment.order_ids (300) 改成 Text：同桌累積的未結帳訂單很多時不會超過長度
- table_payment.dining_table_id 改為可空、外鍵改成 ON DELETE SET NULL (具名 fk_table_payment_dining_table_id)：
  刪除桌位時付款中 / 待人工退款的線上結帳紀錄不會跟著消失

Revision ID: 0031_widen_cols_tablepay_fk
Revises: 0030_staff_deleted_at_fix
Create Date: 2026-11-09 10:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0031_widen_cols_tablepay_fk'
down_revision = '0030_staff_deleted_at_fix'
branch_labels = None
depends_on = None

# SQLite 的外鍵沒有名稱：batch mode 重建資料表時依此命名，才能刪除 (與 0002 相同)
NAMING = {'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'}
FK_NAME = 'fk_table_payment_dining_table_id'


def _fk_names(bind, table, column, ref_table):
    """某欄位指向 ref_table 的現有外鍵名稱 (SQLite 沒有名稱時依 NAMING 命名，供 batch mode 刪除)"""
    names = []
    for fk in sa.inspect(bind).get_foreign_keys(table):
        if fk['constrained_columns'] == [column] and fk['referred_table'] == ref_table:
            names.append(fk['name'] or NAMING['fk'] % {
                'table_name': table, 'column_0_name': column, 'referred_table_name': ref_table})
    return names


def _replace_table_payment_fk(ondelete, nullable):
    bind = op.get_bind()
    names = _fk_names(bind, 'table_payment', 'dining_table_id', 'dining_table')
    with op.batch_alter_table('table_payment', naming_convention=NAMING) as batch_op:
        for name in names:
            batch_op.drop_constraint(name, type_='foreignkey')
        batch_op.alter_column('dining_table_id', existing_type=sa.Integer(), nullable=nullable)
        batch_op.create_foreign_key(FK_NAME, 'dining_table', ['dining_table_id'], ['id'], ondelete=ondelete)


def upgrade():
    with op.batch_alter_table('admin_log', schema=None) as batch_op:
        batch_op.alter_column('username', existing_type=sa.String(length=50), type_=sa.String(length=120),
                              existing_nullable=False)
    with op.batch_alter_table('staff_audit_log', schema=None) as batch_op:
        batch_op.alter_column('staff_username', existing_type=sa.String(length=100), type_=sa.String(length=120),
                              existing_nullable=False)
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.alter_column('cashier_username', existing_type=sa.String(length=100), type_=sa.String(length=120),
                              existing_nullable=True)
    with op.batch_alter_table('settlement', schema=None) as batch_op:
        batch_op.alter_column('order_ids', existing_type=sa.String(length=500), type_=sa.Text(),
                              existing_nullable=False)
    with op.batch_alter_table('table_payment', schema=None) as batch_op:
        batch_op.alter_column('order_ids', existing_type=sa.String(length=300), type_=sa.Text(),
                              existing_nullable=False)
    _replace_table_payment_fk('SET NULL', nullable=True)


def downgrade():
    # 桌位已刪除 (dining_table_id 為空) 的紀錄放不回必填欄位：先刪除
    op.execute('DELETE FROM table_payment WHERE dining_table_id IS NULL')
    _replace_table_payment_fk('CASCADE', nullable=False)
    with op.batch_alter_table('table_payment', schema=None) as batch_op:
        batch_op.alter_column('order_ids', existing_type=sa.Text(), type_=sa.String(length=300),
                              existing_nullable=False)
    with op.batch_alter_table('settlement', schema=None) as batch_op:
        batch_op.alter_column('order_ids', existing_type=sa.Text(), type_=sa.String(length=500),
                              existing_nullable=False)
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.alter_column('cashier_username', existing_type=sa.String(length=120), type_=sa.String(length=100),
                              existing_nullable=True)
    with op.batch_alter_table('staff_audit_log', schema=None) as batch_op:
        batch_op.alter_column('staff_username', existing_type=sa.String(length=120), type_=sa.String(length=100),
                              existing_nullable=False)
    with op.batch_alter_table('admin_log', schema=None) as batch_op:
        batch_op.alter_column('username', existing_type=sa.String(length=120), type_=sa.String(length=50),
                              existing_nullable=False)
