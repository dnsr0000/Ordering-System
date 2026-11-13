
from app.services import menu_service

import re
from datetime import datetime
from types import SimpleNamespace

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.extensions import db, limiter
from app.models.enums import OrderSource, OrderStatus, OrderType, PaymentMethod, normalize_payment_method
from app.models.menu import MenuItem, ModifierOption
from app.models.order import Order, OrderItem
from app.models.sync import NodeStock, SyncNode
from app.routes.sync_admin import node_name_map
from app.errors import AppError
from app.services import (branch_service, coupon_service, eta_service, feedback_service, group_order_service, i18n_service,
                          intake_service, inventory_service, own_delivery_service, pass_service, payment_service,
                          pickup_service, privacy_service, receipt_service, reorder_service, settings_service,
                          table_pay_service, table_service)
from app.services.customer_session import close_customer_session, current_member, needs_pin_confirm, require_confirmed

from app.services.order_service import find_by_client_order_id, find_promo, process_checkout
from app.services.reward_service import get_or_create_reward_setting
from app.sync import runtime as sync_runtime
from app.utils import get_json_dict, hub_only, json_etag_response, safe_id, safe_int, store_only

customer_bp = Blueprint('customer', __name__)

PICKUP_NODE_KEY = 'pickup_node_uuid'


def _favorite_items(user_id, limit=3):
    """會員最常點的餐點 (不含兌換品與已取消訂單)"""
    qty = func.sum(OrderItem.quantity)
    rows = db.session.execute(
        select(OrderItem.menu_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.user_id == user_id, Order.status != OrderStatus.CANCELLED,
               OrderItem.is_reward.is_(False), OrderItem.menu_item_id.is_not(None))
        .group_by(OrderItem.menu_item_id).order_by(qty.desc()).limit(limit)
    ).scalars().all()
    by_id = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_(rows)).all()} if rows else {}
    return [by_id[i] for i in rows if i in by_id]


# ------------------------------------------------------------------------------
# 線上端 (hub)：顧客選擇取餐分店，菜單庫存以該分店回報的數量顯示
# ------------------------------------------------------------------------------
def _online_nodes():
    """開放線上點餐、且目前連線中的分店 (本店家、啟用中)"""
    offline_after = current_app.config['SYNC_NODE_OFFLINE_AFTER']
    return [n for n in SyncNode.query.filter_by(tenant_id=g.tenant_id, is_active=True, accepts_online_orders=True)
            .order_by(SyncNode.id).all() if n.is_online(offline_after)]


def _pickup_node(nodes, adjust=True):
    """
    目前選擇的取餐分店；顧客還沒選過且只有一家時自動選取。
    選過的分店目前不在清單 (暫時離線 / 停用 / 暫停線上點餐)：點餐頁每 30 秒的輪詢 (adjust=False) 不改 session、回傳 None
    (菜單顯示無法供應，結帳時提示該分店目前離線)，不可靜默改選別家；重新載入點餐頁 (adjust=True) 才清除，
    只剩一家時改選並提示顧客 (頁首同步顯示新的分店)。
    """
    chosen = session.get(PICKUP_NODE_KEY)
    node = next((n for n in nodes if n.uuid == chosen), None)
    if node is not None:
        return node
    if chosen:
        if not adjust:
            return None
        session.pop(PICKUP_NODE_KEY, None)
    if len(nodes) != 1:
        return None
    node = nodes[0]
    session[PICKUP_NODE_KEY] = node.uuid
    if chosen:
        flash(f'您原本選擇的取餐分店目前暫停線上點餐，取餐分店已改為【{node.name}】，請確認後再結帳', 'warning')
    return node


def _node_stock(node):
    """所選分店回報的庫存快照 {品項 ID: (庫存, 是否售完)}；尚未選分店時為空"""
    if node is None:
        return {}
    return {s.menu_item_id: (s.stock or 0, bool(s.is_sold_out))
            for s in NodeStock.query.filter_by(node_id=node.id).all()}


def _sold_out_ids(items, node_stock=None):
    """售完的品項 ID：單機/分店依本機庫存；線上端依所選分店回報的庫存 (沒有回報過的品項視為售完)"""
    if node_stock is None:
        return {i.id for i in items if not i.is_available}
    return {i.id for i in items if i.id not in node_stock or node_stock[i.id][1] or node_stock[i.id][0] <= 0}


