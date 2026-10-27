"""取餐通知：餐點完成時 LINE 推播給綁定 LINE 的會員 (叫號看板語音與手機追蹤頁由前端處理)"""
from app.extensions import db
from app.services import deferred, settings_service


def order_ready(order):
    """訂單轉為已出餐時呼叫 (在狀態變更的交易中)；推播在交易提交後送出"""
    if order is None or not order.user_id:
        return
    cfg = settings_service.get('notify', order.tenant_id)
    if not cfg.get('line_ready'):
        return
    deferred.defer(push_ready, order.id)


def push_ready(order_id):
    from app.models.order import Order
    from app.services import line_service
    order = db.session.get(Order, order_id)
    if order is None or order.user is None or not order.user.line_user_id:
        return False
    cfg = settings_service.get('notify', order.tenant_id)
    template = cfg.get('line_ready_template') or '您的餐點 (取餐號 {number}) 已完成，請至櫃檯取餐！'
    shop = order.user.tenant.name if getattr(order.user, 'tenant', None) else ''
    if not shop:
        from app.models.tenant import Tenant
        t = db.session.get(Tenant, order.tenant_id)
        shop = t.name if t else ''
    text = template.replace('{number}', f'{order.display_pickup_number:03d}').replace('{shop}', shop)
    return line_service.push_text(order.tenant_id, order.user.line_user_id, text)
