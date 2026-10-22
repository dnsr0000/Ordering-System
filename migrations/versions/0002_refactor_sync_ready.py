"""重構：同步欄位、正規化套餐配餐、憑證狀態、整數金額、外鍵與新資料表

- 所有資料表加上 uuid (全域唯一，本機與線上同步時使用) 與 updated_at
- combo_option.item1~3_id / item1~3_customizable → combo_option_item 關聯表
- reward_redemption.is_used / is_refunded → status (open / used / refunded)
- order_item 加上 is_reward，品名不再帶「[點數兌換]」前綴
- coupon.discount_value / min_spend 改為整數 (百分比折扣存 85 代表 85 折)
- 補上外鍵與刪除規則 (先清除孤兒參照，PostgreSQL 會強制檢查外鍵)
- 新增 daily_counter (取餐號) 與 auth_throttle (PIN 登入鎖定)

Revision ID: 0002_refactor_sync_ready
Revises: 0001_baseline
Create Date: 2026-10-22
"""
import re
import uuid

import sqlalchemy as sa
from alembic import op

revision = '0002_refactor_sync_ready'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None

SYNC_TABLES = (
    'tenant', 'staff_account', 'user', 'admin_log', 'staff_audit_log', 'customer_log', 'reward_setting',
    'reward_redemption', 'menu_item', 'combo_option', 'modifier_option', 'order', 'order_item', 'coupon', 'user_coupon',
)
NAMING = {'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'}
_REWARD_PREFIX = re.compile(r'^(🎁\s*)?(\[點數兌換\]\s*)?')


def _t(name, *cols):
    return sa.table(name, *(sa.column(c) for c in cols))


def _fk_names(bind, table, column):
    """找出某欄位現有外鍵的名稱 (SQLite 沒有名稱時依 NAMING 命名，供 batch mode 刪除)"""
    names = []
    for fk in sa.inspect(bind).get_foreign_keys(table):
        if fk['constrained_columns'] == [column]:
            names.append(fk['name'] or NAMING['fk'] % {
                'table_name': table, 'column_0_name': column, 'referred_table_name': fk['referred_table']})
    return names


def _replace_fk(batch, bind, table, column, ref_table, ondelete):
    for name in _fk_names(bind, table, column):
        batch.drop_constraint(name, type_='foreignkey')
    batch.create_foreign_key(f'fk_{table}_{column}', ref_table, [column], ['id'], ondelete=ondelete)


def _null_orphans(bind, table, column, ref_table):
    t, r = _t(table, 'id', column), _t(ref_table, 'id')
    bind.execute(t.update().where(t.c[column].is_not(None), ~t.c[column].in_(sa.select(r.c.id))).values({column: None}))


def _delete_orphans(bind, table, column, ref_table):
    t, r = _t(table, 'id', column), _t(ref_table, 'id')
    bind.execute(t.delete().where(~t.c[column].in_(sa.select(r.c.id))))


