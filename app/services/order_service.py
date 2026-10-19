import re
import time
from datetime import datetime
from flask import g, has_request_context, session
from app.extensions import db, acquire_distributed_lock
from app.models.menu import MenuItem, ComboOption, ModifierOption
from app.models.order import Order, OrderItem
from app.models.user import User, RewardRedemption, RewardSetting, CustomerLog
from app.models.coupon import Coupon, UserCoupon
from app.services.event_bus import order_event_bus
from app.services.ai_service import invalidate_ai_advice
from app.routes.common import record_staff_audit
from app.utils import (
    ALLOWED_PAYMENT_METHODS,
    ALLOWED_ORDER_TYPES,
    safe_int
)

_LAST_POPULAR_UPDATE_TS = {}

# 合法狀態流轉表：定義每個狀態允許轉換的下一階段，徹底杜絕倒退
LEGAL_TRANSITIONS = {
    'Pending': {'Completed', 'Cancelled'},
    'Completed': {'PickedUp', 'Cancelled'},
    'PickedUp': {'Cancelled'},
    'Cancelled': set(),
}

# 角色權限矩陣：集中管理各角色允許執行的狀態流轉
ROLE_TRANSITION_MATRIX = {
    'owner': {
        ('Pending', 'Completed'), ('Pending', 'Cancelled'),
        ('Completed', 'PickedUp'), ('Completed', 'Cancelled'),
        ('PickedUp', 'Cancelled')
    },
    'manager': {
        ('Pending', 'Completed'), ('Pending', 'Cancelled'),
        ('Completed', 'PickedUp'), ('Completed', 'Cancelled'),
        ('PickedUp', 'Cancelled')
    },
    'staff': {
        ('Pending', 'Completed'), ('Pending', 'Cancelled'),
        ('Completed', 'PickedUp'), ('Completed', 'Cancelled')
        # 嚴格禁止 staff 取消已取餐訂單 ('PickedUp', 'Cancelled')
    },
    'kitchen': {
        ('Pending', 'Completed')
        # 嚴格禁止 kitchen 執行取餐核銷或取消訂單
    }
}

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
        return min(subtotal, max(0.0, round(subtotal * (1.0 - safe_percent), 2)))


def update_popular_items(force=False):
    tid = getattr(g, 'tenant_id', 1) if has_request_context() else 1
    now = time.time()
    last_ts = _LAST_POPULAR_UPDATE_TS.get(tid, 0)
    if not force and (now - last_ts < 300):
        return
    _LAST_POPULAR_UPDATE_TS[tid] = now


def verify_promo_code(code, subtotal, user_id=None, is_pos=False, tenant_id=None):
    """
    全系統統一促銷碼與優惠券驗證函式
    """
    clean_code = str(code or '').strip().upper()
    try:
        subtotal_val = float(subtotal or 0)
    except (ValueError, TypeError):
        subtotal_val = 0.0

    if not clean_code:
        return {
            'valid': False,
            'discount': 0,
            'message': '請輸入優惠代碼！',
            'coupon': None,
            'user_coupon': None
        }

    tid = tenant_id or (getattr(g, 'tenant_id', None) if has_request_context() else None)
    coupon = None
    target_user_coupon = None

    if user_id:
        q = UserCoupon.query.filter_by(user_id=user_id, code=clean_code, is_used=False)
        if tid:
            q = q.filter_by(tenant_id=tid)
        target_user_coupon = q.first()
        if target_user_coupon:
            coupon = target_user_coupon.coupon

    if not coupon:
        pq = Coupon.query.filter_by(code=clean_code)
        if tid:
            pq = pq.filter_by(tenant_id=tid)
        public_coupon = pq.first()

        if public_coupon and (public_coupon.reward_points == 0 and not public_coupon.is_reward):
            coupon = public_coupon
        elif public_coupon and public_coupon.is_reward:
            if not user_id:
                msg = '此為會員專屬紅利券，請先於 POS 綁定會員！' if is_pos else '此為會員專屬紅利券，請先登入會員！'
                return {
                    'valid': False,
                    'discount': 0,
                    'message': msg,
                    'coupon': None,
                    'user_coupon': None
                }
            else:
                return {
                    'valid': False,
                    'discount': 0,
                    'message': '該會員尚未持有此優惠券，或該券已核銷！',
                    'coupon': None,
                    'user_coupon': None
                }
        else:
            return {
                'valid': False,
                'discount': 0,
                'message': '無效的優惠代碼！',
                'coupon': None,
                'user_coupon': None
            }

    if subtotal_val < coupon.min_spend:
        return {
            'valid': False,
            'discount': 0,
            'message': f'未達使用門檻！需消費滿 ${int(coupon.min_spend)} 元才可折抵。',
            'coupon': coupon,
            'user_coupon': target_user_coupon
        }

    discount = calc_promo_discount(coupon, subtotal_val)
    if coupon.discount_type == 'fixed':
        msg = f'已折抵現金 ${int(discount)} 元！'
    else:
        safe_percent = sanitize_discount_value('percent', coupon.discount_value)
        msg = f'已套用 {round(safe_percent * 10, 1)} 折優惠，折抵 ${int(discount)} 元！'

    return {
        'valid': True,
        'discount': discount,
        'message': msg,
        'coupon': coupon,
        'user_coupon': target_user_coupon
    }


