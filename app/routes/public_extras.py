"""
顧客端的公開頁面：
- /track/<訂單 uuid>：手機查看出餐進度 (收據上的 QR code)，出餐時震動 / 提示音 / 瀏覽器通知；出餐後可評分
- PWA：/manifest.webmanifest、/sw.js (Service Worker，斷線時顯示離線頁)、/offline
"""
import os

from flask import Blueprint, Response, abort, current_app, g, jsonify, render_template, url_for

from app.extensions import db, limiter
from app.services import feedback_service, push_service
from app.utils import get_json_dict
from app.models.order import Order
from app.models.tenant import Tenant
from app.tenant_context import tenant_bypass
from app.utils import public_display_name

public_bp = Blueprint('public', __name__)


def _order_by_uuid(order_uuid):
    order_uuid = str(order_uuid or '')[:36]
    if len(order_uuid) < 30:   # uuid4 無法猜測；太短的值直接拒絕
        abort(404)
    with tenant_bypass():
        order = Order.query.filter_by(uuid=order_uuid).first()
    if order is None:
        abort(404)
    return order


def _driver_position(order):
    """外送員最新位置 (配送中、店家啟用 GPS 回報時)；顧客頁顯示更新時間、地圖連結與和自己的距離"""
    from app.services import delivery_tracking_service as tracking
    if order.delivery_status != 'out' or not tracking.enabled(order.tenant_id):
        return None
    return tracking.latest(order)


def _track_payload(order):
    return {
        'pickup_number': order.display_pickup_number,
        'pickup_pending': order.order_source == 'Online' and not (order.pickup_number or 0) > 0,
        'status': order.status, 'status_label': order.status_label,
        'name': public_display_name(order), 'order_type': order.order_type,
        'created_at': order.created_at.strftime('%H:%M') if order.created_at else '',
        'items': [{'name': i.display_name, 'quantity': i.quantity} for i in order.items],
        'pickup_at': order.pickup_at_label,
        'eta': order.eta_label,
        'delivery_status': order.delivery_status or '', 'delivery_label': order.delivery_status_label,
        'driver': order.driver_name if order.delivery_status in ('assigned', 'out') else '',
        'driver_position': _driver_position(order),
        'feedback': feedback_service.state(order),
        'push_enabled': push_service.enabled(order.tenant_id),
    }


@public_bp.route('/track/<order_uuid>', endpoint='track')
@limiter.limit('60/minute')
def track(order_uuid):
    order = _order_by_uuid(order_uuid)
    shop = db.session.get(Tenant, order.tenant_id)
    return render_template('track.html', order=order, shop=shop, data=dict(_track_payload(order), uuid=order.uuid),
                           chat=chat_widget_config(order))


@public_bp.route('/api/track/<order_uuid>', endpoint='track_api')
@limiter.limit('60/minute')
def track_api(order_uuid):
    with tenant_bypass():
        order = _order_by_uuid(order_uuid)
        return jsonify(dict(_track_payload(order), success=True))


@public_bp.route('/api/track/<order_uuid>/feedback', methods=['POST'], endpoint='track_feedback')
@limiter.limit('10/minute')
def track_feedback(order_uuid):
    """餐後評分 (每張訂單一次)；回傳 Google 評論連結 (高分且店家有設定時)"""
    data = get_json_dict()
    with tenant_bypass():
        order = _order_by_uuid(order_uuid)
        fb, review_url = feedback_service.submit(order, data.get('rating'), data.get('comment'))
        return jsonify({'success': True, 'rating': fb.rating, 'review_url': review_url,
                        'message': '謝謝您的評分！' + (' 歡迎也到 Google 分享您的用餐體驗' if review_url else '')})


@public_bp.route('/api/push/vapid', endpoint='push_vapid')
@limiter.limit('60/minute')
def push_vapid():
    """Web Push 的 VAPID 公鑰 (店家啟用推播時；第一次呼叫自動產生金鑰)"""
    if not push_service.enabled():
        return jsonify({'success': True, 'enabled': False, 'public_key': ''})
    key = push_service.public_key()
    db.session.commit()
    return jsonify({'success': True, 'enabled': True, 'public_key': key})


