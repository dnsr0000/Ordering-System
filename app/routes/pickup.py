import queue
from datetime import datetime
from flask import Blueprint, render_template, Response, jsonify, request, session, g
from app.extensions import db
from app.models.order import Order
from app.services.event_bus import order_event_bus
from app.routes.common import record_staff_audit
from app.utils import role_required

pickup_bp = Blueprint('pickup', __name__)


@pickup_bp.route('/pickup', endpoint='pickup_management')
@role_required('owner', 'manager', 'staff')
def pickup_management():
    return render_template('pickup.html')


@pickup_bp.route('/counter', endpoint='counter_display')
def counter_display():
    return render_template('counter.html')


@pickup_bp.route('/api/counter_orders')
def api_counter_orders():
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    orders = Order.query.filter(
        Order.created_at >= today_start,
        Order.created_at <= today_end,
        Order.status.in_(['Pending', 'Completed'])
    ).order_by(Order.id.desc()).all()

    ready_orders = []
    preparing_orders = []
    for o in orders:
        item = {
            'id': o.id,
            'pickup_number': o.pickup_number if getattr(o, 'pickup_number', None) else o.id,
            'order_type': o.order_type,
            'user_name': o.user.name if o.user else (o.table_number or '訪客'),
            'created_at': o.created_at.strftime('%H:%M') if o.created_at else ''
        }
        if o.status == 'Completed':
            ready_orders.append(item)
        else:
            preparing_orders.append(item)

    return jsonify({
        'success': True,
        'ready_orders': ready_orders,
        'preparing_orders': preparing_orders
    })


@pickup_bp.route('/api/mark_picked_up/<int:id>', methods=['POST'])
@role_required('owner', 'manager', 'staff')
def api_mark_picked_up(id):
    order = Order.query.get_or_404(id)
    if order.status != 'Completed':
        return jsonify({
            'success': False,
            'message': f'訂單目前狀態為 {order.status}，只有已出餐 (Completed) 的訂單可核銷取餐！'
        }), 400

    order.status = 'PickedUp'
    record_staff_audit(
        action_type='MARK_PICKED_UP',
        target_order_id=order.id,
        target_user_id=order.user_id,
        details=f"核銷取餐：取餐編號 #{order.pickup_number} (單號 #{order.id})"
    )
    db.session.commit()
    order_event_bus.notify()
    return jsonify({'success': True, 'message': f'取餐編號 #{order.pickup_number} 已完成取餐！'})


@pickup_bp.route('/api/orders_stream')
def orders_stream():
    tenant_id = getattr(g, 'tenant_id', None) or session.get('tenant_id') or 1
    client_ip = request.remote_addr or '127.0.0.1'
    is_staff = bool(session.get('admin_logged_in') and session.get('staff_account_id'))

    try:
        initial_payload = order_event_bus.get_current_payload(tenant_id)
    except Exception:
        initial_payload = "{}"

    def event_stream():
        client_queue = order_event_bus.subscribe(tenant_id, client_ip=client_ip, is_staff=is_staff)
        try:
            yield f"data: {initial_payload}\n\n"
            while True:
                try:
                    payload = client_queue.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        except (GeneratorExit, BrokenPipeError, ConnectionResetError, IOError):
            pass
        finally:
            order_event_bus.unsubscribe(client_queue)

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )
