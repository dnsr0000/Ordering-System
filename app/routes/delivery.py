"""外送平台：Webhook (平台呼叫) 與後台頁面"""
import hmac
import json

from flask import Blueprint, abort, current_app, jsonify, render_template, request, url_for

from app.errors import AppError
from app.extensions import csrf, db, limiter
from app.models.enums import DELIVERY_SOURCES
from app.models.menu import MenuItem
from app.models.order import Order
from app.services import delivery_service, settings_service
from app.utils import role_required

delivery_bp = Blueprint('delivery', __name__)


@delivery_bp.route('/webhooks/ubereats', methods=['POST'], endpoint='ubereats_webhook')
@csrf.exempt
@limiter.limit('300/minute')
def ubereats_webhook():
    raw = request.get_data()
    try:
        event = json.loads(raw or b'{}')
    except ValueError:
        abort(400)
    store_id = (event.get('meta') or {}).get('user_id') or ''
    tenant_id = delivery_service.tenant_for('ubereats', 'ubereats_store_id', store_id)
    if tenant_id is None:
        abort(404)
    cfg = settings_service.get('delivery', tenant_id)
    if not delivery_service.verify_uber_signature(raw, request.headers.get('X-Uber-Signature', ''),
                                                  cfg.get('ubereats_client_secret') or ''):
        abort(401)
    try:
        delivery_service.handle_uber_event(tenant_id, event)
    except AppError as exc:
        db.session.rollback()
        current_app.logger.error('Uber Eats 訂單處理失敗：%s', exc.message)
    return '', 200


@delivery_bp.route('/webhooks/foodpanda/<vendor_code>', methods=['POST'], endpoint='foodpanda_webhook')
@csrf.exempt
@limiter.limit('300/minute')
def foodpanda_webhook(vendor_code):
    tenant_id = delivery_service.tenant_for('foodpanda', 'foodpanda_vendor_code', vendor_code)
    if tenant_id is None:
        abort(404)
    cfg = settings_service.get('delivery', tenant_id)
    token = cfg.get('foodpanda_token') or ''
    given = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    if not token or not hmac.compare_digest(given, token):
        abort(401)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    try:
        order = delivery_service.handle_foodpanda_order(tenant_id, payload)
    except AppError as exc:
        db.session.rollback()
        return jsonify({'reason': exc.message}), exc.status if exc.status >= 400 else 422
    return jsonify({'remoteResponse': {'remoteOrderId': str(order.id)}})


@delivery_bp.route('/admin/delivery', endpoint='manage')
@role_required('owner', 'manager')
def manage():
    from flask import g
    cfg = settings_service.get('delivery')
    base = request.url_root.rstrip('/')
    orders = Order.query.filter(Order.order_source.in_(DELIVERY_SOURCES)).order_by(Order.id.desc()).limit(100).all()
    unmapped = MenuItem.query.filter((MenuItem.external_code == '') | (MenuItem.external_code.is_(None))).count()
    return render_template('admin_delivery.html', cfg=cfg, orders=orders, unmapped=unmapped,
                           uber_url=base + url_for('delivery.ubereats_webhook'),
                           foodpanda_url=base + url_for('delivery.foodpanda_webhook',
                                                        vendor_code=cfg.get('foodpanda_vendor_code') or 'VENDOR'),
                           tenant=g.tenant)
