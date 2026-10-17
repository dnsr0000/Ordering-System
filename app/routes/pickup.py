import queue
from datetime import datetime
from flask import Blueprint, render_template, Response, jsonify, request, session, g
from app.extensions import db
from app.models.order import Order
from app.services.event_bus import order_event_bus
from app.routes.common import record_staff_audit
from app.utils import role_required
from app.services.order_service import change_order_status

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

@pickup_bp.route('/api/mark_picked_up/<int:order_id>', methods=['POST'])
@role_required('owner', 'manager', 'staff')
def api_mark_picked_up(order_id):
    """櫃檯人員核銷取餐完成（狀態流轉為 PickedUp）"""
    result, status_code = change_order_status(
        order_id=order_id,
        target_status='PickedUp',
        staff_account=g.staff_account,
        tenant_id=g.tenant_id
    )
    return jsonify(result), status_code


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
