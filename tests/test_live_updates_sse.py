"""即時更新 (SSE)：連線配額與淘汰、事件串流、取餐看板遮蔽會員姓名"""

import pytest

from app.extensions import db
from app.models.order import Order
from app.services.event_bus import order_event_bus
from tests.support import (
    default_tenant_id,
    make_member,
)


@pytest.fixture
def clean_bus():
    with order_event_bus._lock:
        order_event_bus._subscribers.clear()
    yield order_event_bus
    with order_event_bus._lock:
        order_event_bus._subscribers.clear()


def test_sse_quotas(app, clean_bus):
    """SSE 配額：單一 IP 10 條、公開看板 25 條、員工 60 條 (超過時淘汰最舊連線)"""
    for _ in range(15):
        clean_bus.subscribe(1, client_ip='198.51.100.50')
    assert len(clean_bus._subscribers) == 10
    clean_bus._subscribers.clear()
    for i in range(35):
        clean_bus.subscribe(1, client_ip=f'198.51.100.{i + 1}')
    assert len(clean_bus._subscribers) == 25
    clean_bus._subscribers.clear()
    for _ in range(70):
        clean_bus.subscribe(1, client_ip='192.168.1.10', is_staff=True)
    assert len(clean_bus._subscribers) == 60


def test_sse_stream(app):
    with order_event_bus._lock:
        order_event_bus._subscribers.clear()
    c = app.test_client()
    c.get('/?shop=default')
    resp = c.get('/api/orders_stream', buffered=False)
    try:
        assert resp.status_code == 200 and resp.mimetype == 'text/event-stream'
        assert 'no-cache' in resp.headers['Cache-Control']
        first = next(iter(resp.response))
        first = first.decode() if isinstance(first, bytes) else first
        assert first.startswith('data: ') and first.endswith('\n\n') and 'orders_changed' in first
        assert len(order_event_bus._subscribers) == 1
    finally:
        resp.close()
    assert len(order_event_bus._subscribers) == 0


def test_counter_board_masks_member_names(app):
    tid = default_tenant_id(app)
    uid = make_member(app, '陳大文', '0933150006')
    with app.app_context():
        db.session.add_all([Order(tenant_id=tid, user_id=uid, table_number='陳大文', total_price=10, status='Pending', pickup_number=77),
                            Order(tenant_id=tid, table_number='訪客-1234', total_price=10, status='Pending', pickup_number=78)])
        db.session.commit()
    board = app.test_client().get('/api/counter_orders').get_json()
    names = [o['user_name'] for o in board['preparing_orders'] + board['ready_orders']]
    assert '陳大文' not in names and '陳○文' in names and '訪客-1234' in names


def test_evicted_sse_connections_get_close_signal(app):
    from app.services.event_bus import order_event_bus
    with order_event_bus._lock:
        order_event_bus._subscribers.clear()
    queues = [order_event_bus.subscribe(1, client_ip='198.51.100.151') for _ in range(13)]
    assert all(q.get_nowait() is None for q in queues[:3])
    with order_event_bus._lock:
        order_event_bus._subscribers.clear()
