import queue
from datetime import datetime
from flask import Blueprint, render_template, Response, jsonify, request, session, g
from app.models.order import Order
from app.services.event_bus import order_event_bus
from app.utils import role_required, public_display_name

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
    # 叫號看板只顯示取餐號與遮罩後的名稱，不需要訂單明細，這裡刻意不預載 items

    ready_orders = []
    preparing_orders = []
    for o in orders:
        item = {
            'id': o.id,
            'pickup_number': o.pickup_number if getattr(o, 'pickup_number', None) else o.id,
            'order_type': o.order_type,
            # 這支 API 不需登入 (給大螢幕叫號用)，會員姓名一律遮罩，避免被路過的人從手機抓到其他顧客的全名
            'user_name': public_display_name(o),
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
                    if payload is None:  # 被連線配額淘汰：結束串流以釋放執行緒，瀏覽器會自動重連
                        return
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