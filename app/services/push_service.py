"""
Web Push 推播：顧客在出餐進度頁 (/track) 按「開啟完成通知」後，瀏覽器的推播訂閱存進 push_subscription；
餐點完成 (出餐) 或外送員出發時，就算顧客已離開頁面 / 關閉瀏覽器也收得到系統通知。
- 協定：RFC 8291 (aes128gcm 加密，http-ece) + RFC 8292 (VAPID 簽章，py-vapid)，以 requests 送到瀏覽器廠商的推播伺服器；
  不依賴 pywebpush (它需要 aiohttp)。
- 金鑰：系統設定 → 推播通知 啟用後，第一次有人訂閱時自動產生 VAPID 金鑰對並存進設定 (私鑰為 secret 欄位)；
  公鑰給瀏覽器 (/api/push/vapid)。
- 只在 HTTPS (或 localhost) 的頁面可用：門市 Wi-Fi 以 http://IP 開啟時瀏覽器不提供 pushManager，前端自動退回頁面內通知。
- 送出在交易提交後的背景工作 (deferred)；推播伺服器回 404 / 410 (訂閱失效) 時刪除訂閱，其他錯誤記在 last_error。
- 每個訂閱綁一張訂單 (order_uuid)；通知送出後或訂單結束 7 天後清除 (maintenance 每日工作)。
"""
from typing import Any
import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

from app.errors import AppError
from app.extensions import db
from app.models.integration import PushSubscription
from app.services import deferred, settings_service

logger = logging.getLogger(__name__)

TTL_SECONDS = 3600
MAX_PER_ORDER = 5
KEEP_DAYS = 7


config = settings_service.reader('push')
enabled = settings_service.flag('push')


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _b64url_decode(text: Any) -> bytes:
    text = str(text or '').strip()
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def ensure_keys(tenant_id: int | None = None) -> tuple[str, str]:
    """回傳 (私鑰 PEM, 公鑰 base64url)；沒有時產生並寫入設定 (呼叫端 commit)"""
    from py_vapid import Vapid
    cfg = config(tenant_id)
    private_pem = (cfg.get('vapid_private_key') or '').strip()
    public_key = (cfg.get('vapid_public_key') or '').strip()
    if private_pem and public_key:
        return private_pem, public_key
    vapid = Vapid()
    vapid.generate_keys()
    private_pem = vapid.private_pem().decode()
    from cryptography.hazmat.primitives import serialization
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    public_key = _b64url(raw)
    settings_service.put('push', {'vapid_private_key': private_pem, 'vapid_public_key': public_key}, tenant_id)
    return private_pem, public_key


def public_key(tenant_id: int | None = None) -> str:
    return ensure_keys(tenant_id)[1]


def subscribe(order: Any, info: Any, user_id: int | None = None) -> Any:
    """存訂閱 (同一端點重複訂閱只更新)。info = 瀏覽器 PushSubscription.toJSON()"""
    if not isinstance(info, dict):
        raise AppError('訂閱資料格式錯誤')
    endpoint = str(info.get('endpoint') or '').strip()
    raw_keys = info.get('keys')
    keys: dict = raw_keys if isinstance(raw_keys, dict) else {}
    p256dh, auth = str(keys.get('p256dh') or '').strip(), str(keys.get('auth') or '').strip()
    if not endpoint.startswith('https://') or len(endpoint) > 500 or not p256dh or not auth or len(p256dh) > 200 \
            or len(auth) > 100:
        raise AppError('訂閱資料不完整')
    ensure_keys(order.tenant_id)   # 正常流程瀏覽器已先取公鑰；這裡再保證金鑰存在
    sub = PushSubscription.query.filter_by(tenant_id=order.tenant_id, endpoint=endpoint, order_uuid=order.uuid) \
        .execution_options(skip_tenant_filter=True).first()
    if sub is None:
        count = PushSubscription.query.filter_by(tenant_id=order.tenant_id, order_uuid=order.uuid) \
            .execution_options(skip_tenant_filter=True).count()
        if count >= MAX_PER_ORDER:
            raise AppError('此訂單的通知裝置已達上限', 429)
        sub = PushSubscription(tenant_id=order.tenant_id, endpoint=endpoint, order_uuid=order.uuid)
        db.session.add(sub)
    sub.p256dh, sub.auth, sub.user_id, sub.last_error = p256dh, auth, user_id, ''
    return sub