def _apply_node_stock(payload, node_stock, sold_out_ids):
    """線上端：把菜單 JSON 中的庫存欄位換成所選分店的庫存 (前端的售完標示、套餐可選與否依此判斷)"""
    for data in payload['items']:
        data['is_available'] = data['id'] not in sold_out_ids
        data['stock'] = node_stock.get(data['id'], (0, True))[0]
        for combo in data.get('combos') or ():
            combo['is_available'] = all(s['menu_item_id'] not in sold_out_ids for s in combo.get('sides') or ())
    return payload


@customer_bp.route('/pickup_node', methods=['POST'], endpoint='set_pickup_node')
@hub_only
def set_pickup_node():
    """線上端：設定取餐分店 (存在 session)"""
    uuid = (request.form.get('node_uuid') or '').strip()
    node = next((n for n in _online_nodes() if n.uuid == uuid), None)
    if node is None:
        flash('此分店目前不開放線上點餐，請重新選擇取餐分店！', 'danger')
        session.pop(PICKUP_NODE_KEY, None)
    else:
        session[PICKUP_NODE_KEY] = node.uuid
        flash(f'取餐分店：{node.name} (菜單已改為顯示該分店的供應狀況)', 'success')
    return redirect(url_for('customer.customer_index'))


@customer_bp.route('/', endpoint='customer_index')
def customer_index():
    items = MenuItem.query.options(selectinload(MenuItem.combo_options)).all()
    modifiers = ModifierOption.query.filter_by(is_active=True).all()
    setting = get_or_create_reward_setting(g.tenant_id)
    member = current_member()
    if session.get('user_id') and member is None:
        # 會員已被刪除或不屬於這家店：結束整組顧客工作階段 (不留姓名 / 進場紀錄 / 登入方式的半登入狀態)
        close_customer_session()
        return redirect(url_for('customer.customer_index'))
    is_hub = sync_runtime.is_hub()
    if member is None and not is_hub and session.get('line_pending'):
        # 共用點餐機：授權 LINE 後沒有登入 / 註冊就回到點餐首頁 → 作廢待綁定 (不可綁到下一位註冊 / 登入的顧客)
        session.pop('line_pending', None)
        
    # --- 改由 menu_service 處理分店與庫存 ---
    pickup_nodes = menu_service.get_online_nodes(g.tenant_id) if is_hub else []
    pickup_node = menu_service.resolve_pickup_node(pickup_nodes) if is_hub else None
    
    # 分店價格 / 上下架 (線上端依顧客選擇的取餐分店)：下架的餐點不顯示
    branch_service.activate(g.tenant_id, branch_service.storefront_branch(pickup_node))
    items = [i for i in items if not i.is_branch_hidden]
    node_stock = menu_service.get_node_stock(pickup_node) if is_hub else None
    sold_out_ids = menu_service.get_sold_out_ids(items, node_stock)
    
    # 不在供應時段 / 販售期間、或原料不足 (禁止販售模式) 的餐點：顯示原因並不可點
    unavailable = {i.id: i18n_service.translate_message(i.service_status()[1]) for i in items if not i.service_status()[0]}
    for item_id in (inventory_service.shortage_items(g.tenant_id) if not is_hub else ()):
        unavailable.setdefault(item_id, '')
        sold_out_ids = sold_out_ids | {item_id}
        
    menu_data = menu_service.storefront_payload(items, modifiers, setting)
    if is_hub:
        menu_service.apply_node_stock_to_payload(menu_data, node_stock, sold_out_ids)
        
    for data in menu_data['items']:
        if data['id'] in sold_out_ids or data['id'] in unavailable:
            data['is_available'] = False
            
    lang = i18n_service.current_language()
    pro = _storefront_extras(member, dining_table=None if is_hub else table_service.current_table(), is_hub=is_hub,
                             pickup_node=pickup_node)
    return render_template(
        'customer.html',
        items=items,
        categories=menu_service.menu_categories(items),
        member=member,
        needs_privacy_consent=privacy_service.needs_consent(member),
        user_points=member.points if member else 0,
        user_coupons=coupon_service.usable_coupons(member.id) if member else [],
        member_passes=[mp.to_dict() for mp in pass_service.member_passes(member.id)] if member else [],
        personalized_items=[i for i in menu_service.get_favorite_items(member.id) if not i.is_branch_hidden] if member else [],
        is_guest=bool(session.get('is_guest')),
        reward_setting=setting,
        modifier_options=modifiers,
        menu_data=menu_data,
        sold_out_ids=sold_out_ids,
        is_online=is_hub,
        pickup_nodes=pickup_nodes,
        pickup_node=pickup_node,
        dining_table=None if is_hub else table_service.current_table(),
        table_pay_enabled=(not is_hub) and table_pay_service.enabled(),
        group_order_enabled=(not is_hub) and group_order_service.enabled(),
        table_expired=False if is_hub else table_service.table_binding_expired(),
        tenant=g.tenant,
        unavailable=unavailable,
        intake=intake_service.customer_notice(g.tenant_id, pickup_node if is_hub else None),
        eta_enabled=eta_service.enabled(),
        lang=lang, langs=i18n_service.available_languages(), lang_labels=i18n_service.LANG_LABELS,
        ui=i18n_service.ui_strings(lang), cat_labels=i18n_service.category_labels(lang),
        allergen_labels=i18n_service.allergen_labels(lang), diet_labels=i18n_service.diet_labels(lang),
        used_allergens=sorted({a for i in items for a in i.allergen_codes}),
        pro=pro,
        attract=None if member is not None or session.get('user_name') or (not is_hub and table_service.current_table())
        else _attract_slides(items, is_hub),
        public_reviews=feedback_service.public_reviews(g.tenant_id) if not session.get('user_name') else [],
        chat=_chat_widget(),
    )


