"""
線上端 (Linux 容器) 的 gunicorn 設定：gunicorn -c deploy/gunicorn.conf.py run:app

重點：
- worker_class = gthread：每個 worker 行程內有一個執行緒池。SSE (/api/orders_stream) 每條長連線
  會一直占住一條執行緒 (與 Waitress 相同，見 app/config.py 的 WAITRESS_THREADS)。
  SSE 連線上限是「每個行程」各自計算 (公開 25 + 員工 60 = 85，見 app/services/event_bus.py)，
  所以每個 worker 的 threads 必須明顯大於 85，否則 SSE 占滿執行緒後一般請求會排隊卡住。
- 多個 worker 時必須設定 REDIS_URL：頻率限制計數與 SSE 訂單通知才會跨行程共用。
- timeout：gthread 的 timeout 是「worker 主迴圈的心跳」逾時，不是單一請求的逾時。
  主迴圈每秒都會回報心跳，與請求執行緒無關，所以長時間的 SSE 串流不會被 timeout 誤殺；
  只有整個 worker 卡死 (例如主迴圈被阻塞) 才會被 master 重啟。
- graceful_timeout：重啟 / 停止時等待既有連線結束的秒數。SSE 連線不會自己結束，
  時間到就直接中斷，瀏覽器的 EventSource 會自動重連，因此不需要設太長。
- 不使用 preload_app：資料庫連線池與 Redis 訂閱執行緒不能跨 fork 共用，每個 worker 各自建立 app。
"""
import os


def _env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return default


# ---- 監聽 ----
bind = os.environ.get('GUNICORN_BIND') or f"0.0.0.0:{os.environ.get('PORT') or '8000'}"

# ---- worker ----
worker_class = 'gthread'
# worker 行程數：一般 2~4 即可 (主要瓶頸在資料庫)；每個 worker 都會建立自己的資料庫連線池
workers = _env_int('GUNICORN_WORKERS', 2)
# 每個 worker 的執行緒數：每條 SSE 長連線占 1 條 (每行程上限 85 條)，請勿低於 100
threads = _env_int('GUNICORN_THREADS', 128, minimum=16)
# 每個 worker 同時保持的連線數上限 (含閒置的 keep-alive 連線)
worker_connections = _env_int('GUNICORN_WORKER_CONNECTIONS', 1000)

# ---- 逾時 ----
# worker 心跳逾時 (秒)；gthread 下與 SSE 串流長度無關 (見檔案開頭說明)
timeout = _env_int('GUNICORN_TIMEOUT', 60)
graceful_timeout = _env_int('GUNICORN_GRACEFUL_TIMEOUT', 20)
# 反向代理 (Caddy) 與 gunicorn 之間的 keep-alive 秒數
keepalive = _env_int('GUNICORN_KEEPALIVE', 5)

# 心跳檔放在記憶體檔案系統，避免容器的 overlay 檔案系統 I/O 延遲讓 worker 被誤判逾時
if os.path.isdir('/dev/shm'):
    worker_tmp_dir = '/dev/shm'

preload_app = False
# 容器內不需要 gunicornc 控制介面 (預設會在家目錄建立 socket)
control_socket_disable = True

# ---- 記錄：全部輸出到 stdout / stderr，由 docker logs 收集 ----
accesslog = os.environ.get('GUNICORN_ACCESSLOG', '-') or None
errorlog = '-'
loglevel = os.environ.get('GUNICORN_LOGLEVEL', 'info')
# 應用程式的 print / logging 也一併輸出
capture_output = True
# %(h)s 是直接連線的位址 (反向代理)；真實來源 IP 由 app 依 TRUSTED_PROXY_COUNT 從 X-Forwarded-For 取得
access_log_format = '%({x-forwarded-for}i)s %(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'

# 來源 IP / HTTPS 判斷交給 app 的 ProxyFix (TRUSTED_PROXY_COUNT)；gunicorn 本身維持預設 (只信任本機)
