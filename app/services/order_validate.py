"""
結帳前的檢查 _validate (由 order_checkout 拆出；交易開始前執行，不寫資料庫)：付款方式與通路、外送到府、班別、會員、
品項 (含改價核准)、寄杯 / 混合付款 / 禮物卡、加購配額、促銷、庫存需求、線上 / 桌邊的付款狀態、刷卡 / LINE Pay / 發票 / 預約時間。
欄位寫在 order_checkout.Checkout 上，各階段的順序見該類別說明。
"""
from datetime import datetime

from flask import current_app

from app.errors import AppError
from app.extensions import db
from app.models.enums import DELIVERY_SOURCES, OrderSource, OrderType, PaymentMethod, normalize_payment_method
from app.models.promotion import Promotion
from app.models.user import User
from app.services import payment_service, promotion_service, settings_service
from app.services.order_pricing import CheckoutLine, _parse_lines, _price_channel, _validate_addon_quota
from app.services.order_stock import online_pickup_node
from app.sync import runtime as sync_runtime
from app.utils import safe_int


def _validate(c):
    """
    交易前的所有檢查 (不寫資料庫)：付款方式與通路、外送到府、班別、會員、品項 (含改價核准)、寄杯 / 混合付款 / 禮物卡、
    加購配額、促銷、庫存需求、線上 / 桌邊的付款狀態、刷卡 / LINE Pay / 發票 / 預約時間。
    """
    c.payment = normalize_payment_method(c.payment_method)
    if c.payment is None:
        raise AppError(f'不支援的付款方式：{c.payment_method}')
    if c.order_type not in {t.value for t in OrderType}:
        raise AppError(f'不支援的用餐方式：{c.order_type}')
    c.is_pos = c.order_source == OrderSource.POS
    c.is_delivery = c.order_source in DELIVERY_SOURCES
    c.is_hub = sync_runtime.is_hub()
    # 系統內部建立的訂單 (斷線暫存單補送、團體預購送單)：固定以櫃檯現金建立，不受點餐機付款方式設定限制
    c.internal = bool(c.skip_intake) or isinstance(c.pickup_at, datetime)
    c.online_payment = c.is_hub and bool(current_app.config.get('ONLINE_PAYMENT', True))
    if c.online_payment and c.payment in (PaymentMethod.STORED_VALUE, PaymentMethod.GIFT_CARD, PaymentMethod.SPLIT):
        # 線上點餐一律經綠界付款 (付款完成才送分店)：儲值金 / 禮物卡在線上端扣款後又會導去綠界付全額
        raise AppError(f'線上點餐不支援「{c.payment}」付款，請改用線上付款', 400)
    if c.is_delivery:
        c.order_type, c.payment = OrderType.DELIVERY, PaymentMethod.PLATFORM
    elif c.order_type == OrderType.DELIVERY:
        raise AppError('外送訂單請由外送平台串接或 POS「外送單」登錄')
    elif c.payment == PaymentMethod.PLATFORM:
        raise AppError('平台代收只用於外送平台訂單')
    elif c.dining_table is None and not c.is_hub and not c.internal:
        allowed = payment_service.allowed_methods('pos' if c.is_pos else 'kiosk', c.tenant_id)
        wallet_ok = c.payment == PaymentMethod.STORED_VALUE and settings_service.get('member', c.tenant_id).get('wallet_enabled')
        gift_cfg = settings_service.get('gift_card', c.tenant_id)
        gift_ok = c.payment == PaymentMethod.GIFT_CARD and gift_cfg.get('enabled') and (c.is_pos or gift_cfg.get('kiosk_enabled'))
        split_ok = c.payment == PaymentMethod.SPLIT and c.is_pos   # 各筆付款方式在 parse_tenders 檢查
        if c.payment not in allowed and not wallet_ok and not gift_ok and not split_ok:
            raise AppError(f'此點餐管道未開放「{c.payment}」付款，請改用其他付款方式')

    c.home = None
    if c.order_type == OrderType.HOME_DELIVERY:   # 自有外送 (外送到府)：資料驗證在決定取餐分店之後
        if c.dining_table is not None or not (c.is_hub or c.is_pos):
            raise AppError('外送到府請由線上點餐或 POS 電話訂餐')

    c.pos_cfg = settings_service.get('pos', c.tenant_id)
    c.shift_open = payment_service.open_shift_id(c.tenant_id)
    if c.is_pos and c.pos_cfg.get('require_shift') and c.shift_open is None:
        raise AppError('尚未開班：請先在 POS 按「交班」輸入開班零用金後再結帳', 409, need_shift=True)

    c.user = db.session.get(User, safe_int(c.user_id, 0)) if c.user_id else None
    if c.user is not None and c.user.tenant_id != c.tenant_id:
        c.user = None
    c.price_channel = _price_channel(c.order_source, c.order_type, c.is_hub)
    # 分店價格 / 上下架：分店 (與桌邊、點餐機) 用本分店的設定；線上端用顧客選擇的取餐分店
    from app.services import branch_service
    branch_service.activate(c.tenant_id, c.node_uuid if c.is_hub else sync_runtime.local_node_uuid())
    # 預約取餐時間 (空白 = 盡快)：先解析，預約單的供應時段 / 星期 / 販售期間 (含套餐配餐) 以取餐時間判斷
    c.scheduled_at = None
    if isinstance(c.pickup_at, datetime):   # 系統內部建立的預約單 (團體預購，時間已在建立時驗證)
        c.scheduled_at = c.pickup_at
    elif c.is_hub and c.pickup_at:          # 線上點餐的預約取餐時間
        from app.services import pickup_service
        c.scheduled_at = pickup_service.parse(c.pickup_at, c.tenant_id)
    c.lines = _parse_lines(c.items, c.tenant_id, c.price_channel, c.scheduled_at or c.now, allow_override=c.is_pos,
                           member=c.user is not None)
    c.approved_by = ''
    if any(ln.original_price is not None for ln in c.lines):
        if not c.pos_cfg.get('allow_price_override', True):
            raise AppError('目前設定不允許收銀改價', 403)
        if not c.approve:
            raise AppError('改價需要主管核准', 403, need_approval=True)
        c.approved_by = c.approve('discount', '收銀改價')

    if any(ln.is_reward for ln in c.lines) and c.user is None:
        raise AppError('請先綁定會員才可使用紅利點數兌換商品！')
    if c.payment == PaymentMethod.STORED_VALUE and c.user is None:
        raise AppError('使用儲值金付款請先登入 / 綁定會員！')
    c.pass_lines = []
    if any(ln.is_pass for ln in c.lines):   # 寄杯兌換：必須是會員自己的寄杯、餐點相符 (次數於交易中條件式扣除)
        from app.services import pass_service
        if c.is_delivery:
            raise AppError('外送平台訂單不能使用寄杯')
        c.pass_lines = [(ln, pass_service.find_for_line(c.tenant_id, ln.pass_id, c.user, ln.menu_item))
                      for ln in c.lines if ln.is_pass]
    c.tenders = None
    if c.payment == PaymentMethod.SPLIT:   # 混合付款 (POS)：各筆付款先檢查格式，金額合計於交易中比對應付金額
        if not c.is_pos or c.dining_table is not None:
            raise AppError('混合付款只能在 POS 櫃檯結帳使用')
        c.tenders = payment_service.parse_tenders(c.tenant_id, (c.payment_info or {}).get('tenders'), c.user)
    c.gift_code = ''
    if c.payment == PaymentMethod.GIFT_CARD:
        from app.services import giftcard_service
        c.gift_code = giftcard_service.find(c.tenant_id, (c.payment_info or {}).get('gift_code')).code
    quota_error = _validate_addon_quota(
        [(ln.menu_item, ln.quantity) for ln in c.lines
         if not ln.is_add_on and not ln.is_reward and not ln.is_pass and not (ln.combo and ln.combo.can_addon is False)],
        [(ln.menu_item, ln.quantity) for ln in c.lines if ln.is_add_on],
    )
    if quota_error:
        raise AppError(quota_error)

    # 促銷活動 (外送平台訂單以平台價格為準、團體預購以團購頁顯示的金額為準，不套用本店促銷)
    # 明確以本店篩選促銷活動 (背景排程送出的團購沒有請求情境，不會自動加上店家條件)
    c.is_group = str(c.external_ref or '').startswith('group:')   # 團購送單固定帶 external_ref='group:<token>'
    c.promo_result = promotion_service.PromotionResult() if c.is_delivery or c.is_group else promotion_service.evaluate(
        c.lines, promotion_service.channel_for(c.order_source, c.order_type, c.is_hub), has_member=c.user is not None,
        now=c.now, promotions=Promotion.query.filter_by(tenant_id=c.tenant_id, is_active=True).all())
    for gift, qty, promo_name in c.promo_result.gifts:
        c.lines.append(CheckoutLine(gift, qty, 0, f'[{promo_name}] 贈品', is_gift=True))

    c.demands, c.gift_demands = {}, {}
    for ln in c.lines:
        target = c.gift_demands if ln.is_gift else c.demands
        target[ln.menu_item.id] = target.get(ln.menu_item.id, 0) + ln.quantity
        for side_id in ln.side_item_ids:
            c.demands[side_id] = c.demands.get(side_id, 0) + ln.quantity
    c.subtotal = sum(ln.unit_price * ln.quantity for ln in c.lines)
    c.stock_node = None
    c.payment_status = ''
    if c.is_hub:
        if c.user is None:
            raise AppError('線上點餐請先登入會員 (訪客請至門市點餐)！', 401)
        c.stock_node = online_pickup_node(c.tenant_id, c.node_uuid)
        c.node_uuid, c.order_source = c.stock_node.uuid, OrderSource.ONLINE
        if c.online_payment:
            from app.services import payment_ecpay
            payment_ecpay.require_keys_allowed()   # 測試金鑰未被允許時不成立待付款訂單 (付不了，只會逾時取消)
            c.payment_status = 'pending'  # 付款完成前不送分店 (見 payment_ecpay)
        elif c.payment not in (PaymentMethod.STORED_VALUE, PaymentMethod.GIFT_CARD):
            # 不收線上付款 (ONLINE_PAYMENT=0)：到取餐分店付款，不可當成已收款 (記收款、開發票、贈點)
            c.payment_status = 'unpaid'
    else:
        c.node_uuid = sync_runtime.local_node_uuid()
        if c.dining_table is not None:
            c.payment_status = 'unpaid'
        elif isinstance(c.pickup_at, datetime):
            # 團體預購：取餐時現場付款 (送單時不記收款、不開發票，只印廚房單)
            c.payment_status = 'unpaid'
    if c.order_type == OrderType.HOME_DELIVERY:
        # 線上端依取餐分店回報的本店外送設定 (是否開放、配送區域) 驗證；分店沒有回報時沿用總部設定
        from app.services import own_delivery_service
        c.home = own_delivery_service.validate(c.delivery, 'online' if c.is_hub else 'pos', c.tenant_id,
                                               node=c.stock_node if c.is_hub else None)

    # 付款細節 (交易外先檢查，避免取號後才發現資料不完整)
    c.card_auth, c.card_last4, c.one_time_key = '', '', ''
    if c.payment == PaymentMethod.CREDIT_CARD and not c.is_hub and c.dining_table is None:
        c.card_auth, c.card_last4 = payment_service.validate_card_details(c.payment_info, c.is_pos, c.tenant_id)
    if c.payment == PaymentMethod.LINE_PAY and c.is_pos:
        from app.services import linepay
        if linepay.is_enabled(c.tenant_id):
            c.one_time_key = linepay.normalize_one_time_key((c.payment_info or {}).get('one_time_key'))
            c.payment_status = 'authorizing'   # 建單後立即向 LINE Pay 扣款，失敗則取消
    if c.invoice_info:
        from app.services import einvoice_ecpay
        einvoice_ecpay.normalize_invoice_info(c.invoice_info, settings_service.get('einvoice', c.tenant_id))
    # 接單控制：暫停接單 / 每時段訂單上限 (POS、外送平台、系統內部建立的預約單不檢查)
    if not c.skip_intake:
        from app.services import intake_service
        intake_service.check(c)
