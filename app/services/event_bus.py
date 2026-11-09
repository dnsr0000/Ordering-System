"""
訂單即時通知 (SSE)。訂單建立或狀態變更時通知同店家的看板重新抓取資料。
- 本機 (單一行程)：行程內佇列直接分送。
- 線上 (多個行程)：設定 REDIS_URL 後經由 Redis Pub/Sub 轉送，每個行程再分送給自己的連線。
連線數有配額 (單一 IP / 公開看板 / 員工)，超過時淘汰最舊的連線並送出結束訊號，釋放伺服器執行緒；
瀏覽器的 EventSource 會自動重連。公開 / 員工配額是每個行程所有店家合計，超過時淘汰「連線最多的那家店」最舊的連線
(本店已是最多的就淘汰本店的)，一家店的裝置數量不會把其他店的看板擠掉。
"""
import json
import logging
import queue
import threading
import time
from collections import Counter

logger = logging.getLogger(__name__)
REDIS_CHANNEL = 'kiosk:orders'


class OrderEventBus:
    retry_base = 1.0   # Redis 斷線後第一次重新連線前等待秒數 (之後倍增到 30 秒)
    retry_publish_after = 30.0   # Redis 發布失敗後這段時間內直接只通知本行程 (不讓每次通知都卡在連線逾時)

    def __init__(self, max_public_subscribers=25, max_per_ip=10, max_staff_subscribers=60):
        self._subscribers = {}  # queue -> {'tenant_id', 'ip', 'is_staff'}
        self._lock = threading.Lock()
        self._max_public = max_public_subscribers
        self._max_per_ip = max_per_ip
        self._max_staff = max_staff_subscribers
        self._redis = None
        self._redis_down_until = 0.0   # time.monotonic()：在此之前不嘗試發布到 Redis
        self._listening = False        # 本行程的監聽執行緒目前是否已訂閱 Redis 頻道
        self._listener = None
        self._listener_lock = threading.Lock()

    # ---------------- 設定 ----------------
    def init_app(self, app):
        """
        啟動時連不上 Redis 也保留用戶端 (不永久改為單一行程模式)：暫時只通知本行程，之後的發布每
        retry_publish_after 秒重試一次，監聽執行緒也會自行重新連線 (多 worker 時看板不會無聲地停止更新)。
        """
        url = app.config.get('REDIS_URL')
        if not url:
            return
        try:
            import redis
            self._redis = redis.from_url(url, socket_connect_timeout=2)
        except Exception as e:  # noqa: BLE001  (未安裝 redis 套件或網址格式錯誤)
            logger.warning('Redis 設定無法使用 (%s)，即時通知改用單一行程模式', e)
            self._redis = None
            return
        try:
            self._redis.ping()
        except Exception as e:  # noqa: BLE001
            logger.warning('Redis 暫時無法連線 (%s)，即時通知先只送本行程，稍後自動重試', e)
            self._redis_down_until = time.monotonic() + self.retry_publish_after

    def _ensure_listener(self):
        """
        背景執行緒訂閱 Redis 頻道 (每個行程一條)。兩個連線同時第一次訂閱時只啟動一條 (否則每則通知送兩次)。
        Redis 此時連不上也照常啟動執行緒，由 _listen_forever 自行重新連線 (不可讓這條 SSE 連線因此中斷)。
        """
        if self._redis is None:
            return
        with self._listener_lock:
            if self._listener is not None and self._listener.is_alive():
                return
            pubsub = None
            try:
                pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(REDIS_CHANNEL)   # 先同步訂閱，啟動後立刻發生的通知不漏接
                self._listening = True
            except Exception as e:  # noqa: BLE001
                logger.warning('Redis 訂單通知訂閱失敗 (%s)，背景自動重新連線', e)
                try:
                    if pubsub is not None:
                        pubsub.close()
                except Exception:  # noqa: BLE001
                    pass
                pubsub = None
            self._listener = threading.Thread(target=self._listen_forever, args=(pubsub,),
                                              name='order-event-listener', daemon=True)
            self._listener.start()

    def _listen_forever(self, pubsub):
        """Redis 斷線 / 重啟時 listen() 會拋出例外：重新連線訂閱 (1 → 30 秒退避)，不可讓執行緒就此結束
        (否則 notify 仍發布成功、但本行程再也收不到，POS / 廚房 / 取餐看板停止即時更新直到重開)"""
        delay = self.retry_base
        while True:
            try:
                if pubsub is None:
                    pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
                    pubsub.subscribe(REDIS_CHANNEL)
                    logger.info('Redis 訂單通知已重新連線')
                    delay = self.retry_base
                self._listening = True
                for msg in pubsub.listen():
                    try:
                        data = json.loads(msg['data'])
                        self._fanout(data.get('tenant_id'), msg['data'].decode() if isinstance(msg['data'], bytes) else msg['data'])
                    except Exception:  # noqa: BLE001
                        logger.exception('處理 Redis 訂單通知失敗')
            except Exception as e:  # noqa: BLE001
                logger.warning('Redis 訂單通知中斷 (%s)，%d 秒後重新連線', e, delay)
            self._listening = False
            try:
                if pubsub is not None:
                    pubsub.close()
            except Exception:  # noqa: BLE001
                pass
            pubsub = None
            time.sleep(delay)
            delay = min(delay * 2, 30)

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

    def _evict_fair(self, tenant_id, predicate, limit):
        """
        (需持有 lock) 行程合計配額 (所有店家共用)：超過時淘汰「連線最多的那家店」最舊的連線；
        加上這條新連線後本店已是最多 (或同為最多) 時淘汰本店的，一家店的裝置數量不會擠掉其他店的看板。
        """
        while True:
            matching = [(q, meta['tenant_id']) for q, meta in self._subscribers.items() if predicate(meta)]
            if len(matching) < limit:
                return
            counts = Counter(tid for _, tid in matching)
            victim = max(counts, key=counts.get)   # 同為最多時取最舊連線所屬的店家
            if tenant_id in counts and counts[tenant_id] + 1 >= counts[victim]:
                victim = tenant_id
            self._evict(next(q for q, tid in matching if tid == victim))

    def subscribe(self, tenant_id, client_ip='', is_staff=False):
        ip = str(client_ip or '-')
        with self._lock:
            if is_staff:
                self._evict_fair(tenant_id, lambda m: m['is_staff'], self._max_staff)
            else:
                self._evict_oldest(lambda m: not m['is_staff'] and m['ip'] == ip, self._max_per_ip)
                self._evict_fair(tenant_id, lambda m: not m['is_staff'], self._max_public)
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
        if self._redis is not None and time.monotonic() >= self._redis_down_until:
            try:
                self._redis.publish(REDIS_CHANNEL, payload)
                self._redis_down_until = 0.0
                # 本行程的監聽執行緒還沒重新連上時，其他行程收得到，本行程的連線另外直接分送
                if self._listening or not self._subscribers:
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning('Redis 發布失敗 (%s)，改為只通知本行程 (%d 秒後重試)', e, self.retry_publish_after)
                self._redis_down_until = time.monotonic() + self.retry_publish_after
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
