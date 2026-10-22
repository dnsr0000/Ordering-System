from flask import Blueprint, g, jsonify, render_template
from sqlalchemy.orm import selectinload

from app.models.enums import OrderStatus
from app.models.order import Order
from app.services.order_service import complete_all_pending_orders, todays_range
from app.utils import role_required

kitchen_bp = Blueprint('kitchen', __name__)
KITCHEN_ROLES = ('owner', 'manager', 'kitchen')


@kitchen_bp.route('/kitchen', endpoint='kitchen_display')
@role_required(*KITCHEN_ROLES)
def kitchen_display():
    return render_template('kitchen.html')


@kitchen_bp.route('/api/kitchen_orders')
@role_required(*KITCHEN_ROLES)
def api_kitchen_orders():
    start, end = todays_range()
    orders = (Order.query.options(selectinload(Order.items))
              .filter(Order.status == OrderStatus.PENDING, Order.created_at >= start, Order.created_at < end)
              .order_by(Order.created_at.asc()).all())
    result = []
    for o in orders:
        lines = {}
        for i in o.items:  # 同品項同客製化合併成一行
            note = '' if i.is_reward and i.customization == '紅利免費兌換' else (i.customization or '')
            line = lines.setdefault((i.item_name, note), {'name': i.item_name, 'quantity': 0, 'customization': note})
            line['quantity'] += i.quantity or 1
        result.append({
            'id': o.id,
            'pickup_number': o.display_pickup_number,
            'table_number': o.table_number,
            'order_type': o.order_type,
            'need_cutlery': bool(o.need_cutlery),
            'note': o.note or '',
            'created_at': o.created_at.strftime('%H:%M:%S'),
            'timestamp': o.created_at.timestamp(),
            'items': list(lines.values()),
        })
    return jsonify({'success': True, 'orders': result})


@kitchen_bp.route('/api/kitchen_complete_all', methods=['POST'])
@role_required(*KITCHEN_ROLES)
def api_kitchen_complete_all():
    return jsonify({'success': True, 'count': complete_all_pending_orders(g.tenant_id)})
