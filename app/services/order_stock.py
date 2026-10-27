"""
取餐號與庫存 (由 order_service 拆出)：每日取餐號計數、條件式 UPDATE 扣 / 回補庫存 (本機與線上端的分店庫存快照)、
線上訂單的取餐分店、訂單佔用的庫存 (含套餐配餐)。
"""
import json
import logging
from datetime import date, datetime

from flask import current_app
from sqlalchemy import case, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.extensions import db
from app.models.menu import ComboOption, MenuItem
from app.models.order import DailyCounter, Order
from app.models.sync import NodeStock, SyncNode

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

PICKUP_COUNTER = 'pickup'
MAX_PICKUP_NUMBER = 999


def next_pickup_number(tenant_id):
    """取得今日下一個取餐號 (1~999 循環)。必須在結帳交易內呼叫：計數列會鎖到交易結束。"""
    today = date.today()
    key = dict(tenant_id=tenant_id, business_date=today, name=PICKUP_COUNTER)
    stmt = (
        update(DailyCounter)
        .where(*(getattr(DailyCounter, k) == v for k, v in key.items()))
        .values(value=DailyCounter.value % MAX_PICKUP_NUMBER + 1)
        .returning(DailyCounter.value)
        .execution_options(synchronize_session=False)
    )
    value = db.session.execute(stmt).scalar()
    if value is None:
        # 今天第一張：建立計數列 (並接續今天已存在訂單的最後號碼，例如升級當天)。
        # 兩個請求同時建立時，後到的會撞上主鍵，改為直接更新即可。
        last = db.session.execute(
            select(Order.pickup_number)
            .where(Order.tenant_id == tenant_id, Order.created_at >= datetime.combine(today, datetime.min.time()))
            .order_by(Order.id.desc()).limit(1)
        ).scalar() or 0
        try:
            with db.session.begin_nested():
                db.session.execute(insert(DailyCounter).values(**key, value=last))
        except IntegrityError:
            pass
        value = db.session.execute(stmt).scalar()
    return value


def _change_stock(tenant_id, demands, sign, node_id=None):
    """
    依品項 ID 排序後逐一增減庫存 (固定順序避免 PostgreSQL 交叉鎖死)；扣庫存失敗回傳該品項。
    node_id 有值 (線上端的線上點餐) 時改用該分店回報的庫存快照。
    """
    if node_id is not None:
        return _change_node_stock(node_id, demands, sign)
    for item_id in sorted(demands):
        qty = demands[item_id]
        stmt = update(MenuItem).where(MenuItem.id == item_id, MenuItem.tenant_id == tenant_id)
        if sign < 0:
            stmt = stmt.where(MenuItem.stock >= qty, MenuItem.is_sold_out.is_not(True)).values(
                stock=MenuItem.stock - qty,
                is_sold_out=case((MenuItem.stock - qty <= 0, True), else_=MenuItem.is_sold_out),
            )
        else:
            # 回補庫存：庫存原本歸零而被自動標為售完者解除售完；店員手動標示的售完 (庫存 > 0) 保持不變
            stmt = stmt.values(
                stock=MenuItem.stock + qty,
                is_sold_out=case((MenuItem.stock <= 0, False), else_=MenuItem.is_sold_out),
            )
        if db.session.execute(stmt.execution_options(synchronize_session=False)).rowcount == 0 and sign < 0:
            return item_id
    return None


def _deduct_stock(tenant_id, demands, gift_demands=None, node_id=None):
    """
    扣庫存 (依品項 ID 排序，固定鎖的順序)。促銷贈品是「有就送」：贈品那一份扣不到時略過贈品，不讓整筆訂單失敗。
    回傳 (不足的品項 ID 或 None, 送完而略過的贈品 ID 集合)。
    """
    gift_demands = gift_demands or {}
    dropped = set()
    for item_id in sorted(set(demands) | set(gift_demands)):
        base, gift = demands.get(item_id, 0), gift_demands.get(item_id, 0)
        if gift:
            if _change_stock(tenant_id, {item_id: base + gift}, -1, node_id=node_id) is None:
                continue
            dropped.add(item_id)
        if base and _change_stock(tenant_id, {item_id: base}, -1, node_id=node_id) is not None:
            return item_id, dropped
    return None, dropped