def _chat_widget():
    from app.routes.public_extras import chat_widget_config
    return chat_widget_config()


def _storefront_extras(member, dining_table, is_hub, pickup_node=None):
    """
    點餐頁的進階設定 (付款方式、電子發票、儲值金、服務費、會員折扣、LINE 登入、過敏原顯示)。
    pickup_node：線上端顧客選的取餐分店 (外送到府依該分店回報的設定顯示；尚未選分店時沿用線上端設定)
    """
    from app.services import line_service
    pos_cfg = settings_service.get('pos')
    scope = pos_cfg.get('service_charge_scope') or []
    member_cfg = settings_service.get('member')
    # 線上點餐開啟線上付款 (ONLINE_PAYMENT)：一律導向綠界付款 (付款後依實際方式記錄)，不列點餐機的付款方式 (現金等)；
    # 儲值金 / 禮物卡不可用 (會先扣款又被要求線上付全額)
    online_payment = is_hub and bool(current_app.config.get('ONLINE_PAYMENT', True))
    methods = [PaymentMethod.CREDIT_CARD.value] if online_payment else payment_service.allowed_methods('kiosk')
    wallet = bool(member_cfg.get('wallet_enabled')) and member is not None and not online_payment
    if wallet and '儲值金' not in methods:
        methods = methods + ['儲值金']
    if not wallet:
        methods = [m for m in methods if m != '儲值金']
    gift_cfg = settings_service.get('gift_card')
    gift_kiosk = bool(gift_cfg.get('enabled') and gift_cfg.get('kiosk_enabled')) and not is_hub and dining_table is None
    if gift_kiosk and '禮物卡' not in methods:
        methods = methods + ['禮物卡']
    if not gift_kiosk:
        methods = [m for m in methods if m != '禮物卡']
    tier = member.tier if member is not None and member_cfg.get('tiers_enabled', True) else None
    od = own_delivery_service.node_config(pickup_node) if is_hub else None
    return {
        'methods': methods or ['Cash'],
        'onlinePayment': online_payment,
        'invoiceEnabled': bool(settings_service.get('einvoice').get('enabled')) and dining_table is None,
        'walletBalance': member.wallet_balance if member is not None else 0,
        'walletEnabled': wallet,
        'serviceChargePercent': pos_cfg.get('service_charge_percent') or 0,
        'serviceChargeDineIn': ('table' in scope) if dining_table is not None else ('kiosk_dine_in' in scope and not is_hub),
        'tierName': tier.name if tier else '',
        'tierDiscount': tier.discount_percent if tier else 0,
        # 會員等級的點數倍率 (與 order_checkout 的 multiplier 算法相同)
        'tierPointsMultiplier': (max(0, tier.points_multiplier) if tier.points_multiplier is not None else 1) if tier else 1,
        'lineLogin': line_service.login_enabled(),
        'receiptEmail': receipt_service.email_enabled(),
        'receiptLine': receipt_service.line_enabled() and bool(member and member.line_user_id),
        # 人臉登入 (可能誤認) 的工作階段不預填會員 Email (個資)
        'memberEmail': (member.email if member is not None and not needs_pin_confirm() else '') or '',
        'lineBound': bool(member and member.line_user_id),
        'showAllergens': bool(settings_service.get('menu').get('show_allergens', True)),
        'showCalories': bool(settings_service.get('menu').get('show_calories', True)),
        'showCarbon': bool(settings_service.get('menu').get('show_carbon', True)),
        'voiceOrdering': bool(settings_service.get('menu').get('voice_ordering', True)),
        'pickupSlots': [{'value': t.strftime('%Y-%m-%dT%H:%M'), 'label': lab}
                        for t, lab in pickup_service.slots()] if is_hub else [],
        'serviceCall': dining_table is not None,
        'etaEnabled': eta_service.enabled() and not is_hub,
        'a11yButton': bool(settings_service.get('menu').get('accessibility_button', True)),
        'homeDelivery': ({'zones': od['zones'], 'freeOver': od['free_over'], 'eta': od['eta_minutes'],
                          'phone': member.phone if member is not None else ''}
                         if is_hub and od['enabled'] else None),
    }


