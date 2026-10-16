import re
import time
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, g
from app.extensions import db
from app.models.order import Order
from app.services.order_service import cancel_order_and_rollback
from app.services.event_bus import order_event_bus
from app.services.ai_service import invalidate_ai_advice
from app.routes.common import record_staff_audit
from app.utils import role_required

kitchen_bp = Blueprint('kitchen', __name__)


@kitchen_bp.route('/kitchen', endpoint='kitchen_display')
@role_required('owner', 'manager', 'kitchen')
def kitchen_display():
    return render_template('kitchen.html')


@kitchen_bp.route('/api/kitchen_orders')
@role_required('owner', 'manager', 'kitchen')
def api_kitchen_orders():
    today = datetime.now().date()
    pending_orders = Order.query.filter(Order.status == 'Pending', db.func.date(Order.created_at) == today).order_by(Order.created_at.asc()).all()
    orders_data = []
    for o in pending_orders:
        aggregated = {}
        for i in o.items:
            clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', i.item_name).strip()
            custom_note = '' if i.customization == '紅利免費兌換' else (i.customization or '')
            key = (clean_name, custom_note)
            if key not in aggregated:
                aggregated[key] = {'name': clean_name, 'quantity': 0, 'customization': custom_note}
            aggregated[key]['quantity'] += (i.quantity or 1)

        orders_data.append({
            'id': o.id,
            'pickup_number': o.pickup_number if getattr(o, 'pickup_number', None) else o.id,
            'table_number': o.table_number,
            'order_type': o.order_type,
            'need_cutlery': bool(o.need_cutlery) if getattr(o, 'need_cutlery', None) is not None else True,
            'note': o.note or '',
            'created_at': o.created_at.strftime('%H:%M:%S') if o.created_at else '',
            'timestamp': o.created_at.timestamp() if o.created_at else time.time(),
            'items': list(aggregated.values())
        })
    return jsonify({'success': True, 'orders': orders_data})


@kitchen_bp.route('/api/kitchen_update_status/<int:id>', methods=['POST'])
@role_required('owner', 'manager', 'kitchen')
def kitchen_update_status(id):
    order = Order.query.get_or_404(id)
    data = request.get_json() or {}
    new_status = data.get('status', 'Completed')

    if new_status == 'Cancelled':
        pts_before = order.user.points if order.user else 0
        success, msg = cancel_order_and_rollback(order)
        if not success:
            return jsonify({'success': False, 'message': msg}), 400
        pts_after = order.user.points if order.user else 0
        record_staff_audit(
            action_type='CANCEL_ORDER',
            target_order_id=order.id,
            target_user_id=order.user_id,
            points_delta=(pts_after - pts_before),
            details=f"取消訂單 #{order.id} (取餐 #{order.pickup_number}) 並回滾庫存與點數"
        )
        db.session.commit()
        return jsonify({'success': True, 'message': msg})

    if order.status == 'Cancelled':
        return jsonify({'success': False, 'message': '已取消的訂單無法變更狀態！'}), 400

    if new_status in ['Pending', 'Completed']:
        order.status = new_status
        if new_status == 'Completed':
            order.completed_at = datetime.now()
        db.session.commit()
        invalidate_ai_advice(g.tenant_id)
        order_event_bus.notify()
        return jsonify({'success': True, 'message': f'訂單 #{order.id} 狀態更新為 {new_status}'})
    return jsonify({'success': False, 'message': '無效狀態'}), 400


@kitchen_bp.route('/api/kitchen_complete_all', methods=['POST'])
@role_required('owner', 'manager', 'kitchen')
def kitchen_complete_all():
    today = datetime.now().date()
    now = datetime.now()

    updated_count = Order.query.filter(
        Order.status == 'Pending',
        db.func.date(Order.created_at) == today
    ).update(
        {Order.status: 'Completed', Order.completed_at: now},
        synchronize_session=False
    )

    db.session.commit()
    invalidate_ai_advice(g.tenant_id)
    order_event_bus.notify()

    return jsonify({
        'success': True,
        'message': f'✅ 已將 {updated_count} 筆訂單出餐！',
        'count': updated_count
    })
