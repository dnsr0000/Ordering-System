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
from datetime import datetime, timedelta

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderSource
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.settings import StoreSetting
from app.models.tenant import Tenant
from app.services import monitor_service, settings_service
from app.services.http_client import request_json

logger = logging.getLogger(__name__)

UBER_TOKEN_URL = 'https://login.uber.com/oauth/v2/token'
UBER_API = 'https://api.uber.com'


def _http(method, url, **kwargs):
    """實際送出 (測試時以 monkeypatch 取代)；回傳 (狀態碼, JSON)"""
    return request_json(method, url, **kwargs)


PLATFORM_LABELS = {'ubereats': ('Uber Eats', 'Store ID'), 'foodpanda': ('foodpanda', '店家代碼')}


def _tenant_active(tenant_id):
    tenant = db.session.get(Tenant, tenant_id)
    return tenant is not None and tenant.is_active


def unconfigured_tenants(platform, key_field):
    """啟用該平台、但沒有填平台店家代碼的 (啟用中) 店家 id"""
    out = []
    for row in StoreSetting.query.filter_by(key='delivery').execution_options(skip_tenant_filter=True).all():
        cfg = settings_service.get('delivery', row.tenant_id)
        if cfg.get(f'{platform}_enabled') and not str(cfg.get(key_field) or '') and _tenant_active(row.tenant_id):
            out.append(row.tenant_id)
    return out


def tenant_for(platform, key_field, key_value):
    """
    依 Webhook 帶的店家代碼找出對應的店家 (店家未填代碼時，使用唯一啟用該平台且未填代碼的店家)。
    已停用的店家不接單 (回傳 None → 404，平台判定失敗)：停用後沒有人能登入處理訂單。
    """
    candidates = []
    for row in StoreSetting.query.filter_by(key='delivery').execution_options(skip_tenant_filter=True).all():
        cfg = settings_service.get('delivery', row.tenant_id)
        if not cfg.get(f'{platform}_enabled'):
            continue
        configured = str(cfg.get(key_field) or '')
        if key_value and configured == str(key_value):
            # 代碼相符但店家已停用：不接單，也不改用其他「未填代碼」的店家 (那是別家店)
            return row.tenant_id if _tenant_active(row.tenant_id) else None
        if not configured and _tenant_active(row.tenant_id):   # 店家沒有填代碼：只有一家啟用時才視為該店 (填了代碼但不相符則不接受)
            candidates.append(row.tenant_id)
    if len(candidates) > 1:
        # 多店家主機上兩家以上都沒填代碼：無法判斷是哪一家的訂單，全部拒收；通知每一家補填代碼
        name, field = PLATFORM_LABELS.get(platform, (platform, '店家代碼'))
        for tid in candidates:
            monitor_service.alert(f'有 {len(candidates)} 家店啟用 {name} 串接但都沒有填寫 {name} {field}，'
                                  f'平台訂單無法判斷屬於哪一家而被拒收；請到「系統設定 → 外送平台」填寫',
                                  key=f'delivery-ambiguous-{platform}-{tid}', tenant_id=tid, level='warning')
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


REF_FALLBACK_HOURS = 12   # 以平台單號 (external_ref) 比對既有訂單的時間範圍：只擋重送 / 同一張單手動登錄，不擋日後撞號的新單


def _platform_key(source, platform):
    """手動登錄的「其他平台」(DELIVERY) 要把平台名稱納入去重鍵：不同平台的相同單號是不同的訂單"""
    if source == OrderSource.DELIVERY and platform:
        return f':{str(platform).strip().lower()}'
    return ''


def platform_order_uuid(tenant_id, source, external_ref, platform=''):
    """
    平台訂單的去重識別 (client_order_id) 由「店家 + 平台 + 平台單號」決定：平台重送通知、同時送達兩次、或店員同時手動登錄同一張單，
    都會落在同一個識別 (同店唯一索引)，只會建立一張訂單、只扣一次庫存。
    platform：手動登錄 DELIVERY 來源時的平台名稱 (Lalamove / GrabFood…)；其他來源不使用 (維持既有識別)。
    """
    tenant = db.session.get(Tenant, tenant_id)
    key = f'{tenant.uuid if tenant else tenant_id}:{source}{_platform_key(source, platform)}:{str(external_ref).strip().lower()}'
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'kiosk-platform-order:{key}'))


def find_platform_order(tenant_id, source, external_ref, platform='', dedupe_ref=None):
    """
    以去重識別找既有的平台訂單 (dedupe_ref：平台的唯一訂單識別，例如 Uber 的 order UUID；預設即 external_ref)；
    找不到時以平台單號比對最近 REF_FALLBACK_HOURS 小時內的訂單 (舊版識別建立的單、或店員以單號手動登錄的同一張單)。
    """
    cid = platform_order_uuid(tenant_id, source, dedupe_ref or external_ref, platform)
    found = Order.query.filter_by(tenant_id=tenant_id, client_order_id=cid).execution_options(skip_tenant_filter=True).first()
    if found is not None:
        return found
    q = Order.query.filter_by(tenant_id=tenant_id, external_ref=external_ref, order_source=source) \
        .filter(Order.created_at >= datetime.now() - timedelta(hours=REF_FALLBACK_HOURS))
    if _platform_key(source, platform):
        q = q.filter(Order.table_number == f'{platform} {external_ref}'[:40])   # 手動登錄時桌號欄 = 「平台 單號」
    return q.execution_options(skip_tenant_filter=True).first()


