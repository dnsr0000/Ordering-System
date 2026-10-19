import re
import time
from datetime import datetime
from flask import Blueprint, render_template, jsonify, g
from app.extensions import db
from app.models.order import Order
from app.services.order_service import complete_all_pending_orders
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
    pending_orders = Order.query.filter(
        Order.status == 'Pending', 
        db.func.date(Order.created_at) == today
    ).order_by(Order.created_at.asc()).all()
    
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

@kitchen_bp.route('/api/kitchen_complete_all', methods=['POST'])
@role_required('owner', 'manager', 'kitchen')
def api_kitchen_complete_all():
    """KDS 廚房端一鍵全部出餐"""
    result, status_code = complete_all_pending_orders(tenant_id=g.tenant_id)
    return jsonify(result), status_code
