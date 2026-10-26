from datetime import timedelta

from flask import Blueprint, g, jsonify, render_template, request
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.printing import KitchenStation
from app.services import kitchen_service, settings_service
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.order_service import complete_all_pending_orders, todays_orders_condition, todays_range
from app.utils import get_json_dict, role_required, safe_int

kitchen_bp = Blueprint('kitchen', __name__)
KITCHEN_ROLES = ('owner', 'manager', 'kitchen')
# 尚未完成付款的訂單 (線上待付款、LINE Pay 扣款中) 不送廚房
HIDDEN_PAYMENT_STATUSES = ('pending', 'authorizing', 'expired')
PREP_LEAD_MINUTES = 15   # 預約取餐的訂單在廚房看板以「取餐前 15 分鐘」排序


def _station_arg():
    sid = safe_int(request.args.get('station'), 0)
    if not sid:
        return None
    station = db.session.get(KitchenStation, sid)
    if station is None or station.tenant_id != g.tenant_id:
        raise AppError('查無此工作站', 404)
    return station


@kitchen_bp.route('/kitchen', endpoint='kitchen_display')
@role_required(*KITCHEN_ROLES)
def kitchen_display():
    station = _station_arg()
    cfg = settings_service.get('kds')
    return render_template('kitchen.html', stations=kitchen_service.active_stations(), station=station,
                           kds={'warnMinutes': cfg.get('warn_minutes') or 8, 'lateMinutes': cfg.get('late_minutes') or 15,
                                'sound': bool(cfg.get('sound', True)), 'stationId': station.id if station else None})


@kitchen_bp.route('/api/kitchen_orders')
@role_required(*KITCHEN_ROLES)
def api_kitchen_orders():
    station = _station_arg()
    start, end = todays_range()
    orders = (Order.query.options(selectinload(Order.items))
              .filter(Order.status == OrderStatus.PENDING, todays_orders_condition(start, end, include_future=False),
                      Order.payment_status.notin_(HIDDEN_PAYMENT_STATUSES))
              .order_by(Order.created_at.asc()).all())
    resolve = kitchen_service.station_resolver()
    menu = {m.id: m for m in MenuItem.query.all()}
    stations = {s.id: s.name for s in kitchen_service.active_stations()}
    result = []
    for o in orders:
        lines = {}
        mine_pending = False
        for i in o.items:  # 同品項同客製化合併成一行
            sid = resolve(menu.get(i.menu_item_id))
            if station is not None and sid != station.id:
                continue
            if station is not None and i.prepared_at is None:
                mine_pending = True
            note = '' if i.is_reward and i.customization == '紅利免費兌換' else (i.customization or '')
            key = (i.item_name, note, sid, i.prepared_at is not None)
            line = lines.setdefault(key, {'name': i.item_name, 'quantity': 0, 'customization': note,
                                          'station': stations.get(sid, ''), 'done': i.prepared_at is not None,
                                          'voided': 0})
            line['quantity'] += i.quantity if i.quantity is not None else 1   # 單品作廢後剩下的份數 (可能為 0)
            line['voided'] += i.voided_qty or 0
        if station is not None and not mine_pending:
            continue   # 這一站沒有待做的品項
        result.append({
            'id': o.id,
            'pickup_number': o.display_pickup_number,
            'table_number': o.table_number,
            'order_type': o.order_type,
            'order_source': o.order_source,
            'need_cutlery': bool(o.need_cutlery),
            'note': o.note or '',
            'is_rush': bool(o.is_rush),
            'pickup_at': o.pickup_at_label,
            'created_at': o.created_at.strftime('%H:%M:%S'),
            'timestamp': o.created_at.timestamp(),
            # 預約取餐的訂單依「取餐前 PREP_LEAD_MINUTES 分鐘」排序，不佔用現在要做的位置
            'sort_at': (o.pickup_at - timedelta(minutes=PREP_LEAD_MINUTES)).timestamp() if o.pickup_at
            else o.created_at.timestamp(),
            'items': list(lines.values()),
        })
    result.sort(key=lambda r: (not r['is_rush'], max(r['timestamp'], r['sort_at'])))
    return jsonify({'success': True, 'orders': result})


@kitchen_bp.route('/api/kitchen_complete_all', methods=['POST'])
@role_required(*KITCHEN_ROLES)
def api_kitchen_complete_all():
    return jsonify({'success': True, 'count': complete_all_pending_orders(g.tenant_id)})


@kitchen_bp.route('/api/kitchen/orders/<int:order_id>/station_done', methods=['POST'])
@role_required(*KITCHEN_ROLES)
def api_station_done(order_id):
    """工作站完成自己的品項；所有工作站都完成時訂單自動轉為已出餐"""
    data = get_json_dict()
    done, completed = kitchen_service.complete_station(order_id, safe_int(data.get('station_id'), 0), g.tenant_id,
                                                       current_user)
    return jsonify({'success': True, 'items_done': done, 'order_completed': completed})


@kitchen_bp.route('/api/kitchen/orders/<int:order_id>/rush', methods=['POST'])
@role_required('owner', 'manager', 'staff', 'kitchen')
def api_kitchen_rush(order_id):
    data = get_json_dict()
    order = kitchen_service.set_rush(order_id, g.tenant_id, data.get('is_rush', True))
    record_staff_audit('ORDER_RUSH', target_order_id=order.id,
                       details=f"訂單 #{order.id} {'設為加急' if order.is_rush else '取消加急'}")
    db.session.commit()
    order_event_bus.notify(g.tenant_id)
    return jsonify({'success': True, 'is_rush': order.is_rush})
