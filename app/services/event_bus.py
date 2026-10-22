"""
訂單即時通知 (SSE)。訂單建立或狀態變更時通知同店家的看板重新抓取資料。
- 本機 (單一行程)：行程內佇列直接分送。
- 線上 (多個行程)：設定 REDIS_URL 後經由 Redis Pub/Sub 轉送，每個行程再分送給自己的連線。
連線數有配額 (單一 IP / 公開看板 / 員工)，超過時淘汰最舊的連線並送出結束訊號，釋放伺服器執行緒；
瀏覽器的 EventSource 會自動重連。
"""
import json
import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)
REDIS_CHANNEL = 'kiosk:orders'


class OrderEventBus:
    def __init__(self, max_public_subscribers=25, max_per_ip=10, max_staff_subscribers=60):
        self._subscribers = {}  # queue -> {'tenant_id', 'ip', 'is_staff'}
        self._lock = threading.Lock()
        self._max_public = max_public_subscribers
        self._max_per_ip = max_per_ip
        self._max_staff = max_staff_subscribers
        self._redis = None
        self._listener = None

    # ---------------- 設定 ----------------
    def init_app(self, app):
        url = app.config.get('REDIS_URL')
        if not url:
            return
        try:
            import redis
            self._redis = redis.from_url(url, socket_connect_timeout=2)
            self._redis.ping()
        except Exception as e:  # noqa: BLE001
            logger.warning('Redis 無法連線 (%s)，即時通知改用單一行程模式', e)
            self._redis = None

    def _ensure_listener(self):
        if self._redis is None or self._listener is not None:
            return
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(REDIS_CHANNEL)

        def _run():
            for msg in pubsub.listen():
                try:
                    data = json.loads(msg['data'])
                    self._fanout(data.get('tenant_id'), msg['data'].decode() if isinstance(msg['data'], bytes) else msg['data'])
                except Exception:  # noqa: BLE001
                    logger.exception('處理 Redis 訂單通知失敗')

        self._listener = threading.Thread(target=_run, name='order-event-listener', daemon=True)
        self._listener.start()

    # ---------------- 訂閱 ----------------
    def _evict(self, q):
        """(需持有 lock) 淘汰連線並送出結束訊號 (None)，讓串流迴圈結束"""
        self._subscribers.pop(q, None)
        try:
            q.put_nowait(None)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass

    def _evict_oldest(self, predicate, limit):
        matching = [q for q, meta in self._subscribers.items() if predicate(meta)]
        while len(matching) >= limit:
            self._evict(matching.pop(0))

    def subscribe(self, tenant_id, client_ip='', is_staff=False):
        ip = str(client_ip or '-')
        with self._lock:
            if is_staff:
                self._evict_oldest(lambda m: m['is_staff'], self._max_staff)
            else:
                self._evict_oldest(lambda m: not m['is_staff'] and m['ip'] == ip, self._max_per_ip)
                self._evict_oldest(lambda m: not m['is_staff'], self._max_public)
            q = queue.Queue(maxsize=10)
            self._subscribers[q] = {'tenant_id': tenant_id, 'ip': ip, 'is_staff': bool(is_staff)}
        self._ensure_listener()
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.pop(q, None)

    # ---------------- 通知 ----------------
    @staticmethod
    def make_payload(tenant_id):
        return json.dumps({'type': 'orders_changed', 'tenant_id': tenant_id, 'ts': time.time()})

    def notify(self, tenant_id):
        payload = self.make_payload(tenant_id)
        if self._redis is not None:
            try:
                self._redis.publish(REDIS_CHANNEL, payload)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning('Redis 發布失敗 (%s)，改為只通知本行程', e)
        self._fanout(tenant_id, payload)

    def _fanout(self, tenant_id, payload):
        with self._lock:
            for q, meta in list(self._subscribers.items()):
                if meta['tenant_id'] != tenant_id:
                    continue
                try:
                    q.put_nowait(payload)
                except queue.Full:  # 看板太久沒讀：丟掉最舊的一則再放 (只需要「有變動」這件事)
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except (queue.Empty, queue.Full):
                        self._subscribers.pop(q, None)


order_event_bus = OrderEventBus()
