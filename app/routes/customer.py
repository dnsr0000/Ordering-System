import re
from datetime import datetime
from flask import Blueprint, render_template, request, session, jsonify, redirect, url_for, g
from app.extensions import db, acquire_distributed_lock
from app.models.user import User, CustomerLog, RewardSetting, RewardRedemption
from app.models.menu import MenuItem, ComboOption, ModifierOption
from app.models.order import Order, OrderItem
from app.models.coupon import Coupon, UserCoupon
from app.services.order_service import update_popular_items, sanitize_discount_value, calc_promo_discount
from app.services.event_bus import order_event_bus
from app.services.ai_service import invalidate_ai_advice
from app.routes.common import clear_customer_session
from app.utils import rate_limit, safe_int, ALLOWED_PAYMENT_METHODS, ALLOWED_ORDER_TYPES
from app.models.tenant import Tenant

customer_bp = Blueprint('customer', __name__)


@customer_bp.route('/', endpoint='customer_index')
def customer_index():
    update_popular_items(force=False)
    items = MenuItem.query.all()
    categories = sorted(list(set(item.category for item in items if item.category)))
    user_points = 0
    user_coupons = []
    personalized_items = []
    is_guest = session.get('is_guest', False)
    modifier_options = ModifierOption.query.filter_by(is_active=True).all()

    reward_setting = RewardSetting.query.first()
    if not reward_setting:
        reward_setting = RewardSetting(is_enabled=True, points_per_dollar=1, max_discount_per_order=0, spend_per_point=100)
        db.session.add(reward_setting)
        db.session.commit()

    if session.get('user_id'):
        user = db.session.get(User, session['user_id'])
        if not user or user.tenant_id != g.tenant_id:
            clear_customer_session()
            return redirect(url_for('customer.customer_index'))

        user_points = user.points
        session['user_points'] = user.points
        user_coupons = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
        session['is_guest'] = False
        is_guest = False

        user_orders = Order.query.filter(Order.user_id == user.id, Order.status != 'Cancelled').all()
        if not user_orders and user.name and user.name.strip() not in ['訪客', 'Guest', '']:
            user_orders = Order.query.filter(Order.user_id.is_(None), Order.table_number == user.name.strip(), Order.status != 'Cancelled').all()

        if user_orders:
            item_dict = {it.name.strip(): it for it in items}
            user_item_counts = {}
            for order in user_orders:
                for oi in order.items:
                    if '[點數兌換]' in oi.item_name or oi.item_name.startswith('🎁') or oi.customization == '紅利免費兌換':
                        continue
                    clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', oi.item_name).strip()
                    if clean_name in item_dict:
                        user_item_counts[clean_name] = user_item_counts.get(clean_name, 0) + int(oi.quantity or 1)

            sorted_fav_names = sorted(user_item_counts.keys(), key=lambda k: user_item_counts[k], reverse=True)[:3]
            personalized_items = [item_dict[name] for name in sorted_fav_names if name in item_dict]

    return render_template(
        'customer.html',
        items=items,
        categories=categories,
        user_points=user_points,
        user_coupons=user_coupons,
        personalized_items=personalized_items,
        is_guest=is_guest,
        reward_setting=reward_setting,
        modifier_options=modifier_options,
        tenant=g.tenant
    )


@customer_bp.route('/api/user_available_coupons')
def api_user_available_coupons():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'coupons': [], 'points': 0})

    db.session.expire_all()
    user = db.session.get(User, user_id)
    if not user or user.tenant_id != g.tenant_id:
        return jsonify({'success': False, 'coupons': [], 'points': 0})

    avail_cps = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
    coupons_list = []
    for uc in avail_cps:
        cp = uc.coupon
        if cp:
            d_desc = f"[折${int(cp.discount_value)}]" if cp.discount_type == 'fixed' else f"[{round(cp.discount_value * 10, 1)}折]"
            coupons_list.append({
                'code': uc.code,
                'title': f"{d_desc} {cp.title}",
                'min_spend': cp.min_spend
            })

    return jsonify({
        'success': True,
        'points': user.points,
        'coupons': coupons_list
    })


