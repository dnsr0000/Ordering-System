"""
系統監控：
- 事件紀錄：錯誤與警示寫入 database/system_events.jsonl (後台「系統監控」頁顯示最近的紀錄)。
- 通知：Webhook (Slack / Discord / Teams)、Email、LINE；同一個問題 10 分鐘內只通知一次。
  通知在背景排程執行緒送出 (不拖慢請求、不在記錄錯誤時又引發錯誤)。
- 時鐘校驗：以 SNTP 查詢校時伺服器，計算本機時鐘誤差。
"""
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from datetime import datetime

import requests

from app.errors import AppError
from app.services import settings_service

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 * 1024 * 1024
KEEP_LINES = 1500
THROTTLE_SECONDS = 600
_lock = threading.Lock()
_last_sent = {}
_alert_queue = queue.Queue(maxsize=500)
_guard = threading.local()
_clock_cache = {'at': 0.0, 'offset': None, 'server': '', 'error': ''}


def _events_path():
    from flask import current_app, has_app_context
    base = current_app.config['DATABASE_DIR'] if has_app_context() else os.path.join(os.getcwd(), 'database')
    return os.path.join(base, 'system_events.jsonl')


def record_event(level, message, source='', path=None):
    """寫入一筆事件 (檔案超過上限時保留最後 KEEP_LINES 行)"""
    entry = {'at': datetime.now().isoformat(timespec='seconds'), 'level': level, 'source': source,
             'message': str(message)[:2000]}
    try:
        path = path or _events_path()
        with _lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
            if os.path.getsize(path) > MAX_FILE_BYTES:
                with open(path, encoding='utf-8') as fh:
                    lines = fh.readlines()[-KEEP_LINES:]
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.writelines(lines)
    except OSError:
        pass
    return entry


def recent_events(limit=100, level=None):
    try:
        with open(_events_path(), encoding='utf-8') as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for raw in reversed(lines):
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if level and item.get('level') != level:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def alert(message, key=None, tenant_id=None, level='error', source='system'):
    """記錄並排入通知佇列 (同一 key 10 分鐘內只通知一次)"""
    record_event(level, message, source)
    key = key or message[:80]
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < THROTTLE_SECONDS:
            return False
        _last_sent[key] = now
    try:
        _alert_queue.put_nowait((tenant_id, level, message))
    except queue.Full:
        return False
    return True


def _default_tenant_id():
    from app.models.tenant import Tenant
    t = Tenant.query.filter_by(slug='default').first()
    return t.id if t else None


def send_alert_now(tenant_id, level, message):
    """實際送出通知 (需 app context)；回傳送出的管道清單"""
    tenant_id = tenant_id or _default_tenant_id()
    if tenant_id is None:
        return []
    cfg = settings_service.get('monitor', tenant_id)
    text = f"[點餐系統{'錯誤' if level == 'error' else '通知'}] {message}"
    sent = []
    if cfg.get('webhook_url'):
        from app.services.webhook_service import check_public
        try:
            check_public(cfg['webhook_url'])   # 不可指向內網 / 本機 (與 API Webhook 相同；WEBHOOK_ALLOW_PRIVATE=1 放寬)
            requests.post(cfg['webhook_url'], json={'text': text, 'content': text[:1900]}, timeout=10, allow_redirects=False)
            sent.append('webhook')
        except (requests.RequestException, AppError) as exc:
            logger.warning('Webhook 通知失敗：%s', getattr(exc, 'message', exc))
    if cfg.get('email_recipients'):
        from app.services import mail_service
        try:
            mail_service.send_mail(tenant_id, cfg['email_recipients'], '點餐系統通知', text)
            sent.append('email')
        except AppError as exc:
            logger.warning('Email 通知失敗：%s', exc.message)
    if cfg.get('line_alerts'):
        from app.services import line_service
        ids = settings_service.split_lines(settings_service.get('line', tenant_id).get('admin_user_ids'))
        if any(line_service.push_text(tenant_id, uid, text) for uid in ids):
            sent.append('line')
    return sent


