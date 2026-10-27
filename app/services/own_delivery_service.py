"""
自有外送 (外送到府)：配送區域與外送費、下單時驗證地址電話、派車 (指派外送員 → 出發 → 送達 / 失敗)。
派車在出餐分店執行，配送狀態同步回線上端 (顧客在追蹤頁看到)。
"""
from datetime import datetime

from app.errors import AppError
from app.extensions import db
from app.models.enums import DELIVERY_STATUS_LABELS, OrderStatus, OrderType
from app.models.order import Order
from app.services import settings_service

PHONE_DIGITS = (8, 10)


def config(tenant_id=None):
    return settings_service.get('own_delivery', tenant_id)


def enabled_for(channel, tenant_id=None):
    cfg = config(tenant_id)
    return bool(cfg.get('enabled')) and channel in (cfg.get('channels') or [])


def zones(tenant_id=None):
    """[{'name', 'fee', 'min_spend'}]"""
    out = []
    for raw in str(config(tenant_id).get('zones') or '').splitlines():
        parts = [p.strip() for p in raw.replace('：', ':').split(':')]
        if not parts or not parts[0]:
            continue
        try:
            fee = max(0, int(parts[1])) if len(parts) > 1 and parts[1] else 0
            min_spend = max(0, int(parts[2])) if len(parts) > 2 and parts[2] else 0
        except ValueError:
            continue
        out.append({'name': parts[0][:30], 'fee': fee, 'min_spend': min_spend})
    return out[:20]


def drivers(tenant_id=None):
    return [d.strip()[:60] for d in str(config(tenant_id).get('drivers') or '').splitlines() if d.strip()][:50]


def validate(info, channel, tenant_id):
    """下單時檢查外送資料；回傳 {'address', 'phone', 'zone'}"""
    if not enabled_for(channel, tenant_id):
        raise AppError('目前未開放外送到府')
    info = info if isinstance(info, dict) else {}
    address = ' '.join(str(info.get('address') or '').split())[:200]
    phone = ''.join(ch for ch in str(info.get('phone') or '') if ch.isdigit() or ch in '+-')[:30]
    zone = str(info.get('zone') or '').strip()
    if len(address) < 6:
        raise AppError('請填寫完整的外送地址')
    digits = sum(ch.isdigit() for ch in phone)
    if not PHONE_DIGITS[0] <= digits <= 12:
        raise AppError('請填寫正確的聯絡電話')
    if zone not in {z['name'] for z in zones(tenant_id)}:
        raise AppError('請選擇配送區域')
    return {'address': address, 'phone': phone, 'zone': zone}


def fee_for(zone_name, subtotal, tenant_id=None):
    """外送費 (未達最低消費拋出 AppError)"""
    zone = next((z for z in zones(tenant_id) if z['name'] == zone_name), None)
    if zone is None:
        raise AppError('請選擇配送區域')
    if subtotal < zone['min_spend']:
        raise AppError(f'「{zone["name"]}」外送需消費滿 ${zone["min_spend"]} (目前 ${subtotal})')
    free_over = int(config(tenant_id).get('free_over') or 0)
    return 0 if free_over and subtotal >= free_over else zone['fee']


# ---------------------------------------------------------------------------
# 派車
# ---------------------------------------------------------------------------
def board_orders(start, end):
    """今天 (或預約今天) 的外送到府訂單，未結束的在前"""
    from app.services.order_service import todays_orders_condition
    orders = Order.query.filter(Order.order_type == OrderType.HOME_DELIVERY, todays_orders_condition(start, end)) \
        .order_by(Order.id.desc()).all()
    rank = {'pending': 0, 'assigned': 1, 'out': 2, 'failed': 3, 'delivered': 4, '': 5}
    return sorted(orders, key=lambda o: (o.status == OrderStatus.CANCELLED, rank.get(o.delivery_status or '', 5), o.id))


def _require_open(order):
    if order.order_type != OrderType.HOME_DELIVERY:
        raise AppError('這不是外送到府的訂單')
    if order.status == OrderStatus.CANCELLED:
        raise AppError('訂單已取消', 409)
    if order.delivery_status in ('delivered', 'failed'):
        raise AppError(f'此訂單已{DELIVERY_STATUS_LABELS[order.delivery_status]}', 409)


def assign(order, driver):
    _require_open(order)
    driver = str(driver or '').strip()[:60]
    if not driver:
        raise AppError('請選擇外送員')
    if order.delivery_status == 'out':
        raise AppError('已出發配送，無法改派', 409)
    order.driver_name, order.delivery_status = driver, 'assigned'


def depart(order):
    _require_open(order)
    if not order.driver_name:
        raise AppError('請先指派外送員')
    order.delivery_status, order.dispatched_at = 'out', datetime.now()


def complete(order, staff):
    """送達：訂單狀態改為已取餐 (尚未出餐的先標記出餐)"""
    from app.services.order_service import change_order_status
    _require_open(order)
    if order.status == OrderStatus.PENDING:
        change_order_status(order.id, OrderStatus.COMPLETED, staff, order.tenant_id)
    db.session.refresh(order)
    if order.status == OrderStatus.COMPLETED:
        change_order_status(order.id, OrderStatus.PICKED_UP, staff, order.tenant_id)
    db.session.refresh(order)
    order.delivery_status, order.delivered_at = 'delivered', datetime.now()


def fail(order, reason=''):
    _require_open(order)
    order.delivery_status = 'failed'
    order.note = (f'{order.note or ""} [配送失敗] {reason}'.strip())[:200]