def _attract_slides(items, is_hub):
    """點餐機首頁的待機輪播 (門市)：推薦 / 新品 / 特價且有照片的餐點，最多 8 張"""
    cfg = settings_service.get('menu')
    seconds = int(cfg.get('attract_seconds') or 0)
    if is_hub or seconds <= 0:
        return None
    pool = [i for i in items if i.image_path and i.is_available and i.service_status()[0] and not i.is_reward]
    featured = [i for i in pool if i.is_recommended or i.is_manual_popular or i.is_new
                or (i.is_discount and (i.discount_price or 0) > 0)]
    slides = [{'name': i.name, 'price': i.unit_price, 'image': url_for('media.menu_image', filename=i.image_path)}
              for i in (featured or pool)[:8]]
    return {'seconds': seconds, 'message': cfg.get('attract_message') or '觸碰螢幕開始點餐', 'slides': slides}


@customer_bp.route('/api/voice/parse', methods=['POST'], endpoint='voice_parse')
@limiter.limit('30/minute')
def api_voice_parse():
    """語音點餐：把顧客說的話解析成品項 (前端先顯示確認畫面，顧客確認後才加入購物車)"""
    from app.services import voice_order_service
    cfg = settings_service.get('menu')
    if not cfg.get('voice_ordering', True):
        raise AppError('本店未開放語音點餐', 404)
    text = str(get_json_dict().get('text') or '').strip()[:voice_order_service.MAX_TEXT]
    if not text:
        raise AppError('沒有聽到內容，請再說一次')
    branch_service.activate_for_request(g.tenant_id)
    items = [i for i in MenuItem.query.all() if not i.is_branch_hidden and not i.is_reward]
    addons = [m.to_dict() for m in ModifierOption.query.filter_by(is_active=True, category='addons').all()]
    # 可否點與菜單頁相同 (線上端看取餐分店庫存、門市看原料不足)；與輪詢 API 相同不改選取餐分店 (adjust=False，不 flash)
    is_hub = sync_runtime.is_hub()
    pickup_node = _pickup_node(_online_nodes(), adjust=False) if is_hub else None
    avail = _availability_map(items, is_hub, pickup_node)
    result = voice_order_service.parse(text, items, addons, use_llm=bool(cfg.get('voice_llm', True)),
                                       availability={k: v[0] for k, v in avail.items()})
    return jsonify(dict(result, success=True))


@customer_bp.route('/api/user_available_coupons')
def api_user_available_coupons():
    member = current_member()
    if member is None:
        return jsonify({'success': False, 'coupons': [], 'points': 0})
    db.session.refresh(member)
    coupons = coupon_service.usable_coupons(member.id)
    return jsonify({'success': True, 'points': member.points, 'coupons': [uc.to_dict() for uc in coupons]})


@customer_bp.route('/api/verify_promo', methods=['POST'])
@limiter.limit('30/minute')
def verify_promo():
    data = get_json_dict()
    member = current_member()
    result = find_promo(data.get('promo_code'), data.get('subtotal'), member.id if member else None,
                        guard=(g.tenant_id, request.remote_addr or '-'))
    db.session.commit()   # 保存無效代碼的失敗次數
    return jsonify(dict(result.to_dict(), message_i18n=i18n_service.translate_message(result.message)))


@customer_bp.route('/shop/<tenant_slug>', endpoint='tenant_storefront')
def tenant_storefront(tenant_slug):
    return redirect(url_for('customer.customer_index'))  # 店家已由 tenant_context 依網址綁定