def _send_one(sub: Any, payload: dict, private_pem: str, contact: str) -> tuple[bool, bool, str]:
    """送一則推播。回傳 (ok, 是否應刪除訂閱, 錯誤訊息)"""
    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec
    from py_vapid import Vapid
    url = urlparse(sub.endpoint)
    vapid = Vapid.from_pem(private_pem.encode())
    headers = vapid.sign({'aud': f'{url.scheme}://{url.netloc}', 'sub': f'mailto:{contact}',
                          'exp': int(time.time()) + 12 * 3600})
    server_key = ec.generate_private_key(ec.SECP256R1())
    body = http_ece.encrypt(json.dumps(payload, ensure_ascii=False).encode('utf-8'), salt=os.urandom(16),
                            private_key=server_key, dh=_b64url_decode(sub.p256dh), auth_secret=_b64url_decode(sub.auth),
                            version='aes128gcm')
    headers.update({'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream', 'TTL': str(TTL_SECONDS),
                    'Urgency': 'high'})
    try:
        resp = requests.post(sub.endpoint, data=body, headers=headers, timeout=10)
    except requests.RequestException as exc:
        return False, False, str(exc)[:200]
    if resp.status_code in (200, 201, 202):
        return True, False, ''
    return False, resp.status_code in (404, 410), f'HTTP {resp.status_code}'[:200]


def send_to_order(order_uuid: str, payload: dict, tenant_id: int) -> int:
    """背景工作：推播給該訂單的所有訂閱；成功或訂閱失效都刪除訂閱 (一張訂單只通知一次)"""
    cfg = config(tenant_id)
    if not cfg.get('enabled'):
        return 0
    private_pem = (cfg.get('vapid_private_key') or '').strip()
    if not private_pem:
        return 0
    contact = (cfg.get('contact_email') or '').strip() or 'admin@example.com'
    subs = PushSubscription.query.filter_by(tenant_id=tenant_id, order_uuid=order_uuid) \
        .execution_options(skip_tenant_filter=True).all()
    sent = 0
    for sub in subs:
        ok, gone, error = _send_one(sub, payload, private_pem, contact)
        if ok or gone:
            db.session.delete(sub)
            sent += int(ok)
        else:
            sub.last_error = error
            logger.warning('Web Push 失敗 (%s)：%s', sub.endpoint[:60], error)
    db.session.commit()
    return sent


def _deferred_send(order_id: int, kind: str) -> int:
    from app.models.order import Order
    from app.models.tenant import Tenant
    order = db.session.get(Order, order_id)
    if order is None:
        return 0
    shop = db.session.get(Tenant, order.tenant_id)
    number = f'{order.display_pickup_number:03d}'
    if kind == 'delivery_out':
        title, body = '外送員已出發', f'取餐號 #{number} 的餐點正在配送中，請留意電話'
    elif kind == 'feedback_reply':
        title, body = '店家回覆了您的評價', f'訂單 #{order.id}：打開查看店家的回覆'
    else:
        title, body = '餐點已完成', f'取餐號 #{number}，請至櫃檯取餐'
    payload = {'title': f'{shop.name if shop else ""}：{title}'.strip('：'), 'body': body,
               'url': f'/track/{order.uuid}', 'tag': f'order-{order.uuid}'}
    return send_to_order(order.uuid, payload, order.tenant_id)


def notify_order(order: Any, kind: str = 'ready') -> None:
    """訂單出餐 / 外送出發時呼叫 (交易中)：有訂閱且功能啟用時排入交易後推播"""
    if order is None or not enabled(order.tenant_id):
        return
    has = PushSubscription.query.filter_by(tenant_id=order.tenant_id, order_uuid=order.uuid) \
        .execution_options(skip_tenant_filter=True).first() is not None
    if has:
        deferred.defer(_deferred_send, order.id, kind)


def cleanup(now: datetime | None = None) -> int:
    """每日維護：超過 KEEP_DAYS 的訂閱刪除 (訂單早已結束)"""
    cutoff = (now or datetime.now()) - timedelta(days=KEEP_DAYS)
    n = PushSubscription.query.filter(PushSubscription.created_at < cutoff).execution_options(skip_tenant_filter=True) \
        .delete(synchronize_session=False)
    return n