@customer_bp.route('/api/verify_promo', methods=['POST'])
def verify_promo():
    data = request.get_json() or {}
    code = str(data.get('promo_code', '')).strip().upper()
    try:
        subtotal = float(data.get('subtotal', 0))
    except (ValueError, TypeError):
        subtotal = 0.0
    user_id = session.get('user_id')

    if not code:
        return jsonify({'valid': False, 'discount': 0, 'message': '請輸入優惠代碼！'})

    coupon = None
    if user_id:
        user_coupon = UserCoupon.query.filter_by(user_id=user_id, code=code, is_used=False).first()
        if user_coupon:
            coupon = user_coupon.coupon

    if not coupon:
        public_coupon = Coupon.query.filter_by(code=code).first()
        if public_coupon and (public_coupon.reward_points == 0 and not public_coupon.is_reward):
            coupon = public_coupon
        elif public_coupon and public_coupon.is_reward:
            if not user_id:
                return jsonify({'valid': False, 'discount': 0, 'message': '此為會員紅利專屬券，請先登入會員！'})
            else:
                return jsonify({'valid': False, 'discount': 0, 'message': '您尚未在回饋商城兌換此券，或該券已被使用！'})
        else:
            return jsonify({'valid': False, 'discount': 0, 'message': '無效的優惠代碼！'})

    if subtotal < coupon.min_spend:
        return jsonify({'valid': False, 'discount': 0, 'message': f'未達使用門檻！需消費滿 ${int(coupon.min_spend)} 元才可折抵。'})

    discount = calc_promo_discount(coupon, subtotal)
    if coupon.discount_type == 'fixed':
        msg = f'已折抵現金 ${int(discount)} 元！'
    else:
        safe_percent = sanitize_discount_value('percent', coupon.discount_value)
        msg = f'已套用 {round(safe_percent * 10, 1)} 折優惠，折抵 ${int(discount)} 元！'

    return jsonify({'valid': True, 'discount': discount, 'message': msg})


@customer_bp.route('/shop/<tenant_slug>', endpoint='tenant_storefront')
def tenant_storefront(tenant_slug):
    return redirect(url_for('customer.customer_index'))


