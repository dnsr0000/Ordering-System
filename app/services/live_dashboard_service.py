"""
總部即時看板：今天各分店 (線上端彙整同步上來的訂單；單機 / 分店只有本店) 的營業額、訂單數、客單價、製作中張數、
分店連線狀態，以及每小時營業額、熱銷品項、與昨天同一時間的比較。
"""
from collections import defaultdict
from datetime import datetime, timedelta

from flask import current_app

from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.sync import SyncNode
from app.services.order_service import todays_range
from app.services.report_service import REVENUE_STATUSES
from app.sync import runtime as sync_runtime

LOCAL = '本店'


def _revenue_orders(orders):
    return [o for o in orders if o.status != OrderStatus.CANCELLED and (o.payment_status or '') in REVENUE_STATUSES]


def snapshot(tenant_id, now=None):
    now = now or datetime.now()
    start, end = todays_range()
    if now.date() != start.date():
        start = datetime.combine(now.date(), datetime.min.time())
        end = start + timedelta(days=1)
    orders = Order.query.filter(Order.tenant_id == tenant_id, Order.created_at >= start, Order.created_at < end).all()
    valid = _revenue_orders(orders)
    is_hub = sync_runtime.is_hub()
    nodes = SyncNode.query.filter_by(tenant_id=tenant_id).order_by(SyncNode.id).all() if is_hub else []
    names = {n.uuid: n.name for n in nodes}
    offline_after = current_app.config.get('SYNC_NODE_OFFLINE_AFTER', 120)

    by_node = defaultdict(lambda: {'revenue': 0, 'orders': 0, 'pending': 0, 'cancelled': 0, 'online': 0})
    for o in orders:
        key = names.get(o.node_uuid, LOCAL if not is_hub else (o.node_uuid and '其他' or '線上端'))
        row = by_node[key]
        if o.status == OrderStatus.CANCELLED:
            row['cancelled'] += 1
            continue
        if (o.payment_status or '') not in REVENUE_STATUSES:
            continue
        row['orders'] += 1
        row['revenue'] += o.total_price or 0
        if o.status == OrderStatus.PENDING:
            row['pending'] += 1
        if o.order_source == 'Online':
            row['online'] += 1
    stores = []
    for n in nodes:
        row = by_node.pop(n.name, {'revenue': 0, 'orders': 0, 'pending': 0, 'cancelled': 0, 'online': 0})
        stores.append(dict(row, name=n.name, connected=n.is_online(offline_after) if n.is_active else False,
                           active=n.is_active,
                           last_seen=n.last_seen_at.strftime('%H:%M') if n.last_seen_at else '-',
                           avg_ticket=round(row['revenue'] / row['orders']) if row['orders'] else 0))
    for name, row in by_node.items():
        stores.append(dict(row, name=name, connected=True, active=True, last_seen='-',
                           avg_ticket=round(row['revenue'] / row['orders']) if row['orders'] else 0))
    stores.sort(key=lambda r: -r['revenue'])

    hourly = [0] * 24
    items = defaultdict(int)
    for o in valid:
        hourly[o.created_at.hour] += o.total_price or 0
        for it in o.items:
            if not it.is_gift:
                items[it.item_name] += it.quantity or 0
    revenue = sum(o.total_price or 0 for o in valid)

    y0 = start - timedelta(days=1)
    y_now = now - timedelta(days=1)
    yesterday = _revenue_orders(Order.query.filter(Order.tenant_id == tenant_id, Order.created_at >= y0,
                                                   Order.created_at < y_now).all())
    y_revenue = sum(o.total_price or 0 for o in yesterday)
    return {
        'at': now.strftime('%H:%M:%S'), 'is_hub': is_hub,
        'revenue': revenue, 'orders': len(valid), 'avg_ticket': round(revenue / len(valid)) if valid else 0,
        'pending': sum(1 for o in valid if o.status == OrderStatus.PENDING),
        'cancelled': sum(1 for o in orders if o.status == OrderStatus.CANCELLED),
        'yesterday_same_time': y_revenue,
        'vs_yesterday': round((revenue - y_revenue) * 100 / y_revenue, 1) if y_revenue else None,
        'stores': stores, 'hourly': hourly, 'hour_max': max(hourly) or 1,
        'top_items': [{'name': k, 'qty': v} for k, v in sorted(items.items(), key=lambda kv: -kv[1])[:10]],
        'recent': [{'id': o.id, 'time': o.created_at.strftime('%H:%M'), 'store': names.get(o.node_uuid, LOCAL),
                    'total': o.total_price, 'status': o.status_label, 'source': o.order_source}
                   for o in sorted(orders, key=lambda o: o.id, reverse=True)[:12]],
    }