@customer_bp.route('/submit_order', methods=['POST'])
@limiter.limit('20/minute')
def submit_order():
    data = get_json_dict()
    member = current_member()
    order_type = str(data.get('order_type') or OrderType.DINE_IN).strip()
    # 線上端：取餐分店為顧客在首頁選擇的分店 (未選擇時 process_checkout 會要求先選)
    node_uuid = session.get(PICKUP_NODE_KEY) if sync_runtime.is_hub() else None
    # 門市桌邊掃碼點餐：以綁定的桌位下單 (綁定已失效時不可當成一般點餐機訂單送出，避免漏結帳)
    dining_table = None
    if not sync_runtime.is_hub():
        had_table = table_service.TABLE_SESSION_KEY in session
        dining_table = table_service.current_table()
        if had_table and dining_table is None:
            raise AppError('桌位 QR code 已失效，請重新掃描桌上的 QR code 後再送出！', 409)
    items = data.get('items')
    if member is not None and needs_pin_confirm() and (
            normalize_payment_method(data.get('payment_method')) == PaymentMethod.STORED_VALUE or safe_int(data.get('use_points'), 0) > 0
            or (isinstance(items, list) and any(isinstance(i, dict) and (i.get('is_reward') or i.get('pass_id'))
                                                for i in items))):
        # 人臉登入 (可能誤認) 的工作階段動用儲值金 / 點數 / 紅利 / 寄杯：與會員條碼、POS 相同，需再輸入 PIN (403 need_pin)
        require_confirmed(member, data.get('pin'), '使用儲值金、點數、紅利兌換或寄杯')
    order, replayed = process_checkout(
        tenant_id=g.tenant_id,
        items=items,
        payment_method=data.get('payment_method', 'Cash'),
        order_type=order_type,
        need_cutlery=bool(data.get('need_cutlery', True)) and order_type in (OrderType.TAKE_OUT, OrderType.HOME_DELIVERY),
        note=data.get('note', ''),
        promo_code=data.get('promo_code', ''),
        use_points=data.get('use_points', 0),
        table_input=session.get('user_name', '訪客'),
        user_id=member.id if member else None,
        order_source=OrderSource.KIOSK,
        customer_log_id=session.get('customer_log_id'),
        client_ip=request.remote_addr or '',
        client_order_id=data.get('client_order_id'),
        node_uuid=node_uuid,
        dining_table=dining_table,
        invoice_info=data.get('invoice') or None,
        pickup_at=data.get('pickup_at') or None,
        delivery=data.get('delivery') or None,
        expected_total=data.get('expected_total'),
        # 點餐機只收禮物卡卡號 (刷卡授權碼等仍由 POS 提供)
        payment_info={'gift_code': (data.get('payment_info') or {}).get('gift_code')} if isinstance(data.get('payment_info'), dict) else None,
    )
    # 以 client_order_id 重送時可能回傳已取消 (付款狀態仍是 pending) 的舊訂單：不再給付款連結
    extra = ({'payment_url': url_for('payment.pay', order_uuid=order.uuid)}
             if order.payment_status == 'pending' and order.status != OrderStatus.CANCELLED else {})
    from app.routes.pos import track_qr
    extra['track_qr'] = track_qr(order)
    return jsonify(dict(order.to_receipt(), replayed=replayed, **_pickup_info(order, node_name_map()), **extra))


OFFLINE_MAX_MINUTES = 180   # 斷線暫存超過 3 小時的訂單不再自動補送 (顧客多半已離開)，改由店員確認


