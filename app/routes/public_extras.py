"""
顧客端的公開頁面：
- /track/<訂單 uuid>：手機查看出餐進度 (收據上的 QR code)，出餐時震動 / 提示音 / 瀏覽器通知；出餐後可評分
- PWA：/manifest.webmanifest、/sw.js (Service Worker，斷線時顯示離線頁)、/offline
"""
import os

from flask import Blueprint, Response, abort, current_app, g, jsonify, render_template, url_for

from app.errors import AppError
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


def _use_order_shop(shop):
    """這個請求改用訂單所屬店家 (不寫入 session)：收據 QR 開啟的手機 session 多半是別家 / 預設店家，
    可用語言 (menu.languages)、錯誤訊息翻譯等要依訂單的店家"""
    if shop is not None:
        from app.tenant_context import _use_tenant
        _use_tenant(shop, remember=False)


def _driver_position(order):
    """外送員最新位置 (配送中、店家啟用 GPS 回報時)；顧客頁顯示更新時間、地圖連結與和自己的距離"""
    from app.services import delivery_tracking_service as tracking
    if order.delivery_status != 'out' or not tracking.enabled_for_order(order):
        return None
    return tracking.latest(order)


def _gps_enabled(order):
    """外送單且這張訂單可回報外送員位置 (與 _driver_position 同一個判斷)：關閉 GPS 回報的店家，
    追蹤頁不顯示「等待外送員回報位置…」區塊"""
    from app.services import delivery_tracking_service as tracking
    return bool(order.delivery_status) and tracking.enabled_for_order(order)


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
        'gps_enabled': _gps_enabled(order),
        'feedback': feedback_service.state(order),
        'push_enabled': push_service.enabled(order.tenant_id),
    }


@public_bp.route('/track/<order_uuid>', endpoint='track')
@limiter.limit('60/minute')
def track(order_uuid):
    order = _order_by_uuid(order_uuid)
    shop = db.session.get(Tenant, order.tenant_id)
    # 收據 QR / 推播連結常以沒有該店 session 的手機開啟：品項等關聯資料要跨店讀取 (與 /api/track 相同)，
    # 否則依目前店家過濾後品項清單是空的
    with tenant_bypass():
        data = dict(_track_payload(order), uuid=order.uuid)
    _use_order_shop(shop)
    return render_template('track.html', order=order, shop=shop, data=data, chat=chat_widget_config(order))


@public_bp.route('/api/track/<order_uuid>', endpoint='track_api')
@limiter.limit('60/minute')
def track_api(order_uuid):
    with tenant_bypass():
        order = _order_by_uuid(order_uuid)
        _use_order_shop(db.session.get(Tenant, order.tenant_id))
        return jsonify(dict(_track_payload(order), success=True))


@public_bp.route('/api/track/<order_uuid>/feedback', methods=['POST'], endpoint='track_feedback')
@limiter.limit('10/minute')
def track_feedback(order_uuid):
    """餐後評分 (每張訂單一次)；回傳 Google 評論連結 (高分且店家有設定時)"""
    data = get_json_dict()
    with tenant_bypass():
        order = _order_by_uuid(order_uuid)
        fb, review_url = feedback_service.submit(order, data.get('rating'), data.get('comment'))
        from app.services import i18n_service
        _use_order_shop(db.session.get(Tenant, order.tenant_id))   # 翻譯依訂單店家的可用語言
        message ='謝謝您的評分！' + (' 歡迎也到 Google 分享您的用餐體驗' if review_url else '')
        return jsonify({'success': True, 'rating': fb.rating, 'review_url': review_url,
                        'message': message, 'message_i18n': i18n_service.translate_message(message)})


@public_bp.route('/api/push/vapid', endpoint='push_vapid')
@limiter.limit('60/minute')
def push_vapid():
    """
    Web Push 的 VAPID 公鑰 (店家啟用推播時；第一次呼叫自動產生金鑰)。
    ?order=<訂單 uuid>：出餐進度頁用訂單所屬店家的金鑰 (掃收據 QR 的手機沒有該店 session；
    送出推播時以訂單店家的私鑰簽章，公鑰不同推播伺服器會拒收)。
    """
    from flask import request
    order_uuid = request.args.get('order')
    with tenant_bypass():
        tenant_id = _order_by_uuid(order_uuid).tenant_id if order_uuid else None
        if not push_service.enabled(tenant_id):
            return jsonify({'success': True, 'enabled': False, 'public_key': ''})
        key = push_service.public_key(tenant_id)
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
            raise AppError('本店未啟用推播通知', 404)
        if order.status in ('Completed', 'PickedUp', 'Cancelled') and not order.delivery_status:
            raise AppError('此訂單已完成，不需要通知', 409)
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
    # 只有「以 uuid 找訂單」跨店查詢；回答 (菜單、過敏原) 一定要在店家過濾下進行，否則會混入整台主機所有店家的菜單
    with tenant_bypass():
        if len(order_uuid) >= 30:
            order = Order.query.filter_by(uuid=order_uuid).first()
    tenant_id = order.tenant_id if order is not None else g.tenant_id
    if not chatbot_service.enabled(tenant_id):
        raise AppError('本店未啟用客服機器人', 404)
    shop = db.session.get(Tenant, tenant_id)
    history = data.get('history') if isinstance(data.get('history'), list) else []
    if order is not None and g.tenant_id != tenant_id:   # 追蹤頁可能是別家店的訂單：以訂單的店家查菜單
        if shop is None:
            raise AppError('本店未啟用客服機器人', 404)
        from app.tenant_context import _use_tenant
        _use_tenant(shop, remember=False)
    result = chatbot_service.answer(data.get('message'), order=order, history=history[-6:], tenant_id=tenant_id,
                                    shop_name=shop.name if shop else '')
    return jsonify(dict(result, success=True))


