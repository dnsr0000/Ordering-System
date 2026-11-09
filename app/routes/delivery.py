"""外送平台：Webhook (平台呼叫) 與後台頁面"""
import hmac
import ipaddress
import json
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, jsonify, render_template, request, url_for

from app.errors import AppError
from app.extensions import csrf, db, limiter
from app.models.enums import DELIVERY_SOURCES, MANAGER_ROLES
from app.models.menu import MenuItem
from app.models.order import Order
from app.services import delivery_service, settings_service
from app.sync import runtime as sync_runtime
from app.utils import role_required, store_only

delivery_bp = Blueprint('delivery', __name__)


# 平台 Webhook 只在門市主機 (單機 / 分店) 接收：線上端 (hub) 的訂單一律要求會員登入與取餐分店，平台單會全部被拒
@delivery_bp.route('/webhooks/ubereats', methods=['POST'], endpoint='ubereats_webhook')
@csrf.exempt
@limiter.limit('300/minute')
@store_only
def ubereats_webhook():
    raw = request.get_data()
    try:
        event = json.loads(raw or b'{}')
    except ValueError:
        abort(400)
    if not isinstance(event, dict) or not isinstance(event.get('meta') or {}, dict):
        abort(400)
    store_id = (event.get('meta') or {}).get('user_id') or ''
    tenant_id = delivery_service.tenant_for('ubereats', 'ubereats_store_id', store_id)
    if tenant_id is None:
        abort(404)
    cfg = settings_service.get('delivery', tenant_id)
    signature = request.headers.get('X-Uber-Signature', '')
    if not signature.isascii():   # hmac.compare_digest 遇到非 ASCII 的 str 會拋 TypeError
        abort(401)
    if not delivery_service.verify_uber_signature(raw, signature, cfg.get('ubereats_client_secret') or ''):
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
@store_only
def foodpanda_webhook(vendor_code):
    tenant_id = delivery_service.tenant_for('foodpanda', 'foodpanda_vendor_code', vendor_code)
    if tenant_id is None:
        abort(404)
    cfg = settings_service.get('delivery', tenant_id)
    token = cfg.get('foodpanda_token') or ''
    given = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    if not token or not hmac.compare_digest(given.encode('utf-8'), token.encode('utf-8')):
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
@role_required(*MANAGER_ROLES)
def manage():
    from flask import g
    from app.services import table_service
    cfg = settings_service.get('delivery')
    orders = Order.query.filter(Order.order_source.in_(DELIVERY_SOURCES)).order_by(Order.id.desc()).limit(100).all()
    unmapped = MenuItem.query.filter((MenuItem.external_code == '') | (MenuItem.external_code.is_(None))).count()
    uber_url = foodpanda_url = ''
    # 多店家主機上兩家以上啟用同一平台卻都沒填店家代碼：Webhook 無法判斷是哪一家的訂單而全部拒收 → 提示必須填寫
    ambiguous = [label for platform, key_field, label in (
                     ('ubereats', 'ubereats_store_id', 'Uber Eats Store ID'),
                     ('foodpanda', 'foodpanda_vendor_code', 'foodpanda 店家代碼'))
                 if cfg.get(f'{platform}_enabled') and not cfg.get(key_field)
                 and len(delivery_service.unconfigured_tenants(platform, key_field)) > 1]
    if sync_runtime.is_hub():
        # 平台訂單要在出餐門市建立 (線上端的訂單一律要求會員登入與取餐分店，平台單沒有會員會被拒)
        flash('線上端不接收外送平台訂單：請在出餐門市主機的「外送平台」頁複製 Webhook 網址設定到平台 '
              '(門市需以 PUBLIC_BASE_URL 提供 https 公開網址，例如 Cloudflare Tunnel)。', 'warning')
    else:
        base = table_service.public_base(shop=False)   # PUBLIC_BASE_URL 優先 (店長常在店內用區網 IP 開後台)
        if not _is_public_https(base):
            flash('外送平台需要 https 公開網址才能把訂單送進來：請在 .env 設定 PUBLIC_BASE_URL '
                  '(例如 Cloudflare Tunnel 的網址) 並重新啟動，下方的 Webhook 網址才會正確。', 'warning')
        uber_url = base + url_for('delivery.ubereats_webhook')
        vendor_code = cfg.get('foodpanda_vendor_code') or ''
        if vendor_code or 'foodpanda 店家代碼' not in ambiguous:   # 有歧義時不給 VENDOR 佔位網址 (複製去用也會被拒收)
            foodpanda_url = base + url_for('delivery.foodpanda_webhook', vendor_code=vendor_code or 'VENDOR')
    return render_template('admin_delivery.html', cfg=cfg, orders=orders, unmapped=unmapped,
                           uber_url=uber_url, foodpanda_url=foodpanda_url, ambiguous=ambiguous, tenant=g.tenant)


def _is_public_https(base):
    """https 且不是 localhost / 私有 IP (平台的伺服器連得到)"""
    parts = urlsplit(base or '')
    host = parts.hostname or ''
    if parts.scheme != 'https' or not host or host == 'localhost' or host.endswith('.localhost'):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)
