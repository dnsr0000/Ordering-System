"""
外送員 GPS 追蹤 (自有外送)。

- 指派外送員時在訂單上產生 driver_token；派車頁顯示外送員專用連結 / QR code (/driver/<token>)。
- 外送員手機開啟該頁並允許定位後，每隔數秒把位置 POST 到 /api/driver/<token>/position；
  位置存在 DeliveryPosition (本機資料、不同步)，顧客追蹤頁 (/track) 從同一台主機讀最新位置。
- 連結的主機：PUBLIC_BASE_URL / 店家設定的 QR code 網址優先；分店 (node) 沒設定時用總部 (線上端) 網址，
  因為外送員的手機在店外連不到門市區網；訂單與 driver_token 會同步到線上端，所以線上端查得到這張訂單。
"""
import math
import uuid
from urllib.parse import quote
from datetime import datetime

from flask import current_app, g, has_request_context, request
from sqlalchemy import delete, select

from app.errors import AppError
from app.extensions import db
from app.models.order import DeliveryPosition, Order
from app.services import settings_service
from app.sync import runtime as sync_runtime

ACTIVE_STATUSES = ('assigned', 'out')
TOKEN_MIN_LEN = 30


def enabled(tenant_id=None):
    cfg = settings_service.get('own_delivery', tenant_id)
    return bool(cfg.get('enabled')) and bool(cfg.get('gps_tracking', True))


def interval_seconds(tenant_id=None):
    return max(5, min(120, int(settings_service.get('own_delivery', tenant_id).get('gps_interval') or 15)))


def ensure_token(order):
    """指派外送員時產生憑證 (已有就沿用，改派同一張訂單連結不變)"""
    if not order.driver_token:
        order.driver_token = str(uuid.uuid4())
    return order.driver_token


def public_base():
    """外送員手機要連到的主機網址"""
    cfg_url = current_app.config.get('PUBLIC_BASE_URL') or ''
    tenant_url = (getattr(getattr(g, 'tenant', None), 'table_qr_base_url', '') or '') if has_request_context() else ''
    hub_url = current_app.config.get('SYNC_HUB_URL') if sync_runtime.is_node() else ''
    local = request.url_root if has_request_context() else ''
    return str(cfg_url or tenant_url or hub_url or local).rstrip('/')


def driver_url(order):
    return f'{public_base()}/driver/{order.driver_token}' if order.driver_token else ''


def find_order(token):
    """以外送員憑證找訂單 (跨店家；憑證為 uuid4，太短的直接拒絕)"""
    from app.tenant_context import tenant_bypass
    token = str(token or '')[:36]
    if len(token) < TOKEN_MIN_LEN:
        return None
    with tenant_bypass():
        return Order.query.filter_by(driver_token=token).first()


def is_active(order):
    return (order.delivery_status or '') in ACTIVE_STATUSES and order.status != 'Cancelled'


def record_position(order, lat, lng, accuracy=0):
    """外送員回報位置；已送達 / 失敗 / 取消的訂單不再接受 (409)"""
    if not is_active(order):
        raise AppError('此訂單已結束配送，不需再回報位置', 409)
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise AppError('定位資料不正確')
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or math.isnan(lat) or math.isnan(lng):
        raise AppError('定位資料不正確')
    try:
        accuracy = max(0.0, min(100000.0, float(accuracy or 0)))
    except (TypeError, ValueError):
        accuracy = 0.0
    pos = DeliveryPosition(tenant_id=order.tenant_id, order_uuid=order.uuid, lat=lat, lng=lng, accuracy=accuracy,
                           recorded_at=datetime.now())
    db.session.add(pos)
    db.session.flush()
    _prune(order)
    return pos


def _prune(order):
    """每張訂單只留最近 MAX_PER_ORDER 筆"""
    keep = select(DeliveryPosition.id).where(DeliveryPosition.order_uuid == order.uuid) \
        .order_by(DeliveryPosition.id.desc()).limit(DeliveryPosition.MAX_PER_ORDER)
    db.session.execute(delete(DeliveryPosition).where(DeliveryPosition.order_uuid == order.uuid,
                                                      DeliveryPosition.id.notin_(keep))
                       .execution_options(synchronize_session=False))


def latest(order):
    """最新位置 (dict) 或 None；只在配送進行中回傳 (送達後不再顯示外送員位置)"""
    if not is_active(order):
        return None
    pos = DeliveryPosition.query.filter_by(order_uuid=order.uuid).order_by(DeliveryPosition.id.desc()).first()
    if pos is None:
        return None
    data = pos.to_dict()
    data['age_seconds'] = max(0, int((datetime.now() - pos.recorded_at).total_seconds()))
    data['map_url'] = f'https://www.google.com/maps?q={data["lat"]},{data["lng"]}'
    return data


def trail(order, limit=50):
    """最近的軌跡 (舊 → 新)"""
    rows = DeliveryPosition.query.filter_by(order_uuid=order.uuid).order_by(DeliveryPosition.id.desc()).limit(limit).all()
    return [p.to_dict() for p in reversed(rows)]


def distance_km(lat1, lng1, lat2, lng2):
    """兩點直線距離 (公里，Haversine)"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def driver_payload(order):
    """外送員頁需要的訂單資料 (不含顧客姓名以外的個資；電話只給撥號用)"""
    from app.models.enums import DELIVERY_STATUS_LABELS
    return {
        'order_id': order.id,
        'pickup_number': order.display_pickup_number,
        'address': order.delivery_address or '',
        'phone': order.delivery_phone or '',
        'zone': order.delivery_zone or '',
        'note': order.note or '',
        'items': [{'name': i.display_name, 'quantity': i.quantity} for i in order.items],
        'total': order.total_price or 0,
        'paid': bool(order.is_paid),
        'payment_method': order.payment_method or '',
        'driver': order.driver_name or '',
        'delivery_status': order.delivery_status or '',
        'delivery_label': DELIVERY_STATUS_LABELS.get(order.delivery_status or '', ''),
        'active': is_active(order),
        'interval': interval_seconds(order.tenant_id),
        'nav_url': 'https://www.google.com/maps/dir/?api=1&destination=' + quote(order.delivery_address or ''),
    }
