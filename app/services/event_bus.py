import time
import json
import queue
import threading
from datetime import datetime
from flask import current_app
from app.extensions import db
from app.models.order import Order

class OrderEventBus:
    def __init__(self, max_subscribers=150):
        self._subscribers = set()
        self._lock = threading.Lock()
        self._max_subscribers = max_subscribers

    def subscribe(self):
        with self._lock:
            if len(self._subscribers) >= self._max_subscribers:
                oldest_q = next(iter(self._subscribers))
                self._subscribers.remove(oldest_q)
            q = queue.Queue(maxsize=10)
            self._subscribers.add(q)
            return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    def get_current_payload(self):
        try:
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
            orders_status = db.session.query(Order.id, Order.status).filter(
                Order.created_at >= today_start,
                Order.created_at <= today_end
            ).order_by(Order.id.asc()).all()
            
            state_signature = ",".join(f"{oid}:{status}" for oid, status in orders_status)
            pending_count = sum(1 for _, s in orders_status if s == 'Pending')
            
            return json.dumps({
                'timestamp': time.time(),
                'state_signature': state_signature,
                'pending_count': pending_count
            })
        except Exception as e:
            return json.dumps({'timestamp': time.time(), 'state_signature': '', 'pending_count': 0})

    def notify(self, app_instance=None):
        app = app_instance or current_app._get_current_object()
        try:
            with app.app_context():
                payload = self.get_current_payload()
        except Exception as e:
            print(f"[!] SSE 廣播打包異常: {e}")
            return

        dead_queues = []
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except Exception:
                        dead_queues.append(q)
            for dq in dead_queues:
                self._subscribers.discard(dq)

order_event_bus = OrderEventBus()