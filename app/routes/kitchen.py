import re
import time
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify
from app.extensions import db
from app.models.order import Order
from app.services.order_service import cancel_order_and_rollback
from app.services.event_bus import order_event_bus

kitchen_bp = Blueprint('kitchen', __name__)

@kitchen_bp.route('/kitchen', endpoint='kitchen_display')
def kitchen_display():
    return render_template('kitchen.html')

@kitchen_bp.route('/api/kitchen_orders')
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
def kitchen_update_status(id):
    order = Order.query.get_or_404(id)
    data = request.get_json() or {}
    new_status = data.get('status', 'Completed')

    if new_status == 'Cancelled':
        success, msg = cancel_order_and_rollback(order)
        return jsonify({'success': True, 'message': msg})

    if new_status in ['Pending', 'Completed']:
        order.status = new_status
        if new_status == 'Completed':
            order.completed_at = datetime.now()
        db.session.commit()
        order_event_bus.notify()
        return jsonify({'success': True, 'message': f'訂單 #{order.id} 狀態更新為 {new_status}'})
    return jsonify({'success': False, 'message': '無效狀態'}), 400

@kitchen_bp.route('/api/kitchen_complete_all', methods=['POST'])
def kitchen_complete_all():
    today = datetime.now().date()
    pending = Order.query.filter(Order.status == 'Pending', db.func.date(Order.created_at) == today).all()
    if not pending:
        return jsonify({'success': False, 'message': '目前沒有待製作的訂單！'})
    now = datetime.now()
    for o in pending:
        o.status = 'Completed'
        o.completed_at = now
    db.session.commit()
    order_event_bus.notify()
    return jsonify({'success': True, 'message': f'✅ 已將 {len(pending)} 筆訂單出餐！', 'count': len(pending)})