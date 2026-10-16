import os
import threading
import redis
from contextlib import contextmanager
from flask_sqlalchemy import SQLAlchemy
from app.config import Config

db = SQLAlchemy()

# 本地備援鎖
_LOCAL_FALLBACK_LOCK = threading.Lock()
LLM_LOCK = threading.Lock()

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
    print("✅ Redis 連線成功，啟用分散式互斥鎖與 Pub/Sub！")
except Exception as e:
    print(f"⚠️ Redis 未連線 ({e})，降級使用本地 Process 執行緒鎖（僅限單行程開發環境）。")
    redis_client = None

@contextmanager
def acquire_distributed_lock(lock_name="lock:order_checkout", timeout=10, blocking_timeout=5):
    """
    分散式互斥鎖 (Distributed Mutex Lock)
    - timeout: 鎖持有最長秒數 (防止伺服器 Crash 導致 Deadlock)
    - blocking_timeout: 搶鎖最大等待時間
    """
    if redis_client:
        lock = redis_client.lock(
            name=lock_name,
            timeout=timeout,
            blocking_timeout=blocking_timeout
        )
        acquired = lock.acquire()
        if not acquired:
            raise TimeoutError(f"取得分散式鎖 [{lock_name}] 逾時，系統繁忙中！")
        try:
            yield lock
        finally:
            try:
                lock.release()
            except redis.exceptions.LockNotOwnedError:
                pass  # 超時自動釋放時避免報錯
    else:
        # 無 Redis 環境時的執行緒降級方案
        acquired = _LOCAL_FALLBACK_LOCK.acquire(timeout=blocking_timeout)
        if not acquired:
            raise TimeoutError(f"取得本地鎖 [{lock_name}] 逾時！")
        try:
            yield None
        finally:
            _LOCAL_FALLBACK_LOCK.release()