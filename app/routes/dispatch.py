"""外送派車 (/admin/dispatch)：今天的外送到府訂單，指派外送員 → 出發 → 送達 / 失敗 (收銀員以上，在出餐分店操作)"""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.errors import AppError
from app.extensions import db
from app.models.enums import DELIVERY_STATUS_LABELS
from app.models.order import Order
from app.services import own_delivery_service
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.order_service import todays_range
from app.sync import runtime as sync_runtime
from app.utils import role_required, safe_int

dispatch_bp = Blueprint('dispatch', __name__)
POS_ROLES = ('owner', 'manager', 'staff')


@dispatch_bp.route('/admin/dispatch', methods=['GET', 'POST'], endpoint='board')
@role_required(*POS_ROLES)
def board():
    if request.method == 'POST':
        if sync_runtime.is_hub():
            raise AppError('外送派車請在出餐分店操作 (線上端只顯示狀態)')
        order = db.session.get(Order, safe_int(request.form.get('order_id'), 0))
        if order is None or order.tenant_id != g.tenant_id:
            raise AppError('查無此訂單', 404)
        action = request.form.get('action')
        if action == 'assign':
            own_delivery_service.assign(order, request.form.get('driver'))
        elif action == 'depart':
            own_delivery_service.depart(order)
        elif action == 'complete':
            own_delivery_service.complete(order, current_user)
        elif action == 'fail':
            own_delivery_service.fail(order, (request.form.get('reason') or '').strip()[:80])
        else:
            raise AppError('不支援的操作')
        record_staff_audit('DELIVERY_' + action.upper(), target_order_id=order.id,
                           details=f'外送訂單 #{order.id}：{DELIVERY_STATUS_LABELS.get(order.delivery_status, "")}'
                                   + (f' ({order.driver_name})' if order.driver_name else ''))
        db.session.commit()
        order_event_bus.notify(g.tenant_id)
        flash(f'✅ 訂單 #{order.id}：{DELIVERY_STATUS_LABELS.get(order.delivery_status, "")}', 'success')
        return redirect(url_for('dispatch.board'))
    start, end = todays_range()
    return render_template('admin_dispatch.html', orders=own_delivery_service.board_orders(start, end),
                           drivers=own_delivery_service.drivers(), labels=DELIVERY_STATUS_LABELS,
                           read_only=sync_runtime.is_hub(), enabled=own_delivery_service.config().get('enabled'))
