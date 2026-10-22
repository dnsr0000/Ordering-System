# 線上端 (SYNC_ROLE=hub) 映像檔：gunicorn (gthread) + PostgreSQL + Redis
# 建置：docker compose build    (完整部署步驟見 DEPLOY.txt)
ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE}

# 程式以「naive 本地時間」計算營業日 (換日結案、今日訂單、取餐號)，時區一定要是台灣時間
ENV TZ=Asia/Taipei \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AUTO_MIGRATE=0 \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 kiosk \
    && useradd --system --uid 10001 --gid kiosk --home-dir /app --no-create-home --shell /usr/sbin/nologin kiosk

WORKDIR /app

# 先只複製 requirements.txt，程式碼變動時可沿用已安裝套件的快取層；測試用套件 (pytest、playwright) 不裝
COPY requirements.txt .
RUN grep -v -E '^(pytest|playwright)([=<>~ ;]|$)' requirements.txt > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY . .

# Windows 取出的檔案可能是 CRLF 換行，shell 腳本需轉成 LF 才能執行；
# database/ 與 uploads/ 會掛 volume，先建好並交給執行身分 (程式碼本身維持 root 擁有、唯讀)
RUN sed -i 's/\r$//' deploy/entrypoint.sh \
    && chmod 755 deploy/entrypoint.sh \
    && mkdir -p database uploads/member uploads/menu models \
    && chown -R kiosk:kiosk database uploads \
    && python -c "import time; assert time.strftime('%z') == '+0800', '時區設定錯誤: ' + time.strftime('%Z %z')" \
    && python -c "import cv2, gunicorn, psycopg, redis"

USER kiosk

EXPOSE 8000

# /healthz 會檢查資料庫連線；第一次啟動要跑資料庫遷移，start-period 給長一點
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request as u; r = u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4); sys.exit(0 if r.status == 200 else 1)"]

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "run:app"]
