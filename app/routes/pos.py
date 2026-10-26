from flask import Blueprint, abort, g, jsonify, render_template, request
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from app.errors import AppError
from app.extensions import db, limiter
from app.models.coupon import Coupon
from app.models.enums import OrderSource
from app.models.menu import MenuItem, ModifierOption
from app.models.order import Order
from app.models.user import User
from app.routes.sync_admin import node_name_map
from app.services import (approval_service, coupon_service, giftcard_service, loyalty_service, order_service,
                          own_delivery_service, pass_service, payment_service, print_service, settings_service,
                          shift_service, wallet_service)
from app.services.audit import record_staff_audit
from app.services.auth_throttle import clear_pin_lockout
from app.services.menu_service import menu_categories, storefront_payload
from app.services.order_service import find_promo, process_checkout, todays_orders_condition, todays_range
from app.services import table_service
from app.services.reward_service import get_or_create_reward_setting
from app.sync import runtime as sync_runtime
from app.utils import WEAK_PIN_MESSAGE, get_json_dict, is_weak_pin, role_required, safe_int, validate_pin

pos_bp = Blueprint('pos', __name__)
POS_ROLES = ('owner', 'manager', 'staff')


def _member_dict(user):
    coupons = coupon_service.usable_coupons(user.id)
    return {
        'id': user.id, 'name': user.name, 'phone': user.phone, 'points': user.points or 0,
        'has_pin': bool(user.pin_hash),
        'coupons': [uc.to_dict() for uc in coupons if uc.coupon],
        'wallet_balance': user.wallet_balance or 0,
        'tier': user.tier.name if user.tier else '',
        'tier_discount': user.tier.discount_percent if user.tier else 0,
        'birthday': user.birthday.isoformat() if user.birthday else '',
        'line_bound': bool(user.line_user_id),
        'stamps': loyalty_service.stamp_card(user),
        'passes': [mp.to_dict() for mp in pass_service.member_passes(user.id)],
    }


def _approver(data):
    """給結帳流程使用的主管核准回呼 (請求內容帶 approver_username / approver_password)"""
    return lambda perm, label: approval_service.require_approval(perm, data, label)


def pos_settings_payload():
    """POS 頁面需要的設定 (付款方式、服務費、刷卡機、LINE Pay、發票、交班…)"""
    from app.services import linepay
    pos_cfg = settings_service.get('pos')
    card = settings_service.get('card_terminal')
    inv = settings_service.get('einvoice')
    member = settings_service.get('member')
    shift = shift_service.current_shift(g.tenant_id)
    return {
        'methods': payment_service.allowed_methods('pos'),
        'serviceChargePercent': pos_cfg.get('service_charge_percent') or 0,
        'serviceChargeScope': pos_cfg.get('service_charge_scope') or [],
        'discountApprovalPercent': pos_cfg.get('discount_approval_percent') or 0,
        'allowPriceOverride': bool(pos_cfg.get('allow_price_override', True)),
        'requireShift': bool(pos_cfg.get('require_shift')),
        'cardRequireAuth': bool(card.get('require_auth_code')),
        'cardRequireLast4': bool(card.get('require_last4')),
        'linePayIntegrated': linepay.is_enabled(),
        'invoiceEnabled': bool(inv.get('enabled')),
        'walletEnabled': bool(member.get('wallet_enabled')),
        'canDiscount': current_user.has_permission('discount'),
        'canRefund': current_user.has_permission('refund'),
        'canCashDrawer': current_user.has_permission('cash_drawer'),
        'canWallet': current_user.has_permission('wallet'),
        'canDelivery': current_user.has_permission('delivery'),
        'deliveryPlatforms': settings_service.split_lines(settings_service.get('delivery').get('manual_platforms')),
        'hasDrawer': bool(print_service.drawer_printers(g.tenant_id)),
        'giftCardEnabled': giftcard_service.enabled(),
        'homeDelivery': ({'zones': own_delivery_service.zones(),
                          'freeOver': int(own_delivery_service.config().get('free_over') or 0)}
                         if not sync_runtime.is_hub() and own_delivery_service.enabled_for('pos') else None),
        'giftPresets': giftcard_service.preset_amounts(),
        'shift': {'id': shift.id, 'opened_by': shift.opened_by,
                  'opened_at': shift.opened_at.strftime('%H:%M')} if shift else None,
    }


def _get_member(user_id):
    user = db.session.get(User, safe_int(user_id, 0))
    if user is None or user.tenant_id != g.tenant_id:
        raise AppError('查無此會員！', 404)
    return user


@pos_bp.route('/pos', endpoint='pos_terminal')
@role_required(*POS_ROLES)
def pos_terminal():
    from app.services import branch_service
    branch_service.activate(g.tenant_id, branch_service.storefront_branch())   # 本分店的價格 / 上下架
    items = [i for i in MenuItem.query.options(selectinload(MenuItem.combo_options)).all() if not i.is_branch_hidden]
    modifiers = ModifierOption.query.filter_by(is_active=True).all()
    setting = get_or_create_reward_setting(g.tenant_id)
    public_coupons = [c for c in Coupon.query.filter_by(is_reward=False, member_only=False).all() if c.period_error() is None]
    pos_cfg = pos_settings_payload()
    return render_template(
        'pos.html',
        pos_cfg=pos_cfg,
        pos_footer=settings_service.get('pos').get('receipt_footer') or '謝謝光臨，請憑取餐號碼取餐！',
        items=items,
        categories=menu_categories(items),
        modifier_options=modifiers,
        coupons=public_coupons,
        reward_setting=setting,
        menu_data=dict(storefront_payload(items, modifiers, setting),
                       publicCoupons=[{'code': c.code, 'title': c.title, 'label': c.discount_label,
                                       'min_spend': c.min_spend} for c in public_coupons],
                       passProducts=[p.to_dict() for p in pass_service.active_products()],
                       pos=pos_cfg),
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
    from app.services import member_qr_service
    if member_qr_service.looks_like_token(q):   # 掃描會員手機上的動態條碼
        user = member_qr_service.resolve(q)
        return jsonify({'success': True, 'multiple': False, 'member': _member_dict(user), 'via_qr': True})
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
        order_discount=data.get('order_discount'),
        approve=_approver(data),
        payment_info=data.get('payment_info') or {},
        invoice_info=data.get('invoice') or None,
        expected_total=data.get('expected_total'),
        is_rush=bool(data.get('is_rush')),
        delivery=data.get('delivery') or None,
    )
    return jsonify(dict(order.to_receipt(), replayed=replayed, track_qr=track_qr(order)))


def track_qr(order):
    """收據上的出餐進度 QR code (手機掃描開啟 /track/<訂單>)；沒有安裝 segno 時省略"""
    try:
        base = (g.tenant.table_qr_base_url or '').rstrip('/') or request.url_root.rstrip('/')
        return table_service.qr_data_uri(f'{base}/track/{order.uuid}')
    except AppError:
        return ''


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
        'uuid': o.uuid, 'subtotal': o.subtotal, 'service_charge': o.service_charge or 0,
        'refunded_amount': o.refunded_amount or 0, 'refundable': o.refundable_amount if o.is_paid else 0,
        'tenders': payment_service.paid_by_method(o) if o.payment_method == '混合付款' else {},
        'is_rush': bool(o.is_rush), 'payment_reference': o.masked_payment_reference, 'pickup_at': o.pickup_at_label,
        'delivery_status': o.delivery_status or '', 'delivery_status_label': o.delivery_status_label,
        'delivery_address': o.delivery_address or '',
        'external_ref': o.external_ref or '', 'invoice': o.invoice_summary(),
        'points_earned': o.points_earned or 0, 'points_used': o.points_used or 0,
    }


