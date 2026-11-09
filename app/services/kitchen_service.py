"""
廚房工作站：品項歸屬 (品項指定的工作站優先，其次依分類)、各站出餐進度。
各站把自己負責的品項標記完成；整張訂單所有品項都完成時自動轉為「已出餐」。
沒有建立任何工作站時，廚房看板維持原本的整張訂單模式。
"""
import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select, update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem
from app.models.printing import KitchenStation


def active_stations(tenant_id=None):
    """
    啟用中的工作站。有傳 tenant_id 時明確限定店家：沒有請求情境 (背景執行緒) 時自動店家過濾不會生效，
    不限定會撈到別店的工作站 (同名分類對應到別店工作站 id)。
    """
    if tenant_id is not None:
        q = KitchenStation.query.filter_by(tenant_id=tenant_id, is_active=True).execution_options(skip_tenant_filter=True)
    else:
        q = KitchenStation.query.filter_by(is_active=True)
    return q.order_by(KitchenStation.sort_order, KitchenStation.id).all()


def station_resolver(stations=None, tenant_id=None):
    """回傳 fn(menu_item) → station_id 或 None (不屬於任何工作站)"""
    stations = stations if stations is not None else active_stations(tenant_id)
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


def has_quantity(item):
    """還有份數要做 (單品作廢到 0 份的品項不算待做)；舊資料 quantity 為空時視為 1 份"""
    return item.quantity is None or item.quantity > 0


def menu_by_uuid(tenant_id):
    """本店餐點 {uuid: MenuItem} (解析套餐配餐用)"""
    from app.models.menu import MenuItem
    return {m.uuid: m for m in MenuItem.query.filter(MenuItem.tenant_id == tenant_id)
            .execution_options(skip_tenant_filter=True).all()}


def combo_sides_of(item, by_uuid):
    """套餐主餐明細記錄的配餐 (combo_sides 的 uuid 清單) → 本店 MenuItem 清單 (已刪除的餐點略過)"""
    if not item.combo_sides:
        return []
    try:
        uuids = [str(u) for u in json.loads(item.combo_sides)]
    except (TypeError, ValueError):
        return []
    return [by_uuid[u] for u in uuids if u in by_uuid]


def side_line(item, side):
    """
    套餐配餐的虛擬明細 (配餐不是獨立的 OrderItem，只記在主餐的 combo_sides)：給 KDS / 工作站出單顯示用。
    id 為 None (不能直接標記完成)；份數與完成狀態跟著主餐。
    """
    return SimpleNamespace(id=None, parent_id=item.id, menu_item_id=side.id, item_name=side.name,
                           quantity=item.quantity, customization=f'套餐配餐 ({item.item_name})',
                           prepared_at=item.prepared_at, voided_qty=0, is_reward=False)


def items_for_station(order, station_id, resolve):
    """
    這張訂單中屬於該工作站的品項；station_id=None 代表全部。
    套餐的配餐依配餐本身的工作站加上虛擬明細 (side_line，接在主餐後面)，工作站印表機才印得到套餐飲料、固定配餐。
    """
    from app.models.menu import MenuItem
    by_uuid = menu_by_uuid(order.tenant_id) if any(it.combo_sides for it in order.items) else {}
    out = []
    for it in order.items:
        if station_id is None:
            out.append(it)
        else:
            mi = db.session.get(MenuItem, it.menu_item_id) if it.menu_item_id else None
            if resolve(mi) == station_id:
                out.append(it)
        for side in combo_sides_of(it, by_uuid):
            if station_id is None or resolve(side) == station_id:
                out.append(side_line(it, side))
    return out


def complete_station(order_id, station_id, tenant_id, staff):
    """工作站完成這張訂單的品項；所有品項都完成時訂單轉為已出餐。回傳 (本站完成數, 訂單是否已出餐)"""
    from app.services.order_service import change_order_status
    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != tenant_id:
        raise AppError('查無此訂單！', 404)
    # 鎖住訂單列：兩個工作站同時按完成時排隊處理，後面那個才看得到前一個已完成的品項 (否則兩邊都以為還有別站沒做完，
    # 訂單永遠停在製作中)；SQLite 本來就一次只有一個寫入交易
    db.session.execute(select(Order.id).where(Order.id == order.id).with_for_update())
    db.session.refresh(order)
    if order.status != OrderStatus.PENDING:
        raise AppError('訂單已不在製作中', 409)
    station = db.session.get(KitchenStation, station_id)
    if station is None or station.tenant_id != tenant_id:
        raise AppError('查無此工作站', 404)
    resolve = station_resolver(tenant_id=tenant_id)
    # 套餐配餐的虛擬明細 (id 為 None) 跟著主餐，不在這裡標記
    mine = [it.id for it in items_for_station(order, station.id, resolve) if it.id is not None and it.prepared_at is None]
    now = datetime.now()
    if mine:
        db.session.execute(update(OrderItem).where(OrderItem.id.in_(mine), OrderItem.prepared_at.is_(None))
                           .values(prepared_at=now).execution_options(synchronize_session=False))
    db.session.expire(order, ['items'])
    # 不屬於任何工作站的品項 (沒有對應)、整個作廢 (剩 0 份) 的品項不阻擋出餐
    remaining = [it for it in order.items
                 if it.prepared_at is None and has_quantity(it) and resolve(_menu(it)) is not None]
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
