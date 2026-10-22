import queue

from flask import Blueprint, Response, g, jsonify, render_template, request
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app.models.enums import OrderStatus
from app.models.order import Order
from app.services.event_bus import order_event_bus
from app.services.order_service import todays_range
from app.utils import public_display_name, role_required

pickup_bp = Blueprint('pickup', __name__)
SSE_KEEPALIVE_SECONDS = 15


@pickup_bp.route('/pickup', endpoint='pickup_management')
@role_required('owner', 'manager', 'staff')
def pickup_management():
    return render_template('pickup.html')


@pickup_bp.route('/api/pickup_orders')
@role_required('owner', 'manager', 'staff')
def api_pickup_orders():
    """櫃檯取餐核銷：今日已出餐、等待顧客取餐的訂單 (含明細)"""
    start, end = todays_range()
    orders = (Order.query.options(selectinload(Order.items))
              .filter(Order.status == OrderStatus.COMPLETED, Order.created_at >= start, Order.created_at < end)
              .order_by(Order.id.asc()).all())
    return jsonify({'success': True, 'orders': [{
        'id': o.id,
        'pickup_number': o.display_pickup_number,
        'user_name': o.user.name if o.user else (o.table_number or '非會員'),
        'order_type': o.order_type,
        'need_cutlery': bool(o.need_cutlery),
        'note': o.note or '',
        'created_at': o.created_at.strftime('%H:%M'),
        'items': [i.to_dict() for i in o.items],
    } for o in orders]})


@pickup_bp.route('/counter', endpoint='counter_display')
def counter_display():
    return render_template('counter.html')


@pickup_bp.route('/api/counter_orders')
def api_counter_orders():
    """公開叫號看板 (免登入)：只回傳取餐號與遮罩後的名稱"""
    start, end = todays_range()
    orders = (Order.query.filter(Order.created_at >= start, Order.created_at < end,
                                 Order.status.in_([OrderStatus.PENDING, OrderStatus.COMPLETED]))
              .order_by(Order.id.desc()).all())
    board = {OrderStatus.PENDING: [], OrderStatus.COMPLETED: []}
    for o in orders:
        board[o.status].append({
            'id': o.id,
            'pickup_number': o.display_pickup_number,
            'order_type': o.order_type,
            'user_name': public_display_name(o),
            'created_at': o.created_at.strftime('%H:%M'),
        })
    return jsonify({'success': True, 'ready_orders': board[OrderStatus.COMPLETED],
                    'preparing_orders': board[OrderStatus.PENDING]})


@pickup_bp.route('/api/orders_stream')
def orders_stream():
    """SSE：訂單有變動時送出 orders_changed 事件，看板收到後重新抓取資料"""
    tenant_id, ip, is_staff = g.tenant_id, request.remote_addr, current_user.is_authenticated

    def stream():
        client_queue = order_event_bus.subscribe(tenant_id, client_ip=ip, is_staff=is_staff)
        try:
            yield f"data: {order_event_bus.make_payload(tenant_id)}\n\n"
            while True:
                try:
                    payload = client_queue.get(timeout=SSE_KEEPALIVE_SECONDS)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if payload is None:  # 被連線配額淘汰：結束串流釋放執行緒，瀏覽器會自動重連
                    return
                yield f"data: {payload}\n\n"
        finally:
            order_event_bus.unsubscribe(client_queue)

    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
