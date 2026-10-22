from flask import Blueprint, abort, g, jsonify, render_template, request
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from app.errors import AppError
from app.extensions import db, limiter
from app.models.coupon import Coupon, UserCoupon
from app.models.enums import OrderSource
from app.models.menu import MenuItem, ModifierOption
from app.models.order import Order
from app.models.user import User
from app.routes.sync_admin import node_name_map
from app.services.audit import record_staff_audit
from app.services.auth_throttle import clear_pin_lockout
from app.services.menu_service import menu_categories, storefront_payload
from app.services.order_service import find_promo, process_checkout, todays_range
from app.services import table_service
from app.services.reward_service import get_or_create_reward_setting
from app.sync import runtime as sync_runtime
from app.utils import WEAK_PIN_MESSAGE, get_json_dict, is_weak_pin, role_required, safe_int, validate_pin

pos_bp = Blueprint('pos', __name__)
POS_ROLES = ('owner', 'manager', 'staff')


def _member_dict(user):
    coupons = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
    return {
        'id': user.id, 'name': user.name, 'phone': user.phone, 'points': user.points or 0,
        'has_pin': bool(user.pin_hash),
        'coupons': [uc.to_dict() for uc in coupons if uc.coupon],
    }


def _get_member(user_id):
    user = db.session.get(User, safe_int(user_id, 0))
    if user is None or user.tenant_id != g.tenant_id:
        raise AppError('查無此會員！', 404)
    return user


@pos_bp.route('/pos', endpoint='pos_terminal')
@role_required(*POS_ROLES)
def pos_terminal():
    items = MenuItem.query.options(selectinload(MenuItem.combo_options)).all()
    modifiers = ModifierOption.query.filter_by(is_active=True).all()
    setting = get_or_create_reward_setting(g.tenant_id)
    public_coupons = Coupon.query.filter_by(is_reward=False).all()
    return render_template(
        'pos.html',
        items=items,
        categories=menu_categories(items),
        modifier_options=modifiers,
        coupons=public_coupons,
        reward_setting=setting,
        menu_data=dict(storefront_payload(items, modifiers, setting),
                       publicCoupons=[{'code': c.code, 'title': c.title, 'label': c.discount_label,
                                       'min_spend': c.min_spend} for c in public_coupons]),
        tenant=g.tenant,
        staff=current_user,
    )


