"""
備料預測：以過去幾週「同一個星期幾」的銷售量平均，預估指定日期各餐點份數、各時段來客比例，
並依配方換算原料需求、與目前庫存比較 (不足的原料可直接產生採購建議)。
"""
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func

from app.extensions import db
from app.models.enums import OrderStatus
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.services import inventory_service

WEEKDAYS = '一二三四五六日'


def forecast(target=None, weeks=4, today=None):
    today = today or date.today()
    target = target or today + timedelta(days=1)
    days = [target - timedelta(days=7 * k) for k in range(1, weeks + 8) if target - timedelta(days=7 * k) < today][:weeks]
    per_day = defaultdict(lambda: defaultdict(int))
    hours = defaultdict(int)
    active_days = set()
    for d in days:
        t0 = datetime.combine(d, datetime.min.time())
        t1 = t0 + timedelta(days=1)
        rows = db.session.query(OrderItem.menu_item_id, func.sum(OrderItem.quantity)) \
            .join(Order, Order.id == OrderItem.order_id) \
            .filter(Order.created_at >= t0, Order.created_at < t1, Order.status != OrderStatus.CANCELLED,
                    OrderItem.menu_item_id.isnot(None)) \
            .group_by(OrderItem.menu_item_id).all()
        for item_id, qty in rows:
            per_day[item_id][d] += int(qty or 0)
            active_days.add(d)
        for (created,) in db.session.query(Order.created_at).filter(
                Order.created_at >= t0, Order.created_at < t1, Order.status != OrderStatus.CANCELLED).all():
            hours[created.hour] += 1
    used = sorted(active_days)
    n = max(1, len(used))
    items = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_(list(per_day))).all()} if per_day else {}
    rows = []
    for item_id, by_day in per_day.items():
        item = items.get(item_id)
        if item is None:
            continue
        total = sum(by_day.values())
        rows.append({'id': item_id, 'name': item.name, 'category': item.category or '',
                     'avg': round(total / n, 1), 'qty': math.ceil(total / n),
                     'history': [by_day.get(d, 0) for d in used]})
    rows.sort(key=lambda r: (-r['qty'], r['name']))
    total_orders = sum(hours.values())
    hour_rows = [{'hour': h, 'share': round(c * 100 / total_orders, 1)} for h, c in sorted(hours.items())] if total_orders else []
    needs = inventory_service.needs_for({r['id']: r['qty'] for r in rows if r['qty'] > 0})
    ingredients = []
    if needs:
        from app.models.inventory import Ingredient
        for ing in Ingredient.query.filter(Ingredient.id.in_(list(needs))).order_by(Ingredient.name).all():
            need = round(needs[ing.id], 2)
            ingredients.append({'id': ing.id, 'name': ing.name, 'unit': ing.unit, 'need': need,
                                'stock': round(ing.stock or 0, 2), 'short': round(max(0, need - (ing.stock or 0)), 2)})
    return {'target': target, 'weekday': WEEKDAYS[target.weekday()], 'days': used, 'weeks': len(used),
            'items': rows, 'hours': hour_rows, 'ingredients': ingredients,
            'total_qty': sum(r['qty'] for r in rows)}