@pos_bp.route('/api/pos/today_orders')
@role_required(*POS_ROLES)
def api_pos_today_orders():
    """
    POS 當日訂單 + 即時庫存 (SSE 通知後重新抓取)。
    只查本店今日訂單並預載明細 (會員為 joined 載入)，不計算營運分析，比 /api/admin_live_orders 輕量。
    """
    start, end = todays_range()
    orders = (Order.query.options(selectinload(Order.items), selectinload(Order.invoices))
              .filter(todays_orders_condition(start, end))
              .order_by(Order.id.desc()).all())
    node_names = node_name_map()
    stock = (db.session.query(MenuItem.id, MenuItem.stock, MenuItem.is_sold_out)
             .order_by(MenuItem.id).all())
    return jsonify({
        'success': True,
        'orders': [_live_order_dict(o, node_names) for o in orders],
        'unpaid_tables': [] if sync_runtime.is_hub() else table_service.unpaid_tables(),
        'service_calls': [] if sync_runtime.is_hub() else table_service.open_calls(),
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
                                        data.get('cash_received', 0), current_user, order_ids=data.get('order_ids'),
                                        payments=data.get('payments'), invoice_info=data.get('invoice') or None,
                                        payment_info=data)
    return jsonify(dict(result, success=True))


@pos_bp.route('/api/pos/service_calls/<int:call_id>/done', methods=['POST'])
@role_required(*POS_ROLES)
def api_service_call_done(call_id):
    """桌邊呼叫已處理"""
    table_service.handle_call(call_id, current_user.username)
    return jsonify({'success': True, 'service_calls': table_service.open_calls()})


@pos_bp.route('/api/pos/tables/<int:table_id>/move', methods=['POST'])
@role_required(*POS_ROLES)
def api_move_table(table_id):
    """換桌 / 併桌：把未結帳訂單移到另一桌"""
    if sync_runtime.is_hub():
        abort(404)
    data = get_json_dict()
    result = table_service.move_orders(g.tenant_id, table_id, data.get('to_table_id'), data.get('order_ids'))
    return jsonify(dict(result, success=True))


@pos_bp.route('/api/pos/tables', methods=['GET'])
@role_required(*POS_ROLES)
def api_pos_tables():
    """所有啟用中的桌位 (換桌 / 併桌時選擇目的桌)"""
    if sync_runtime.is_hub():
        abort(404)
    from app.models.table import DiningTable
    tables = DiningTable.query.filter_by(is_active=True).order_by(DiningTable.sort_order, DiningTable.id).all()
    return jsonify({'success': True, 'tables': [{'id': t.id, 'name': t.name} for t in tables]})


@pos_bp.route('/api/pos/floor', methods=['GET'])
@role_required(*POS_ROLES)
def api_pos_floor():
    """桌位平面圖：每桌狀態 (空桌 / 用餐中 / 呼叫中 / 即將訂位)、用餐時間與未結帳金額"""
    if sync_runtime.is_hub():
        abort(404)
    return jsonify(dict(table_service.floor_plan(), success=True))


# ------------------------------------------------------------------------------
# 作廢 / 部分退款 / 補印
# ------------------------------------------------------------------------------
def _order(order_id):
    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != g.tenant_id:
        raise AppError('查無此訂單！', 404)
    return order


@pos_bp.route('/api/pos/orders/<int:order_id>/refund', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('30/minute')
def api_partial_refund(order_id):
    data = get_json_dict()
    _order(order_id)
    approved_by = approval_service.require_approval('refund', data, '部分退款')
    refund = payment_service.partial_refund(order_id, data.get('amount'), data.get('reason'), approved_by=approved_by,
                                            tenant_id=g.tenant_id, method=data.get('method') or None)
    db.session.commit()
    msg = {'done': '已退款', 'manual': '已登記，請在刷卡機 / 對應系統完成退款', 'pending': 'LINE Pay 退款處理中'}
    return jsonify({'success': True, 'refund_id': refund.id, 'status': refund.status,
                    'message': f'訂單 #{order_id} 退款 ${refund.amount}：{msg.get(refund.status, refund.status)}'})


@pos_bp.route('/api/pos/orders/<int:order_id>/void_item', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('30/minute')
def api_void_item(order_id):
    """單品作廢 / 改單：{item_id, quantity, reason, method (混合付款時退回哪一種付款方式)}"""
    data = get_json_dict()
    order = _order(order_id)
    if not any(i.id == safe_int(data.get('item_id'), 0) for i in order.items):
        raise AppError('這張訂單沒有這個品項', 404)
    approved_by = approval_service.require_approval('refund', data, '作廢品項')
    result = order_service.void_item(g.tenant_id, data.get('item_id'), data.get('quantity'), data.get('reason'),
                                     approved_by=approved_by, refund_method=data.get('method') or None)
    db.session.commit()
    msg = f"已作廢，退款 ${result['amount']}" if result['refund_status'] else (
        f"已作廢，應付金額改為 ${result['order_total']}" if result['amount'] else '已作廢')
    return jsonify({'success': True, 'message': msg, **result})


@pos_bp.route('/api/pos/orders/<int:order_id>/reprint', methods=['POST'])
@role_required(*POS_ROLES)
def api_reprint(order_id):
    from app.services import print_service
    sent = print_service.reprint_receipt(_order(order_id))
    return jsonify({'success': True, 'message': '已送出補印' if sent else '已排入列印佇列 (印表機目前無回應)'})


@pos_bp.route('/api/pos/orders/<int:order_id>/rush', methods=['POST'])
@role_required(*POS_ROLES)
def api_rush(order_id):
    from app.services import kitchen_service
    data = get_json_dict()
    order = kitchen_service.set_rush(order_id, g.tenant_id, data.get('is_rush', True))
    record_staff_audit('ORDER_RUSH', target_order_id=order.id,
                       details=f"訂單 #{order.id} {'設為加急' if order.is_rush else '取消加急'}")
    db.session.commit()
    from app.services.event_bus import order_event_bus
    order_event_bus.notify(g.tenant_id)
    return jsonify({'success': True, 'is_rush': order.is_rush})


@pos_bp.route('/api/pos/approval_check', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('20/minute')
def api_approval_check():
    """預先驗證主管帳密 (前端核准視窗用)"""
    data = get_json_dict()
    perm = data.get('permission') or 'discount'
    approver = approval_service.require_approval(perm, data, '主管核准')
    db.session.commit()
    return jsonify({'success': True, 'approver': approver})


# ------------------------------------------------------------------------------
# 交班
# ------------------------------------------------------------------------------
@pos_bp.route('/api/pos/drawer', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('20/minute')
def api_open_drawer():
    """手動開錢櫃 (找零、換鈔)：需要「錢櫃 / 交班」權限 (沒有權限時主管核准)，並留下稽核紀錄"""
    data = get_json_dict()
    reason = str(data.get('reason') or '').strip()[:60] or '手動開啟'
    if not print_service.drawer_printers(g.tenant_id):
        raise AppError('尚未設定接錢櫃的收據印表機 (系統設定 → 印表機與廚房工作站 → 勾選「接錢櫃」)')
    approver = approval_service.require_approval('cash_drawer', data, '開錢櫃')
    record_staff_audit('CASH_DRAWER_OPEN', details=f'{current_user.username} 手動開錢櫃：{reason}'
                                                   + (f' (核准：{approver})' if approver != current_user.username else ''))
    db.session.commit()
    count = print_service.open_drawer(g.tenant_id, reason)
    return jsonify({'success': True, 'count': count, 'message': '已送出開錢櫃指令'})


@pos_bp.route('/pos/display', endpoint='customer_display')
@role_required(*POS_ROLES)
def customer_display():
    """客顯螢幕 (第二螢幕)：與同一台電腦的 POS 視窗以 BroadcastChannel 同步購物車與應付金額"""
    return render_template('pos_display.html', tenant=g.tenant,
                           message=settings_service.get('pos').get('display_message') or '歡迎光臨')


@pos_bp.route('/api/pos/shift', methods=['GET'])
@role_required(*POS_ROLES)
def api_shift_status():
    shift = shift_service.current_shift(g.tenant_id)
    return jsonify({'success': True, 'shift': shift_service.shift_report(shift) if shift else None,
                    'denominations': list(shift_service.DENOMINATIONS)})


@pos_bp.route('/api/pos/shift/open', methods=['POST'])
@approval_service.permission_required('cash_drawer')
def api_shift_open():
    data = get_json_dict()
    shift = shift_service.open_shift(g.tenant_id, current_user.username, data.get('opening_cash'), data.get('note'))
    db.session.commit()
    return jsonify({'success': True, 'shift': shift_service.shift_report(shift)})


@pos_bp.route('/api/pos/shift/movement', methods=['POST'])
@approval_service.permission_required('cash_drawer')
def api_shift_movement():
    from app.models.payment import PaymentKind
    data = get_json_dict()
    kind = PaymentKind.PAY_IN if data.get('kind') == 'pay_in' else PaymentKind.PAY_OUT
    shift_service.cash_movement(g.tenant_id, current_user.username, data.get('amount'), kind, data.get('note'))
    db.session.commit()
    return jsonify({'success': True, 'shift': shift_service.shift_report(shift_service.current_shift(g.tenant_id))})


@pos_bp.route('/api/pos/shift/close', methods=['POST'])
@approval_service.permission_required('cash_drawer')
def api_shift_close():
    data = get_json_dict()
    shift, report = shift_service.close_shift(g.tenant_id, current_user.username, data.get('counted_cash'),
                                              data.get('denominations'), data.get('note'))
    db.session.commit()
    printed = False
    if data.get('print'):
        from app.services import print_service
        try:
            print_service.print_text_document(g.tenant_id, '交班報表', shift_service.report_lines(report))
            printed = True
        except AppError:
            db.session.rollback()
    return jsonify({'success': True, 'report': report, 'printed': printed, 'shift_id': shift.id})


# ------------------------------------------------------------------------------
# 會員儲值
# ------------------------------------------------------------------------------
@pos_bp.route('/api/pos/wallet/topup', methods=['POST'])
@approval_service.permission_required('wallet')
@limiter.limit('20/minute')
def api_wallet_topup():
    data = get_json_dict()
    user = _get_member(data.get('user_id'))
    amount, bonus = wallet_service.topup(g.tenant_id, user, data.get('amount'), data.get('payment_method', 'Cash'),
                                         current_user.username, data.get('payment_info'))
    record_staff_audit('WALLET_TOPUP', target_user_id=user.id,
                       details=f"會員 {user.name} 儲值 ${amount}" + (f" (贈送 ${bonus})" if bonus else '')
                               + f"，付款:{data.get('payment_method', 'Cash')}")
    db.session.commit()
    db.session.refresh(user)
    return jsonify({'success': True, 'member': _member_dict(user), 'amount': amount, 'bonus': bonus,
                    'message': f'已為【{user.name}】儲值 ${amount}' + (f'，加贈 ${bonus}' if bonus else '')
                               + f'，目前餘額 ${user.wallet_balance}'})


# ------------------------------------------------------------------------------
# 禮物卡
# ------------------------------------------------------------------------------
@pos_bp.route('/api/pos/gift_cards/sell', methods=['POST'])
@approval_service.permission_required('wallet')
@limiter.limit('20/minute')
def api_gift_card_sell():
    """售出禮物卡：回傳完整卡號 (交給顧客)；有收據印表機時可列印卡號"""
    data = get_json_dict()
    card = giftcard_service.sell(g.tenant_id, data.get('amount'), data.get('payment_method', 'Cash'),
                                 current_user.username, data.get('payment_info'), data.get('note'))
    record_staff_audit('GIFT_CARD_SELL', details=f'售出禮物卡 {card.masked_code} ${card.initial_amount}'
                                                 f'，付款:{data.get("payment_method", "Cash")}')
    db.session.commit()
    printed = False
    if data.get('print'):
        try:
            print_service.print_text_document(g.tenant_id, '禮物卡', [
                f'面額：${card.initial_amount:,}', f'卡號：{card.code[:4]} {card.code[4:8]} {card.code[8:12]} {card.code[12:]}',
                f'售出：{card.created_at:%Y-%m-%d}', '-', '本禮物卡無使用期限，可於各分店使用。', '請妥善保管卡號，遺失恕不補發。'])
            printed = True
        except AppError:
            db.session.rollback()
    return jsonify({'success': True, 'card': giftcard_service.summary(card), 'printed': printed,
                    'message': f'已售出禮物卡 ${card.initial_amount:,}，卡號 {card.code}'})


@pos_bp.route('/api/pos/gift_cards/lookup', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('30/minute')
def api_gift_card_lookup():
    """查詢禮物卡餘額"""
    card = giftcard_service.find(g.tenant_id, get_json_dict().get('code'))
    return jsonify({'success': True, 'card': giftcard_service.summary(card)})


# ------------------------------------------------------------------------------
# 外送平台訂單 (手動登錄)
# ------------------------------------------------------------------------------
@pos_bp.route('/api/pos/delivery_order', methods=['POST'])
@approval_service.permission_required('delivery')
@limiter.limit('30/minute')
def api_delivery_order():
    from app.models.enums import OrderSource
    data = get_json_dict()
    platform = str(data.get('platform') or '').strip()[:30]
    ref = str(data.get('external_ref') or '').strip()[:60]
    if not platform or not ref:
        raise AppError('請選擇外送平台並輸入平台訂單編號')
    source = {'uber eats': OrderSource.UBER_EATS, 'ubereats': OrderSource.UBER_EATS,
              'foodpanda': OrderSource.FOODPANDA}.get(platform.lower(), OrderSource.DELIVERY)
    from app.services import delivery_service
    dup = delivery_service.find_platform_order(g.tenant_id, source, ref)
    if dup is not None:
        raise AppError(f'平台訂單 {ref} 已登錄過 (#{dup.id})', 409)
    try:
        # 訂單 uuid 由平台單號決定：與平台 Webhook 同時送達或兩台 POS 同時登錄，都只會建立一張
        order, replayed = process_checkout(
            tenant_id=g.tenant_id, items=data.get('items'), order_source=source, order_type='外送',
            payment_method='平台代收', table_input=f'{platform} {ref}'[:40], note=data.get('note', ''),
            cashier_username=current_user.username, client_ip=request.remote_addr or '',
            client_order_id=delivery_service.platform_order_uuid(g.tenant_id, source, ref), external_ref=ref)
    except AppError as exc:
        dup = delivery_service.find_platform_order(g.tenant_id, source, ref) if exc.status == 409 else None
        if dup is not None:
            raise AppError(f'平台訂單 {ref} 已登錄過 (#{dup.id})', 409)
        raise
    return jsonify(dict(order.to_receipt(), replayed=replayed))
