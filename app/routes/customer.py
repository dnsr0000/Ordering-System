from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.extensions import db, limiter
from app.models.coupon import UserCoupon
from app.models.enums import OrderSource, OrderStatus, OrderType
from app.models.menu import MenuItem, ModifierOption
from app.models.order import Order, OrderItem
from app.models.sync import NodeStock, SyncNode
from app.routes.sync_admin import node_name_map
from app.errors import AppError
from app.services import privacy_service, table_service
from app.services.customer_session import current_member
from app.services.menu_service import menu_categories, storefront_payload
from app.services.order_service import find_promo, process_checkout
from app.services.reward_service import get_or_create_reward_setting
from app.sync import runtime as sync_runtime
from app.utils import get_json_dict

customer_bp = Blueprint('customer', __name__)

PICKUP_NODE_KEY = 'pickup_node_uuid'


def _favorite_items(user_id, limit=3):
    """會員最常點的餐點 (不含兌換品與已取消訂單)"""
    qty = func.sum(OrderItem.quantity)
    rows = db.session.execute(
        select(OrderItem.menu_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.user_id == user_id, Order.status != OrderStatus.CANCELLED,
               OrderItem.is_reward.is_(False), OrderItem.menu_item_id.is_not(None))
        .group_by(OrderItem.menu_item_id).order_by(qty.desc()).limit(limit)
    ).scalars().all()
    by_id = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_(rows)).all()} if rows else {}
    return [by_id[i] for i in rows if i in by_id]


# ------------------------------------------------------------------------------
# 線上端 (hub)：顧客選擇取餐分店，菜單庫存以該分店回報的數量顯示
# ------------------------------------------------------------------------------
def _online_nodes():
    """開放線上點餐、且目前連線中的分店 (本店家、啟用中)"""
    offline_after = current_app.config['SYNC_NODE_OFFLINE_AFTER']
    return [n for n in SyncNode.query.filter_by(tenant_id=g.tenant_id, is_active=True, accepts_online_orders=True)
            .order_by(SyncNode.id).all() if n.is_online(offline_after)]


def _pickup_node(nodes):
    """目前選擇的取餐分店；只有一家時自動選取，選過的分店已停用或暫停線上點餐時清除"""
    chosen = session.get(PICKUP_NODE_KEY)
    node = next((n for n in nodes if n.uuid == chosen), None)
    if node is None and len(nodes) == 1:
        node = nodes[0]
    if node is None:
        session.pop(PICKUP_NODE_KEY, None)
    elif chosen != node.uuid:
        session[PICKUP_NODE_KEY] = node.uuid
    return node


def _node_stock(node):
    """所選分店回報的庫存快照 {品項 ID: (庫存, 是否售完)}；尚未選分店時為空"""
    if node is None:
        return {}
    return {s.menu_item_id: (s.stock or 0, bool(s.is_sold_out))
            for s in NodeStock.query.filter_by(node_id=node.id).all()}


def _sold_out_ids(items, node_stock=None):
    """售完的品項 ID：單機/分店依本機庫存；線上端依所選分店回報的庫存 (沒有回報過的品項視為售完)"""
    if node_stock is None:
        return {i.id for i in items if not i.is_available}
    return {i.id for i in items if i.id not in node_stock or node_stock[i.id][1] or node_stock[i.id][0] <= 0}


def _apply_node_stock(payload, node_stock, sold_out_ids):
    """線上端：把菜單 JSON 中的庫存欄位換成所選分店的庫存 (前端的售完標示、套餐可選與否依此判斷)"""
    for data in payload['items']:
        data['is_available'] = data['id'] not in sold_out_ids
        data['stock'] = node_stock.get(data['id'], (0, True))[0]
        for combo in data.get('combos') or ():
            combo['is_available'] = all(s['menu_item_id'] not in sold_out_ids for s in combo.get('sides') or ())
    return payload


@customer_bp.route('/pickup_node', methods=['POST'], endpoint='set_pickup_node')
def set_pickup_node():
    """線上端：設定取餐分店 (存在 session)"""
    if not sync_runtime.is_hub():
        abort(404)
    uuid = (request.form.get('node_uuid') or '').strip()
    node = next((n for n in _online_nodes() if n.uuid == uuid), None)
    if node is None:
        flash('❌ 此分店目前不開放線上點餐，請重新選擇取餐分店！', 'danger')
        session.pop(PICKUP_NODE_KEY, None)
    else:
        session[PICKUP_NODE_KEY] = node.uuid
        flash(f'✅ 取餐分店：{node.name} (菜單已改為顯示該分店的供應狀況)', 'success')
    return redirect(url_for('customer.customer_index'))


@customer_bp.route('/', endpoint='customer_index')
def customer_index():
    items = MenuItem.query.options(selectinload(MenuItem.combo_options)).all()
    modifiers = ModifierOption.query.filter_by(is_active=True).all()
    setting = get_or_create_reward_setting(g.tenant_id)
    member = current_member()
    if session.get('user_id') and member is None:  # 會員已被刪除或不屬於這家店
        session.pop('user_id', None)
        return redirect(url_for('customer.customer_index'))
    is_hub = sync_runtime.is_hub()
    pickup_nodes = _online_nodes() if is_hub else []
    pickup_node = _pickup_node(pickup_nodes) if is_hub else None
    node_stock = _node_stock(pickup_node) if is_hub else None
    sold_out_ids = _sold_out_ids(items, node_stock)
    menu_data = storefront_payload(items, modifiers, setting)
    if is_hub:
        _apply_node_stock(menu_data, node_stock, sold_out_ids)
    return render_template(
        'customer.html',
        items=items,
        categories=menu_categories(items),
        member=member,
        needs_privacy_consent=privacy_service.needs_consent(member),
        user_points=member.points if member else 0,
        user_coupons=UserCoupon.query.filter_by(user_id=member.id, is_used=False).all() if member else [],
        personalized_items=_favorite_items(member.id) if member else [],
        is_guest=bool(session.get('is_guest')),
        reward_setting=setting,
        modifier_options=modifiers,
        menu_data=menu_data,
        sold_out_ids=sold_out_ids,
        is_online=is_hub,
        pickup_nodes=pickup_nodes,
        pickup_node=pickup_node,
        dining_table=None if is_hub else table_service.current_table(),
        table_expired=False if is_hub else table_service.table_binding_expired(),
        tenant=g.tenant,
    )


