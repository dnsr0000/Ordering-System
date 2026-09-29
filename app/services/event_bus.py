import time
import json
import queue
import threading
from datetime import datetime
from flask import current_app, g, has_request_context
from app.extensions import db
from app.models.order import Order

class OrderEventBus:
    def __init__(self, max_subscribers=150):
        self._subscribers = {}
        self._lock = threading.Lock()
        self._max_subscribers = max_subscribers

    def subscribe(self, tenant_id):
        with self._lock:
            if len(self._subscribers) >= self._max_subscribers:
                oldest_q = next(iter(self._subscribers))
                self._subscribers.pop(oldest_q, None)
            q = queue.Queue(maxsize=10)
            self._subscribers[q] = tenant_id
            return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.pop(q, None)

    def get_current_payload(self, tenant_id):
        if tenant_id is None:
            return json.dumps({'timestamp': time.time(), 'state_signature': ''})
        try:
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
            orders_status = db.session.query(Order.id, Order.status).execution_options(
                skip_tenant_filter=True
            ).filter(
                Order.tenant_id == tenant_id,
                Order.created_at >= today_start,
                Order.created_at <= today_end
            ).order_by(Order.id.asc()).all()
            
            state_signature = ",".join(f"{oid}:{status}" for oid, status in orders_status)
            
            return json.dumps({
                'timestamp': time.time(),
                'state_signature': state_signature
            })
        except Exception as e:
            print(f"[!] 取得訂單特徵簽章失敗: {e}")
            return json.dumps({'timestamp': time.time(), 'state_signature': ''})

    def notify(self, app_instance=None):
        app = app_instance or current_app._get_current_object()
        tenant_id = getattr(g, 'tenant_id', None) if has_request_context() else None
        try:
            with app.app_context():
                payload = self.get_current_payload(tenant_id)
        except Exception as e:
            print(f"[!] SSE 廣播打包異常: {e}")
            return

        dead_queues = []
        with self._lock:
            for q, subscriber_tenant_id in list(self._subscribers.items()):
                if subscriber_tenant_id != tenant_id:
                    continue
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except Exception:
                        dead_queues.append(q)
            for dq in dead_queues:
                self._subscribers.pop(dq, None)

order_event_bus = OrderEventBus()