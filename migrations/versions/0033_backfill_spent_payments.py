"""資料回填：會員累積消費 (user.total_spent) 與 payment_record 啟用前歷史訂單的收款紀錄

1. user.total_spent：0006 新增欄位時沒有依歷史訂單回填，升級前就有的會員一律是 0。
   這裡用和 wallet_service.recompute_spent 完全相同的條件重算：未取消、付款狀態屬於營收
   (report_service.REVENUE_STATUSES) 的本機訂單 SUM(total_price - refunded_amount)，再加上 archived_spent。
   total_spent 是 local_only (不同步)：線上端與門市各自依本機訂單回填，不動 sync_rev / updated_at。

2. payment_record：0006 才開始記錄收款明細，升級前已收款的訂單沒有 sale 紀錄，升級前期間的收款方式統計、
   會計傳票、金流手續費都對不上訂單營收。這裡替這些訂單補一筆近似的 sale 紀錄：
   - 只補每個店家第一筆 payment_record 之前建立的訂單 (沒有任何紀錄的店家不設上限)。payment_record 只往線上端上傳，
     門市本來就沒有線上端收款的 sale 紀錄，沒有這個上限會替 0006 之後的線上訂單補出多餘的紀錄
   - 跳過已有任何收款紀錄 (不分種類) 的訂單、桌位結帳的訂單 (收款記在結帳單上) 與金額為 0 的訂單
   - sync_rev = 0：平常不送往線上端 (同步只送 sync_rev 大於游標的列)，線上端跑同一支遷移時依自己的訂單回填。
     分店從頭重新上傳 (游標 -1，例如線上端升級後結構改變) 時仍會送出：uuid 由訂單 uuid 固定算出 (uuid5)，
     線上端與分店替同一張訂單回填的紀錄 uuid 相同，payment_record 是 insert_only，線上端只會保留一筆，不會重複計算
   注意：混合付款 (method='混合付款') 與部分退款的舊訂單沒辦法精確還原各付款方式的金額，這些回填只是近似值
   (note 標示「0006 前歷史訂單回填 (近似值)」，staff_username = 'system')。

不 import app 的模型或服務 (常數直接寫在這裡)，避免日後程式改版讓這支遷移壞掉。

Revision ID: 0033_backfill_spent_payments
Revises: 0032_local_cols_idempotency
Create Date: 2026-11-09 10:30:00.000000

"""
import uuid
from datetime import datetime

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0033_backfill_spent_payments'
down_revision = '0032_local_cols_idempotency'
branch_labels = None
depends_on = None

CANCELLED = 'Cancelled'                        # OrderStatus.CANCELLED
REVENUE_STATUSES = ('', 'settled', 'paid')     # report_service.REVENUE_STATUSES
BACKFILL_NOTE = '0006 前歷史訂單回填 (近似值)'
BACKFILL_STAFF = 'system'
BATCH = 1000
# 回填紀錄的 uuid 由訂單 uuid 固定算出：線上端與分店各自回填同一張訂單時 uuid 相同，同步時不會變成兩筆
BACKFILL_NAMESPACE = uuid.UUID('6f1c2a4e-0006-4b5f-9a1d-7c3e5b2a0006')

ORDER = sa.table('order', sa.column('id', sa.Integer), sa.column('uuid', sa.String), sa.column('tenant_id', sa.Integer),
                 sa.column('status', sa.String), sa.column('payment_status', sa.String),
                 sa.column('payment_method', sa.String), sa.column('total_price', sa.Integer),
                 sa.column('created_at', sa.DateTime), sa.column('paid_at', sa.DateTime),
                 sa.column('settlement_id', sa.Integer))
PAYMENT_RECORD = sa.table('payment_record', sa.column('order_id', sa.Integer), sa.column('kind', sa.String),
                          sa.column('method', sa.String), sa.column('amount', sa.Integer),
                          sa.column('received', sa.Integer), sa.column('change', sa.Integer),
                          sa.column('reference', sa.String), sa.column('card_last4', sa.String),
                          sa.column('note', sa.String), sa.column('staff_username', sa.String),
                          sa.column('shift_id', sa.Integer), sa.column('settlement_id', sa.Integer),
                          sa.column('created_at', sa.DateTime), sa.column('tenant_id', sa.Integer),
                          sa.column('uuid', sa.String), sa.column('updated_at', sa.DateTime),
                          sa.column('sync_rev', sa.BigInteger))


def _backfill_uuid(order_uuid):
    return str(uuid.uuid5(BACKFILL_NAMESPACE, order_uuid)) if order_uuid else str(uuid.uuid4())


def _backfill_total_spent():
    statuses = ', '.join(f"'{s}'" for s in REVENUE_STATUSES)
    # user 與 order 是保留字，一律加雙引號 (SQLite 與 PostgreSQL 都能跑)
    op.execute(
        'UPDATE "user" SET total_spent = COALESCE((SELECT SUM(o.total_price - o.refunded_amount) FROM "order" o '
        f"WHERE o.user_id = \"user\".id AND o.status != '{CANCELLED}' AND o.payment_status IN ({statuses})), 0) "
        '+ COALESCE(archived_spent, 0)')


def _backfill_payment_records():
    bind = op.get_bind()
    o, pr = ORDER, PAYMENT_RECORD
    cutoffs = dict(bind.execute(sa.select(pr.c.tenant_id, sa.func.min(pr.c.created_at))
                                .group_by(pr.c.tenant_id)).all())
    tenant_ids = [t for (t,) in bind.execute(sa.select(o.c.tenant_id).distinct()).all()]
    base = [o.c.status != CANCELLED, o.c.payment_status.in_(REVENUE_STATUSES), o.c.total_price > 0,
            o.c.settlement_id.is_(None), ~sa.exists().where(pr.c.order_id == o.c.id)]
    for tenant_id in tenant_ids:
        cond = [o.c.tenant_id == tenant_id, *base]
        if cutoffs.get(tenant_id) is not None:
            cond.append(o.c.created_at < cutoffs[tenant_id])
        last_id = 0
        while True:
            # 依 id 分批讀取 (插入的列不影響之後批次的條件：已排除的訂單 id 都比 last_id 小)
            rows = bind.execute(sa.select(o.c.id, o.c.uuid, o.c.payment_method, o.c.total_price, o.c.created_at, o.c.paid_at)
                                .where(*cond, o.c.id > last_id).order_by(o.c.id).limit(BATCH)).all()
            if not rows:
                break
            last_id = rows[-1].id
            now = datetime.now()
            bind.execute(pr.insert(), [dict(
                order_id=r.id, kind='sale', method=r.payment_method or 'Cash', amount=int(r.total_price),
                received=int(r.total_price), change=0, reference='', card_last4='', note=BACKFILL_NOTE,
                staff_username=BACKFILL_STAFF, shift_id=None, settlement_id=None,
                created_at=r.paid_at or r.created_at or now, tenant_id=tenant_id, uuid=_backfill_uuid(r.uuid),
                updated_at=now, sync_rev=0) for r in rows])


def upgrade():
    _backfill_total_spent()
    _backfill_payment_records()


def downgrade():
    # total_spent 的回填值不必還原；只刪除這支遷移補的收款紀錄
    pr = PAYMENT_RECORD
    op.get_bind().execute(pr.delete().where(pr.c.kind == 'sale', pr.c.staff_username == BACKFILL_STAFF,
                                            pr.c.note == BACKFILL_NOTE))
