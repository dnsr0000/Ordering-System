"""
外送平台訂單匯入 (需向平台申請 POS 串接資格並取得金鑰，後台「系統設定 → 外送平台」填寫)。

品項對應：平台上的品項代碼 (Uber Eats 的 external_data / foodpanda 的 remoteCode) = 本店餐點的「外送 / 條碼代碼」，
找不到代碼時以品名比對；仍找不到則拒單 (Uber) 或回應錯誤 (foodpanda)，並通知管理者。

- Uber Eats：Webhook (X-Uber-Signature = HMAC-SHA256(client secret, 原始內容)) → 取得訂單明細 → 建立訂單 → 接單 / 拒單
  文件：https://developer.uber.com/docs/eats/guides/order-integration
- foodpanda (Delivery Hero POS 串接)：平台以 Bearer 權杖呼叫 Webhook 並附上完整訂單；回應 remoteOrderId
"""
import hashlib
import hmac
import logging
import uuid

import requests

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderSource
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.settings import StoreSetting
from app.models.tenant import Tenant
from app.services import monitor_service, settings_service

logger = logging.getLogger(__name__)

UBER_TOKEN_URL = 'https://login.uber.com/oauth/v2/token'
UBER_API = 'https://api.uber.com'


def _http(method, url, **kwargs):
    """實際送出 (測試時以 monkeypatch 取代)；回傳 (狀態碼, JSON)"""
    resp = requests.request(method, url, timeout=kwargs.pop('timeout', 15), **kwargs)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def tenant_for(platform, key_field, key_value):
    """依 Webhook 帶的店家代碼找出對應的店家 (店家未填代碼時，使用唯一啟用該平台且未填代碼的店家)"""
    candidates = []
    for row in StoreSetting.query.filter_by(key='delivery').execution_options(skip_tenant_filter=True).all():
        cfg = settings_service.get('delivery', row.tenant_id)
        if not cfg.get(f'{platform}_enabled'):
            continue
        configured = str(cfg.get(key_field) or '')
        if key_value and configured == str(key_value):
            return row.tenant_id
        if not configured:   # 店家沒有填代碼：只有一家啟用時才視為該店 (填了代碼但不相符則不接受)
            candidates.append(row.tenant_id)
    return candidates[0] if len(candidates) == 1 else None


def map_items(tenant_id, products):
    """products：[(代碼, 名稱, 數量, 備註)] → process_checkout 的 items；找不到的品項放在 missing"""
    items, missing = [], []
    for code, name, qty, note in products:
        item = None
        if code:
            item = MenuItem.query.filter_by(tenant_id=tenant_id, external_code=str(code)) \
                .execution_options(skip_tenant_filter=True).first()
        if item is None and name:
            item = MenuItem.query.filter_by(tenant_id=tenant_id, name=str(name).strip()) \
                .execution_options(skip_tenant_filter=True).first()
        if item is None:
            missing.append(name or code)
            continue
        items.append({'menu_item_id': item.id, 'quantity': max(1, int(qty or 1)), 'customization': str(note or '')[:100]})
    return items, missing


def platform_order_uuid(tenant_id, source, external_ref):
    """
    平台訂單的訂單 uuid 由「店家 + 平台 + 平台單號」決定：平台重送通知、同時送達兩次、或店員同時手動登錄同一張單，
    都會落在同一個 uuid (資料庫唯一索引)，只會建立一張訂單、只扣一次庫存。
    """
    tenant = db.session.get(Tenant, tenant_id)
    key = f'{tenant.uuid if tenant else tenant_id}:{source}:{str(external_ref).strip().lower()}'
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'kiosk-platform-order:{key}'))


def find_platform_order(tenant_id, source, external_ref):
    cid = platform_order_uuid(tenant_id, source, external_ref)
    return (Order.query.filter_by(uuid=cid).execution_options(skip_tenant_filter=True).first()
            or Order.query.filter_by(tenant_id=tenant_id, external_ref=external_ref, order_source=source)
            .execution_options(skip_tenant_filter=True).first())


def create_platform_order(tenant_id, source, external_ref, items, note=''):
    """以系統身分建立外送訂單 (重複的平台單號 — 包含同時送達的重送 — 直接回傳既有訂單)"""
    from flask import g
    from app.services.order_service import process_checkout
    existing = find_platform_order(tenant_id, source, external_ref)
    if existing is not None:
        return existing, True
    tenant = db.session.get(Tenant, tenant_id)
    g.tenant, g.tenant_id = tenant, tenant_id
    try:
        order, replayed = process_checkout(
            tenant_id=tenant_id, items=items, order_source=source, order_type='外送', payment_method='平台代收',
            table_input=f"{'Uber Eats' if source == OrderSource.UBER_EATS else 'foodpanda'} {external_ref}"[:40],
            note=note, cashier_username=source, external_ref=external_ref,
            client_order_id=platform_order_uuid(tenant_id, source, external_ref))
    except AppError:
        db.session.rollback()
        existing = find_platform_order(tenant_id, source, external_ref)
        if existing is not None:   # 同時有另一個請求 (重送或店員手動登錄) 先建立了這張單
            return existing, True
        raise
    return order, replayed