@pos_bp.route('/api/pos/member_lookup', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('45/minute')
def api_pos_member_lookup():
    """
    以手機或姓名查詢本店會員：指定 member_id 或完整手機直接回傳；
    模糊比對多筆時回傳 candidates 讓店員點選，避免綁錯同名會員扣錯點數。
    """
    data = get_json_dict()
    if safe_int(data.get('member_id'), 0) > 0:
        return jsonify({'success': True, 'multiple': False, 'member': _member_dict(_get_member(data['member_id']))})
    q = str(data.get('query', '')).strip()
    if not q:
        raise AppError('請輸入會員手機號碼或姓名！')
    exact = User.query.filter_by(phone=q).first()
    matches = [exact] if exact else (
        User.query.filter(or_(User.phone.contains(q), User.name.contains(q))).order_by(User.id).limit(20).all())
    if not matches:
        raise AppError(f'查無符合「{q}」的本店會員！', 404)
    if len(matches) == 1:
        return jsonify({'success': True, 'multiple': False, 'member': _member_dict(matches[0])})
    return jsonify({'success': True, 'multiple': True, 'candidates': [_member_dict(u) for u in matches]})


@pos_bp.route('/api/pos/set_member_pin', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('15/minute')
def api_pos_set_member_pin():
    """店員當面核對身分後，協助會員設定/重設 PIN 並解除鎖定"""
    data = get_json_dict()
    pin = str(data.get('pin', '')).strip()
    if not validate_pin(pin):
        raise AppError('請輸入 4 至 6 位數字的安全 PIN 碼！')
    user = _get_member(data.get('user_id'))
    if is_weak_pin(pin, user.phone):
        raise AppError(WEAK_PIN_MESSAGE)
    user.set_pin(pin)
    record_staff_audit('POS_SET_MEMBER_PIN', target_user_id=user.id,
                       details=f"櫃檯協助會員 {user.name} ({user.phone}) 設定/重設安全 PIN 碼並解除鎖定")
    db.session.commit()
    clear_pin_lockout(g.tenant_id, user.phone)
    return jsonify({'success': True, 'message': f'已成功為會員【{user.name}】設定安全 PIN 碼！'})


@pos_bp.route('/api/pos/verify_promo', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('60/minute')
def api_pos_verify_promo():
    data = get_json_dict()
    result = find_promo(data.get('promo_code'), data.get('subtotal'), safe_int(data.get('user_id'), 0) or None, is_pos=True)
    return jsonify(result.to_dict())


@pos_bp.route('/api/pos/checkout', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('30/minute')
def api_pos_checkout():
    data = get_json_dict()
    order, replayed = process_checkout(
        tenant_id=g.tenant_id,
        items=data.get('items'),
        payment_method=data.get('payment_method', 'Cash'),
        order_type=data.get('order_type', '內用'),
        need_cutlery=data.get('need_cutlery', False),
        note=data.get('note', ''),
        promo_code=data.get('promo_code', ''),
        use_points=data.get('use_points', 0),
        table_input=data.get('table_number', ''),
        cash_received=data.get('cash_received', 0),
        user_id=data.get('user_id'),
        order_source=OrderSource.POS,
        cashier_username=current_user.username,
        client_ip=request.remote_addr or '',
        client_order_id=data.get('client_order_id'),
    )
    return jsonify(dict(order.to_receipt(), replayed=replayed))


def _live_order_dict(o, node_names=None):
    """POS「當日訂單」視窗與補印收據需要的欄位 (node_names：線上端顯示取餐分店名稱)"""
    return {
        'id': o.id, 'pickup_number': o.display_pickup_number,
        'pickup_pending': o.order_source == OrderSource.ONLINE and not (o.pickup_number or 0) > 0,
        'pickup_node_name': (node_names or {}).get(o.node_uuid, '') if o.node_uuid else '',
        'user_name': o.user.name if o.user else (o.table_number or '非會員'),
        'table_number': o.table_number or '', 'payment_status': o.payment_status or '',
        'order_type': o.order_type, 'order_source': o.order_source or 'Kiosk',
        'cashier_username': o.cashier_username or '',
        'cash_received': o.cash_received if o.cash_received is not None else o.total_price,
        'cash_change': o.cash_change or 0, 'payment_method': o.payment_method,
        'need_cutlery': bool(o.need_cutlery), 'note': o.note or '',
        'created_at': o.created_at.strftime('%m-%d %H:%M') if o.created_at else '',
        'status': o.status,
        'discount_amount': o.discount_amount or 0, 'total_price': o.total_price,
        'items': [i.to_dict() for i in o.items],
    }


@pos_bp.route('/api/pos/today_orders')
@role_required(*POS_ROLES)
def api_pos_today_orders():
    """
    POS 當日訂單 + 即時庫存 (SSE 通知後重新抓取)。
    只查本店今日訂單並預載明細 (會員為 joined 載入)，不計算營運分析，比 /api/admin_live_orders 輕量。
    """
    start, end = todays_range()
    orders = (Order.query.options(selectinload(Order.items))
              .filter(Order.created_at >= start, Order.created_at < end)
              .order_by(Order.id.desc()).all())
    node_names = node_name_map()
    stock = (db.session.query(MenuItem.id, MenuItem.stock, MenuItem.is_sold_out)
             .order_by(MenuItem.id).all())
    return jsonify({
        'success': True,
        'orders': [_live_order_dict(o, node_names) for o in orders],
        'unpaid_tables': [] if sync_runtime.is_hub() else table_service.unpaid_tables(),
        'menu_items': [{'id': mid, 'stock': qty or 0, 'is_sold_out': bool(sold_out) or (qty is not None and qty <= 0)}
                       for mid, qty, sold_out in stock],
    })


@pos_bp.route('/api/pos/tables/<int:table_id>/settle', methods=['POST'])
@role_required(*POS_ROLES)
def api_settle_table(table_id):
    """桌位結帳：結清該桌所有未結帳的掃碼點餐訂單"""
    if sync_runtime.is_hub():
        abort(404)
    data = get_json_dict()
    result = table_service.settle_table(g.tenant_id, table_id, data.get('payment_method', 'Cash'),
                                        data.get('cash_received', 0), current_user)
    return jsonify(dict(result, success=True))
