"""跨檔請求 unowned:app/sync (app/sync/registry.py) 的回歸測試；套用結果一律 rollback，不影響其他測試"""
import uuid as uuidlib
from contextlib import contextmanager
from datetime import datetime

import sqlalchemy as sa

from app.extensions import db
from tests.support import get_or_create_tenant

OLD = datetime(2024, 1, 2, 3, 4, 5)
NEW = datetime(2025, 6, 7, 8, 9, 10)


@contextmanager
def _ctx(app, slug):
    with app.app_context():
        tid = get_or_create_tenant(app, slug)
        try:
            yield tid
        finally:
            db.session.rollback()


def _set_updated_at(model, row_id, when):
    table = model.__table__
    db.session.execute(sa.update(table).where(table.c.id == row_id).values(updated_at=when))
    db.session.commit()


def _row(model, row_id):
    table = model.__table__
    return db.session.execute(sa.select(table).where(table.c.id == row_id)).mappings().one()


def test_local_only_columns_are_not_synced():
    from app.models.order import Order, OrderItem
    from app.models.tenant import StaffAccount
    from app.sync.registry import BY_MODEL, synced_columns
    assert 'totp_last_counter' not in synced_columns(BY_MODEL[StaffAccount])     # B-172
    assert 'feed_rev' not in synced_columns(BY_MODEL[Order])                     # R2-182
    assert 'prepared_stations' not in synced_columns(BY_MODEL[OrderItem])        # R2-015
    assert 'prepared_at' not in synced_columns(BY_MODEL[OrderItem])


def test_pos_request_ids_and_order_payment_refs_are_local_only():
    from app.models.member import GiftCard
    from app.models.order import Order
    from app.models.payment import PaymentRecord, Refund
    from app.sync.registry import BY_MODEL, synced_columns
    assert 'request_id' not in synced_columns(BY_MODEL[GiftCard])        # R2-029
    assert 'request_id' not in synced_columns(BY_MODEL[PaymentRecord])   # B-230
    assert 'request_id' not in synced_columns(BY_MODEL[Refund])          # B-231
    assert 'payment_refs' in BY_MODEL[Order].local_only                  # B-047
    assert 'payment_refs' not in synced_columns(BY_MODEL[Order])
    assert 'node_uuid' in synced_columns(BY_MODEL[PaymentRecord])        # B-386：收款分店要上傳


def test_b172_totp_login_keeps_staff_updated_at_and_sync_rev(app):
    from app.models.tenant import StaffAccount
    with _ctx(app, 'fix-unowned-sync-b172') as tid:
        staff = StaffAccount(tenant_id=tid, username=f'totp-{uuidlib.uuid4().hex[:8]}', role='staff', is_active=True)
        staff.set_password('StaffPass!2026')
        db.session.add(staff)
        db.session.commit()
        _set_updated_at(StaffAccount, staff.id, OLD)
        before = _row(StaffAccount, staff.id)
        db.session.expire_all()
        staff = db.session.get(StaffAccount, staff.id)
        staff.totp_last_counter = 123456789   # 兩步驟驗證登入寫入的防重放序號
        db.session.commit()
        after = _row(StaffAccount, staff.id)
        assert after['totp_last_counter'] == 123456789
        assert after['updated_at'] == OLD               # 不讓整列在 LWW 變新
        assert after['sync_rev'] == before['sync_rev']  # 也不標記上傳


def test_b472_hub_applies_newer_feedback_reply_and_public(app):
    from app.models.order import Order, OrderFeedback
    from app.sync.engine import Applier, encode_rows
    from app.sync.registry import BY_MODEL
    with _ctx(app, 'fix-unowned-sync-b472') as tid:
        order = Order(tenant_id=tid, total_price=100, node_uuid='node-a')
        db.session.add(order)
        db.session.commit()
        fb = OrderFeedback(tenant_id=tid, order_id=order.id, rating=5, comment='好吃')
        db.session.add(fb)
        db.session.commit()
        _set_updated_at(OrderFeedback, fb.id, OLD)
        incoming = encode_rows(BY_MODEL[OrderFeedback], [_row(OrderFeedback, fb.id)])[0]
        incoming.update(reply='謝謝光臨', replied_at=NEW.isoformat(), is_public=True, updated_at=NEW.isoformat())
        Applier(tid, 'hub', source_node_uuid='node-a').apply({'order_feedback': [incoming]})
        row = _row(OrderFeedback, fb.id)
        assert (row['reply'], bool(row['is_public'])) == ('謝謝光臨', True)
        assert row['replied_at'] == NEW
