"""營運分析：今日營收、客單價、待製作與預估出餐時間、熱門排行"""
import math
import statistics
from collections import Counter
from datetime import date, datetime, time, timedelta

from flask import has_app_context
from sqlalchemy import and_, or_
from sqlalchemy.orm import selectinload

from app.models.enums import OrderStatus
from app.models.order import Order
from app.services.eta_service import HIDDEN_PAYMENT_STATUSES
from app.services.report_service import is_revenue
from app.sync import runtime as sync_runtime

DEFAULT_MINUTES_PER_ORDER = 3.0   # 今天還沒有出餐紀錄時，每張待製作訂單估幾分鐘
OUTLIER_HARD_CAP_MINUTES = 90.0   # 超過即視為「忘了按出餐」之類的異常，不納入平均


def mean_without_outliers(values, hard_cap=OUTLIER_HARD_CAP_MINUTES):
    """先丟掉負數與超過上限的值；樣本數 >= 4 時再以 IQR 排除離群值。回傳 (平均, 樣本數)"""
    vals = sorted(v for v in values if 0 <= v <= hard_cap)
    if len(vals) >= 4:
        q1, _q2, q3 = statistics.quantiles(vals, n=4, method='inclusive')
        iqr = q3 - q1
        vals = [v for v in vals if q1 - 1.5 * iqr <= v <= q3 + 1.5 * iqr]
    if not vals:
        return 0.0, 0
    return sum(vals) / len(vals), len(vals)


def load_dashboard_orders():
    """分析只需要「今天的訂單 (含前幾天預約今天取餐的) + 所有尚未出餐的訂單」，並預載明細避免 N+1"""
    from app.services.order_service import todays_range
    start, end = todays_range()
    return (Order.query.options(selectinload(Order.items))
            .filter(or_(Order.created_at >= start, Order.status == OrderStatus.PENDING,
                        and_(Order.pickup_at >= start, Order.pickup_at < end)))
            .order_by(Order.id.desc()).all())


def _net(order):
    """實收：扣掉部分退款 (單品作廢等) 後的金額"""
    return max(0, (order.total_price or 0) - (order.refunded_amount or 0))


def _in_between(value, start, end):
    return value is not None and start <= value < end


def _in_kitchen_today(o, start, end):
    """
    與廚房看板相同的條件 (order_lifecycle.todays_orders_condition(include_future=False) 且已送廚房)：
    今天建立 (不含預約明天以後取餐)、預約今天取餐、或前幾天建立今天才付款的製作中訂單；尚未完成付款的線上單不算
    """
    if o.status != OrderStatus.PENDING or (o.payment_status or '') in HIDDEN_PAYMENT_STATUSES:
        return False
    not_future = o.pickup_at is None or o.pickup_at < end
    created_today = _in_between(o.created_at, start, end) and not_future
    paid_today = (o.payment_status == 'paid' and _in_between(o.paid_at, start, end)
                  and o.created_at is not None and o.created_at < start and not_future)
    return created_today or _in_between(o.pickup_at, start, end) or paid_today


def _eta_pending(kitchen_orders):
    """預估等候用的排隊張數：線上端彙整多家分店的訂單，各分店的廚房分開排隊，取最多的一家"""
    if not (has_app_context() and sync_runtime.is_hub()):
        return len(kitchen_orders)
    by_node = Counter(o.node_uuid for o in kitchen_orders)
    return max(by_node.values(), default=0)


def build_order_analytics(orders, limit=3):
    today = date.today()
    start = datetime.combine(today, time.min)
    end = start + timedelta(days=1)
    todays = [o for o in orders if o.created_at and o.created_at.date() == today]
    # 營收 / 訂單數 / 客單價 / 熱銷與營運報表同一口徑：未取消且已收款 (待付款、授權中、桌邊未結帳、待退款不算)
    valid = [o for o in todays if is_revenue(o)]

    revenue = sum(_net(o) for o in valid)
    paid = [o for o in valid if _net(o) > 0]  # 客單價排除實付 0 元的純點數兌換單

    # 待製作 / 預估出餐與廚房看板一致 (含預約今天取餐、不含預約明天以後與未付款的線上單)；
    # 前幾日遺留且不是預約今天以後取餐的另以 stale_pending_count 提示
    kitchen = [o for o in orders if _in_kitchen_today(o, start, end)]
    pending = len(kitchen)
    stale_pending = sum(1 for o in orders
                        if o.status == OrderStatus.PENDING and o.created_at and o.created_at < start
                        and (o.payment_status or '') not in HIDDEN_PAYMENT_STATUSES
                        and (o.pickup_at is None or o.pickup_at < start) and not _in_kitchen_today(o, start, end))
    eta_pending = _eta_pending(kitchen)

    # 平均出餐時間：建單到出餐 (分鐘)；系統換日自動結案的訂單沒有 completed_at，不列入
    completed = [o for o in todays if o.status in (OrderStatus.COMPLETED, OrderStatus.PICKED_UP)]
    durations = [(o.completed_at - o.created_at).total_seconds() / 60.0
                 for o in completed if o.completed_at and o.created_at and o.completed_at >= o.created_at]
    avg_minutes, samples = mean_without_outliers(durations)
    per_order = avg_minutes if samples else DEFAULT_MINUTES_PER_ORDER

    stats = {}
    for o in valid:
        for i in o.items:
            st = stats.setdefault(i.item_name, {'quantity': 0, 'revenue': 0})
            qty = i.quantity if i.quantity is not None else 1   # 單品作廢後可能為 0
            st['quantity'] += qty
            st['revenue'] += int(i.price or 0) * qty
    top = sorted(stats.items(), key=lambda kv: (kv[1]['quantity'], kv[1]['revenue']), reverse=True)[:limit]

    return {
        'today_orders': len(valid),
        'today_revenue': revenue,
        'avg_order_value': round(revenue / len(paid), 1) if paid else 0,
        'pending_count': pending,
        'stale_pending_count': stale_pending,
        'eta_minutes': int(math.ceil(eta_pending * per_order - 1e-9)) if eta_pending else 0,
        'eta_per_order_minutes': round(per_order, 1),
        'eta_based_on_samples': samples,
        'completed_today': len(completed),
        'avg_order_time': round(avg_minutes, 1) if samples else 0,
        'top_items': [{'name': name, 'quantity': st['quantity'], 'revenue': st['revenue']} for name, st in top],
    }