# ---------------------------------------------------------------------------
# Uber Eats
# ---------------------------------------------------------------------------
def verify_uber_signature(raw_body, signature, client_secret):
    if not signature or not client_secret:
        return False
    expected = hmac.new(client_secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _uber_token(cfg):
    status, body = _http('POST', UBER_TOKEN_URL, data={
        'client_id': cfg.get('ubereats_client_id'), 'client_secret': cfg.get('ubereats_client_secret'),
        'grant_type': 'client_credentials', 'scope': 'eats.order'})
    if status != 200 or not body.get('access_token'):
        raise AppError(f'Uber 授權失敗 ({status})：{body.get("error") or body}')
    return body['access_token']


def handle_uber_event(tenant_id, event):
    """處理 orders.notification：取得明細 → 建立訂單 → 接單 (失敗則拒單)"""
    if event.get('event_type') != 'orders.notification':
        return None
    cfg = settings_service.get('delivery', tenant_id)
    order_id = (event.get('meta') or {}).get('resource_id') or ''
    href = event.get('resource_href') or f'{UBER_API}/v2/eats/order/{order_id}'
    token = _uber_token(cfg)
    headers = {'Authorization': f'Bearer {token}'}
    status, detail = _http('GET', href, headers=headers)
    if status != 200:
        raise AppError(f'取得 Uber Eats 訂單失敗 ({status})')
    products = [(it.get('external_data') or '', it.get('title') or '', it.get('quantity') or 1,
                 '、'.join(o.get('title', '') for o in (it.get('selected_modifier_groups') or []) for o in (o.get('selected_items') or [])))
                for it in (detail.get('cart') or {}).get('items') or []]
    items, missing = map_items(tenant_id, products)
    ref = str(detail.get('display_id') or order_id)
    if missing or not items:
        _http('POST', f'{UBER_API}/v1/eats/orders/{order_id}/deny_pos_order', headers=headers,
              json={'reason': {'explanation': f"找不到品項：{'、'.join(missing)}", 'code': 'ITEM_AVAILABILITY'}})
        monitor_service.alert(f'Uber Eats 訂單 {ref} 有無法對應的品項 ({"、".join(missing)})，已拒單；請在「菜單進階設定」填寫外送代碼',
                              key=f'uber-map-{ref}', tenant_id=tenant_id, level='warning')
        return None
    try:
        order, _ = create_platform_order(tenant_id, OrderSource.UBER_EATS, ref, items,
                                         note=(detail.get('cart') or {}).get('special_instructions') or '')
    except AppError as exc:
        _http('POST', f'{UBER_API}/v1/eats/orders/{order_id}/deny_pos_order', headers=headers,
              json={'reason': {'explanation': exc.message[:100], 'code': 'STORE_CLOSED'}})
        raise
    if cfg.get('ubereats_auto_accept', True):
        _http('POST', f'{UBER_API}/v1/eats/orders/{order_id}/accept_pos_order', headers=headers,
              json={'reason': 'accepted', 'external_reference_id': str(order.id)})
    return order


# ---------------------------------------------------------------------------
# foodpanda (Delivery Hero)
# ---------------------------------------------------------------------------
def handle_foodpanda_order(tenant_id, payload):
    products = [(p.get('remoteCode') or '', p.get('name') or '', p.get('quantity') or 1, p.get('comment') or '')
                for p in payload.get('products') or []]
    ref = str(payload.get('code') or payload.get('token') or '')
    if not ref:
        raise AppError('缺少平台訂單編號')
    items, missing = map_items(tenant_id, products)
    if missing or not items:
        monitor_service.alert(f'foodpanda 訂單 {ref} 有無法對應的品項 ({"、".join(missing)})', key=f'fp-map-{ref}',
                              tenant_id=tenant_id, level='warning')
        raise AppError(f"找不到品項：{'、'.join(missing)}", 422)
    comments = payload.get('comments') or {}
    note = comments.get('customerComment') if isinstance(comments, dict) else ''
    order, _ = create_platform_order(tenant_id, OrderSource.FOODPANDA, ref, items, note=note or '')
    return order


@settings_service.register_tester('delivery')
def _test(cfg):
    messages = []
    if cfg.get('ubereats_enabled'):
        _uber_token(cfg)
        messages.append('Uber Eats 金鑰有效')
    if cfg.get('foodpanda_enabled'):
        messages.append('foodpanda 權杖已設定 (由平台呼叫本站 Webhook，無法主動測試)' if cfg.get('foodpanda_token')
                        else 'foodpanda 尚未設定權杖')
    if not messages:
        raise AppError('尚未啟用任何外送平台串接')
    return '；'.join(messages)
