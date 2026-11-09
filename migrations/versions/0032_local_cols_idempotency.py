"""冪等鍵、API 異動序號、促銷明細、出餐工作站、分店外送設定等新增欄位

- order.feed_rev：API v1 cursor 用的本機異動序號 (與 sync_rev 共用 sync_clock 序號空間)，
  以 sync_rev 回填，串接端已存的整數游標仍有效；索引 ix_order_tenant_feed_rev (tenant_id, feed_rev, id)
- order.promotion_details：結帳時各促銷活動的 JSON 明細 (舊訂單留空，報表改用舊的推算方式)
- order.payment_refs：本訂單歷次產生的綠界特店訂單編號 (逗號分隔)，逾時查詢逐一查詢
- order.paid_at 索引 ix_order_paid_at：「今天的訂單」包含今天才付款的訂單 (PostgreSQL 可用 BitmapOr)
- order_item.prepared_stations：已完成這個明細的工作站 id (JSON 清單，本機資料)
- payment_record.node_uuid：收款的分店 (舊資料留空，報表先看訂單的分店)
- payment_record.request_id / refund.request_id / gift_card.request_id：POS 重送的冪等鍵 (NULL 不參與唯一性)
- marketing_campaign.previous_status：執行前的狀態 (執行中斷後恢復成原本的暫停 / 啟用)
- sync_node.own_delivery_json：分店回報本店生效中的自有外送設定

Revision ID: 0032_local_cols_idempotency
Revises: 0031_widen_cols_tablepay_fk
Create Date: 2026-11-09 10:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0032_local_cols_idempotency'
down_revision = '0031_widen_cols_tablepay_fk'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.add_column(sa.Column('feed_rev', sa.BigInteger(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('promotion_details', sa.Text(), server_default='', nullable=False))
        batch_op.add_column(sa.Column('payment_refs', sa.Text(), server_default='', nullable=False))
    # feed_rev 與 sync_rev 共用同一個序號空間：以 sync_rev 回填，API 串接端已存的游標不會漏掉或重送舊異動
    op.execute('UPDATE "order" SET feed_rev = sync_rev')
    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.create_index('ix_order_tenant_feed_rev', ['tenant_id', 'feed_rev', 'id'], unique=False)
        batch_op.create_index(batch_op.f('ix_order_paid_at'), ['paid_at'], unique=False)

    with op.batch_alter_table('order_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('prepared_stations', sa.Text(), server_default='', nullable=False))

    with op.batch_alter_table('payment_record', schema=None) as batch_op:
        batch_op.add_column(sa.Column('node_uuid', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('request_id', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_payment_record_node_uuid'), ['node_uuid'], unique=False)
        batch_op.create_index('uq_payment_record_tenant_request', ['tenant_id', 'request_id'], unique=True)

    with op.batch_alter_table('refund', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_id', sa.String(length=64), nullable=True))
        batch_op.create_index('uq_refund_tenant_request', ['tenant_id', 'request_id'], unique=True)

    with op.batch_alter_table('gift_card', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_id', sa.String(length=36), nullable=True))
        batch_op.create_index('ix_gift_card_tenant_request_id', ['tenant_id', 'request_id'], unique=True)

    with op.batch_alter_table('marketing_campaign', schema=None) as batch_op:
        batch_op.add_column(sa.Column('previous_status', sa.String(length=10), server_default='', nullable=False))

    with op.batch_alter_table('sync_node', schema=None) as batch_op:
        batch_op.add_column(sa.Column('own_delivery_json', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('sync_node', schema=None) as batch_op:
        batch_op.drop_column('own_delivery_json')

    with op.batch_alter_table('marketing_campaign', schema=None) as batch_op:
        batch_op.drop_column('previous_status')

    with op.batch_alter_table('gift_card', schema=None) as batch_op:
        batch_op.drop_index('ix_gift_card_tenant_request_id')
        batch_op.drop_column('request_id')

    with op.batch_alter_table('refund', schema=None) as batch_op:
        batch_op.drop_index('uq_refund_tenant_request')
        batch_op.drop_column('request_id')

    with op.batch_alter_table('payment_record', schema=None) as batch_op:
        batch_op.drop_index('uq_payment_record_tenant_request')
        batch_op.drop_index(batch_op.f('ix_payment_record_node_uuid'))
        batch_op.drop_column('request_id')
        batch_op.drop_column('node_uuid')

    with op.batch_alter_table('order_item', schema=None) as batch_op:
        batch_op.drop_column('prepared_stations')

    with op.batch_alter_table('order', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_order_paid_at'))
        batch_op.drop_index('ix_order_tenant_feed_rev')
        batch_op.drop_column('payment_refs')
        batch_op.drop_column('promotion_details')
        batch_op.drop_column('feed_rev')
