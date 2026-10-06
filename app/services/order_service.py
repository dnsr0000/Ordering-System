import re
from datetime import datetime
from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.coupon import UserCoupon
from app.services.event_bus import order_event_bus
from app.services.ai_service import _AI_ADVICE_STATE, fallback_advice 

def sanitize_discount_value(discount_type, discount_value):
    try:
        val = float(discount_value)
    except (ValueError, TypeError):
        return 0.0
    if discount_type == 'percent':
        if 10.0 < val <= 100.0:
            val = val / 100.0
        elif 1.0 < val <= 10.0:
            val = val / 10.0
        return round(min(max(val, 0.01), 0.99), 2)
    else:
        return max(0.0, round(val, 2))

def calc_promo_discount(coupon, subtotal):
    """計算優惠券的實際折扣金額（內建防呆與上下限夾擠）"""
    if not coupon or subtotal <= 0:
        return 0
    if coupon.discount_type == 'fixed':
        return min(coupon.discount_value, subtotal)
    else:
        safe_percent = sanitize_discount_value('percent', coupon.discount_value)
        return min(subtotal, max(0.0, round(subtotal * (1.0 - safe_percent))))

def update_popular_items():
    try:
        db.session.query(MenuItem).update({MenuItem.is_recommended: False})
        db.session.flush()

        sales_stats = db.session.query(
            OrderItem.item_name,
            db.func.sum(OrderItem.quantity).label('total_qty')
        ).join(Order, OrderItem.order_id == Order.id
        ).filter(Order.status != 'Cancelled'
        ).filter(~OrderItem.item_name.like('%[點數兌換]%')
        ).group_by(OrderItem.item_name
        ).order_by(db.desc('total_qty')
        ).all()

        top_3_names = [name for name, qty in sales_stats[:3] if qty and qty > 0]
        if top_3_names:
            MenuItem.query.filter(MenuItem.name.in_(top_3_names)).update(
                {MenuItem.is_recommended: True}, synchronize_session=False
            )

        MenuItem.query.filter_by(is_manual_popular=True).update(
            {MenuItem.is_recommended: True}, synchronize_session=False
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"自動更新熱門失敗: {e}")

def cancel_order_and_rollback(order):
    if order.status == 'Cancelled':
        return False, "該訂單早已取消，不重複執行回滾！"

    tenant_id = getattr(order, 'tenant_id', None)
    reward_points_to_refund = 0

    # 1. 庫存回補與計算需退還的紅利兌換點數
    for oi in order.items:
        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', oi.item_name).strip()
        
        # 加上 tenant_id 限制，防止跨店抓錯商品
        query = MenuItem.query.filter_by(name=clean_name)
        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)
        menu_item = query.first()

        if menu_item:
            # 回補庫存
            menu_item.stock = (menu_item.stock or 0) + (oi.quantity or 1)
            if menu_item.stock > 0 and menu_item.is_sold_out:
                menu_item.is_sold_out = False

            # 若該品項為點數兌換餐點，計算應退回的點數
            is_reward_item = getattr(oi, 'is_reward', False) or ('[點數兌換]' in (oi.item_name or ''))
            if is_reward_item and menu_item.is_reward:
                unit_pts = menu_item.reward_discount_points if (
                    menu_item.reward_discount_points and menu_item.reward_discount_points > 0
                ) else menu_item.reward_points
                
                if unit_pts and unit_pts > 0:
                    reward_points_to_refund += unit_pts * (oi.quantity or 1)

    # 2. 會員點數、優惠券回補
    if order.user_id:
        user = db.session.get(User, order.user_id)
        if user:
            # 回補「結帳折抵金額」所用的點數
            if getattr(order, 'points_used', 0) and order.points_used > 0:
                user.points = (user.points or 0) + order.points_used
            
            # 回補「兌換商品本身」所扣除的紅利點數
            if reward_points_to_refund > 0:
                user.points = (user.points or 0) + reward_points_to_refund

            # 扣回該筆訂單所贈送/獲得的點數
            if getattr(order, 'points_earned', 0) and order.points_earned > 0:
                user.points = max(0, (user.points or 0) - order.points_earned)

            # 復原已使用的優惠券狀態
            if getattr(order, 'coupon_code', None):
                coupon_query = UserCoupon.query.filter_by(
                    user_id=user.id, code=order.coupon_code, is_used=True
                )
                if tenant_id:
                    coupon_query = coupon_query.filter_by(tenant_id=tenant_id)
                used_coupon = coupon_query.first()
                if used_coupon:
                    used_coupon.is_used = False
                    used_coupon.used_at = None

    order.status = 'Cancelled'
    db.session.commit()
    return True, "訂單已成功取消，庫存、折抵點數及兌換點數均已全額復原！"
    order.status = 'Cancelled'
    db.session.commit()
    update_popular_items()
    _AI_ADVICE_STATE['last_signature'] = None  # 🌟 補回：訂單取消時即時重置 AI 建議快取簽章
    order_event_bus.notify()
    return True, f"訂單 #{order.id} 已成功取消，庫存、點數與優惠券已全數回滾返還！"

