import time
import json
import queue
import threading
from datetime import datetime
from flask import current_app, g, has_request_context
from app.extensions import db, redis_client
from app.models.order import Order


class OrderEventBus:
    def __init__(self, max_public_subscribers=25, max_per_ip=10, max_staff_subscribers=60):
        self._subscribers = {}  # q -> {'tenant_id': int, 'ip': str, 'is_staff': bool}
        self._lock = threading.Lock()
        self._max_public_subscribers = max_public_subscribers
        self._max_per_ip = max_per_ip
        self._max_staff_subscribers = max_staff_subscribers
        self._max_subscribers = max_public_subscribers + max_staff_subscribers

    def subscribe(self, tenant_id, client_ip="", is_staff=False):
        ip_str = str(client_ip or "127.0.0.1").strip()
        staff_flag = bool(is_staff)
        with self._lock:
            if staff_flag:
                staff_qs = [q for q, meta in self._subscribers.items() if isinstance(meta, dict) and meta.get('is_staff')]
                while len(staff_qs) >= self._max_staff_subscribers:
                    oldest_q = staff_qs.pop(0)
                    self._subscribers.pop(oldest_q, None)
            else:
                # 1. 單一 IP 公開看板連線上限 (預設 10 條)
                same_ip_qs = [
                    q for q, meta in self._subscribers.items()
                    if isinstance(meta, dict) and not meta.get('is_staff') and meta.get('ip') == ip_str
                ]
                while len(same_ip_qs) >= self._max_per_ip:
                    oldest_ip_q = same_ip_qs.pop(0)
                    self._subscribers.pop(oldest_ip_q, None)

                # 2. 公開看板總連線上限 (預設 25 條)
                pub_qs = [q for q, meta in self._subscribers.items() if not isinstance(meta, dict) or not meta.get('is_staff')]
                while len(pub_qs) >= self._max_public_subscribers:
                    oldest_pub_q = pub_qs.pop(0)
                    self._subscribers.pop(oldest_pub_q, None)

            q = queue.Queue(maxsize=10)
            self._subscribers[q] = {
                'tenant_id': tenant_id,
                'ip': ip_str,
                'is_staff': staff_flag,
            }
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

    def notify(self, app_instance=None, tenant_id=None):
        try:
            app = app_instance or current_app._get_current_object()
        except Exception:
            app = None
        resolved_tid = tenant_id if tenant_id is not None else (getattr(g, 'tenant_id', None) if has_request_context() else None)
        payload = json.dumps({'timestamp': time.time(), 'state_signature': ''})
        if app is not None:
            try:
                with app.app_context():
                    payload = self.get_current_payload(resolved_tid)
            except Exception as e:
                print(f"[!] SSE 廣播打包異常: {e}")

        if redis_client and resolved_tid is not None:
            try:
                redis_client.publish(f"kiosk:orders_stream:{resolved_tid}", payload)
            except Exception:
                pass

        dead_queues = []
        with self._lock:
            for q, meta in list(self._subscribers.items()):
                sub_tid = meta.get('tenant_id') if isinstance(meta, dict) else meta
                if resolved_tid is not None and sub_tid != resolved_tid:
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