def process_checkout(
    items_data,
    payment_method='Cash',
    order_type='內用',
    need_cutlery=False,
    note='',
    promo_code='',
    use_points=0,
    table_input='',
    cash_received=0,
    user_id=None,
    order_source='Kiosk',
    cashier_username='',
    customer_log_id=None,
    client_ip='',
    tenant_id=None
):
    """
    全系統統一原子結帳函式
    整合分散式鎖、防超賣扣減、點數上限、優惠券核銷與稽核日誌
    """
    payment_method = str(payment_method or 'Cash').strip()
    order_type = str(order_type or '內用').strip()
    if payment_method not in ALLOWED_PAYMENT_METHODS:
        return {'success': False, 'error': f'不支援的付款方式：{payment_method}', 'message': f'不支援的付款方式：{payment_method}'}, 400
    if order_type not in ALLOWED_ORDER_TYPES:
        return {'success': False, 'error': f'不支援的用餐方式：{order_type}', 'message': f'不支援的用餐方式：{order_type}'}, 400
    if not items_data or not isinstance(items_data, list):
        return {'success': False, 'error': '點餐清單為空！', 'message': '點餐清單為空！'}, 400

    tid = tenant_id or (getattr(g, 'tenant_id', None) if has_request_context() else None) or 1

    clean_user_id = safe_int(user_id, 0)
    user = db.session.get(User, clean_user_id) if clean_user_id > 0 else None
    if not user or user.tenant_id != tid:
        user = None
        user_id = None
    else:
        user_id = user.id

    all_active_modifiers = ModifierOption.query.filter_by(tenant_id=tid, is_active=True).all()
    modifier_db_map = {mod.id: mod for mod in all_active_modifiers}
    modifier_names_set = {mod.name for mod in all_active_modifiers}

    verified_items = []
    item_demands = {}
    reward_item_demands = []
    claimed_redemption_matches = []

    for client_item in items_data:
        if not isinstance(client_item, dict):
            continue
        raw_name = str(client_item.get('name', '')).strip()
        raw_custom = str(client_item.get('customization', '')).strip()
        quantity = max(1, safe_int(client_item.get('quantity', 1), 1))
        is_add_on = bool(client_item.get('is_add_on', False))
        raw_client_id = str(client_item.get('id', ''))
        is_claimed_reward = ('[點數兌換]' in raw_name) or ('reward_' in raw_client_id) or bool(client_item.get('is_reward'))
        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', raw_name).strip()

        menu_item = None
        item_db_id = safe_int(client_item.get('menu_item_id') or client_item.get('id'), 0)
        if item_db_id > 0:
            menu_item = MenuItem.query.filter_by(id=item_db_id, tenant_id=tid).first()
        if not menu_item:
            menu_item = MenuItem.query.filter_by(name=clean_name, tenant_id=tid).first()

        if not menu_item:
            return {'success': False, 'error': f'餐點【{clean_name}】不存在於系統中！', 'message': f'餐點【{clean_name}】不存在於系統中！'}, 400

        item_demands[menu_item.id] = item_demands.get(menu_item.id, 0) + quantity

        extra_modifier_price = 0
        verified_modifier_labels = []
        raw_modifier_ids = client_item.get('modifier_ids')
        if not isinstance(raw_modifier_ids, list):
            raw_modifier_ids = []
        clean_modifier_ids = [int(mid) for mid in raw_modifier_ids if str(mid).isdigit()]

        if clean_modifier_ids:
            is_combo = bool(re.search(r'\[套餐:\s*([^\]]+)\]', raw_custom))
            if menu_item.modifiers != 'addons' and not is_combo and not menu_item.can_be_add_on:
                return {'success': False, 'error': f'餐點【{menu_item.name}】不支援客製化加料！', 'message': f'餐點【{menu_item.name}】不支援客製化加料！'}, 400

            for mid in clean_modifier_ids:
                if mid in modifier_db_map:
                    mod = modifier_db_map[mid]
                    extra_modifier_price += mod.price
                    verified_modifier_labels.append(f"{mod.name}(+{mod.price})")
                else:
                    return {'success': False, 'error': '包含無效或已停用的加料品項！', 'message': '包含無效或已停用的加料品項！'}, 400

        safe_custom_parts = []
        for part in [p.strip() for p in raw_custom.split('、') if p.strip()]:
            if any(name in part for name in modifier_names_set):
                continue
            cleaned_part = re.sub(r'\(\+\d+\)', '', part).strip()
            if cleaned_part:
                safe_custom_parts.append(cleaned_part)

        safe_custom_parts.extend(verified_modifier_labels)
        safe_customization = "、".join(safe_custom_parts) if safe_custom_parts else ""

        combo_option_id = None
        display_name = menu_item.name
        if is_claimed_reward:
            if not user_id:
                return {'success': False, 'error': '請先綁定會員才可使用紅利點數兌換商品！', 'message': '請先綁定會員才可使用紅利點數兌換商品！'}, 400
            if not menu_item.is_reward:
                return {'success': False, 'error': f'餐點【{menu_item.name}】未開放紅利兌換！', 'message': f'餐點【{menu_item.name}】未開放紅利兌換！'}, 400

            unit_pts = menu_item.reward_discount_points if (
                menu_item.reward_discount_points and menu_item.reward_discount_points > 0
            ) else menu_item.reward_points
            if not unit_pts or unit_pts <= 0:
                return {'success': False, 'error': f'餐點【{menu_item.name}】未設定有效兌換點數！', 'message': f'餐點【{menu_item.name}】未設定有效兌換點數！'}, 400

            unused_vouchers = RewardRedemption.query.filter_by(
                tenant_id=tid,
                user_id=user_id,
                menu_item_id=menu_item.id,
                is_used=False,
                is_refunded=False,
                order_id=None
            ).all()

            vouchers_to_use = unused_vouchers[:quantity]
            for v_obj in vouchers_to_use:
                claimed_redemption_matches.append(v_obj)

            needed_from_pts = quantity - len(vouchers_to_use)
            if needed_from_pts > 0:
                reward_item_demands.append((menu_item, needed_from_pts, unit_pts))

            unit_price = extra_modifier_price
            display_name = f"[點數兌換] {menu_item.name}"
            if not safe_customization:
                safe_customization = '紅利免費兌換'
        elif is_add_on:
            if not menu_item.can_be_add_on:
                return {'success': False, 'error': f'餐點【{menu_item.name}】非合法加購品項！', 'message': f'餐點【{menu_item.name}】非合法加購品項！'}, 400
            unit_price = menu_item.add_on_price + extra_modifier_price
        else:
            base_price = menu_item.discount_price if (menu_item.is_discount and menu_item.discount_price > 0) else menu_item.price
            combo_extra_price = 0
            combo_match = re.search(r'\[套餐:\s*([^\]]+)\]', raw_custom)
            if combo_match:
                combo_name = combo_match.group(1).strip()
                combo_opt = ComboOption.query.filter_by(tenant_id=tid, main_item_id=menu_item.id, name=combo_name).first()
                if combo_opt:
                    combo_extra_price = combo_opt.additional_price
                    combo_option_id = combo_opt.id
                    for side_id in (combo_opt.item1_id, combo_opt.item2_id, combo_opt.item3_id):
                        if side_id:
                            item_demands[side_id] = item_demands.get(side_id, 0) + quantity
            unit_price = base_price + combo_extra_price + extra_modifier_price

        verified_items.append({
            'menu_item_id': menu_item.id,
            'combo_option_id': combo_option_id,
            'menu_item_name': menu_item.name,
            'display_name': display_name,
            'price': int(unit_price),
            'quantity': quantity,
            'customization': safe_customization
        })

    if not verified_items:
        return {'success': False, 'error': '無有效結帳品項！', 'message': '無有效結帳品項！'}, 400

    subtotal = sum(v['price'] * v['quantity'] for v in verified_items)

    try:
        with acquire_distributed_lock(f"lock:order_checkout:{tid}", timeout=8, blocking_timeout=5):
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
            # 取餐號 1~999 循環：接續「當日最後一張訂單」的號碼 (依 id 倒序)。
            # 不可改用 MAX()：一旦當日最大值到 999，(999 % 999) + 1 永遠等於 1，之後每張單都會拿到 1。
            last_pickup = db.session.query(Order.pickup_number).filter(
                Order.tenant_id == tid,
                Order.created_at >= today_start,
                Order.created_at <= today_end
            ).order_by(Order.id.desc()).limit(1).scalar()
            next_pickup = (last_pickup % 999) + 1 if (last_pickup and last_pickup > 0) else 1

            for item_id, demanded_qty in item_demands.items():
                result = db.session.execute(
                    db.text("UPDATE menu_item SET stock = stock - :qty, is_sold_out = CASE WHEN stock - :qty <= 0 THEN 1 ELSE is_sold_out END WHERE id = :id AND tenant_id = :tenant_id AND stock >= :qty"),
                    {"id": item_id, "qty": demanded_qty, "tenant_id": tid}
                )
                if result.rowcount == 0:
                    db.session.rollback()
                    m_item = db.session.get(MenuItem, item_id)
                    curr = m_item.stock if m_item else 0
                    return {
                        'success': False,
                        'insufficient_stock': True,
                        'error': f'餐點或配餐【{m_item.name if m_item else ""}】庫存剩 {curr} 份，庫存不足！',
                        'message': f'餐點或配餐【{m_item.name if m_item else ""}】庫存剩 {curr} 份，庫存不足！'
                    }, 400

            created_redemptions = []
            total_reward_item_pts = 0
            for m_item, qty, unit_pts in reward_item_demands:
                pts_needed = unit_pts * qty
                pts_res = db.session.execute(
                    db.text("UPDATE user SET points = points - :pts WHERE id = :id AND tenant_id = :tenant_id AND points >= :pts"),
                    {"id": user_id, "pts": pts_needed, "tenant_id": tid}
                )
                if pts_res.rowcount == 0:
                    db.session.rollback()
                    return {'success': False, 'error': f'會員紅利點數不足（需 {pts_needed} 點），無法兌換【{m_item.name}】！', 'message': f'會員紅利點數不足（需 {pts_needed} 點），無法兌換【{m_item.name}】！'}, 400
                total_reward_item_pts += pts_needed
                for _ in range(qty):
                    r_obj = RewardRedemption(
                        tenant_id=tid,
                        user_id=user_id,
                        menu_item_id=m_item.id,
                        points_cost=unit_pts,
                        is_used=True,
                        is_refunded=False
                    )
                    db.session.add(r_obj)
                    created_redemptions.append(r_obj)

            clean_promo_code = str(promo_code or '').strip().upper()
            promo_discount = 0
            target_user_coupon = None
            if clean_promo_code and user_id:
                target_user_coupon = UserCoupon.query.filter_by(tenant_id=tid, user_id=user_id, code=clean_promo_code, is_used=False).first()
                if target_user_coupon and subtotal >= target_user_coupon.coupon.min_spend:
                    cp = target_user_coupon.coupon
                    promo_discount = calc_promo_discount(cp, subtotal)
                    coupon_update = db.session.execute(
                        db.text("UPDATE user_coupon SET is_used = 1, used_at = :now WHERE id = :id AND tenant_id = :tenant_id AND is_used = 0"),
                        {"id": target_user_coupon.id, "now": datetime.now(), "tenant_id": tid}
                    )
                    if coupon_update.rowcount == 0:
                        db.session.rollback()
                        return {'success': False, 'error': '該優惠券已被核銷！', 'message': '該優惠券已被核銷！'}, 400

            if clean_promo_code and not target_user_coupon:
                pub_cp = Coupon.query.filter_by(tenant_id=tid, code=clean_promo_code, is_reward=False, reward_points=0).first()
                if pub_cp and subtotal >= pub_cp.min_spend:
                    promo_discount = calc_promo_discount(pub_cp, subtotal)

            setting = RewardSetting.query.filter_by(tenant_id=tid).first()
            ppd = setting.points_per_dollar if (setting and setting.points_per_dollar > 0) else 1
            max_discount_limit = setting.max_discount_per_order if setting else 0
            spend_threshold = setting.spend_per_point if (setting and setting.spend_per_point > 0) else 100
            is_points_enabled = setting.is_enabled if setting else True

            remaining_amount = max(0, subtotal - promo_discount)
            points_used_val = 0
            points_discount = 0

            use_points_clean = max(0, safe_int(use_points, 0))
            if user_id and use_points_clean > 0 and is_points_enabled:
                curr_pts_row = db.session.execute(
                    db.text("SELECT points FROM user WHERE id = :id AND tenant_id = :tenant_id"),
                    {"id": user_id, "tenant_id": tid}
                ).fetchone()
                avail_points = int(curr_pts_row[0] or 0) if curr_pts_row else 0

                max_cash = remaining_amount
                if max_discount_limit > 0:
                    max_cash = min(max_cash, max_discount_limit)

                actual_points = min(avail_points, use_points_clean)
                points_discount = min(actual_points // ppd, max_cash)
                points_used_val = int(points_discount * ppd)

                if points_used_val > 0:
                    pts_update = db.session.execute(
                        db.text("UPDATE user SET points = points - :used WHERE id = :id AND tenant_id = :tenant_id AND points >= :used"),
                        {"id": user_id, "used": points_used_val, "tenant_id": tid}
                    )
                    if pts_update.rowcount == 0:
                        db.session.rollback()
                        return {'success': False, 'error': '會員紅利點數不足！', 'message': '會員紅利點數不足！'}, 400

            final_price = int(max(0, remaining_amount - points_discount))
            total_discount = int(promo_discount + points_discount)
            points_earned = int(final_price // spend_threshold) if user_id else 0

            cash_received_clean = max(0, safe_int(cash_received, 0))
            if order_source == 'POS' and payment_method == 'Cash' and cash_received_clean > 0 and cash_received_clean < final_price:
                db.session.rollback()
                return {'success': False, 'error': f'實收現金 (${cash_received_clean}) 小於應付總額 (${final_price})！', 'message': f'實收現金 (${cash_received_clean}) 小於應付總額 (${final_price})！'}, 400

            actual_received = cash_received_clean if (order_source == 'POS' and payment_method == 'Cash' and cash_received_clean >= final_price) else final_price
            cash_change = max(0, actual_received - final_price) if (order_source == 'POS' and payment_method == 'Cash') else 0

            if user_id and points_earned > 0:
                db.session.execute(
                    db.text("UPDATE user SET points = points + :earned WHERE id = :id AND tenant_id = :tenant_id"),
                    {"id": user_id, "earned": points_earned, "tenant_id": tid}
                )

            table_input_clean = str(table_input or '').strip()[:40]
            if table_input_clean:
                table_display = f"{table_input_clean} ({user.name})" if user and user.name not in table_input_clean else table_input_clean
            elif user:
                table_display = f"POS-{user.name}" if order_source == 'POS' else user.name
            else:
                table_display = "櫃檯POS" if order_source == 'POS' else "訪客"

            now_dt = datetime.now()
            items_summary_str = ", ".join([f"{v['display_name']} x{v['quantity']}" for v in verified_items])

            final_cust_log_id = customer_log_id
            if not final_cust_log_id:
                user_type_desc = ('POS會員' if user else 'POS訪客') if order_source == 'POS' else ('會員' if user else '訪客')
                identifier_desc = f"{user.name} ({user.phone})" if (user and order_source == 'POS') else (user.name if user else table_display[:100])
                prefix = f"[POS 取餐#{next_pickup}] " if order_source == 'POS' else f"[取餐#{next_pickup}] "
                new_cust_log = CustomerLog(
                    tenant_id=tid,
                    user_type=user_type_desc,
                    identifier=identifier_desc,
                    login_at=now_dt,
                    logout_at=now_dt,
                    ordered_items=f"{prefix}{items_summary_str}",
                    ip_address=client_ip
                )
                db.session.add(new_cust_log)
                db.session.flush()
                final_cust_log_id = new_cust_log.id
            else:
                c_log_obj = db.session.get(CustomerLog, final_cust_log_id)
                if c_log_obj:
                    c_log_obj.ordered_items = items_summary_str
                    c_log_obj.logout_at = now_dt

            new_order = Order(
                tenant_id=tid,
                user_id=user_id,
                table_number=table_display[:50],
                total_price=final_price,
                payment_method=payment_method,
                order_type=order_type,
                order_source=order_source,
                cashier_username=cashier_username,
                cash_received=actual_received,
                cash_change=cash_change,
                need_cutlery=bool(need_cutlery),
                note=str(note or '')[:200],
                status='Pending',
                points_used=points_used_val,
                points_earned=points_earned,
                discount_amount=total_discount,
                coupon_code=clean_promo_code,
                pickup_number=next_pickup,
                customer_log_id=final_cust_log_id,
                created_at=now_dt
            )
            db.session.add(new_order)
            db.session.flush()

            for r_obj in created_redemptions:
                r_obj.order_id = new_order.id
            for v_obj in claimed_redemption_matches:
                v_obj.is_used = True
                v_obj.order_id = new_order.id

            receipt_items = []
            for v in verified_items:
                order_item = OrderItem(
                    tenant_id=tid,
                    order_id=new_order.id,
                    menu_item_id=v['menu_item_id'],
                    combo_option_id=v['combo_option_id'],
                    item_name=v['display_name'],
                    price=v['price'],
                    quantity=v['quantity'],
                    customization=v['customization']
                )
                db.session.add(order_item)
                receipt_items.append({
                    'name': v['display_name'],
                    'item_name': v['display_name'],
                    'price': v['price'],
                    'quantity': v['quantity'],
                    'subtotal': v['price'] * v['quantity'],
                    'customization': v['customization']
                })

            if order_source == 'POS':
                net_pts_delta = points_earned - (points_used_val + total_reward_item_pts)
                record_staff_audit(
                    action_type='POS_CREATE_ORDER',
                    target_order_id=new_order.id,
                    target_user_id=user_id,
                    points_delta=net_pts_delta,
                    details=(
                        f"POS 結帳單號 #{new_order.id} (取餐 #{next_pickup:03d}) | "
                        f"顧客:{table_display} | 付款:{payment_method} 應收:${final_price} 實收:${actual_received} 找零:${cash_change} | "
                        f"兌換扣點:{total_reward_item_pts} 折抵扣點:{points_used_val} 贈點:{points_earned} 優惠碼:{clean_promo_code or '無'}"
                    )
                )

            db.session.commit()

    except TimeoutError:
        return {'success': False, 'error': '系統結帳佇列繁忙，請重試！', 'message': '系統結帳佇列繁忙，請重試！'}, 503
    except Exception as e:
        db.session.rollback()
        print(f"[!] 結帳異常: {e}")
        return {'success': False, 'error': '結帳交易發生異常，請重試！', 'message': '結帳交易發生異常，請重試！'}, 500

    db.session.expire_all()
    remaining_points = 0
    if user_id:
        refreshed_user = db.session.get(User, user_id)
        remaining_points = refreshed_user.points if refreshed_user else 0

    update_popular_items(force=False)
    invalidate_ai_advice(tid)
    order_event_bus.notify()

    return {
        'success': True,
        'order_id': new_order.id,
        'pickup_number': next_pickup,
        'table_number': table_display,
        'user_name': user.name if user else table_display,
        'subtotal': int(subtotal),
        'discount_amount': total_discount,
        'points_used': points_used_val,
        'points_earned': points_earned,
        'total_price': final_price,
        'cash_received': actual_received,
        'cash_change': cash_change,
        'payment_method': payment_method,
        'order_type': order_type,
        'order_source': order_source,
        'need_cutlery': bool(need_cutlery),
        'note': str(note or '')[:200],
        'created_at': new_order.created_at.strftime('%Y-%m-%d %H:%M:%S') if new_order.created_at else '',
        'cashier': cashier_username,
        'items': receipt_items,
        'remaining_points': remaining_points
    }, 200
def change_order_status(order_id, target_status, staff_account=None, tenant_id=None):
    """
    全系統唯一核心訂單狀態機函式
    - 先驗權限 (403 Forbidden)，再驗訂單存在 (404) 與狀態流轉合法性 (409 Conflict)，避免無權限者探測訂單狀態
    - SQL 條件式更新 (CAS) 併發互斥防護 (409 Conflict)
    - 取消訂單原子全量回滾 (庫存/點數/兌換品點數/優惠券)
    """
    tid = tenant_id or (getattr(g, 'tenant_id', None) if has_request_context() else None) or 1
    target_status = str(target_status or '').strip()

    valid_statuses = {'Pending', 'Completed', 'PickedUp', 'Cancelled'}
    if target_status not in valid_statuses:
        return {'success': False, 'error': f'不支援的訂單狀態：{target_status}', 'message': f'不支援的訂單狀態：{target_status}'}, 400

    # 0. 目標狀態不可達 (409)：流轉表中沒有任何流程能轉入該狀態 (例如 Pending)。
    #    此判斷與角色、訂單皆無關，回應固定，不會洩漏任何資訊，且比 403 更能說明原因。
    reachable_targets = {t for targets in LEGAL_TRANSITIONS.values() for t in targets}
    if target_status not in reachable_targets:
        msg = f'非法狀態轉換：沒有任何流程可以將訂單變更為【{target_status}】！'
        return {'success': False, 'error': msg, 'message': msg}, 409

    # 1. 角色層級授權 (403)：在讀取訂單之前先驗證「此角色是否可能執行此目標狀態」。
    #    無權限者不論訂單存在與否、目前處於何種狀態，回應一律相同，無法藉由 404/409 探測訂單資訊。
    staff = staff_account or getattr(g, 'staff_account', None)
    role = getattr(staff, 'role', None) or (
        (session.get('admin_role') or session.get('staff_role')) if has_request_context() else ''
    ) or ''
    role_transitions = ROLE_TRANSITION_MATRIX.get(role, set())
    role_targets = {t for (_, t) in role_transitions}

    def _forbidden():
        msg = f'權限不足：角色 [{role}] 無權將訂單變更為【{target_status}】！'
        return {'success': False, 'error': msg, 'message': msg}, 403

    if target_status not in role_targets:
        return _forbidden()

    # 2. 訂單存在性 (404，僅限有基本權限者)
    order = db.session.get(Order, order_id)
    if not order or order.tenant_id != tid:
        return {'success': False, 'error': '查無此訂單！', 'message': '查無此訂單！'}, 404

    current_status = order.status

    # 3. 狀態重複判斷 (409 Conflict)
    if current_status == target_status:
        return {
            'success': False,
            'error': f'訂單已處於【{current_status}】狀態，請勿重複操作！',
            'message': f'訂單已處於【{current_status}】狀態，請勿重複操作！'
        }, 409

    # 4. 狀態轉換流轉表防禦 (409 Conflict)
    if target_status not in LEGAL_TRANSITIONS.get(current_status, set()):
        return {
            'success': False,
            'error': f'非法狀態轉換：不可從【{current_status}】轉換為【{target_status}】！',
            'message': f'非法狀態轉換：不可從【{current_status}】轉換為【{target_status}】！'
        }, 409

    # 5. 細部轉換權限 (403)：例如 staff 不可取消「已取餐」訂單 (訊息與步驟 1 相同，不帶出訂單狀態)
    if (current_status, target_status) not in role_transitions:
        return _forbidden()

    # 6. 防併發：SQL 條件式更新 (CAS - Compare-And-Swap)
    now_dt = datetime.now()
    completed_time = now_dt if target_status == 'Completed' else order.completed_at

    cas_result = db.session.execute(
        db.text("""
            UPDATE "order"
            SET status = :target_status,
                completed_at = :completed_at
            WHERE id = :id AND tenant_id = :tenant_id AND status = :current_status
        """),
        {
            "target_status": target_status,
            "completed_at": completed_time,
            "id": order.id,
            "tenant_id": tid,
            "current_status": current_status
        }
    )

    if cas_result.rowcount == 0:
        db.session.rollback()
        return {
            'success': False,
            'error': '訂單狀態已被其他操作者變更，請重新整理！',
            'message': '訂單狀態已被其他操作者變更，請重新整理！'
        }, 409

    # 7. 狀態更新後之後續事務 (已保證單次原子執行)
    order.status = target_status
    if target_status == 'Completed':
        order.completed_at = completed_time

    points_delta = 0

    if target_status == 'Cancelled':
        # (a) 回滾主餐庫存
        for item in order.items:
            if item.menu_item_id:
                db.session.execute(
                    db.text("""
                        UPDATE menu_item 
                        SET stock = stock + :qty,
                            is_sold_out = CASE WHEN stock + :qty > 0 THEN 0 ELSE is_sold_out END
                        WHERE id = :id AND tenant_id = :tenant_id
                    """),
                    {"id": item.menu_item_id, "qty": item.quantity, "tenant_id": tid}
                )

            # (b) 回滾套餐配餐庫存
            if item.combo_option_id:
                combo_opt = db.session.get(ComboOption, item.combo_option_id)
                if combo_opt:
                    for side_id in (combo_opt.item1_id, combo_opt.item2_id, combo_opt.item3_id):
                        if side_id:
                            db.session.execute(
                                db.text("""
                                    UPDATE menu_item 
                                    SET stock = stock + :qty,
                                        is_sold_out = CASE WHEN stock + :qty > 0 THEN 0 ELSE is_sold_out END
                                    WHERE id = :id AND tenant_id = :tenant_id
                                """),
                                {"id": side_id, "qty": item.quantity, "tenant_id": tid}
                            )

        # (c) 還原紅利兌換商品憑證狀態，並累計應退還點數
        redemptions = RewardRedemption.query.filter_by(
            tenant_id=tid,
            order_id=order.id
        ).all()
        total_redeemed_pts = 0
        for r_obj in redemptions:
            if not r_obj.is_refunded:
                r_obj.is_used = False
                r_obj.is_refunded = True
                total_redeemed_pts += (r_obj.points_cost or 0)

        # (d) 回滾會員點數與優惠券（若為會員訂單）
        if order.user_id:
            user = db.session.get(User, order.user_id)
            if user:
                net_pts_return = (order.points_used or 0) + total_redeemed_pts - (order.points_earned or 0)
                if net_pts_return != 0:
                    user.points = max(0, (user.points or 0) + net_pts_return)
                    points_delta = net_pts_return

            if order.coupon_code:
                user_coupon = UserCoupon.query.filter_by(
                    tenant_id=tid,
                    user_id=order.user_id,
                    code=order.coupon_code,
                    is_used=True
                ).first()
                if user_coupon:
                    user_coupon.is_used = False
                    user_coupon.used_at = None

        record_staff_audit(
            action_type='CANCEL_ORDER',
            target_order_id=order.id,
            target_user_id=order.user_id,
            points_delta=points_delta,
            details=f"取消訂單 #{order.id} (取餐 #{order.pickup_number:03d})，已全量回滾庫存、商品兌換點數({total_redeemed_pts}點)、折抵與贈點"
        )

    elif target_status == 'PickedUp':
        record_staff_audit(
            action_type='MARK_PICKED_UP',
            target_order_id=order.id,
            target_user_id=order.user_id,
            details=f"核銷完成取餐單號 #{order.id} (取餐 #{order.pickup_number:03d})"
        )

    db.session.commit()
    invalidate_ai_advice(tid)
    order_event_bus.notify()

    return {
        'success': True,
        'order_id': order.id,
        'status': order.status,
        'message': f'訂單狀態已成功更新為【{order.status}】'
    }, 200

def complete_all_pending_orders(tenant_id=None):
    """
    廚房一鍵全部出餐函式
    將所有處於 Pending 狀態之訂單一次性流轉為 Completed
    """
    tid = tenant_id or (getattr(g, 'tenant_id', None) if has_request_context() else None) or 1
    now_dt = datetime.now()

    updated_count = Order.query.filter_by(
        tenant_id=tid,
        status='Pending'
    ).update({
        'status': 'Completed',
        'completed_at': now_dt
    }, synchronize_session='fetch')

    db.session.commit()
    invalidate_ai_advice(tid)
    order_event_bus.notify()

    return {'success': True, 'count': updated_count}, 200


def build_order_analytics(orders, limit=3):
    """
    營運分析指標計算函式
    計算當日訂單數、營收、平均客單價、待製作單數、預估出餐時間與熱銷排行
    """
    today = datetime.now().date()
    today_valid_orders = [
        o for o in orders 
        if o.created_at and o.created_at.date() == today and o.status != 'Cancelled'
    ]

    today_revenue = sum(o.total_price or 0 for o in today_valid_orders)
    today_orders_count = len(today_valid_orders)
    avg_order_value = round(today_revenue / today_orders_count, 1) if today_orders_count > 0 else 0

    pending_orders = [o for o in orders if o.status == 'Pending']
    pending_count = len(pending_orders)
    eta_minutes = pending_count * 3

    item_counts = {}
    for o in today_valid_orders:
        for i in o.items:
            clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', i.item_name).strip()
            item_counts[clean_name] = item_counts.get(clean_name, 0) + (i.quantity or 1)

    sorted_items = sorted(item_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    top_items = [{'name': name, 'quantity': qty} for name, qty in sorted_items]

    return {
        'today_orders': today_orders_count,
        'today_revenue': today_revenue,
        'avg_order_value': avg_order_value,
        'pending_count': pending_count,
        'eta_minutes': eta_minutes,
        'top_items': top_items
    }
def set_item_reward_status(item_id, is_reward=True, reward_points=0, reward_discount_points=0, tenant_id=None):
    """
    全系統統一餐點紅利商城上下架管理函式
    完整檢驗兌換點數不可高於原價、特惠點數合法性，且僅在狀態或點數有實質變動時寫入 StaffAuditLog 稽核日誌
    """
    tid = tenant_id or (getattr(g, 'tenant_id', None) if has_request_context() else None) or 1
    clean_id = safe_int(item_id, 0)
    
    item = MenuItem.query.filter_by(id=clean_id, tenant_id=tid).first()
    if not item:
        return {'success': False, 'message': '查無此餐點品項！'}, 404

    is_reward_bool = bool(is_reward)
    pts = max(0, safe_int(reward_points, 0))
    disc_pts = max(0, safe_int(reward_discount_points, 0))

    if is_reward_bool:
        if pts <= 0:
            return {'success': False, 'message': '啟用紅利兌換時，「兌換所需點數」必須大於 0！'}, 400
        if pts > item.price:
            return {'success': False, 'message': f'兌換所需點數 ({pts} 點) 不得高於原單價 (${item.price})！'}, 400
        if disc_pts > 0 and disc_pts >= pts:
            return {'success': False, 'message': '「限時特惠點數」必須小於「兌換所需點數」！'}, 400

    # 檢查紅利狀態或點數是否有實質改變，避免未異動時產生審計雜訊
    changed = (
        bool(item.is_reward) != is_reward_bool or
        (is_reward_bool and (item.reward_points != pts or item.reward_discount_points != disc_pts))
    )

    if is_reward_bool:
        item.is_reward = True
        item.reward_points = pts
        item.reward_discount_points = disc_pts
        action_detail = f"上架/更新紅利商品：{item.name} (兌換點數:{item.reward_points}, 特惠點數:{item.reward_discount_points})"
    else:
        item.is_reward = False
        item.reward_points = 0
        item.reward_discount_points = 0
        action_detail = f"下架紅利商品：{item.name}"

    # 僅在狀態或數值有實質異動時才寫入稽核紀錄
    if changed:
        record_staff_audit(
            action_type='TOGGLE_REWARD_ITEM',
            details=action_detail
        )

    db.session.commit()
    return {
        'success': True,
        'message': '紅利商品狀態已成功更新！',
        'item_id': item.id,
        'is_reward': item.is_reward
    }, 200