def create_platform_order(tenant_id, source, external_ref, items, note='', dedupe_ref=None):
    """
    以系統身分建立外送訂單 (重複的平台單號 — 包含同時送達的重送 — 直接回傳既有訂單)。
    external_ref 是顯示用的平台單號；dedupe_ref 是平台唯一的訂單識別 (有的話以它去重，避免短單號日後撞號)。
    """
    from flask import g
    from app.services.order_service import process_checkout
    existing = find_platform_order(tenant_id, source, external_ref, dedupe_ref=dedupe_ref)
    if existing is not None:
        return existing, True
    tenant = db.session.get(Tenant, tenant_id)
    g.tenant, g.tenant_id = tenant, tenant_id
    try:
        order, replayed = process_checkout(
            tenant_id=tenant_id, items=items, order_source=source, order_type='外送', payment_method='平台代收',
            table_input=f"{'Uber Eats' if source == OrderSource.UBER_EATS else 'foodpanda'} {external_ref}"[:40],
            note=note, cashier_username=source, external_ref=external_ref,
            client_order_id=platform_order_uuid(tenant_id, source, dedupe_ref or external_ref))
    except AppError:
        db.session.rollback()
        existing = find_platform_order(tenant_id, source, external_ref, dedupe_ref=dedupe_ref)
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
    # 轉成 bytes 再比較：兩邊都是 str 時，簽章含非 ASCII 字元會拋 TypeError (公開 Webhook 回 500)
    return hmac.compare_digest(expected.encode('utf-8'), signature.strip().lower().encode('utf-8'))


def _uber_token(cfg):
    status, body = _http('POST', UBER_TOKEN_URL, data={
        'client_id': cfg.get('ubereats_client_id'), 'client_secret': cfg.get('ubereats_client_secret'),
        'grant_type': 'client_credentials', 'scope': 'eats.order'})
    if status != 200 or not body.get('access_token'):
        raise AppError(f'Uber 授權失敗 ({status})：{body.get("error") or body}')
    return body['access_token']


UBER_CANCEL_EVENTS = ('orders.cancel', 'orders.failure')


def _cancel_uber_order(tenant_id, event):
    """平台取消 / 訂單失敗 (例如關閉自動接單後逾時未接)：本店對應的訂單走正常取消流程 (回補庫存)，並通知店家停止製作"""
    from flask import g
    from app.models.enums import OrderStatus
    from app.services.order_lifecycle import system_cancel
    order_id = str((event.get('meta') or {}).get('resource_id') or '')
    if not order_id:
        return None
    order = Order.query.filter_by(tenant_id=tenant_id, order_source=OrderSource.UBER_EATS,
                                  client_order_id=platform_order_uuid(tenant_id, OrderSource.UBER_EATS, order_id)) \
        .execution_options(skip_tenant_filter=True).first()
    if order is None or order.status == OrderStatus.CANCELLED:
        return None
    g.tenant, g.tenant_id = db.session.get(Tenant, tenant_id), tenant_id
    reason = 'Uber Eats 平台已取消此訂單' if event.get('event_type') == 'orders.cancel' else 'Uber Eats 訂單失敗 (平台未成立)'
    oid, ref = order.id, order.external_ref
    result = system_cancel(oid, tenant_id, reason)
    monitor_service.alert(f'Uber Eats 訂單 {ref} (#{oid}) {reason}' + ('，本店訂單已取消並回補庫存，請停止製作' if result is not None
                                                              else '，但本店訂單無法自動取消 (可能已完成)，請人工確認'),
                          key=f'uber-cancel-{oid}', tenant_id=tenant_id, level='warning')
    return result


def handle_uber_event(tenant_id, event):
    """處理 orders.notification：取得明細 → 建立訂單 → 接單 (失敗則拒單)；平台取消 / 失敗事件：取消本店訂單"""
    if event.get('event_type') in UBER_CANCEL_EVENTS:
        return _cancel_uber_order(tenant_id, event)
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
        # display_id 是給人看的短代碼 (日後會撞號)：去重以 Uber 的訂單 UUID (resource_id) 為準
        order, _ = create_platform_order(tenant_id, OrderSource.UBER_EATS, ref, items,
                                         note=(detail.get('cart') or {}).get('special_instructions') or '',
                                         dedupe_ref=order_id or None)
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
    from flask import g
    tid = getattr(g, 'tenant_id', None)
    for platform, key_field in (('ubereats', 'ubereats_store_id'), ('foodpanda', 'foodpanda_vendor_code')):
        if cfg.get(f'{platform}_enabled') and not str(cfg.get(key_field) or ''):
            others = [t for t in unconfigured_tenants(platform, key_field) if t != tid]
            if others:   # 多店家主機：兩家以上都沒填代碼時平台訂單無法對應，全部拒收
                name, field = PLATFORM_LABELS[platform]
                raise AppError(f'本主機另有 {len(others)} 家店啟用 {name} 但也沒有填寫 {field}，平台訂單會無法判斷店家而被拒收；'
                               f'請填寫 {name} {field}')
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