@customer_bp.route('/api/kiosk/offline_order', methods=['POST'], endpoint='offline_order')
@limiter.limit('60/minute')
@store_only
def offline_order():
    """
    點餐機斷線時暫存在瀏覽器的訂單，連線恢復後補送 (static/js/kiosk-offline.js)。
    一律以訪客、櫃檯付款 (現金) 建立，不使用點數 / 優惠券 / 紅利 / 寄杯；以 client_order_id 去重 (重送回傳原訂單)。
    """
    data = get_json_dict()
    if str(data.get('shop') or '') != g.tenant.slug:
        raise AppError('暫存單的店家與目前店家不同，請由店員處理', 409, wrong_shop=True)
    payload = data.get('order') if isinstance(data.get('order'), dict) else {}
    client_order_id = str(payload.get('client_order_id') or '').strip()
    if not client_order_id:
        raise AppError('暫存單缺少訂單識別碼')
    try:
        queued_at = datetime.fromisoformat(str(data.get('queued_at') or ''))
    except ValueError:
        raise AppError('暫存時間格式錯誤')
    if queued_at.tzinfo is not None:
        queued_at = queued_at.astimezone().replace(tzinfo=None)
    if (datetime.now() - queued_at).total_seconds() > OFFLINE_MAX_MINUTES * 60:
        raise AppError(f'暫存超過 {OFFLINE_MAX_MINUTES // 60} 小時，請店員確認顧客是否仍在等候後重新點餐', 410,
                       expired=True)
    items = payload.get('items') if isinstance(payload.get('items'), list) else []
    if any(isinstance(i, dict) and (i.get('is_reward') or i.get('pass_id')) for i in items):
        raise AppError('紅利兌換與寄杯無法以暫存單補送', 409)
    code = re.sub(r'[^0-9A-Z]', '', str(data.get('code') or '').upper())[:6]
    existing = find_by_client_order_id(g.tenant_id, client_order_id)
    if existing is not None:   # 斷線前其實已送達 (只是沒收到回應) 或重複補送
        return _offline_replayed(existing, code)
    _offline_intake_check()
    order_type = payload.get('order_type') if payload.get('order_type') in (OrderType.DINE_IN, OrderType.TAKE_OUT) \
        else OrderType.DINE_IN
    note = f"[斷線暫存 {code} {queued_at:%H:%M}] {str(payload.get('note') or '')}".strip()
    try:
        order, replayed = process_checkout(
            tenant_id=g.tenant_id, items=items, payment_method='Cash', order_type=order_type,
            need_cutlery=bool(payload.get('need_cutlery', True)) and order_type == OrderType.TAKE_OUT,
            note=note[:200], table_input=f'暫存單 {code}', user_id=None, order_source=OrderSource.KIOSK,
            client_ip=request.remote_addr or '', client_order_id=client_order_id,
            invoice_info=payload.get('invoice') or None, skip_intake=True)
    except AppError as exc:
        # 原本的 /submit_order 還在處理中 (逾時後補送)：同一個訂單編號先完成的那張就是這張暫存單
        same = find_by_client_order_id(g.tenant_id, client_order_id) if exc.status == 409 else None
        if same is None:
            raise
        return _offline_replayed(same, code)
    if not replayed:
        from app.services.audit import record_staff_audit
        record_staff_audit('OFFLINE_ORDER', target_order_id=order.id, tenant_id=g.tenant_id, as_system=True,
                           details=f'點餐機斷線暫存單 {code} 補送 (暫存於 {queued_at:%m/%d %H:%M})')
        db.session.commit()
    return jsonify(_offline_result(order, replayed, code))


def _offline_intake_check():
    """
    暫停接單 / 每時段上限：只有在「裝置管理」註冊過的點餐機補送的暫存單才免檢查 (點餐機當時已收單)；
    未註冊的用戶端 (例如顧客手機、瀏覽器直接呼叫) 無法證明當時確實離線，照常套用接單控制。
    """
    from app.services import device_service
    device = device_service.current()
    if device is not None and device.kind == 'kiosk':
        return
    intake_service.check(SimpleNamespace(tenant_id=g.tenant_id, is_pos=False, is_delivery=False, is_hub=False,
                                         pickup_at=None, now=None, stock_node=None, node_uuid=None))


def _offline_result(order, replayed, code):
    """只回傳點餐機需要的欄位 (取餐號、金額)，不含會員、付款、發票等收據內容"""
    return {'success': True, 'replayed': replayed, 'code': code, 'order_id': order.id,
            'pickup_number': order.pickup_number, 'total_price': order.total_price}


def _offline_replayed(order, code):
    # 只有點餐機的訪客訂單、或本次進場 (同一位顧客，例如會員結帳時回應遺失) 的訂單可能是這張暫存單 (不可藉此查到別人的訂單)
    same_visit = bool(order.customer_log_id) and order.customer_log_id == session.get('customer_log_id')
    if order.order_source != OrderSource.KIOSK or (order.user_id is not None and not same_visit):
        raise AppError('這張暫存單的訂單編號已被其他訂單使用，請店員確認', 409)
    return jsonify(_offline_result(order, True, code))


