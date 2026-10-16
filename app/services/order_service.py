import re
import time
from datetime import datetime
from flask import g, has_request_context
from app.extensions import db
from app.models.menu import MenuItem, ComboOption
from app.models.order import Order, OrderItem
from app.models.user import User, RewardRedemption
from app.models.coupon import UserCoupon
from app.services.event_bus import order_event_bus
from app.services.ai_service import _AI_ADVICE_STATE, fallback_advice, invalidate_ai_advice
from app.extensions import db, acquire_distributed_lock

_LAST_POPULAR_UPDATE_TS = {}


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
    if not coupon or subtotal <= 0:
        return 0
    if coupon.discount_type == 'fixed':
        return min(coupon.discount_value, subtotal)
    else:
        safe_percent = sanitize_discount_value('percent', coupon.discount_value)
        return min(subtotal, max(0.0, round(subtotal * (1.0 - safe_percent))))


def update_popular_items(force=False):
    tid = getattr(g, 'tenant_id', None) if has_request_context() else 1
    now_ts = time.time()
    if not force and (now_ts - _LAST_POPULAR_UPDATE_TS.get(tid, 0)) < 45:
        return
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
        _LAST_POPULAR_UPDATE_TS[tid] = now_ts
    except Exception as e:
        db.session.rollback()
        print(f"自動更新熱門失敗: {e}")

def cancel_order_and_rollback(order):
    if order.status == 'Cancelled':
        return False, "該訂單早已取消，不重複執行回滾！"

    tenant_id = getattr(order, 'tenant_id', None) or (getattr(g, 'tenant_id', None) if has_request_context() else 1) or 1
    reward_points_to_refund = 0

    # 取得結帳與取消排他互斥鎖，避免高併發下庫存競爭與 Lost Update
    with acquire_distributed_lock(f"lock:order_checkout:{tenant_id}", timeout=8, blocking_timeout=5):
        if order.status == 'Cancelled':
            return False, "該訂單早已取消，不重複執行回滾！"

        # 1. 以原子 SQL 回補主餐與套餐配餐庫存，並計算需退還的紅利兌換點數
        for oi in order.items:
            qty = oi.quantity or 1
            clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', oi.item_name or '').strip()

            menu_item = None
            if getattr(oi, 'menu_item_id', None):
                menu_item = db.session.get(MenuItem, oi.menu_item_id)
            if not menu_item:
                query = MenuItem.query.execution_options(skip_tenant_filter=True).filter_by(name=clean_name)
                if tenant_id:
                    query = query.filter_by(tenant_id=tenant_id)
                menu_item = query.first()

            if menu_item:
                # 原子更新主餐庫存，避免 ORM 記憶體覆蓋
                db.session.execute(
                    db.text("UPDATE menu_item SET stock = stock + :qty, is_sold_out = CASE WHEN stock + :qty > 0 THEN 0 ELSE is_sold_out END WHERE id = :id"),
                    {"qty": qty, "id": menu_item.id}
                )

                # 回補套餐配餐庫存
                combo_opt = None
                if getattr(oi, 'combo_option_id', None):
                    combo_opt = db.session.get(ComboOption, oi.combo_option_id)
                if not combo_opt and oi.customization:
                    m_combo = re.search(r'\[套餐:\s*([^\]]+)\]', oi.customization)
                    if m_combo:
                        combo_name = m_combo.group(1).strip()
                        combo_opt = ComboOption.query.execution_options(skip_tenant_filter=True).filter_by(
                            main_item_id=menu_item.id, name=combo_name
                        ).first()
                if combo_opt:
                    for side_id in (combo_opt.item1_id, combo_opt.item2_id, combo_opt.item3_id):
                        if side_id:
                            db.session.execute(
                                db.text("UPDATE menu_item SET stock = stock + :qty, is_sold_out = CASE WHEN stock + :qty > 0 THEN 0 ELSE is_sold_out END WHERE id = :id"),
                                {"qty": qty, "id": side_id}
                            )

                is_reward_item = ('[點數兌換]' in (oi.item_name or '')) or (oi.customization == '紅利免費兌換')
                if is_reward_item and menu_item.is_reward:
                    unit_pts = menu_item.reward_discount_points if (
                        menu_item.reward_discount_points and menu_item.reward_discount_points > 0
                    ) else menu_item.reward_points
                    if unit_pts and unit_pts > 0:
                        reward_points_to_refund += unit_pts * qty

        # 標記關聯之 RewardRedemption 為已退還
        redemptions = RewardRedemption.query.execution_options(skip_tenant_filter=True).filter_by(
            order_id=order.id, is_refunded=False
        ).all()
        for r_obj in redemptions:
            r_obj.is_refunded = True

        # 2. 會員點數、優惠券回補
        if order.user_id:
            user = db.session.get(User, order.user_id)
            if user:
                if getattr(order, 'points_used', 0) and order.points_used > 0:
                    user.points = (user.points or 0) + order.points_used
                if reward_points_to_refund > 0:
                    user.points = (user.points or 0) + reward_points_to_refund
                if getattr(order, 'points_earned', 0) and order.points_earned > 0:
                    user.points = max(0, (user.points or 0) - order.points_earned)

                if getattr(order, 'coupon_code', None):
                    coupon_query = UserCoupon.query.execution_options(skip_tenant_filter=True).filter_by(
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
        db.session.expire_all()

    update_popular_items(force=True)
    invalidate_ai_advice(tenant_id)
    order_event_bus.notify(tenant_id=tenant_id)
    return True, f"訂單 #{order.id} 已成功取消，庫存、配餐庫存、點數與優惠券已全數回滾返還！"


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
