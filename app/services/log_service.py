"""
記錄檔與請求識別碼 (第五輪)：
- 檔案記錄：LOG_FILE (預設 database/logs/app.log) 以 RotatingFileHandler 輪替 (LOG_MAX_BYTES × LOG_BACKUP_COUNT)，
  不會像 `>> server.log` 那樣無限成長；設 LOG_FILE= (空字串) 只輸出到主控台 (Docker 由 json-file 驅動程式輪替)。
  有檔案記錄時主控台只輸出 WARNING 以上 (門市的 start_store.cmd 仍把主控台導到 database/server.log，只留啟動訊息與錯誤)。
- 請求識別碼：每個請求產生 g.request_id (或沿用反向代理送來的 X-Request-ID)，回應標頭帶回 X-Request-ID；
  每一行記錄都有 [rid]，500 頁面 / JSON 也顯示，顧客回報錯誤時可以直接在記錄檔裡找到那一次請求。
"""
import logging
import os
import re
import sys
import uuid
from logging.handlers import RotatingFileHandler

from flask import Flask, g, has_request_context, request

FORMAT = '%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s'
RID_RE = re.compile(r'^[A-Za-z0-9._-]{4,64}$')
HEADER = 'X-Request-ID'


class RequestIdFilter(logging.Filter):
    """把目前請求的識別碼放進每一筆記錄 (背景執行緒 / 排程為 '-')"""

    def filter(self, record):
        record.request_id = current_request_id() or '-'
        return True


def current_request_id() -> str:
    if not has_request_context():
        return ''
    return getattr(g, 'request_id', '') or ''


def new_request_id() -> str:
    incoming = (request.headers.get(HEADER) or '').strip()
    if incoming and RID_RE.match(incoming):
        return incoming
    return uuid.uuid4().hex[:12]


def configure_logging(app: Flask) -> None:
    """create_app 呼叫：設定根記錄器 (只做一次)；請求識別碼的 before / after_request"""
    root = logging.getLogger()
    rid_filter = RequestIdFilter()
    log_file = (app.config.get('LOG_FILE') or '').strip()
    file_on = bool(log_file) and not app.config.get('TESTING')
    if not any(getattr(h, '_kiosk_console', False) for h in root.handlers):
        root.setLevel(logging.INFO)
        for h in list(root.handlers):   # 先前 basicConfig 的主控台 handler 換成帶 request_id 的格式 (其他的不動，例如 pytest 的擷取)
            if type(h) is logging.StreamHandler and getattr(h, 'stream', None) in (sys.stderr, sys.stdout):
                root.removeHandler(h)
        console = logging.StreamHandler()
        console._kiosk_console = True  # type: ignore[attr-defined]
        console.setFormatter(logging.Formatter(FORMAT))
        console.addFilter(rid_filter)
        console.setLevel(logging.WARNING if file_on else logging.INFO)
        root.addHandler(console)
    if file_on:   # 有檔案記錄時主控台只留警告以上 (同一個行程先建立過測試 app 時，主控台 handler 已存在)
        for h in root.handlers:
            if getattr(h, '_kiosk_console', False):
                h.setLevel(logging.WARNING)
    if file_on and not any(isinstance(h, RotatingFileHandler) and getattr(h, '_kiosk_file', False) for h in root.handlers):
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=int(app.config.get('LOG_MAX_BYTES') or 10 * 1024 * 1024),
                                      backupCount=int(app.config.get('LOG_BACKUP_COUNT') or 5), encoding='utf-8')
        handler._kiosk_file = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.addFilter(rid_filter)
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
        logging.getLogger(__name__).info('記錄檔：%s (每檔 %d MB，保留 %d 個)', log_file,
                                         handler.maxBytes // (1024 * 1024), handler.backupCount)

    @app.before_request
    def _assign_request_id():
        g.request_id = new_request_id()

    @app.after_request
    def _echo_request_id(resp):
        rid = current_request_id()
        if rid:
            resp.headers.setdefault(HEADER, rid)
        return resp