@customer_bp.route('/submit_order', methods=['POST'])
@rate_limit(max_requests=20, window_seconds=60, key_prefix="customer_submit_order", methods=('POST',))
def submit_order():
    data = request.get_json() or {}
    raw_items = data.get('items', [])
    payment_method = str(data.get('payment_method', 'Cash')).strip()
    order_type = str(data.get('order_type', '內用')).strip()

    if payment_method not in ALLOWED_PAYMENT_METHODS:
        return jsonify({'error': f'不支援的付款方式：{payment_method}'}), 400
    if order_type not in ALLOWED_ORDER_TYPES:
        return jsonify({'error': f'不支援的用餐方式：{order_type}'}), 400

    need_cutlery = bool(data.get('need_cutlery', True)) if order_type == '外帶' else False
    note = str(data.get('note', '')).strip()[:200]
    promo_code = str(data.get('promo_code', '')).strip().upper()
    use_points = max(0, safe_int(data.get('use_points', 0), 0))

    if not raw_items or not isinstance(raw_items, list):
        return jsonify({'error': '購物車為空'}), 400

    raw_user_id = session.get('user_id')
    user = db.session.get(User, raw_user_id) if raw_user_id else None
    if not user or user.tenant_id != g.tenant_id:
        user = None
        user_id = None
    else:
        user_id = user.id

    all_active_modifiers = ModifierOption.query.filter_by(is_active=True).all()
    modifier_db_map = {mod.id: mod for mod in all_active_modifiers}
    modifier_names_set = {mod.name for mod in all_active_modifiers}

    verified_items = []
    item_demands = {}
    reward_claims_needed = []  # (menu_item, quantity, unit_pts)

    for client_item in raw_items:
        if not isinstance(client_item, dict):
            continue
        raw_name = str(client_item.get('name', '')).strip()
        raw_custom = str(client_item.get('customization', '')).strip()
        quantity = max(1, safe_int(client_item.get('quantity', 1), 1))
        is_add_on = bool(client_item.get('is_add_on', False))
        raw_id_str = str(client_item.get('id', ''))
        is_claimed_reward = ('[點數兌換]' in raw_name) or ('reward_' in raw_id_str) or bool(client_item.get('is_reward'))
        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', raw_name).strip()

        menu_item = MenuItem.query.filter_by(name=clean_name).first()
        if not menu_item:
            fallback_id = safe_int(client_item.get('id'), 0)
            if fallback_id <= 0 and raw_id_str.startswith('reward_'):
                parts = raw_id_str.split('_')
                if len(parts) >= 2:
                    fallback_id = safe_int(parts[1], 0)
            if fallback_id > 0:
                menu_item = MenuItem.query.filter_by(id=fallback_id, tenant_id=g.tenant_id).first()

        if not menu_item:
            return jsonify({'error': f'餐點【{clean_name}】不存在於系統中！'}), 400

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
                return jsonify({'error': f'餐點【{menu_item.name}】不支援客製化加料！'}), 400

            for mid in clean_modifier_ids:
                if mid in modifier_db_map:
                    mod = modifier_db_map[mid]
                    extra_modifier_price += mod.price
                    verified_modifier_labels.append(f"{mod.name}(+{mod.price})")
                else:
                    return jsonify({'error': '包含無效或已停用的加料品項！'}), 400

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
        if is_claimed_reward:
            if not user_id:
                return jsonify({'error': '非會員無法訂購紅利商城兌換餐點！'}), 403
            if not menu_item.is_reward:
                return jsonify({'error': f'餐點【{menu_item.name}】未開放紅利兌換！'}), 400
            unit_pts = menu_item.reward_discount_points if (
                menu_item.reward_discount_points and menu_item.reward_discount_points > 0
            ) else menu_item.reward_points
            if not unit_pts or unit_pts <= 0:
                return jsonify({'error': f'餐點【{menu_item.name}】未設定有效兌換點數！'}), 400

            reward_claims_needed.append((menu_item, quantity, unit_pts))
            unit_price = extra_modifier_price
            if not safe_customization:
                safe_customization = '紅利免費兌換'
        elif is_add_on:
            if not menu_item.can_be_add_on:
                return jsonify({'error': f'餐點【{menu_item.name}】非合法加購品項！'}), 400
            unit_price = menu_item.add_on_price + extra_modifier_price
        else:
            base_price = menu_item.discount_price if (menu_item.is_discount and menu_item.discount_price > 0) else menu_item.price
            combo_extra_price = 0
            combo_match = re.search(r'\[套餐:\s*([^\]]+)\]', raw_custom)
            if combo_match:
                combo_name = combo_match.group(1).strip()
                combo_opt = ComboOption.query.filter_by(main_item_id=menu_item.id, name=combo_name).first()
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
            'display_name': raw_name or menu_item.name,
            'price': int(unit_price),
            'quantity': quantity,
            'customization': safe_customization,
            'modifier_ids': clean_modifier_ids
        })

    subtotal = sum(v['price'] * v['quantity'] for v in verified_items)

    try:
        with acquire_distributed_lock(f"lock:order_checkout:{g.tenant_id}", timeout=8, blocking_timeout=5):
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
            max_pickup = db.session.query(db.func.max(Order.pickup_number)).filter(
                Order.tenant_id == g.tenant_id,
                Order.created_at >= today_start,
                Order.created_at <= today_end
            ).scalar()
            next_pickup = (max_pickup % 999) + 1 if (max_pickup and max_pickup > 0) else 1

            # 1. 原子扣減主餐與套餐配餐庫存
            for item_id, demanded_qty in item_demands.items():
                result = db.session.execute(
                    db.text("UPDATE menu_item SET stock = stock - :qty, is_sold_out = CASE WHEN stock - :qty <= 0 THEN 1 ELSE is_sold_out END WHERE id = :id AND tenant_id = :tenant_id AND stock >= :qty"),
                    {"id": item_id, "qty": demanded_qty, "tenant_id": g.tenant_id}
                )
                if result.rowcount == 0:
                    db.session.rollback()
                    m_item = db.session.get(MenuItem, item_id)
                    curr = m_item.stock if m_item else 0
                    return jsonify({'success': False, 'insufficient_stock': True, 'message': f'餐點【{m_item.name if m_item else ""}】庫存剩 {curr} 份，已被搶購完畢。'}), 400

            # 2. 優先核銷已在紅利商城兌換之 RewardRedemption 憑證，避免二次重複扣點 (#6)
            bound_redemptions = []
            for m_item, qty, unit_pts in reward_claims_needed:
                unused_vouchers = RewardRedemption.query.filter_by(
                    user_id=user_id, menu_item_id=m_item.id, is_used=False, is_refunded=False
                ).order_by(RewardRedemption.id.asc()).limit(qty).all()

                vouchers_found = len(unused_vouchers)
                for v_obj in unused_vouchers:
                    v_obj.is_used = True
                    bound_redemptions.append(v_obj)

                missing_qty = qty - vouchers_found
                if missing_qty > 0:
                    extra_pts = missing_qty * unit_pts
                    pts_res = db.session.execute(
                        db.text("UPDATE user SET points = points - :pts WHERE id = :id AND tenant_id = :tenant_id AND points >= :pts"),
                        {"id": user_id, "pts": extra_pts, "tenant_id": g.tenant_id}
                    )
                    if pts_res.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'error': f'會員紅利點數不足，無法兌換【{m_item.name}】！'}), 400
                    for _ in range(missing_qty):
                        new_v = RewardRedemption(
                            tenant_id=g.tenant_id,
                            user_id=user_id,
                            menu_item_id=m_item.id,
                            points_cost=unit_pts,
                            is_used=True,
                            is_refunded=False
                        )
                        db.session.add(new_v)
                        bound_redemptions.append(new_v)

            promo_discount = 0
            target_user_coupon = None
            user = db.session.get(User, user_id) if user_id else None

            if promo_code and user:
                target_user_coupon = UserCoupon.query.filter_by(user_id=user.id, code=promo_code, is_used=False).first()
                if target_user_coupon and subtotal >= target_user_coupon.coupon.min_spend:
                    cp = target_user_coupon.coupon
                    promo_discount = calc_promo_discount(cp, subtotal)
                    coupon_update = db.session.execute(
                        db.text("UPDATE user_coupon SET is_used = 1, used_at = :now WHERE id = :id AND tenant_id = :tenant_id AND is_used = 0"),
                        {"id": target_user_coupon.id, "now": datetime.now(), "tenant_id": g.tenant_id}
                    )
                    if coupon_update.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'error': '該優惠券已被核銷！'}), 400

            if promo_code and not target_user_coupon:
                pub_cp = Coupon.query.filter_by(code=promo_code, is_reward=False, reward_points=0).first()
                if pub_cp and subtotal >= pub_cp.min_spend:
                    promo_discount = calc_promo_discount(pub_cp, subtotal)

            setting = RewardSetting.query.first()
            ppd = setting.points_per_dollar if (setting and setting.points_per_dollar > 0) else 1
            max_discount_limit = setting.max_discount_per_order if setting else 0
            spend_threshold = setting.spend_per_point if (setting and setting.spend_per_point > 0) else 100
            is_points_enabled = setting.is_enabled if setting else True

            remaining_amount = max(0, subtotal - promo_discount)
            points_used = 0
            points_discount = 0

            if user and use_points > 0 and is_points_enabled:
                curr_pts_row = db.session.execute(
                    db.text("SELECT points FROM user WHERE id = :id AND tenant_id = :tenant_id"),
                    {"id": user.id, "tenant_id": g.tenant_id}
                ).fetchone()
                avail_pts = int(curr_pts_row[0] or 0) if curr_pts_row else 0

                max_cash = remaining_amount
                if max_discount_limit > 0:
                    max_cash = min(max_cash, max_discount_limit)

                actual_points = min(avail_pts, use_points)
                points_discount = min(actual_points // ppd, max_cash)
                points_used = int(points_discount * ppd)

                if points_used > 0:
                    pts_update = db.session.execute(
                        db.text("UPDATE user SET points = points - :used WHERE id = :id AND tenant_id = :tenant_id AND points >= :used"),
                        {"id": user.id, "used": points_used, "tenant_id": g.tenant_id}
                    )
                    if pts_update.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'error': '紅利點數不足！'}), 400

            final_price = int(max(0, remaining_amount - points_discount))
            total_discount = int(promo_discount + points_discount)
            points_earned = int(final_price // spend_threshold) if user else 0

            if user and points_earned > 0:
                db.session.execute(
                    db.text("UPDATE user SET points = points + :earned WHERE id = :id AND tenant_id = :tenant_id"),
                    {"id": user.id, "earned": points_earned, "tenant_id": g.tenant_id}
                )

            user_name = session.get('user_name', '訪客')
            new_order = Order(
                tenant_id=g.tenant_id,
                user_id=user_id,
                table_number=user_name,
                total_price=final_price,
                payment_method=payment_method,
                order_type=order_type,
                order_source='Kiosk',
                cashier_username='',
                cash_received=final_price,
                cash_change=0,
                need_cutlery=need_cutlery,
                note=note,
                status='Pending',
                points_used=points_used,
                points_earned=points_earned,
                discount_amount=total_discount,
                coupon_code=promo_code if promo_code else '',
                pickup_number=next_pickup,
                customer_log_id=session.get('customer_log_id')
            )
            db.session.add(new_order)
            db.session.flush()

            for v_obj in bound_redemptions:
                v_obj.order_id = new_order.id

            receipt_items = []
            for v in verified_items:
                order_item = OrderItem(
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
                    'price': v['price'],
                    'quantity': v['quantity'],
                    'subtotal': v['price'] * v['quantity'],
                    'customization': v['customization']
                })
            db.session.commit()

    except TimeoutError:
        return jsonify({'error': '下單人潮眾多，系統排隊逾時，請再次點擊結帳！'}), 503
    except Exception as e:
        db.session.rollback()
        print(f"[!] 下單交易異常: {e}")
        return jsonify({'error': '系統繁忙，交易未完成！'}), 500

    db.session.expire_all()
    if user_id:
        refreshed_user = db.session.get(User, user_id)
        session['user_points'] = refreshed_user.points if refreshed_user else 0

    update_popular_items(force=False)
    invalidate_ai_advice(g.tenant_id)

    log_id = session.get('customer_log_id')
    if log_id:
        customer_log = db.session.get(CustomerLog, log_id)
        if customer_log:
            items_summary = ", ".join([f"{it['name']} x{it['quantity']}" for it in receipt_items])
            entry = f"[取餐#{next_pickup} 單號#{new_order.id}] {items_summary}"
            customer_log.ordered_items = f"{customer_log.ordered_items} ； {entry}" if customer_log.ordered_items else entry
            customer_log.logout_at = datetime.now()
            db.session.commit()
        session.pop('customer_log_id', None)

    latest_user_coupons = []
    if user_id:
        avail_cps = UserCoupon.query.filter_by(user_id=user_id, is_used=False).all()
        for uc in avail_cps:
            latest_user_coupons.append({
                'code': uc.code,
                'title': uc.coupon.title,
                'min_spend': uc.coupon.min_spend
            })

    order_event_bus.notify()

    return jsonify({
        'success': True,
        'order_id': new_order.id,
        'pickup_number': next_pickup,
        'user_name': user_name,
        'subtotal': subtotal,
        'discount_amount': total_discount,
        'points_used': points_used,
        'points_earned': points_earned,
        'total_price': final_price,
        'payment_method': payment_method,
        'order_type': order_type,
        'order_source': 'Kiosk',
        'need_cutlery': need_cutlery,
        'note': note,
        'items': receipt_items,
        'current_user_points': session.get('user_points', 0),
        'remaining_points': session.get('user_points', 0),
        'available_coupons': latest_user_coupons
    })


