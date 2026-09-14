import queue
from app.extensions import db
from app.models.order import Order
from app.services.event_bus import order_event_bus
from flask import Blueprint, render_template, Response, jsonify, current_app

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
    """向前端推播訂單狀態變更 (加入 Broken Pipe 與網路中斷防護)"""

    app = current_app._get_current_object()
    try:
        initial_payload = order_event_bus.get_current_payload()
    except Exception as e:
        print(f"[!] 取得初始 Payload 異常: {e}")
        initial_payload = "{}"

    def event_stream():
        client_queue = order_event_bus.subscribe()
        try:
            yield f"data: {initial_payload}\n\n"

            while True:
                try:
                    payload = client_queue.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    # 15 秒發送心跳，若連線已中斷會在 yield 時拋出例外
                    yield ": keep-alive\n\n"
        except (GeneratorExit, BrokenPipeError, ConnectionResetError, IOError):
            pass  # 客戶端斷線或關閉視窗，靜默處理
        finally:
            order_event_bus.unsubscribe(client_queue)

    return Response(
        event_stream(), 
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )