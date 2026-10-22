#!/bin/sh
# 線上端容器啟動流程：
#   1. flask --app run setup：升級資料庫結構並建立預設資料 (可重複執行；已是最新版時不做任何事)
#      只在這裡執行一次，worker 行程一律 AUTO_MIGRATE=0，避免多個 worker 同時遷移資料庫。
#   2. exec gunicorn：取代 shell 成為 PID 1，docker stop 的 SIGTERM 會直接送到 gunicorn 優雅關閉。
# 傳入其他指令時直接執行該指令 (例如 docker compose run --rm app flask --app run db current)。
set -eu

export AUTO_MIGRATE=0

if [ "$#" -gt 0 ] && [ "$1" != "gunicorn" ]; then
    exec "$@"
fi

# 多個 worker 必須共用同一把 Session 簽章金鑰，線上端一律要求明確設定
if [ -z "${SECRET_KEY:-}" ]; then
    echo "未設定 SECRET_KEY：請在 .env.hub 設定一組隨機值 (例如 openssl rand -hex 32) 後再啟動。" >&2
    exit 1
fi

# 資料庫容器剛啟動時可能還無法連線：最多重試 30 次 (每次間隔 2 秒)
tries=0
until flask --app run setup; do
    tries=$((tries + 1))
    if [ "$tries" -ge 30 ]; then
        echo "資料庫升級失敗 (已重試 ${tries} 次)，停止啟動。" >&2
        exit 1
    fi
    echo "資料庫升級失敗，2 秒後重試 (${tries}/30)..." >&2
    sleep 2
done

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec gunicorn -c deploy/gunicorn.conf.py run:app