@customer_bp.route('/api/user_available_coupons')
def api_user_available_coupons():
    member = current_member()
    if member is None:
        return jsonify({'success': False, 'coupons': [], 'points': 0})
    db.session.refresh(member)
    coupons = UserCoupon.query.filter_by(user_id=member.id, is_used=False).all()
    return jsonify({'success': True, 'points': member.points, 'coupons': [uc.to_dict() for uc in coupons if uc.coupon]})


@customer_bp.route('/api/verify_promo', methods=['POST'])
@limiter.limit('30/minute')
def verify_promo():
    data = get_json_dict()
    member = current_member()
    result = find_promo(data.get('promo_code'), data.get('subtotal'), member.id if member else None)
    return jsonify(result.to_dict())


@customer_bp.route('/shop/<tenant_slug>', endpoint='tenant_storefront')
def tenant_storefront(tenant_slug):
    return redirect(url_for('customer.customer_index'))  # 店家已由 tenant_context 依網址綁定


@customer_bp.route('/submit_order', methods=['POST'])
@limiter.limit('20/minute')
def submit_order():
    data = get_json_dict()
    member = current_member()
    order_type = str(data.get('order_type') or OrderType.DINE_IN).strip()
    # 線上端：取餐分店為顧客在首頁選擇的分店 (未選擇時 process_checkout 會要求先選)
    node_uuid = session.get(PICKUP_NODE_KEY) if sync_runtime.is_hub() else None
    # 門市桌邊掃碼點餐：以綁定的桌位下單 (綁定已失效時不可當成一般點餐機訂單送出，避免漏結帳)
    dining_table = None
    if not sync_runtime.is_hub():
        had_table = table_service.TABLE_SESSION_KEY in session
        dining_table = table_service.current_table()
        if had_table and dining_table is None:
            raise AppError('桌位 QR code 已失效，請重新掃描桌上的 QR code 後再送出！', 409)
    order, replayed = process_checkout(
        tenant_id=g.tenant_id,
        items=data.get('items'),
        payment_method=data.get('payment_method', 'Cash'),
        order_type=order_type,
        need_cutlery=bool(data.get('need_cutlery', True)) and order_type == OrderType.TAKE_OUT,
        note=data.get('note', ''),
        promo_code=data.get('promo_code', ''),
        use_points=data.get('use_points', 0),
        table_input=session.get('user_name', '訪客'),
        user_id=member.id if member else None,
        order_source=OrderSource.KIOSK,
        customer_log_id=session.get('customer_log_id'),
        client_ip=request.remote_addr or '',
        client_order_id=data.get('client_order_id'),
        node_uuid=node_uuid,
        dining_table=dining_table,
    )
    extra = {'payment_url': url_for('payment.pay', order_uuid=order.uuid)} if order.payment_status == 'pending' else {}
    return jsonify(dict(order.to_receipt(), replayed=replayed, **_pickup_info(order, node_name_map()), **extra))


def _pickup_info(order, node_names):
    """線上訂單：取餐號由分店收到後才編號 (之前為 0，顯示「待分店確認」)，並附上取餐分店名稱"""
    return {
        'pickup_pending': order.order_source == OrderSource.ONLINE and not (order.pickup_number or 0) > 0,
        'pickup_node_name': node_names.get(order.node_uuid, '') if order.node_uuid else '',
    }


@customer_bp.route('/my_orders', endpoint='my_orders')
def my_orders():
    """會員看自己的訂單；訪客以本次進場紀錄 (customer_log_id) 歸戶，不會看到撞號訪客的訂單"""
    member = current_member()
    if member is not None:
        query = Order.query.filter_by(user_id=member.id)
    elif session.get('customer_log_id'):
        query = Order.query.filter_by(customer_log_id=session['customer_log_id'], user_id=None)
    else:
        return jsonify([])
    orders = query.options(selectinload(Order.items)).order_by(Order.id.desc()).all()
    node_names = node_name_map()
    return jsonify([{
        'order_id': o.id,
        'user_order_no': len(orders) - idx,
        'pickup_number': o.display_pickup_number,
        **_pickup_info(o, node_names),
        'total_price': o.total_price,
        'discount_amount': o.discount_amount or 0,
        'points_used': o.points_used or 0,
        'points_earned': o.points_earned or 0,
        'payment_method': o.payment_method,
        'payment_status': o.payment_status or '',
        'payment_url': url_for('payment.pay', order_uuid=o.uuid) if o.payment_status == 'pending' else '',
        'order_type': o.order_type,
        'status': o.status_label,
        'created_at': o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else '',
        'items': [i.to_dict() for i in o.items],
    } for idx, o in enumerate(orders)])