def consume_stock_unchecked(tenant_id, demands):
    """
    分店收到線上訂單時扣庫存：訂單已在線上成立，不因本機庫存不足而失敗 (最多扣到 0 並標示售完)，
    店員可視情況取消訂單 (取消時會回補)。回傳不足的品項 [(品名, 需要, 原本庫存)] 供通知店員。
    """
    short = []
    if demands:
        for item_id, name, stock in db.session.execute(
                select(MenuItem.id, MenuItem.name, MenuItem.stock).where(MenuItem.id.in_(list(demands)))).all():
            if (stock or 0) < demands[item_id]:
                short.append((name, demands[item_id], max(0, stock or 0)))
    for item_id in sorted(demands):
        qty = demands[item_id]
        db.session.execute(
            update(MenuItem).where(MenuItem.id == item_id, MenuItem.tenant_id == tenant_id)
            .values(stock=case((MenuItem.stock - qty < 0, 0), else_=MenuItem.stock - qty),
                    is_sold_out=case((MenuItem.stock - qty <= 0, True), else_=MenuItem.is_sold_out))
            .execution_options(synchronize_session=False))
    return short


def _change_node_stock(node_id, demands, sign):
    """線上端：以分店回報的庫存預扣 (分店下次回報時會以實際庫存覆蓋)；沒有回報過的品項視為不供應"""
    for item_id in sorted(demands):
        qty = demands[item_id]
        stmt = update(NodeStock).where(NodeStock.node_id == node_id, NodeStock.menu_item_id == item_id)
        if sign < 0:
            stmt = stmt.where(NodeStock.stock >= qty, NodeStock.is_sold_out.is_(False)).values(stock=NodeStock.stock - qty)
        else:
            stmt = stmt.values(stock=NodeStock.stock + qty)
        if db.session.execute(stmt.execution_options(synchronize_session=False)).rowcount == 0 and sign < 0:
            return item_id
    return None


def online_pickup_node(tenant_id, node_uuid):
    """線上端：顧客選擇的取餐分店 (需啟用且開放線上點餐)"""
    node = SyncNode.query.filter_by(tenant_id=tenant_id, uuid=str(node_uuid or ''), is_active=True,
                                    accepts_online_orders=True).first() if node_uuid else None
    if node is None:
        raise AppError('請先選擇取餐分店！')
    if not node.is_online(current_app.config['SYNC_NODE_OFFLINE_AFTER']):
        raise AppError(f'【{node.name}】目前離線，暫停線上點餐，請稍後再試或改選其他分店！')
    return node


def order_stock_demands(order):
    """訂單佔用的庫存 (含套餐配餐) {menu_item_id: 數量}"""
    demands = {}
    recorded = {}
    for item in order.items:
        if item.combo_sides:
            try:
                recorded[item.id] = [str(u) for u in json.loads(item.combo_sides)]
            except (TypeError, ValueError):
                pass
    uuids = {u for sides in recorded.values() for u in sides}
    by_uuid = dict(db.session.execute(select(MenuItem.uuid, MenuItem.id).where(
        MenuItem.tenant_id == order.tenant_id, MenuItem.uuid.in_(uuids))).all()) if uuids else {}
    for item in order.items:
        if item.menu_item_id:
            demands[item.menu_item_id] = demands.get(item.menu_item_id, 0) + item.quantity
        if item.id in recorded:   # 賣出當下記錄的配餐 (已刪除的餐點略過)
            side_ids = [by_uuid[u] for u in recorded[item.id] if u in by_uuid]
        else:                     # 舊訂單沒有記錄：用目前的套餐內容
            combo = db.session.get(ComboOption, item.combo_option_id) if item.combo_option_id else None
            side_ids = combo.side_item_ids if combo else []
        for side_id in side_ids:
            demands[side_id] = demands.get(side_id, 0) + item.quantity
    return demands
