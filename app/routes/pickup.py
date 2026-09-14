from flask import Blueprint, render_template, Response, jsonify
from app.extensions import db
from app.models.order import Order
from app.services.event_bus import order_event_bus

pickup_bp = Blueprint('pickup', __name__)

@pickup_bp.route('/pickup', endpoint='pickup_management')
def pickup_management():
    return render_template('pickup.html')

@pickup_bp.route('/counter', endpoint='counter_display')
def counter_display():
    return render_template('counter.html')

@pickup_bp.route('/api/mark_picked_up/<int:id>', methods=['POST'])
def api_mark_picked_up(id):
    order = Order.query.get_or_404(id)
    order.status = 'PickedUp'
    db.session.commit()
    order_event_bus.notify()
    return jsonify({'success': True, 'message': f'取餐編號 #{order.pickup_number} 已完成取餐！'})

@pickup_bp.route('/api/orders_stream')
def orders_stream():
    def event_stream():
        client_queue = order_event_bus.subscribe()
        try:
            yield f"data: {order_event_bus.get_current_payload()}\n\n"
            while True:
                try:
                    payload = client_queue.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except Exception:
                    yield ": keep-alive\n\n"
        except (GeneratorExit, BrokenPipeError, ConnectionResetError, IOError):
            pass
        finally:
            order_event_bus.unsubscribe(client_queue)

    return Response(
        event_stream(), 
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'}
    )