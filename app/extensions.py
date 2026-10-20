import threading
import redis
from contextlib import contextmanager
from flask_sqlalchemy import SQLAlchemy
from app.config import Config

db = SQLAlchemy()

# 本地備援鎖：依鎖名稱各自獨立 (不同店家/不同用途互不阻塞)；注意為非可重入鎖，同一執行緒不可巢狀取得同名鎖
_LOCAL_LOCKS = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
LLM_LOCK = threading.Lock()


def _get_local_lock(name):
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(name)
        if lock is None:
            lock = _LOCAL_LOCKS[name] = threading.Lock()
        return lock

# 初始化 Redis Client
redis_client = None
try:
    redis_client = redis.from_url(
        Config.REDIS_URL, 
        decode_responses=True,
        protocol=2,
        socket_connect_timeout=2,
        socket_timeout=5
    )
    redis_client.ping()
    print("✅ Redis 連線成功，啟用分散式互斥鎖！")
except Exception as e:
    print(f"⚠️ Redis 未連線 ({e})，降級使用本地 Process 執行緒鎖（僅限單行程開發環境）。")
    redis_client = None

@contextmanager
def acquire_distributed_lock(lock_name="lock:order_checkout", timeout=10, blocking_timeout=5):
    """
    分散式互斥鎖 (Distributed Mutex Lock)
    - timeout: 鎖持有最長秒數 (防止伺服器 Crash 導致 Deadlock)
    - blocking_timeout: 搶鎖最大等待時間
    Redis 連線中斷 (啟動時連得上、執行中掛掉) 時，自動降級為「依名稱區分的本地執行緒鎖」，
    不會讓所有結帳因 Redis 異常而全部失敗。
    """
    redis_lock = None
    if redis_client:
        try:
            redis_lock = redis_client.lock(name=lock_name, timeout=timeout, blocking_timeout=blocking_timeout)
            if not redis_lock.acquire():
                raise TimeoutError(f"取得分散式鎖 [{lock_name}] 逾時，系統繁忙中！")
        except redis.exceptions.RedisError as e:
            print(f"[!] Redis 鎖異常 ({e})，本次降級使用本地執行緒鎖。")
            redis_lock = None

    if redis_lock is not None:
        try:
            yield redis_lock
        finally:
            try:
                redis_lock.release()
            except redis.exceptions.RedisError:
                pass  # 鎖已逾時自動釋放 / Redis 暫時失聯：忽略，避免蓋掉原本的結果或例外
        return

    local_lock = _get_local_lock(lock_name)
    if not local_lock.acquire(timeout=blocking_timeout):
        raise TimeoutError(f"取得本地鎖 [{lock_name}] 逾時！")
    try:
        yield None
    finally:
        local_lock.release()