# ---------------------------------------------------------------- 轉接真人客服 (顧客端；以對話憑證存取，店員在後台「客服對話」回覆)
def _handoff_allowed(tenant_id):
    from app.services import chatbot_service
    return chatbot_service.enabled(tenant_id) and bool(chatbot_service.config(tenant_id).get('handoff_enabled', True))


@public_bp.route('/api/chat/handoff', methods=['POST'], endpoint='chat_handoff')
@limiter.limit('5/minute')
def chat_handoff():
    """{history: [{role, text}], order_uuid?, message?} → 建立對話，回 {token, status, messages}"""
    from app.services import customer_session, support_service
    data = get_json_dict()
    order = None
    order_uuid = str(data.get('order_uuid') or '')[:36]
    with tenant_bypass():
        if len(order_uuid) >= 30:
            order = Order.query.filter_by(uuid=order_uuid).first()
        tenant_id = order.tenant_id if order is not None else g.tenant_id
        if not _handoff_allowed(tenant_id):
            raise AppError('本店未開放真人客服', 404)
        member = customer_session.current_member() if tenant_id == g.tenant_id else None
        chat = support_service.start(tenant_id, data.get('history'), user=member, order=order,
                                     page='track' if order is not None else 'menu', first_message=data.get('message'))
        db.session.commit()
        from flask import session
        session['support_chats'] = (session.get('support_chats') or [])[-4:] + [chat.token]
        return jsonify({'success': True, 'token': chat.token, 'status': chat.status,
                        'messages': support_service.messages_after(chat, 0, for_customer=True)})


def _customer_chat(token):
    """只有建立這個對話的瀏覽器 (同一個顧客工作階段) 能存取；登出後失效"""
    from flask import session

    from app.services import support_service
    if str(token or '') not in (session.get('support_chats') or []):
        raise AppError('找不到這個客服對話', 404)
    chat = support_service.by_token(token)
    if not _handoff_allowed(chat.tenant_id):
        raise AppError('本店未開放真人客服', 404)
    return chat


@public_bp.route('/api/chat/handoff/<token>', endpoint='chat_handoff_poll')
@limiter.limit('60/minute')
def chat_handoff_poll(token):
    """?after=<最後一則訊息 id> → {status, messages} (只回較新的訊息)"""
    from flask import request

    from app.services import support_service
    with tenant_bypass():
        chat = _customer_chat(token)
        return jsonify({'success': True, 'status': chat.status,
                        'messages': support_service.messages_after(chat, request.args.get('after'), for_customer=True)})


@public_bp.route('/api/chat/handoff/<token>/message', methods=['POST'], endpoint='chat_handoff_message')
@limiter.limit('20/minute')
def chat_handoff_message(token):
    from app.services import support_service
    with tenant_bypass():
        chat = _customer_chat(token)
        msg = support_service.customer_message(chat, get_json_dict().get('text'))
        db.session.commit()
        return jsonify({'success': True, 'status': chat.status, 'message': support_service.message_dict(msg, for_customer=True)})


@public_bp.route('/api/chat/handoff/<token>/close', methods=['POST'], endpoint='chat_handoff_close')
@limiter.limit('20/minute')
def chat_handoff_close(token):
    from app.services import support_service
    with tenant_bypass():
        chat = _customer_chat(token)
        support_service.close(chat, support_service.CLOSED_BY_CUSTOMER)
        db.session.commit()
        return jsonify({'success': True, 'status': chat.status})


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
        'handoff': '1' if cfg.get('handoff_enabled', True) else '',
    }


@public_bp.route('/manifest.webmanifest', endpoint='manifest')
def manifest():
    """
    PWA manifest。瀏覽器抓 manifest 預設不帶 Cookie：頁面的 manifest 連結要帶 ?shop=<代號> 才會是該店 (tenant_context 解析)。
    非預設店家的 start_url 帶 /shop/<代號> (主畫面 App 的 Cookie 與瀏覽器分開時仍開到同一家店)，
    id 依店家區分 (同網域的不同店家可各自安裝)；預設店家維持 '/'，已安裝的 App 不會被當成另一個 App。
    """
    tenant = getattr(g, 'tenant', None)
    name = tenant.name if tenant else '自助點餐'
    start_url = url_for('customer.customer_index')
    if tenant is not None and tenant.slug and tenant.slug != 'default':
        start_url = url_for('customer.tenant_storefront', tenant_slug=tenant.slug)
    resp = jsonify({
        'name': f'{name} 線上點餐', 'short_name': name[:12], 'start_url': start_url, 'id': start_url,
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