def upgrade():
    bind = op.get_bind()

    # ---- 1. 新資料表 ----
    op.create_table(
        'daily_counter',
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenant.id'), primary_key=True),
        sa.Column('business_date', sa.Date(), primary_key=True),
        sa.Column('name', sa.String(30), primary_key=True),
        sa.Column('value', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_table(
        'auth_throttle',
        sa.Column('key', sa.String(200), primary_key=True),
        sa.Column('fails', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('strikes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('window_start', sa.DateTime(), nullable=True),
        sa.Column('locked_until', sa.DateTime(), nullable=True),
    )
    op.create_table(
        'combo_option_item',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenant.id'), nullable=False, index=True),
        sa.Column('uuid', sa.String(36), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('combo_option_id', sa.Integer(), sa.ForeignKey('combo_option.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('menu_item_id', sa.Integer(), sa.ForeignKey('menu_item.id', ondelete='CASCADE'), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('customizable', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index('ix_combo_option_item_uuid', 'combo_option_item', ['uuid'], unique=True)

    # ---- 2. 同步欄位：uuid + updated_at (先允許空值，回填後再設為必填並建立唯一索引) ----
    for table in SYNC_TABLES:
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column('uuid', sa.String(36), nullable=True))
            b.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
        t = _t(table, 'id', 'uuid')
        ids = [row[0] for row in bind.execute(sa.select(t.c.id))]
        if ids:
            bind.execute(t.update().where(t.c.id == sa.bindparam('_id')).values(uuid=sa.bindparam('_uuid')),
                         [{'_id': i, '_uuid': str(uuid.uuid4())} for i in ids])
        with op.batch_alter_table(table) as b:
            b.alter_column('uuid', existing_type=sa.String(36), nullable=False)
            b.create_index(f'ix_{table}_uuid', ['uuid'], unique=True)

    # ---- 3. 套餐配餐 → combo_option_item ----
    _delete_orphans(bind, 'combo_option', 'main_item_id', 'menu_item')
    items = {row[0] for row in bind.execute(sa.select(_t('menu_item', 'id').c.id))}
    combo = _t('combo_option', 'id', 'tenant_id', *(f'item{n}_id' for n in (1, 2, 3)), *(f'item{n}_customizable' for n in (1, 2, 3)))
    sides = []
    for row in bind.execute(sa.select(combo)).mappings():
        pos = 0
        for n in (1, 2, 3):
            side_id = row[f'item{n}_id']
            if side_id and side_id in items:
                sides.append({'tenant_id': row['tenant_id'], 'uuid': str(uuid.uuid4()), 'combo_option_id': row['id'],
                              'menu_item_id': side_id, 'position': pos, 'customizable': bool(row[f'item{n}_customizable'])})
                pos += 1
    if sides:
        op.bulk_insert(sa.table('combo_option_item', *(sa.column(c) for c in sides[0])), sides)
    with op.batch_alter_table('combo_option', naming_convention=NAMING) as b:
        for n in (1, 2, 3):
            for name in _fk_names(bind, 'combo_option', f'item{n}_id'):
                b.drop_constraint(name, type_='foreignkey')
            b.drop_column(f'item{n}_id')
            b.drop_column(f'item{n}_customizable')
        _replace_fk(b, bind, 'combo_option', 'main_item_id', 'menu_item', 'CASCADE')

    # ---- 4. 兌換憑證：is_used / is_refunded → status ----
    _delete_orphans(bind, 'reward_redemption', 'user_id', 'user')
    _null_orphans(bind, 'reward_redemption', 'menu_item_id', 'menu_item')
    _null_orphans(bind, 'reward_redemption', 'order_id', 'order')
    with op.batch_alter_table('reward_redemption') as b:
        b.add_column(sa.Column('status', sa.String(20), nullable=False, server_default='open'))
    rr = _t('reward_redemption', 'status', 'is_used', 'is_refunded')
    bind.execute(rr.update().where(rr.c.is_refunded == sa.true()).values(status='refunded'))
    bind.execute(rr.update().where(rr.c.is_refunded.is_not(True), rr.c.is_used == sa.true()).values(status='used'))
    with op.batch_alter_table('reward_redemption', naming_convention=NAMING) as b:
        b.drop_column('is_used')
        b.drop_column('is_refunded')
        b.alter_column('menu_item_id', existing_type=sa.Integer(), nullable=True)
        _replace_fk(b, bind, 'reward_redemption', 'user_id', 'user', 'CASCADE')
        _replace_fk(b, bind, 'reward_redemption', 'menu_item_id', 'menu_item', 'SET NULL')
        b.create_foreign_key('fk_reward_redemption_order_id', 'order', ['order_id'], ['id'], ondelete='SET NULL')

    # ---- 5. 訂單明細：is_reward 旗標，品名去掉兌換前綴 ----
    with op.batch_alter_table('order_item') as b:
        b.add_column(sa.Column('is_reward', sa.Boolean(), nullable=False, server_default=sa.false()))
    oi = _t('order_item', 'id', 'item_name', 'customization', 'is_reward')
    flagged = bind.execute(sa.select(oi.c.id, oi.c.item_name).where(sa.or_(
        oi.c.item_name.like('%[點數兌換]%'), oi.c.item_name.like('🎁%'), oi.c.customization == '紅利免費兌換'))).all()
    if flagged:
        bind.execute(oi.update().where(oi.c.id == sa.bindparam('_id'))
                     .values(is_reward=True, item_name=sa.bindparam('_name')),
                     [{'_id': i, '_name': _REWARD_PREFIX.sub('', name).strip() or name} for i, name in flagged])
    _delete_orphans(bind, 'order_item', 'order_id', 'order')
    _null_orphans(bind, 'order_item', 'menu_item_id', 'menu_item')
    _null_orphans(bind, 'order_item', 'combo_option_id', 'combo_option')
    with op.batch_alter_table('order_item', naming_convention=NAMING) as b:
        _replace_fk(b, bind, 'order_item', 'order_id', 'order', 'CASCADE')
        b.create_foreign_key('fk_order_item_menu_item_id', 'menu_item', ['menu_item_id'], ['id'], ondelete='SET NULL')
        b.create_foreign_key('fk_order_item_combo_option_id', 'combo_option', ['combo_option_id'], ['id'], ondelete='SET NULL')
        b.create_index('ix_order_item_order_id', ['order_id'])

    # ---- 6. 訂單：外鍵與索引 ----
    _null_orphans(bind, 'order', 'user_id', 'user')
    _null_orphans(bind, 'order', 'applied_user_coupon_id', 'user_coupon')
    _null_orphans(bind, 'order', 'customer_log_id', 'customer_log')
    with op.batch_alter_table('order') as b:
        b.create_foreign_key('fk_order_user_id', 'user', ['user_id'], ['id'], ondelete='SET NULL')
        b.create_foreign_key('fk_order_applied_user_coupon_id', 'user_coupon', ['applied_user_coupon_id'], ['id'], ondelete='SET NULL')
        b.create_foreign_key('fk_order_customer_log_id', 'customer_log', ['customer_log_id'], ['id'], ondelete='SET NULL')
        b.create_index('ix_order_status', ['status'])
        b.create_index('ix_order_created_at', ['created_at'])

    # ---- 7. 優惠券金額改為整數 (百分比：0.85 → 85) ----
    cp = _t('coupon', 'id', 'discount_type', 'discount_value', 'min_spend')
    rows = bind.execute(sa.select(cp.c.id, cp.c.discount_type, cp.c.discount_value, cp.c.min_spend)).all()
    if rows:
        def _pct(v):
            v = float(v or 0)
            v = v * 100 if v <= 1 else (v * 10 if v <= 10 else v)
            return int(min(99, max(1, round(v))))
        bind.execute(cp.update().where(cp.c.id == sa.bindparam('_id'))
                     .values(discount_value=sa.bindparam('_v'), min_spend=sa.bindparam('_m'), discount_type=sa.bindparam('_t')),
                     [{'_id': i, '_t': 'percent' if t == 'percent' else 'fixed',
                       '_v': _pct(v) if t == 'percent' else max(0, int(round(float(v or 0)))),
                       '_m': max(0, int(round(float(m or 0))))} for i, t, v, m in rows])
    with op.batch_alter_table('coupon') as b:
        b.alter_column('discount_value', existing_type=sa.Float(), type_=sa.Integer(), nullable=False,
                       postgresql_using='round(discount_value)::integer')
        b.alter_column('min_spend', existing_type=sa.Float(), type_=sa.Integer(), nullable=False,
                       server_default='0', postgresql_using='round(coalesce(min_spend, 0))::integer')
        b.alter_column('discount_type', existing_type=sa.String(20), nullable=False, server_default='fixed')

    # ---- 8. 會員持券外鍵 ----
    _delete_orphans(bind, 'user_coupon', 'user_id', 'user')
    _delete_orphans(bind, 'user_coupon', 'coupon_id', 'coupon')
    with op.batch_alter_table('user_coupon', naming_convention=NAMING) as b:
        _replace_fk(b, bind, 'user_coupon', 'user_id', 'user', 'CASCADE')
        _replace_fk(b, bind, 'user_coupon', 'coupon_id', 'coupon', 'CASCADE')

    # ---- 9. 會員點數不可為空 ----
    u = _t('user', 'points')
    bind.execute(u.update().where(u.c.points.is_(None)).values(points=0))
    with op.batch_alter_table('user') as b:
        b.alter_column('points', existing_type=sa.Integer(), nullable=False, server_default='0')


def downgrade():
    raise NotImplementedError('此版本不支援降版，請由升級前自動另存的資料庫備份 (database/menu.db.bak-*) 還原')
