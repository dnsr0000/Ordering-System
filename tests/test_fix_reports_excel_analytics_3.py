"""修正小組 reports-excel-analytics-3 的回歸測試：即時看板「製作中」與同名分店彙整"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.order import Order
from app.models.sync import SyncNode
from tests.support import get_or_create_tenant, tenant_request


def _order(tid, total, status='Pending', payment_status='', node_uuid=None, n=1):
    return Order(tenant_id=tid, total_price=total, status=status, payment_status=payment_status,
                 payment_method='現金', order_type='內用', pickup_number=n, node_uuid=node_uuid)


def test_live_pending_counts_unpaid_table_orders_like_kitchen(app):
    """B-380：桌邊掃碼餐後結帳 (unpaid) 的單在廚房製作中，也要算進「製作中」；待付款 / 扣款中 / 逾時不算"""
    from app.services import live_dashboard_service
    tid = get_or_create_tenant(app, 'live-pending-shop')
    with tenant_request(app, tid):
        db.session.add_all([
            _order(tid, 100, payment_status='', n=1),              # 已收款、製作中
            _order(tid, 200, payment_status='unpaid', n=2),        # 桌邊掃碼、尚未結帳、製作中
            _order(tid, 300, payment_status='pending', n=3),       # 線上待付款：不送廚房
            _order(tid, 400, payment_status='authorizing', n=4),   # LINE Pay 扣款中：不送廚房
            _order(tid, 500, payment_status='expired', n=5),       # 付款逾時
            _order(tid, 600, status='Completed', n=6),             # 已完成
            _order(tid, 700, status='Cancelled', payment_status='unpaid', n=7),
        ])
        db.session.commit()
        data = live_dashboard_service.snapshot(tid)
    assert data['pending'] == 2
    assert data['revenue'] == 700 and data['orders'] == 2          # 營業額仍只算已收款的單
    local = {s['name']: s for s in data['stores']}['本店']
    assert local['pending'] == 2 and local['orders'] == 2 and local['cancelled'] == 1


def test_live_hub_same_name_nodes_are_not_merged(app, monkeypatch):
    """R2-143：兩家同名分店 (停用舊店後新增同名分店) 的營業額不能全算到第一家"""
    from app.services import live_dashboard_service
    from app.sync import runtime as sync_runtime
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    tid = get_or_create_tenant(app, 'live-samename-shop')
    with tenant_request(app, tid):
        old = SyncNode(tenant_id=tid, name='台北店', token_hash='h-old-live-samename', is_active=False,
                       last_seen_at=datetime.now() - timedelta(days=3))
        new = SyncNode(tenant_id=tid, name='台北店', token_hash='h-new-live-samename', is_active=True,
                       last_seen_at=datetime.now())
        db.session.add_all([old, new])
        db.session.commit()
        assert old.uuid and new.uuid and old.uuid != new.uuid
        db.session.add_all([_order(tid, 200, status='Completed', node_uuid=new.uuid, n=i) for i in range(1, 4)])
        db.session.add(_order(tid, 50, status='Completed', node_uuid=None, n=9))   # 線上端自己收的單
        db.session.commit()
        data = live_dashboard_service.snapshot(tid)
    taipei = [s for s in data['stores'] if s['name'] == '台北店']
    assert len(taipei) == 2
    by_active = {s['active']: s for s in taipei}
    assert by_active[True]['revenue'] == 600 and by_active[True]['orders'] == 3 and by_active[True]['connected']
    assert by_active[False]['revenue'] == 0 and by_active[False]['orders'] == 0
    assert {s['name']: s for s in data['stores']}['線上端']['revenue'] == 50
    assert data['revenue'] == 650


def test_live_avg_ticket_excludes_zero_total_orders(app):
    """R2-144：客單價分母只算實付大於 0 的訂單 (與後台總覽、營運報表一致)，總計與各分店列都一樣"""
    from app.services import live_dashboard_service
    tid = get_or_create_tenant(app, 'live-avgticket-shop')
    with tenant_request(app, tid):
        db.session.add_all([
            _order(tid, 300, status='Completed', n=1),
            _order(tid, 100, status='Completed', n=2),
            _order(tid, 0, status='Completed', n=3),     # 純點數兌換 / 寄杯扣次的 0 元單
        ])
        db.session.commit()
        data = live_dashboard_service.snapshot(tid)
    assert data['orders'] == 3 and data['revenue'] == 400
    assert data['avg_ticket'] == 200
    local = {s['name']: s for s in data['stores']}['本店']
    assert local['orders'] == 3 and local['avg_ticket'] == 200