@public_bp.route('/api/track/<order_uuid>/push', methods=['POST'], endpoint='track_push')
@limiter.limit('20/minute')
def track_push(order_uuid):
    """出餐進度頁訂閱推播：餐點完成 / 外送出發時通知 (訂閱綁定這張訂單)"""
    data = get_json_dict()
    with tenant_bypass():
        order = _order_by_uuid(order_uuid)
        if not push_service.enabled(order.tenant_id):
            return jsonify({'success': False, 'message': '本店未啟用推播通知'}), 404
        if order.status in ('Completed', 'PickedUp', 'Cancelled') and not order.delivery_status:
            return jsonify({'success': False, 'message': '此訂單已完成，不需要通知'}), 409
        push_service.subscribe(order, data.get('subscription'), user_id=order.user_id)
        db.session.commit()
    return jsonify({'success': True})


@public_bp.route('/api/chat', methods=['POST'], endpoint='chat')
@limiter.limit('20/minute')
def chat():
    """顧客端 AI 客服：{message, history: [{role, text}], order_uuid?}；過敏原純檢索，其他才交給 AI"""
    from app.services import chatbot_service
    data = get_json_dict()
    order = None
    order_uuid = str(data.get('order_uuid') or '')[:36]
    with tenant_bypass():
        if len(order_uuid) >= 30:
            order = Order.query.filter_by(uuid=order_uuid).first()
        tenant_id = order.tenant_id if order is not None else g.tenant_id
        if not chatbot_service.enabled(tenant_id):
            return jsonify({'success': False, 'message': '本店未啟用客服機器人'}), 404
        shop = db.session.get(Tenant, tenant_id)
        history = data.get('history') if isinstance(data.get('history'), list) else []
        if order is not None and g.tenant_id != tenant_id:   # 追蹤頁可能是別家店的訂單：以訂單的店家查菜單
            from app.tenant_context import _use_tenant
            _use_tenant(shop, remember=False)
        result = chatbot_service.answer(data.get('message'), order=order, history=history[-6:], tenant_id=tenant_id,
                                        shop_name=shop.name if shop else '')
    return jsonify(dict(result, success=True))


def chat_widget_config(order=None):
    """樣板用：對話窗的設定 (未啟用時回 None)"""
    from app.services import chatbot_service, i18n_service
    tenant_id = order.tenant_id if order is not None else g.tenant_id
    if not chatbot_service.enabled(tenant_id):
        return None
    cfg = chatbot_service.config(tenant_id)
    lang = i18n_service.effective_language()
    ui = i18n_service.ui_strings(lang)
    return {
        'title': ui.get('chat_title', '線上客服'), 'botName': cfg.get('bot_name') or '小幫手',
        'subtitle': ui.get('chat_subtitle', ''), 'welcome': cfg.get('welcome') or '',
        'placeholder': ui.get('chat_placeholder', ''), 'send': ui.get('chat_send', ''), 'aiNote': ui.get('chat_ai_note', ''),
        'failed': ui.get('chat_failed', ''), 'orderUuid': order.uuid if order is not None else '',
        'suggestions': '|'.join(chatbot_service._suggestions(order)),
    }


@public_bp.route('/manifest.webmanifest', endpoint='manifest')
def manifest():
    name = g.tenant.name if getattr(g, 'tenant', None) else '自助點餐'
    resp = jsonify({
        'name': f'{name} 線上點餐', 'short_name': name[:12], 'start_url': url_for('customer.customer_index'),
        'scope': '/', 'display': 'standalone', 'background_color': '#121417', 'theme_color': '#ffc107', 'lang': 'zh-TW',
        'icons': [{'src': url_for('static', filename='img/icon-192.png'), 'sizes': '192x192', 'type': 'image/png'},
                  {'src': url_for('static', filename='img/icon-512.png'), 'sizes': '512x512', 'type': 'image/png',
                   'purpose': 'any maskable'}],
    })
    resp.mimetype = 'application/manifest+json'
    return resp


@public_bp.route('/sw.js', endpoint='service_worker')
def service_worker():
    """Service Worker：快取名稱帶 static/ 的版本摘要，部署新版後舊快取整批清掉 (static_version)"""
    from app.services import static_version
    with open(os.path.join(current_app.static_folder, 'js', 'sw.js'), encoding='utf-8') as fh:
        source = fh.read().replace("'kiosk-static-v2'", f"'kiosk-static-{static_version.bundle_digest()}'", 1)
    resp = Response(source, mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@public_bp.route('/offline', endpoint='offline')
def offline():
    return render_template('offline.html')
