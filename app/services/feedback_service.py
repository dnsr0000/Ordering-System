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


config = settings_service.reader('feedback')


def existing(order):
    return OrderFeedback.query.filter_by(order_id=order.id).execution_options(skip_tenant_filter=True) \
        .order_by(OrderFeedback.id.desc()).first()


def state(order):
    """追蹤頁需要的評分狀態"""
    cfg = config(order.tenant_id)
    fb = existing(order)
    return {'enabled': bool(cfg.get('enabled', True)) and order.status in RATEABLE and _within_window(order),
            'done': fb is not None, 'rating': fb.rating if fb else 0,
            'review_url': _review_url(cfg, fb.rating) if fb else '',
            'reply': (fb.reply if fb else '') or ''}


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
    except (TypeError, ValueError, OverflowError):   # JSON 的 1e400 解析成 inf，int(inf) 拋 OverflowError
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


# ---------------------------------------------------------------------------
# 店家回覆與公開精選
# ---------------------------------------------------------------------------
def _mask_name(name):
    name = (name or '').strip()
    return (name[0] + '＊' * max(1, len(name) - 1)) if name else '匿名顧客'


def reply(fb, text, is_public=None, staff=''):
    """店長回覆一句 (通知顧客：LINE 綁定會員推文字、追蹤頁推播)；is_public 不為 None 時一併設定公開"""
    from app.services import deferred, line_service, push_service
    text = str(text or '').strip()[:300]
    old = (fb.reply or '').strip()
    fb.reply = text
    fb.replied_at = datetime.now() if text else None
    if is_public is not None:
        fb.is_public = bool(is_public)
    order = fb.order
    # 回覆內容有變才通知 (後台表單預填原回覆：只切換「公開」再儲存不可重發 LINE / 推播)
    if text and text != old and order is not None:
        push_service.notify_order(order, 'feedback_reply')
        if order.user is not None and order.user.line_user_id:
            from app.models.tenant import Tenant
            shop = db.session.get(Tenant, order.tenant_id)
            deferred.defer(line_service.push_text, order.tenant_id, order.user.line_user_id,
                           f'{shop.name if shop else ""} 回覆了您對訂單 #{order.id} 的評價：\n{text}')
    return fb


def public_reviews(tenant_id, limit=5, min_rating=4):
    """點餐頁 / 追蹤頁的「顧客好評」：公開、有留言、星數達標的最新幾筆 (姓名遮罩，不帶訂單細節)"""
    rows = (OrderFeedback.query.filter(OrderFeedback.tenant_id == tenant_id, OrderFeedback.is_public.is_(True),
                                       OrderFeedback.rating >= min_rating, OrderFeedback.comment != '')
            .execution_options(skip_tenant_filter=True).order_by(OrderFeedback.id.desc()).limit(limit).all())
    out = []
    for r in rows:
        user = r.order.user if r.order is not None else None
        out.append({'rating': r.rating, 'comment': r.comment, 'reply': r.reply or '',
                    'name': _mask_name(user.name if user is not None else ''),
                    'date': r.created_at.strftime('%Y-%m-%d') if r.created_at else ''})
    return out
