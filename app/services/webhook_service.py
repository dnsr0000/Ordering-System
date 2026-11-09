"""
Webhook (#14)：訂單事件即時通知外部系統 (ERP、會計、自建 App…)。

- 背景排程每 15 秒掃描近 2 天的訂單，與上次記下的狀態 (webhook_order_state) 比對，產生
  order.created / order.paid / order.status_changed / order.cancelled 事件 (不論訂單從點餐機、POS、桌邊、線上或同步而來)。
  端點建立前的舊訂單不送 order.created。
- 以 POST JSON 送出，標頭 X-Kiosk-Event / X-Kiosk-Delivery / X-Kiosk-Timestamp / X-Kiosk-Signature
  (sha256=HMAC-SHA256(secret, "時間戳.本文"))，接收端以 secret 驗證、並以 delivery id 去除重複。
- 2xx 視為成功；失敗依 1、5、15、60、180 分鐘重試，共 6 次後放棄並通知；連續失敗 50 次自動停用端點。
- 只允許 https，且只送到公開網際網路位址 (避免 SSRF)：送出前解析網址並檢查所有 IP，連線時固定使用檢查過的 IP
  (TLS 憑證仍驗證原本的主機名稱，DNS 在檢查與連線之間被換成內網位址也無效)。自架內網整合才設 WEBHOOK_ALLOW_PRIVATE=1。
- 多個行程同時執行時，每筆送出先以條件式更新改為 sending 再送 (不會重複送出)。
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time as _time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

import certifi
import requests
import urllib3
from flask import current_app

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.integration import WEBHOOK_EVENTS, WebhookDelivery, WebhookEndpoint, WebhookOrderState
from app.models.order import Order

logger = logging.getLogger(__name__)
SCAN_DAYS = 2
BACKOFF_MINUTES = (1, 5, 15, 60, 180)
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
DISABLE_AFTER = 50
PAID = ('', 'paid', 'settled', 'refund_required', 'refunded')
PAID_AT_CHECKOUT = ('', 'paid', 'settled')
REFUND = ('refund_required', 'refunded')
# 尚未成立 (線上待付款、LINE Pay 扣款中、逾時未付)：不送任何事件，付款完成時才送 order.created + order.paid
HIDDEN = ('pending', 'authorizing', 'expired')


# ---------------------------------------------------------------------------
# 端點設定
# ---------------------------------------------------------------------------
def validate_url(url):
    url = str(url or '').strip()
    parsed = urlparse(url)
    allow_http = current_app.config.get('WEBHOOK_ALLOW_HTTP')
    if parsed.scheme not in (('https', 'http') if allow_http else ('https',)) or not parsed.hostname:
        raise AppError('Webhook 網址必須是 https:// 開頭的完整網址')
    if any(c == '\\' or c.isspace() or ord(c) < 32 or ord(c) == 127 for c in parsed.netloc):
        raise AppError('Webhook 網址的主機名稱含不允許的字元')   # 反斜線等：各套件解析出的主機不同
    try:
        parsed.port   # 埠號超出範圍 / 不是數字時拋 ValueError
        parsed.hostname.encode('idna')   # 空標籤 (a..b)、超過 63 字的標籤拋 UnicodeError
    except ValueError:
        raise AppError('Webhook 網址的主機名稱或埠號不正確')
    if len(url) > 300:
        raise AppError('Webhook 網址太長')
    return url


def resolve_ips(host):
    """主機名稱解析出的所有 IP (保留 getaddrinfo 的順序、去除重複)"""
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, None)))


def connect_host(url):
    """
    requests / urllib3 實際會連線的主機名稱。不可用 urlparse 判斷：兩者對反斜線等字元的解析不同，
    例如 http://127.0.0.1:8080\\@example.com/ 的 urlparse 主機是 example.com，requests 卻連到 127.0.0.1。
    """
    try:
        host = urllib3.util.parse_url(str(url or '').strip()).host or ''
    except ValueError:
        raise AppError('網址格式不正確')
    return host[1:-1] if host.startswith('[') and host.endswith(']') else host


def check_public(url):
    """送出前檢查目標的所有 IP 都是公開網際網路位址；回傳連線要固定使用的 IP (允許內網時為 None)"""
    if current_app.config.get('WEBHOOK_ALLOW_PRIVATE'):
        return None
    return check_public_host(connect_host(url))


def check_public_all(url):
    """同 check_public，但回傳全部檢查過的 IP (IPv4 優先)，連線失敗時可改試下一個"""
    if current_app.config.get('WEBHOOK_ALLOW_PRIVATE'):
        return None
    return public_ips(connect_host(url))


def public_ips(host):
    """主機名稱 / IP 必須解析到公開網際網路位址，否則拋出 AppError；回傳全部 IP (IPv4 優先，其餘依解析順序)"""
    try:
        ips = list(resolve_ips(host))
    except (OSError, ValueError) as exc:   # 空標籤、超長標籤的主機名稱拋 UnicodeError (屬於 ValueError)
        raise AppError(f'無法解析主機 {host}：{exc}')
    if not ips:
        raise AppError(f'無法解析主機 {host}')
    for ip in ips:
        addr = ipaddress.ip_address(ip.split('%')[0])
        if not addr.is_global or addr.is_multicast:   # 內網、本機、保留、共用 (100.64/10)、雲端中繼資料位址
            raise AppError(f'不可送到內網或本機位址 ({host} → {ip})')
    # IPv4 優先：送出端常沒有 IPv6 對外連線 (Docker 預設網路、部分門市網路)，雙協定目標先連 IPv6 會一直失敗
    return sorted(ips, key=lambda ip: ':' in ip)


def check_public_host(host):
    """
    主機名稱 / IP 必須解析到公開網際網路位址，否則拋出 AppError；回傳優先連線的 IP (IPv4 優先)。
    店家可自行設定的對外連線 (監控 Webhook、線上端的寄信伺服器 / 校時伺服器 / 網路印表機) 都經過這裡，
    避免線上端上任一店家負責人拿來連線上端的內網服務 (資料庫、Redis、雲端中繼資料位址)。
    """
    return public_ips(host)[0]


def check_public_host_on_hub(host):
    """線上端 (多店家共用主機) 才限制為公開位址；門市主機連自己區網內的印表機 / 寄信伺服器是正常的"""
    from app.sync import runtime as sync_runtime
    if sync_runtime.is_hub():
        check_public_host(host)


def save_endpoint(ep, form, staff=''):
    name = str(form.get('name') or '').strip()[:60]
    if not name:
        raise AppError('請輸入名稱')
    events = [e for e in (form.getlist('events') if hasattr(form, 'getlist') else form.get('events') or [])
              if e in WEBHOOK_EVENTS]
    if not events:
        raise AppError('請至少選擇一種事件')
    ep.name, ep.url, ep.events = name, validate_url(form.get('url')), ','.join(events)
    ep.created_by = ep.created_by or staff
    return ep


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------
def order_payload(o):
    return {
        'uuid': o.uuid, 'id': o.id, 'pickup_number': o.pickup_number, 'status': o.status,
        'payment_status': o.payment_status or 'paid', 'payment_method': o.payment_method, 'order_type': o.order_type,
        'order_source': o.order_source, 'total_price': o.total_price, 'subtotal': o.subtotal,
        'discount_amount': o.discount_amount or 0, 'note': o.note or '', 'table': o.table_number or '',
        'node_uuid': o.node_uuid or '', 'created_at': o.created_at.isoformat(timespec='seconds') if o.created_at else '',
        'items': [{'name': i.item_name, 'quantity': i.quantity, 'price': i.price, 'customization': i.customization or '',
                   'voided_qty': i.voided_qty or 0} for i in o.items],
    }


def _enqueue(endpoints, event, order, now, first_seen=False):
    for ep in endpoints:
        if event not in ep.event_list:
            continue
        # 端點建立前的舊訂單不送 order.created；第一次看到的舊訂單 (例如剛新增第一個端點) 其他事件也不送，
        # 否則接收端會收到從沒見過的訂單的付款 / 取消事件
        if (event == 'order.created' or first_seen) and order.created_at and order.created_at < ep.created_at:
            continue
        body = {'id': uuid.uuid4().hex, 'event': event, 'created_at': now.isoformat(timespec='seconds'),
                'data': order_payload(order)}
        db.session.add(WebhookDelivery(tenant_id=ep.tenant_id, endpoint_id=ep.id, event=event,
                                       payload=json.dumps(body, ensure_ascii=False), next_attempt_at=now, created_at=now))


def scan(tenant_id, now=None):
    """比對訂單狀態，產生事件；回傳產生的事件數。由這裡 commit。"""
    now = now or datetime.now()
    endpoints = WebhookEndpoint.query.filter_by(tenant_id=tenant_id, is_active=True) \
        .execution_options(skip_tenant_filter=True).all()
    if not endpoints:
        return 0
    orders = Order.query.filter(Order.tenant_id == tenant_id, Order.created_at >= now - timedelta(days=SCAN_DAYS)) \
        .execution_options(skip_tenant_filter=True).order_by(Order.id).all()
    states = {s.order_id: s for s in WebhookOrderState.query.filter(
        WebhookOrderState.order_id.in_([o.id for o in orders])).all()} if orders else {}
    events = 0
    for o in orders:
        pay = o.payment_status or ''
        st = states.get(o.id)
        if st is None:
            db.session.add(WebhookOrderState(order_id=o.id, tenant_id=tenant_id, status=o.status, payment_status=pay,
                                             seen_at=now))
            if pay in HIDDEN:
                continue   # 尚未成立：付款完成時再送 order.created + order.paid
            _enqueue(endpoints, 'order.created', o, now, first_seen=True)
            events += 1
            if pay in PAID_AT_CHECKOUT:   # 結帳當下就已收款 (點餐機 / POS / 外送平台)：與先待付款再付款的訂單一樣送 order.paid
                _enqueue(endpoints, 'order.paid', o, now, first_seen=True)
                events += 1
            if o.status == OrderStatus.CANCELLED:
                _enqueue(endpoints, 'order.cancelled', o, now, first_seen=True)
                events += 1
            continue
        was_hidden = st.payment_status in HIDDEN
        if st.payment_status != pay:
            if was_hidden and pay not in HIDDEN:   # 付款完成 (或之後的狀態)：訂單這時才成立
                _enqueue(endpoints, 'order.created', o, now)
                events += 1
                if o.status == OrderStatus.CANCELLED:
                    # 已取消後才收到付款 (需退款)：送取消、不送付款。待付款期間可能已記下 Cancelled，後面的狀態比對不會再送
                    _enqueue(endpoints, 'order.cancelled', o, now)
                    events += 1
                    st.status = o.status
                elif pay in PAID and pay not in REFUND:
                    _enqueue(endpoints, 'order.paid', o, now)
                    events += 1
            elif pay in PAID and st.payment_status == 'unpaid':
                _enqueue(endpoints, 'order.paid', o, now)
                events += 1
            st.payment_status = pay
        if pay in HIDDEN:   # 仍未成立 (例如付款逾時取消)：不送事件
            st.status, st.seen_at = o.status, now
            continue
        if st.status != o.status:
            _enqueue(endpoints, 'order.cancelled' if o.status == OrderStatus.CANCELLED else 'order.status_changed', o, now)
            events += 1
            st.status = o.status
        st.seen_at = now
    # 掃描範圍外的舊狀態定期清除
    WebhookOrderState.query.filter(WebhookOrderState.tenant_id == tenant_id,
                                   WebhookOrderState.seen_at < now - timedelta(days=SCAN_DAYS + 1)) \
        .delete(synchronize_session=False)
    db.session.commit()
    return events


def queue_ping(ep, now=None):
    now = now or datetime.now()
    body = {'id': uuid.uuid4().hex, 'event': 'ping', 'created_at': now.isoformat(timespec='seconds'),
            'data': {'message': '測試通知', 'endpoint': ep.name}}
    d = WebhookDelivery(tenant_id=ep.tenant_id, endpoint_id=ep.id, event='ping', payload=json.dumps(body, ensure_ascii=False),
                        next_attempt_at=now, created_at=now)
    db.session.add(d)
    return d


# ---------------------------------------------------------------------------
# 送出
# ---------------------------------------------------------------------------
def sign(secret, timestamp, body):
    return 'sha256=' + hmac.new(secret.encode(), f'{timestamp}.{body}'.encode(), hashlib.sha256).hexdigest()


def _post(url, body, headers, ip=None):
    """
    送出並回傳 HTTP 狀態碼；ip 有值時連線固定到該 IP (Host 標頭與 TLS 憑證仍用原本的主機名稱)。
    ip 可以是 IP 清單：連線層級失敗 (沒有 IPv6 路由、連線被拒、連線逾時，請求還沒送出) 時改試下一個 IP。
    """
    if ip is None:
        resp = requests.post(url, data=body.encode('utf-8'), headers=headers, timeout=5, allow_redirects=False)
        return resp.status_code
    ips = [ip] if isinstance(ip, str) else list(ip)
    parsed = urlparse(url)
    path = (parsed.path or '/') + (f'?{parsed.query}' if parsed.query else '')
    timeout = urllib3.Timeout(connect=5, read=5)
    for n, addr in enumerate(ips):
        if parsed.scheme == 'https':
            pool = urllib3.HTTPSConnectionPool(addr, port=parsed.port or 443, server_hostname=parsed.hostname,
                                               assert_hostname=parsed.hostname, cert_reqs='CERT_REQUIRED',
                                               ca_certs=certifi.where(), timeout=timeout, retries=False)
        else:
            pool = urllib3.HTTPConnectionPool(addr, port=parsed.port or 80, timeout=timeout, retries=False)
        try:
            resp = pool.urlopen('POST', path, body=body.encode('utf-8'), headers=dict(headers, Host=parsed.netloc),
                                redirect=False, preload_content=True)
            return resp.status
        except urllib3.exceptions.ConnectTimeoutError:   # 含 NewConnectionError (連不上這個 IP)
            if n == len(ips) - 1:
                raise
        finally:
            pool.close()


def deliver(d, now=None, post=None):
    """
    送出一筆 (呼叫端已搶下這筆，狀態為 sending)；回傳是否成功。
    DNS 解析與 HTTP 送出前先結束交易，送完再寫回結果：SQLite 整個資料庫同時只能有一個寫入交易，
    持有寫入鎖等接收端回應 (或門市斷網時等 DNS 逾時) 會讓點餐機 / POS 結帳卡住甚至 database is locked。
    """
    from app.services import monitor_service
    now = now or datetime.now()
    ep = d.endpoint
    if ep is not None and not ep.is_active:
        # 停用中的端點：不算一次失敗 (不累加 attempts / fail_streak、不轉 failed、不發警示)，只延後；
        # 重新啟用時 integrations._resume_deliveries 會把待送的事件改為立即送出
        d.status = 'pending'
        d.last_error = '端點已停用'
        d.next_attempt_at = now + timedelta(minutes=60)
        return False
    ok, code, error = False, None, ''
    try:
        if ep is None:
            raise AppError('端點已停用')
        url, secret, body, event = ep.url, ep.secret, d.payload, d.event
        db.session.commit()   # 網路連線期間不持有交易 (這筆仍是 sending，其他行程不會重複送出)
        ips = check_public_all(url)
        ts = str(int(_time.time()))
        payload_id = json.loads(body).get('id', '')
        headers = {'Content-Type': 'application/json', 'User-Agent': 'KioskWebhook/1', 'X-Kiosk-Event': event,
                   'X-Kiosk-Delivery': payload_id, 'X-Kiosk-Timestamp': ts, 'X-Kiosk-Signature': sign(secret, ts, body)}
        code = post(url, body, headers) if post else _post(url, body, headers, ips)
        ok = 200 <= int(code) < 300
        error = '' if ok else f'HTTP {code}'
    except AppError as exc:
        error = exc.message[:300]
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError, ValueError) as exc:
        # ValueError：網址的埠號不正確、主機名稱有空標籤等 (舊資料)；不攔的話這筆會永遠卡在 sending
        error = f'連線失敗：{exc}'[:300]
    d.attempts += 1
    if code is not None:
        d.response_code = code
    d.last_error = error
    if ok:
        d.status, d.sent_at = 'sent', now
        if ep is not None:
            ep.fail_streak, ep.last_status, ep.last_sent_at = 0, f'{d.event} 成功 ({d.response_code})', now
        return True
    if d.attempts >= MAX_ATTEMPTS:
        d.status = 'failed'
        monitor_service.alert(f'Webhook「{ep.name if ep else d.endpoint_id}」{d.event} 送出 {d.attempts} 次都失敗：{d.last_error}',
                              key=f'webhook-{d.endpoint_id}', tenant_id=d.tenant_id, level='warning', source='webhook')
    else:
        d.status = 'pending'   # 依退避時間重試
        d.next_attempt_at = now + timedelta(minutes=BACKOFF_MINUTES[d.attempts - 1])
    if ep is not None:
        ep.fail_streak += 1
        ep.last_status = f'{d.event} 失敗：{d.last_error}'[:120]
        if ep.fail_streak >= DISABLE_AFTER and ep.is_active:
            ep.is_active = False
            monitor_service.alert(f'Webhook「{ep.name}」連續失敗 {DISABLE_AFTER} 次，已自動停用', key=f'webhook-off-{ep.id}',
                                  tenant_id=ep.tenant_id, level='warning', source='webhook')
    return False


def deliver_due(tenant_id=None, now=None, post=None, limit=50):
    from sqlalchemy import update
    now = now or datetime.now()
    # 送到一半行程中止而卡在 sending 超過 10 分鐘：改回待送
    db.session.execute(update(WebhookDelivery).where(WebhookDelivery.status == 'sending',
                                                     WebhookDelivery.updated_at < now - timedelta(minutes=10))
                       .values(status='pending').execution_options(synchronize_session=False, skip_tenant_filter=True))
    db.session.commit()
    q = WebhookDelivery.query.filter(WebhookDelivery.status == 'pending', WebhookDelivery.next_attempt_at <= now) \
        .execution_options(skip_tenant_filter=True)
    if tenant_id is not None:
        q = q.filter(WebhookDelivery.tenant_id == tenant_id)
    sent = 0
    for d in q.order_by(WebhookDelivery.id).limit(limit).all():
        claimed = db.session.execute(update(WebhookDelivery).where(WebhookDelivery.id == d.id,
                                                                   WebhookDelivery.status == 'pending')
                                     .values(status='sending')
                                     .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
        db.session.commit()
        if not claimed:   # 另一個行程剛好搶走
            continue
        db.session.refresh(d)
        # 送出期間維持 sending (不先改回 pending：未提交的變更會在讀取端點時 autoflush，等於送出期間持有寫入鎖)；
        # 結果由 deliver 改為 sent / failed / pending (重試)
        sent += 1 if deliver(d, now, post) else 0
        db.session.commit()
    return sent


def run_all(now=None):
    """背景排程 (每 15 秒)：所有有啟用端點的店家"""
    tenant_ids = [t for (t,) in db.session.query(WebhookEndpoint.tenant_id).filter(WebhookEndpoint.is_active.is_(True))
                  .execution_options(skip_tenant_filter=True).distinct().all()]
    for tid in tenant_ids:
        scan(tid, now)
    return deliver_due(None, now)