@customer_bp.route('/my_orders', endpoint='my_orders')
def my_orders():
    user_id = session.get('user_id')
    user_name = session.get('user_name')

    if user_id:
        orders = Order.query.filter_by(user_id=user_id).order_by(Order.id.desc()).all()
    elif user_name:
        orders = Order.query.filter_by(table_number=user_name).order_by(Order.id.desc()).all()
    else:
        return jsonify([])

    total_count = len(orders)
    result = []
    status_map = {'Pending': '製作中', 'Completed': '已出餐', 'PickedUp': '已取餐', 'Cancelled': '已取消'}

    for idx, o in enumerate(orders):
        items_data = [{
            'name': i.item_name,
            'price': i.price,
            'quantity': i.quantity,
            'subtotal': i.price * i.quantity,
            'customization': i.customization or ''
        } for i in o.items]
        pickup_val = o.pickup_number if (getattr(o, 'pickup_number', None) is not None and o.pickup_number > 0) else o.id

        result.append({
            'order_id': o.id,
            'user_order_no': total_count - idx,
            'pickup_number': pickup_val,
            'total_price': round(o.total_price),
            'discount_amount': round(o.discount_amount or 0),
            'points_used': round(o.points_used or 0),
            'points_earned': o.points_earned or 0,
            'payment_method': o.payment_method,
            'order_type': o.order_type,
            'status': status_map.get(o.status, o.status),
            'created_at': o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else '',
            'items': items_data
        })
    return jsonify(result)