def build_order_analytics(orders, limit=1):
    today = datetime.now().date()
    today_orders = [o for o in orders if o.created_at and o.created_at.date() == today and o.status != 'Cancelled']
    pending_orders = [o for o in today_orders if o.status == 'Pending']
    completed_today_orders = [o for o in today_orders if o.status in ['Completed', 'PickedUp']]

    paid_today_orders = [o for o in today_orders if (o.total_price or 0) > 0]
    total_revenue = sum((o.total_price or 0) for o in paid_today_orders)
    avg_order_value = total_revenue / len(paid_today_orders) if paid_today_orders else 0

    actual_durations = []
    for o in completed_today_orders:
        if getattr(o, 'completed_at', None) and o.created_at:
            diff_min = (o.completed_at - o.created_at).total_seconds() / 60.0
            if 0 < diff_min <= 180:
                actual_durations.append(diff_min)

    if actual_durations:
        avg_order_time = round(sum(actual_durations) / len(actual_durations), 1)
    elif today_orders:
        total_items = sum(sum((i.quantity or 1) for i in o.items) for o in today_orders)
        avg_items_per_order = total_items / len(today_orders) if today_orders else 1
        avg_order_time = round(max(3.0, avg_items_per_order * 2.0 + 3.0), 1)
    else:
        avg_order_time = 0.0

    valid_item_names = {item.name for item in MenuItem.query.all()}
    item_stats = {}
    for order in orders:
        if order.status == 'Cancelled':
            continue
        for item in order.items:
            if '[點數兌換]' in item.item_name or item.item_name.startswith('🎁') or item.customization == '紅利免費兌換':
                continue
            if item.item_name in valid_item_names:
                key = item.item_name
                if key not in item_stats:
                    item_stats[key] = {'quantity': 0, 'revenue': 0}
                item_stats[key]['quantity'] += item.quantity
                item_stats[key]['revenue'] += (item.price or 0) * (item.quantity or 1)

    top_items = [
        {'name': name, 'quantity': stats['quantity'], 'revenue': stats['revenue']}
        for name, stats in sorted(item_stats.items(), key=lambda kv: kv[1]['quantity'], reverse=True)[:limit]
    ]

    total_items_qty = sum(sum((i.quantity or 1) for i in o.items) for o in pending_orders)
    eta_minutes = max(5, total_items_qty * 2 + 4)

    analytics_data = {
        'today_orders': len(today_orders),
        'today_revenue': total_revenue,
        'pending_count': len(pending_orders),
        'avg_order_value': avg_order_value,
        'avg_order_time': avg_order_time,
        'top_items': top_items,
        'eta_minutes': eta_minutes,
        'completed_today': len(completed_today_orders)
    }
    analytics_data['ai_advice'] = _AI_ADVICE_STATE.get('cached_advice') or fallback_advice(analytics_data)

    return analytics_data