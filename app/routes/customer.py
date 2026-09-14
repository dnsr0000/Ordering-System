import re
from datetime import datetime
from flask import Blueprint, render_template, request, session, jsonify
from app.extensions import db, ORDER_CHECKOUT_LOCK
from app.models.user import User, CustomerLog
from app.models.menu import MenuItem, ComboOption
from app.models.order import Order, OrderItem
from app.models.coupon import Coupon, UserCoupon
from app.services.order_service import update_popular_items, sanitize_discount_value
from app.services.event_bus import order_event_bus
from app.services.ai_service import _AI_ADVICE_STATE
from app.routes.common import clear_customer_session

customer_bp = Blueprint('customer', __name__)

@customer_bp.route('/', endpoint='customer_index')
def customer_index():
    update_popular_items()
    items = MenuItem.query.all()
    categories = sorted(list(set(item.category for item in items if item.category)))
    user_points = 0
    user_coupons = []
    personalized_items = []
    is_guest = session.get('is_guest', False)

    if session.get('user_id'):
        user = db.session.get(User, session['user_id'])
        if not user:
            clear_customer_session()
            return redirect(url_for('customer.customer_index'))
        
        if user:
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
        is_guest=is_guest
    )

@customer_bp.route('/api/user_available_coupons')
def api_user_available_coupons():
    """即時查詢當前登入會員最新可用優惠券清單與點數 (購物車側邊欄必備)"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'coupons': [], 'points': 0})

    db.session.expire_all()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'coupons': [], 'points': 0})

    avail_cps = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
    coupons_list = [{
        'code': uc.code,
        'title': uc.coupon.title if uc.coupon else '優惠券',
        'min_spend': uc.coupon.min_spend if uc.coupon else 0
    } for uc in avail_cps]

    return jsonify({
        'success': True,
        'points': user.points,
        'coupons': coupons_list
    })

@customer_bp.route('/api/verify_promo', methods=['POST'])
def verify_promo():
    data = request.get_json() or {}
    code = str(data.get('promo_code', '')).strip().upper()
    subtotal = float(data.get('subtotal', 0))
    user_id = session.get('user_id')

    if not code:
        return jsonify({'valid': False, 'discount': 0, 'message': '請輸入優惠代碼！'})

    coupon = None
    if user_id:
        user_coupon = UserCoupon.query.filter_by(user_id=user_id, code=code, is_used=False).first()
        if user_coupon:
            coupon = user_coupon.coupon

    if not coupon:
        pub_cp = Coupon.query.filter_by(code=code).first()
        if pub_cp and (pub_cp.reward_points == 0 and not pub_cp.is_reward):
            coupon = pub_cp
        elif pub_cp and pub_cp.is_reward:
            return jsonify({'valid': False, 'discount': 0, 'message': '此為會員紅利專屬券，請先兌換或登入！'})
        else:
            return jsonify({'valid': False, 'discount': 0, 'message': '無效的優惠代碼！'})

    if subtotal < coupon.min_spend:
        return jsonify({'valid': False, 'discount': 0, 'message': f'需消費滿 ${int(coupon.min_spend)} 元才可折抵。'})

    if coupon.discount_type == 'fixed':
        discount = min(coupon.discount_value, subtotal)
        msg = f'已折抵現金 ${int(discount)} 元！'
    else:
        safe_percent = sanitize_discount_value('percent', coupon.discount_value)
        discount = min(subtotal, max(0.0, round(subtotal * (1.0 - safe_percent))))
        msg = f'已套用 {round(safe_percent * 10, 1)} 折優惠，折抵 ${int(discount)} 元！'

    return jsonify({'valid': True, 'discount': discount, 'message': msg})

@customer_bp.route('/submit_order', methods=['POST'])
def submit_order():
    data = request.get_json() or {}
    raw_items = data.get('items', [])
    payment_method = data.get('payment_method', 'Cash')
    order_type = data.get('order_type', '內用')
    need_cutlery = bool(data.get('need_cutlery', True)) if order_type == '外帶' else False
    note = str(data.get('note', '')).strip()[:200]
    promo_code = str(data.get('promo_code', '')).strip().upper()
    use_points = int(data.get('use_points', 0))

    if not raw_items:
        return jsonify({'error': '購物車為空'}), 400

    user_id = session.get('user_id')
    verified_items = []
    item_demands = {}

    for client_item in raw_items:
        raw_name = str(client_item.get('name', '')).strip()
        raw_custom = str(client_item.get('customization', '')).strip()
        quantity = max(1, int(client_item.get('quantity', 1)))
        is_add_on = bool(client_item.get('is_add_on', False))
        is_claimed_reward = ('[點數兌換]' in raw_name) or ('reward_' in str(client_item.get('id', '')))
        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', raw_name).strip()

        menu_item = MenuItem.query.filter_by(name=clean_name).first()
        if not menu_item:
            try: menu_item = db.session.get(MenuItem, int(client_item.get('id')))
            except Exception: menu_item = None

        if not menu_item:
            return jsonify({'error': f'餐點【{clean_name}】不存在！'}), 400

        item_demands[menu_item.id] = item_demands.get(menu_item.id, 0) + quantity
        extra_modifier_price = sum(int(m) for m in re.findall(r'\(\+(\d+)\)', raw_custom))

        if is_claimed_reward:
            unit_price = extra_modifier_price
        elif is_add_on:
            unit_price = menu_item.add_on_price + extra_modifier_price
        else:
            base_price = menu_item.discount_price if (menu_item.is_discount and menu_item.discount_price > 0) else menu_item.price
            combo_extra_price = 0
            combo_match = re.search(r'\[套餐:\s*([^\]]+)\]', raw_custom)
            if combo_match:
                combo_opt = ComboOption.query.filter_by(main_item_id=menu_item.id, name=combo_match.group(1).strip()).first()
                if combo_opt:
                    combo_extra_price = combo_opt.additional_price
            unit_price = base_price + combo_extra_price + extra_modifier_price

        verified_items.append({
            'menu_item_id': menu_item.id,
            'display_name': raw_name,
            'price': int(unit_price),
            'quantity': quantity,
            'customization': raw_custom
        })

    subtotal = sum(v['price'] * v['quantity'] for v in verified_items)

    with ORDER_CHECKOUT_LOCK:
        try:
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
            max_pickup = db.session.query(db.func.max(Order.pickup_number)).filter(
                Order.created_at >= today_start, Order.created_at <= today_end
            ).scalar()
            next_pickup = (max_pickup % 999) + 1 if (max_pickup and max_pickup > 0) else 1

            for item_id, demanded_qty in item_demands.items():
                result = db.session.execute(
                    db.text("UPDATE menu_item SET stock = stock - :qty, is_sold_out = CASE WHEN stock - :qty <= 0 THEN 1 ELSE is_sold_out END WHERE id = :id AND stock >= :qty"),
                    {"id": item_id, "qty": demanded_qty}
                )
                if result.rowcount == 0:
                    db.session.rollback()
                    m_item = db.session.get(MenuItem, item_id)
                    curr = m_item.stock if m_item else 0
                    return jsonify({'success': False, 'insufficient_stock': True, 'message': f'餐點【{m_item.name if m_item else ""}】庫存剩 {curr} 份，已被搶購完畢。'}), 400

            promo_discount = 0
            target_user_coupon = None
            user = db.session.get(User, user_id) if user_id else None

            if promo_code and user:
                target_user_coupon = UserCoupon.query.filter_by(user_id=user.id, code=promo_code, is_used=False).first()
                if target_user_coupon and subtotal >= target_user_coupon.coupon.min_spend:
                    cp = target_user_coupon.coupon
                    if cp.discount_type == 'fixed':
                        promo_discount = min(cp.discount_value, subtotal)
                    else:
                        safe_percent = sanitize_discount_value('percent', cp.discount_value)
                        promo_discount = min(subtotal, max(0.0, round(subtotal * (1.0 - safe_percent))))

                    coupon_update = db.session.execute(
                        db.text("UPDATE user_coupon SET is_used = 1, used_at = :now WHERE id = :id AND is_used = 0"),
                        {"id": target_user_coupon.id, "now": datetime.now()}
                    )
                    if coupon_update.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'error': '該優惠券已被核銷！'}), 400

            if promo_code and not target_user_coupon:
                pub_cp = Coupon.query.filter_by(code=promo_code, is_reward=False, reward_points=0).first()
                if pub_cp and subtotal >= pub_cp.min_spend:
                    if pub_cp.discount_type == 'fixed':
                        promo_discount = min(pub_cp.discount_value, subtotal)
                    else:
                        promo_discount = round(subtotal * (1.0 - pub_cp.discount_value))

            remaining_amount = max(0, subtotal - promo_discount)
            points_used = 0
            if user and use_points > 0:
                points_used = int(min(user.points, use_points, remaining_amount))
                if points_used > 0:
                    pts_update = db.session.execute(
                        db.text("UPDATE user SET points = points - :used WHERE id = :id AND points >= :used"),
                        {"id": user.id, "used": points_used}
                    )
                    if pts_update.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'error': '紅利點數不足！'}), 400

            final_price = max(0, remaining_amount - points_used)
            total_discount = promo_discount + points_used
            points_earned = int(final_price // 100) if user else 0

            if user and points_earned > 0:
                db.session.execute(db.text("UPDATE user SET points = points + :earned WHERE id = :id"), {"id": user.id, "earned": points_earned})

            user_name = session.get('user_name', '訪客')
            new_order = Order(
                user_id=user_id,
                table_number=user_name,
                total_price=int(final_price),
                payment_method=payment_method,
                order_type=order_type,
                need_cutlery=need_cutlery,
                note=note,
                status='Pending',
                points_used=points_used,
                points_earned=points_earned,
                discount_amount=int(total_discount),
                coupon_code=promo_code if promo_code else '',
                pickup_number=next_pickup,
                customer_log_id=session.get('customer_log_id')
            )
            db.session.add(new_order)
            db.session.flush()

            receipt_items = []
            for v in verified_items:
                order_item = OrderItem(
                    order_id=new_order.id,
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
        except Exception as e:
            db.session.rollback()
            print(f"[!] 下單交易異常: {e}")
            return jsonify({'error': '系統繁忙，交易未完成！'}), 500

    if user:
        refreshed_user = db.session.get(User, user_id)
        session['user_points'] = refreshed_user.points if refreshed_user else 0

    update_popular_items()
    _AI_ADVICE_STATE['last_signature'] = None

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
    if user:
        avail_cps = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
        for uc in avail_cps:
            latest_user_coupons.append({
                'code': uc.code,
                'title': uc.coupon.title,
                'min_spend': uc.coupon.min_spend
            })

    order_event_bus.notify()

    return jsonify({
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
        'need_cutlery': need_cutlery,
        'note': note,
        'items': receipt_items,
        'current_user_points': session.get('user_points', 0),
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