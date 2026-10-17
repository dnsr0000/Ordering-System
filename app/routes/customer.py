import re
from datetime import datetime
from flask import Blueprint, render_template, request, session, jsonify, redirect, url_for, g
from app.extensions import db
from app.models.user import User, CustomerLog, RewardSetting
from app.models.menu import MenuItem, ModifierOption
from app.models.order import Order
from app.models.coupon import UserCoupon
from app.services.order_service import update_popular_items, verify_promo_code, process_checkout
from app.routes.common import clear_customer_session
from app.utils import rate_limit, safe_int

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
    code = data.get('promo_code', '')
    subtotal = data.get('subtotal', 0)
    user_id = session.get('user_id')

    result = verify_promo_code(
        code=code,
        subtotal=subtotal,
        user_id=user_id,
        is_pos=False,
        tenant_id=getattr(g, 'tenant_id', None) or session.get('tenant_id') or 1
    )
    return jsonify({
        'valid': result['valid'],
        'discount': result['discount'],
        'message': result['message']
    })

@customer_bp.route('/shop/<tenant_slug>', endpoint='tenant_storefront')
def tenant_storefront(tenant_slug):
    return redirect(url_for('customer.customer_index'))

@customer_bp.route('/submit_order', methods=['POST'])
@rate_limit(max_requests=20, window_seconds=60, key_prefix="customer_submit_order", methods=('POST',))
def submit_order():
    data = request.get_json() or {}
    user_id = session.get('user_id')
    user_name = session.get('user_name', '訪客')
    cust_log_id = session.get('customer_log_id')
    order_type = str(data.get('order_type', '內用')).strip()
    need_cutlery = bool(data.get('need_cutlery', True)) if order_type == '外帶' else False

    result, status_code = process_checkout(
        items_data=data.get('items', []),
        payment_method=data.get('payment_method', 'Cash'),
        order_type=order_type,
        need_cutlery=need_cutlery,
        note=data.get('note', ''),
        promo_code=data.get('promo_code', ''),
        use_points=data.get('use_points', 0),
        table_input=user_name,
        cash_received=0,
        user_id=user_id,
        order_source='Kiosk',
        cashier_username='',
        customer_log_id=cust_log_id,
        client_ip=request.remote_addr or '127.0.0.1',
        tenant_id=getattr(g, 'tenant_id', None) or session.get('tenant_id') or 1
    )

    if status_code == 200:
        session['user_points'] = result.get('remaining_points', 0)

    return jsonify(result), status_code
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
