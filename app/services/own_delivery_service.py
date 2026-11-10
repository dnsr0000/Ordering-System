"""
自有外送 (外送到府)：配送區域與外送費、下單時驗證地址電話、派車 (指派外送員 → 出發 → 送達 / 失敗)。
派車在出餐分店執行，配送狀態同步回線上端 (顧客在追蹤頁看到)。
"""
import json
from datetime import datetime

from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import DELIVERY_STATUS_LABELS, OrderStatus, OrderType
from app.models.order import Order
from app.services import settings_service

PHONE_DIGITS = (8, 10)


config = settings_service.reader('own_delivery')


# 分店回報的本店生效設定 (SyncNode.own_delivery_json) 中會覆寫總部設定的欄位
NODE_FIELDS = ('enabled', 'channels', 'zones', 'free_over', 'eta_minutes')


def _reported(node):
    """取餐分店回報的自有外送設定 (dict)；分店沒有回報 (舊版分店 / 尚未同步) 或格式不對時回傳 None"""
    raw = getattr(node, 'own_delivery_json', None) if node is not None else None
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    picked = {k: data[k] for k in NODE_FIELDS if k in data}
    if 'channels' not in picked and 'online' in data:   # 摘要只帶「線上點餐是否開放」(布林) 的回報格式
        picked['channels'] = ['online'] if data.get('online') else []
    return picked or None


def effective_config(tenant_id=None, node=None):
    """
    外送到府的生效設定：線上端傳入取餐分店 (node) 時，以該分店回報的本店設定 (是否開放、管道、配送區域、免運門檻)
    覆寫總部設定；分店沒有回報時沿用總部設定
    """
    cfg = config(tenant_id)
    reported = _reported(node)
    return {**cfg, **reported} if reported else cfg


def _int(value, default):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def node_config(node, tenant_id=None):
    """
    線上點餐選到的取餐分店實際生效的外送到府設定 {'enabled', 'zones', 'free_over', 'eta_minutes'}
    (點餐頁顯示外送選項用)：分店有回報時用分店的值；沒有選分店、舊版分店沒有回報或回報格式壞掉時沿用本機設定
    """
    cfg = effective_config(tenant_id, node)
    return {'enabled': enabled_for('online', tenant_id, node), 'zones': zones(tenant_id, node),
            'free_over': _int(cfg.get('free_over'), 0), 'eta_minutes': _int(cfg.get('eta_minutes'), 45) or 45}


def enabled_for(channel, tenant_id=None, node=None):
    cfg = effective_config(tenant_id, node)
    return bool(cfg.get('enabled')) and channel in (cfg.get('channels') or [])


def zones(tenant_id=None, node=None):
    """[{'name', 'fee', 'min_spend'}]"""
    out = []
    value = effective_config(tenant_id, node).get('zones') or ''
    if isinstance(value, list):   # 分店回報的是已解析的區域清單
        value = '\n'.join(f'{z.get("name", "")}:{z.get("fee", "")}:{z.get("min_spend", "")}'
                          for z in value if isinstance(z, dict))
    for raw in str(value).splitlines():
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


def validate(info, channel, tenant_id, node=None):
    """下單時檢查外送資料；回傳 {'address', 'phone', 'zone'}。node：線上端的取餐分店 (依分店回報的設定驗證)"""
    if not enabled_for(channel, tenant_id, node):
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
    if zone not in {z['name'] for z in zones(tenant_id, node)}:
        raise AppError('請選擇配送區域')
    return {'address': address, 'phone': phone, 'zone': zone}


def fee_for(zone_name, subtotal, tenant_id=None, node=None):
    """外送費 (未達最低消費拋出 AppError)。node：線上端的取餐分店 (依分店回報的配送區域與免運門檻計算)"""
    zone = next((z for z in zones(tenant_id, node) if z['name'] == zone_name), None)
    if zone is None:
        raise AppError('請選擇配送區域')
    if subtotal < zone['min_spend']:
        raise AppError(f'「{zone["name"]}」外送需消費滿 ${zone["min_spend"]} (目前 ${subtotal})')
    try:
        free_over = max(0, int(effective_config(tenant_id, node).get('free_over') or 0))
    except (TypeError, ValueError):
        free_over = 0
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
    changed = bool(order.driver_name) and order.driver_name != driver
    order.driver_name, order.delivery_status = driver, 'assigned'
    from app.services import delivery_tracking_service
    # 外送員手機回報位置的連結 (派車頁顯示 QR code)；改派給另一位時換新連結，舊連結失效
    delivery_tracking_service.ensure_token(order, new_driver=changed)


def depart(order):
    _require_open(order)
    if not order.driver_name:
        raise AppError('請先指派外送員')
    # 只能從「已指派」出發；條件式更新避免連按兩下 (兩個請求同時通過檢查) 重設出發時間並重送推播
    claimed = db.session.execute(
        update(Order).where(Order.id == order.id, Order.tenant_id == order.tenant_id, Order.delivery_status == 'assigned')
        .values(delivery_status='out').execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    db.session.refresh(order)
    if not claimed:
        raise AppError('此訂單已出發配送' if order.delivery_status == 'out' else '請先指派外送員', 409)
    order.dispatched_at = datetime.now()
    from app.services import delivery_tracking_service
    delivery_tracking_service.ensure_token(order)
    from app.services import push_service
    push_service.notify_order(order, 'delivery_out')


def complete(order, staff):
    """送達：訂單狀態改為已取餐 (尚未出餐的先標記出餐)"""
    from app.services.order_service import change_order_status
    _require_open(order)
    if order.delivery_status != 'out':
        raise AppError('外送員尚未出發，無法標記送達', 409)
    if order.status == OrderStatus.PENDING:
        change_order_status(order.id, OrderStatus.COMPLETED, staff, order.tenant_id)
    db.session.refresh(order)
    if order.status == OrderStatus.COMPLETED:
        change_order_status(order.id, OrderStatus.PICKED_UP, staff, order.tenant_id, trusted_delivery_pickup=True)
    db.session.refresh(order)
    order.delivery_status, order.delivered_at = 'delivered', datetime.now()


def fail(order, reason=''):
    _require_open(order)
    order.delivery_status = 'failed'
    order.note = (f'{order.note or ""} [配送失敗] {reason}'.strip())[:200]