def flush_alerts(max_items=50):
    """送出佇列中的通知 (背景排程呼叫；測試可直接呼叫)"""
    count = 0
    while count < max_items:
        try:
            tenant_id, level, message = _alert_queue.get_nowait()
        except queue.Empty:
            break
        try:
            send_alert_now(tenant_id, level, message)
        except Exception:  # noqa: BLE001
            logger.warning('送出通知失敗', exc_info=True)
        count += 1
    return count


class AlertLogHandler(logging.Handler):
    """ERROR 以上的 log 記入事件檔並排入通知 (避免在處理通知時再次觸發)"""

    def emit(self, record):
        if getattr(_guard, 'busy', False) or record.name.startswith(('werkzeug', 'urllib3')):
            return
        _guard.busy = True
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                msg = f'{msg} ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})'
            alert(msg, key=f'{record.name}:{record.getMessage()[:80]}', source=record.name)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _guard.busy = False


def install_log_handler(app):
    root = logging.getLogger()
    if not any(isinstance(h, AlertLogHandler) for h in root.handlers):
        handler = AlertLogHandler(level=logging.ERROR)
        root.addHandler(handler)


# ---------------------------------------------------------------------------
# 時鐘校驗 (SNTP)
# ---------------------------------------------------------------------------
NTP_EPOCH_DELTA = 2208988800


def query_ntp(server, timeout=3):
    """回傳 (本機時鐘誤差秒數, 往返延遲)；正值代表本機比標準時間快"""
    from app.services.webhook_service import check_public_host_on_hub
    check_public_host_on_hub(server)
    packet = b'\x1b' + 47 * b'\0'
    addr = socket.getaddrinfo(server, 123, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t1 = time.time()
        sock.sendto(packet, addr)
        data, _ = sock.recvfrom(1024)
        t4 = time.time()
    if len(data) < 48:
        raise OSError('NTP 回應格式錯誤')
    recv_sec, recv_frac, tx_sec, tx_frac = struct.unpack('!II', data[32:40]) + struct.unpack('!II', data[40:48])
    t2 = recv_sec - NTP_EPOCH_DELTA + recv_frac / 2 ** 32
    t3 = tx_sec - NTP_EPOCH_DELTA + tx_frac / 2 ** 32
    offset = ((t2 - t1) + (t3 - t4)) / 2      # 標準時間 - 本機時間
    return -offset, (t4 - t1) - (t3 - t2)


def clock_status(tenant_id=None, max_age=600, force=False):
    """{offset, server, error, warn}：offset 為本機比標準時間快幾秒 (快取 10 分鐘)"""
    cfg = settings_service.get('monitor', tenant_id)
    server = cfg.get('clock_server') or 'time.stdtime.gov.tw'
    if force or time.monotonic() - _clock_cache['at'] > max_age or _clock_cache['server'] != server:
        try:
            offset, _delay = query_ntp(server)
            _clock_cache.update(offset=round(offset, 2), error='')
        except (OSError, AppError) as exc:
            _clock_cache.update(offset=None, error=f'無法連線校時伺服器：{getattr(exc, "message", exc)}')
        _clock_cache.update(at=time.monotonic(), server=server)
    offset = _clock_cache['offset']
    warn = offset is not None and abs(offset) > (cfg.get('clock_warn_seconds') or 60)
    return {'offset': offset, 'server': server, 'error': _clock_cache['error'], 'warn': warn}


@settings_service.register_tester('monitor')
def _test(cfg):
    from flask import g
    sent = send_alert_now(g.tenant_id, 'info', '這是一則測試通知，收到代表監控通知設定正確。')
    status = clock_status(g.tenant_id, force=True)
    clock = (f"時鐘誤差 {status['offset']} 秒" if status['offset'] is not None else status['error'])
    if not sent:
        return f'沒有設定任何通知管道 (Webhook / Email / LINE)；{clock}'
    return f"已送出測試通知：{', '.join(sent)}；{clock}"
