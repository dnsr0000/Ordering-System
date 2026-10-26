"""
廚房工作站：品項歸屬 (品項指定的工作站優先，其次依分類)、各站出餐進度。
各站把自己負責的品項標記完成；整張訂單所有品項都完成時自動轉為「已出餐」。
沒有建立任何工作站時，廚房看板維持原本的整張訂單模式。
"""
from datetime import datetime

from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem
from app.models.printing import KitchenStation


def active_stations():
    return KitchenStation.query.filter_by(is_active=True).order_by(KitchenStation.sort_order, KitchenStation.id).all()


def station_resolver(stations=None):
    """回傳 fn(menu_item) → station_id 或 None (不屬於任何工作站)"""
    stations = stations if stations is not None else active_stations()
    active_ids = {s.id for s in stations}
    by_category = {}
    for s in stations:
        for cat in s.category_list:
            by_category.setdefault(cat, s.id)

    def resolve(menu_item):
        if menu_item is None:
            return None
        if menu_item.station_id in active_ids:
            return menu_item.station_id
        return by_category.get((menu_item.category or '').strip())
    return resolve


def items_for_station(order, station_id, resolve):
    """這張訂單中屬於該工作站的品項；station_id=None 代表全部"""
    from app.models.menu import MenuItem
    out = []
    for it in order.items:
        if station_id is None:
            out.append(it)
            continue
        mi = db.session.get(MenuItem, it.menu_item_id) if it.menu_item_id else None
        if resolve(mi) == station_id:
            out.append(it)
    return out


def complete_station(order_id, station_id, tenant_id, staff):
    """工作站完成這張訂單的品項；所有品項都完成時訂單轉為已出餐。回傳 (本站完成數, 訂單是否已出餐)"""
    from app.services.order_service import change_order_status
    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != tenant_id:
        raise AppError('查無此訂單！', 404)
    if order.status != OrderStatus.PENDING:
        raise AppError('訂單已不在製作中', 409)
    station = db.session.get(KitchenStation, station_id)
    if station is None or station.tenant_id != tenant_id:
        raise AppError('查無此工作站', 404)
    resolve = station_resolver()
    mine = [it.id for it in items_for_station(order, station.id, resolve) if it.prepared_at is None]
    now = datetime.now()
    if mine:
        db.session.execute(update(OrderItem).where(OrderItem.id.in_(mine), OrderItem.prepared_at.is_(None))
                           .values(prepared_at=now).execution_options(synchronize_session=False))
    db.session.expire(order, ['items'])
    # 不屬於任何工作站的品項 (沒有對應) 不阻擋出餐
    remaining = [it for it in order.items if it.prepared_at is None and resolve(_menu(it)) is not None]
    if remaining:
        db.session.commit()
        from app.services.event_bus import order_event_bus
        order_event_bus.notify(tenant_id)
        return len(mine), False
    db.session.commit()
    change_order_status(order.id, OrderStatus.COMPLETED, staff, tenant_id)
    return len(mine), True


def _menu(item):
    from app.models.menu import MenuItem
    return db.session.get(MenuItem, item.menu_item_id) if item.menu_item_id else None


def set_rush(order_id, tenant_id, is_rush):
    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != tenant_id:
        raise AppError('查無此訂單！', 404)
    if order.status != OrderStatus.PENDING:
        raise AppError('只有製作中的訂單可以加急', 409)
    order.is_rush = bool(is_rush)
    return order