def _pickup_info(order, node_names):
    """線上訂單：取餐號由分店收到後才編號 (之前為 0，顯示「待分店確認」)，並附上取餐分店名稱"""
    return {
        'pickup_pending': order.order_source == OrderSource.ONLINE and not (order.pickup_number or 0) > 0,
        'pickup_node_name': node_names.get(order.node_uuid, '') if order.node_uuid else '',
    }


def _availability_map(items, is_hub, pickup_node):
    """目前每個品項可否點 (售完 / 不在供應時段 / 原料不足 / 分店庫存)：{id: (可點, 提示)}"""
    node_stock = _node_stock(pickup_node) if is_hub else None
    sold_out_ids = _sold_out_ids(items, node_stock)
    out = {}
    for i in items:
        in_service, hint = i.service_status()
        if not in_service:
            out[i.id] = (False, hint)
        elif i.id in sold_out_ids:
            out[i.id] = (False, '')
        else:
            out[i.id] = (True, '')
    if not is_hub:
        for item_id in inventory_service.shortage_items(g.tenant_id):
            if item_id in out and out[item_id][0]:
                out[item_id] = (False, '')
    return out


@customer_bp.route('/api/menu/availability', endpoint='menu_availability')
@limiter.limit('120/minute')
def api_menu_availability():
    """
    點餐頁每 30 秒輪詢的「可點 / 售完 / 價格」狀態，帶 ETag：內容沒變時回 304 (不傳內容、前端不重畫)。
    售完或恢復供應時點餐機不必重新整理就會更新 (以前只有結帳被擋下或重新載入頁面才知道)。
    """
    import json as _json
    is_hub = sync_runtime.is_hub()
    # 輪詢不可改選取餐分店 (選的分店暫時離線時回傳 None：菜單顯示無法供應，結帳時提示該分店離線)
    pickup_node = _pickup_node(_online_nodes(), adjust=False) if is_hub else None
    branch_service.activate(g.tenant_id, branch_service.storefront_branch(pickup_node))
    items = [i for i in MenuItem.query.all() if not i.is_branch_hidden]
    avail = _availability_map(items, is_hub, pickup_node)
    channels = ('dine_in', 'takeout', 'online', 'delivery')
    # member_prices：會員價 (與頁面載入時的菜單資料相同)，改價後會員的購物車也跟著更新
    payload = {str(i.id): {'available': avail[i.id][0], 'hint': i18n_service.translate_message(avail[i.id][1]), 'price': i.unit_price,
                           'original_price': i.list_price, 'prices': {ch: i.price_for(ch) for ch in channels},
                           'member_prices': {ch: i.price_for(ch, member=True) for ch in channels}}
               for i in items}
    return json_etag_response(_json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def _receipt_order_key():
    """每張訂單的寄送次數上限 (避免拿自己的訂單當寄信機 / 耗盡店家的 LINE 訊息額度)"""
    return f"receipt:{request.view_args.get('order_id')}"


# 每張訂單每天最多寄 5 次 (Email 與 LINE 共用計數)
_receipt_daily_limit = limiter.shared_limit('5/day', scope='receipt_send', key_func=_receipt_order_key)


@customer_bp.route('/api/my_orders/<int:order_id>/receipt/email', methods=['POST'], endpoint='receipt_email')
@limiter.limit('10/minute')
@_receipt_daily_limit
def api_receipt_email(order_id):
    """收據視窗「寄到信箱」：會員自己的或本次進場訪客的訂單；會員可記住 Email (人臉登入的工作階段不記住)"""
    from app.services import receipt_service
    order = _own_order(order_id)
    data = get_json_dict()
    receipt_service.request_email(order, data.get('email'), remember=bool(data.get('remember')) and not needs_pin_confirm())
    db.session.commit()
    return jsonify({'success': True, 'message': '電子收據將寄到您的信箱', 'message_i18n': i18n_service.translate_message('電子收據將寄到您的信箱')})


@customer_bp.route('/api/my_orders/<int:order_id>/receipt/line', methods=['POST'], endpoint='receipt_line')
@limiter.limit('10/minute')
@_receipt_daily_limit
def api_receipt_line(order_id):
    from app.services import receipt_service
    order = _own_order(order_id)
    receipt_service.request_line(order)
    db.session.commit()
    return jsonify({'success': True, 'message': '電子收據將傳到您的 LINE', 'message_i18n': i18n_service.translate_message('電子收據將傳到您的 LINE')})


@customer_bp.route('/api/gift_cards/balance', methods=['POST'], endpoint='gift_card_balance')
@limiter.limit('20/minute')
def api_gift_card_balance():
    """點餐機禮物卡付款前查餘額：只回遮罩卡號與餘額 (卡號 16 碼本身就是憑證，查詢有頻率限制)"""
    from app.services import giftcard_service
    cfg = settings_service.get('gift_card')
    if not (cfg.get('enabled') and cfg.get('kiosk_enabled')) or sync_runtime.is_hub():
        raise AppError('此點餐管道未開放禮物卡付款', 404)
    card = giftcard_service.find(g.tenant_id, get_json_dict().get('code'))
    if card.status != 'active':
        raise AppError('此禮物卡已作廢', 409)
    return jsonify({'success': True, 'masked_code': card.masked_code, 'balance': giftcard_service.balance(card)})


@customer_bp.route('/api/eta', methods=['POST'], endpoint='eta')
@limiter.limit('60/minute')
def api_eta():
    """購物車目前內容的預估等候時間 (分鐘)；功能關閉時 minutes = 0"""
    data = get_json_dict()
    items = data.get('items') if isinstance(data.get('items'), list) else []
    if sync_runtime.is_hub():   # 線上端不知道分店的排隊狀況
        return jsonify({'success': True, 'minutes': 0, 'ready_at': ''})
    return jsonify(dict(eta_service.status_payload(g.tenant_id, eta_service.pairs_from_items(items, g.tenant_id)),
                        success=True))


def _own_order(order_id):
    """我的訂單裡的訂單：會員自己的，或本次進場 (訪客) 的"""
    member = current_member()
    order = db.session.get(Order, safe_id(order_id))
    if order is None or order.tenant_id != g.tenant_id:
        raise AppError('查無此訂單', 404)
    if member is not None and order.user_id == member.id:
        return order
    if member is None and session.get('customer_log_id') and order.customer_log_id == session['customer_log_id'] \
            and order.user_id is None:
        return order
    raise AppError('查無此訂單', 404)


@customer_bp.route('/api/reorder/<int:order_id>', endpoint='reorder')
@limiter.limit('30/minute')
def api_reorder(order_id):
    """再點一次：把該筆訂單的品項還原成購物車格式 (價格由前端依目前菜單重算；已下架 / 紅利 / 寄杯 / 贈品略過)"""
    order = _own_order(order_id)
    lines, skipped = reorder_service.lines_for(order, g.tenant_id)
    return jsonify({'success': True, 'lines': lines, 'skipped': skipped})


@customer_bp.route('/my_orders', endpoint='my_orders')
def my_orders():
    """會員看自己的訂單；訪客以本次進場紀錄 (customer_log_id) 歸戶，不會看到撞號訪客的訂單"""
    member = current_member()
    if member is not None:
        query = Order.query.filter_by(user_id=member.id)
    elif session.get('customer_log_id'):
        query = Order.query.filter_by(customer_log_id=session['customer_log_id'], user_id=None)
    else:
        return jsonify([])
    orders = query.options(selectinload(Order.items)).order_by(Order.id.desc()).all()
    node_names = node_name_map()
    return jsonify([{
        'order_id': o.id,
        'user_order_no': len(orders) - idx,
        'pickup_number': o.display_pickup_number,
        **_pickup_info(o, node_names),
        'total_price': o.total_price,
        'discount_amount': o.discount_amount or 0,
        'points_used': o.points_used or 0,
        'points_earned': o.points_earned or 0,
        'payment_method': o.payment_method,
        'payment_status': o.payment_status or '',
        'payment_url': (url_for('payment.payment_return', order_uuid=o.uuid) if o.payment_note
                        else url_for('payment.pay', order_uuid=o.uuid))
        if o.payment_status == 'pending' and o.status != OrderStatus.CANCELLED else '',
        'payment_note': (o.payment_note or '') if o.payment_status == 'pending' and o.status != OrderStatus.CANCELLED else '',
        'order_type': o.order_type,
        'pickup_at': o.pickup_at_label,
        'eta': o.eta_label,
        'delivery_label': o.delivery_status_label,
        'delivery_status': o.delivery_status or '',
        'status': o.status_label,
        'status_code': o.status,
        'can_reorder': (o.status != OrderStatus.CANCELLED or o.payment_status == 'expired')
                   and any(not i.is_reward and not i.is_gift for i in o.items),
        'created_at': o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else '',
        'items': [i.to_dict() for i in o.items],
    } for idx, o in enumerate(orders)])
