"""營運分析：今日營收、客單價、待製作與預估出餐時間、熱門排行"""
import math
import statistics
from datetime import date

from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from app.models.enums import OrderStatus
from app.models.order import Order

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
    """分析只需要「今天的訂單 + 所有尚未出餐的訂單」，並預載明細避免 N+1"""
    from app.services.order_service import todays_range
    start, _end = todays_range()
    return (Order.query.options(selectinload(Order.items))
            .filter(or_(Order.created_at >= start, Order.status == OrderStatus.PENDING))
            .order_by(Order.id.desc()).all())


def build_order_analytics(orders, limit=3):
    today = date.today()
    todays = [o for o in orders if o.created_at and o.created_at.date() == today]
    valid = [o for o in todays if o.status != OrderStatus.CANCELLED]

    revenue = sum(o.total_price or 0 for o in valid)
    paid = [o for o in valid if (o.total_price or 0) > 0]  # 客單價排除實付 0 元的純點數兌換單

    # 待製作 / 預估出餐只算今天 (與廚房看板一致)；前幾日遺留的另以 stale_pending_count 提示
    pending = sum(1 for o in todays if o.status == OrderStatus.PENDING)
    stale_pending = sum(1 for o in orders if o.status == OrderStatus.PENDING and o.created_at and o.created_at.date() < today)

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
            qty = i.quantity or 1
            st['quantity'] += qty
            st['revenue'] += int(i.price or 0) * qty
    top = sorted(stats.items(), key=lambda kv: (kv[1]['quantity'], kv[1]['revenue']), reverse=True)[:limit]

    return {
        'today_orders': len(valid),
        'today_revenue': revenue,
        'avg_order_value': round(revenue / len(paid), 1) if paid else 0,
        'pending_count': pending,
        'stale_pending_count': stale_pending,
        'eta_minutes': int(math.ceil(pending * per_order - 1e-9)) if pending else 0,
        'eta_per_order_minutes': round(per_order, 1),
        'eta_based_on_samples': samples,
        'completed_today': len(completed),
        'avg_order_time': round(avg_minutes, 1) if samples else 0,
        'top_items': [{'name': name, 'quantity': st['quantity'], 'revenue': st['revenue']} for name, st in top],
    }
