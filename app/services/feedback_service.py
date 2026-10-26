"""
餐後滿意度：顧客在出餐追蹤頁 (/track/<訂單>) 於出餐後填寫 1~5 星與意見。
高分 (系統設定 → 顧客評價) 邀請到 Google 留下評論；低分立即通知店家 (監控通知管道)。
"""
from datetime import datetime, timedelta

from sqlalchemy import func

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.order import Order, OrderFeedback
from app.services import settings_service

RATEABLE = (OrderStatus.COMPLETED, OrderStatus.PICKED_UP)
RATE_WINDOW_DAYS = 7


def config(tenant_id):
    return settings_service.get('feedback', tenant_id)


def existing(order):
    return OrderFeedback.query.filter_by(order_id=order.id).execution_options(skip_tenant_filter=True) \
        .order_by(OrderFeedback.id.desc()).first()


def state(order):
    """追蹤頁需要的評分狀態"""
    cfg = config(order.tenant_id)
    fb = existing(order)
    return {'enabled': bool(cfg.get('enabled', True)) and order.status in RATEABLE and _within_window(order),
            'done': fb is not None, 'rating': fb.rating if fb else 0,
            'review_url': _review_url(cfg, fb.rating) if fb else ''}


def _within_window(order):
    return order.created_at is None or order.created_at >= datetime.now() - timedelta(days=RATE_WINDOW_DAYS)


def _review_url(cfg, rating):
    url = (cfg.get('google_review_url') or '').strip()
    return url if url and rating >= int(cfg.get('review_min_rating') or 4) else ''


def submit(order, rating, comment=''):
    """建立評分 (每張訂單一次)；回傳 (OrderFeedback, Google 評論連結或空字串)"""
    from app.services import monitor_service
    cfg = config(order.tenant_id)
    if not cfg.get('enabled', True):
        raise AppError('本店目前未開放評分', 404)
    if order.status not in RATEABLE:
        raise AppError('餐點完成後才能評分', 409)
    if not _within_window(order):
        raise AppError(f'超過 {RATE_WINDOW_DAYS} 天的訂單無法評分', 409)
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        rating = 0
    if not 1 <= rating <= 5:
        raise AppError('請選擇 1~5 顆星')
    if existing(order) is not None:
        raise AppError('這張訂單已經評分過了，謝謝您！', 409)
    fb = OrderFeedback(tenant_id=order.tenant_id, order_id=order.id, rating=rating,
                       comment=str(comment or '').strip()[:500], created_at=datetime.now())
    db.session.add(fb)
    db.session.commit()
    alert_max = int(cfg.get('alert_max_rating') or 0)
    if alert_max and rating <= alert_max:
        monitor_service.alert(f'顧客評價 {rating} 星 (訂單 #{order.id}，取餐 #{order.display_pickup_number})：'
                              f'{fb.comment or "(未留言)"}', key=f'feedback-{fb.id}', tenant_id=order.tenant_id,
                              level='warning', source='feedback')
    return fb, _review_url(cfg, rating)


def summary(days=30):
    """後台「顧客評價」：平均、各星數筆數、最近的評價 (同一張訂單只取最新一筆)"""
    since = datetime.now() - timedelta(days=days)
    latest = db.session.query(func.max(OrderFeedback.id)).filter(OrderFeedback.created_at >= since) \
        .group_by(OrderFeedback.order_id)
    rows = OrderFeedback.query.filter(OrderFeedback.id.in_(latest)).order_by(OrderFeedback.id.desc()).all()
    dist = {n: 0 for n in range(5, 0, -1)}
    for r in rows:
        dist[r.rating] = dist.get(r.rating, 0) + 1
    count = len(rows)
    return {'count': count, 'average': round(sum(r.rating for r in rows) / count, 2) if count else 0,
            'distribution': dist, 'rows': rows[:300],
            'low': sum(1 for r in rows if r.rating <= 2)}


def order_label(fb):
    o = fb.order if fb.order_id else None
    return f'#{o.id} (取餐 #{o.display_pickup_number})' if isinstance(o, Order) else '-'
