"""
原子結帳 process_checkout (由 order_service 拆出；Kiosk / POS / 桌邊 / 線上 / 外送 / 團購 / 斷線補送共用)。
併發控制說明見 app/services/order_service.py。
"""
import logging
import math
from datetime import datetime

from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.extensions import db
from app.models.enums import (
    DELIVERY_SOURCES, OrderSource, OrderStatus, OrderType, PaymentMethod, VoucherStatus, normalize_payment_method,
)
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.payment import InvoiceStatus, PaymentKind
from app.models.sync import NodeStock, PointLedger
from app.models.table import DiningTable
from app.models.user import CustomerLog, RewardRedemption, User
from app.services import inventory_service, payment_service, promotion_service, settings_service, wallet_service
from app.services.ai_service import invalidate_ai_advice
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.order_pricing import (
    CheckoutLine, _apply_promo, _combo_sides_snapshot, _lock_member, _manual_discount, _parse_lines, _price_channel,
    _service_charge_applies, _validate_addon_quota, normalize_code,
)
from app.services.order_stock import _deduct_stock, next_pickup_number, online_pickup_node
from app.services.points_service import change_points
from app.services.reward_service import get_or_create_reward_setting
from app.services.voucher_service import claim_vouchers_for_item
from app.sync import runtime as sync_runtime
from app.utils import safe_int

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

def _replay(order, *, user_id, customer_log_id, cashier_username):
    """
    重送同一個 client_order_id：只回傳給原本下單的人 (同一位會員、同一次進場或同一位收銀員)，
    其他人拿到別人的訂單編號也看不到收據內容 (會員姓名、剩餘點數、品項)。
    """
    same = ((user_id and order.user_id == safe_int(user_id, 0))
            or (customer_log_id and order.customer_log_id == customer_log_id)
            or (cashier_username and order.cashier_username == cashier_username))
    if not same:
        raise AppError('訂單編號重複，請重新整理後再送出！', 409)
    return order


def process_checkout(*, tenant_id, items, payment_method='Cash', order_type='內用', need_cutlery=False,
                     note='', promo_code='', use_points=0, table_input='', cash_received=0, user_id=None,
                     order_source=OrderSource.KIOSK, cashier_username='', customer_log_id=None,
                     client_ip='', client_order_id=None, node_uuid=None, dining_table=None,
                     order_discount=None, approve=None, payment_info=None, invoice_info=None, is_rush=False,
                     external_ref='', now=None, pickup_at=None, delivery=None, expected_total=None):
    """
    原子結帳。成功回傳 (Order, replayed)；任何驗證失敗拋出 AppError，整筆交易不留痕跡。
    replayed=True 代表同一個 client_order_id 已經結帳過，回傳的是原本那張訂單。
    線上端 (hub) 為線上點餐：node_uuid 為顧客選擇的取餐分店，庫存以該分店回報的數量檢查，
    取餐號由分店收到訂單時編號 (線上端先存 0)。
    dining_table：門市桌邊掃碼點餐的桌位 (內用、標記桌號、餐後至櫃檯結帳：payment_status='unpaid')。

    金額計算順序：小計 (通路價、改價) → 促銷活動 → 優惠券 → 會員等級折扣 → 整單折扣 → 點數折抵 → 服務費。
    expected_total：顧客 / 收銀畫面上看到的應付金額；與實際計算不同 (剛改價、優惠或點數被用掉…) 時結帳失敗 (409 price_changed)，
    回傳目前的單價讓畫面更新後再確認，不會用畫面以外的金額成立訂單。沒有提供時不檢查 (外送平台等)。
    approve(perm, 說明)：需要主管核准時呼叫 (POS 提供；回傳核准人帳號，未核准時拋 403)。
    payment_info：刷卡授權碼 / 卡號末四碼 / LINE Pay 付款碼；invoice_info：電子發票載具 / 統編 / 捐贈。
    """
    now = now or datetime.now()
    dining_table_id = dining_table.id if dining_table is not None else None
    if dining_table is not None:
        order_type, need_cutlery, table_input = OrderType.DINE_IN, False, dining_table.label
        order_source = OrderSource.TABLE
        payment_method = PaymentMethod.CASH  # 實際付款方式在櫃檯結帳時記錄
    client_order_id = str(client_order_id or '').strip()[:36] or None
    owner = dict(user_id=user_id, customer_log_id=customer_log_id, cashier_username=cashier_username)
    if client_order_id:
        existing = Order.query.filter_by(uuid=client_order_id).first()
        if existing is not None:
            return _replay(existing, **owner), True

    payment = normalize_payment_method(payment_method)
    if payment is None:
        raise AppError(f'不支援的付款方式：{payment_method}')
    if order_type not in {t.value for t in OrderType}:
        raise AppError(f'不支援的用餐方式：{order_type}')
    is_pos = order_source == OrderSource.POS
    is_delivery = order_source in DELIVERY_SOURCES
    is_hub = sync_runtime.is_hub()
    if is_delivery:
        order_type, payment = OrderType.DELIVERY, PaymentMethod.PLATFORM
    elif order_type == OrderType.DELIVERY:
        raise AppError('外送訂單請由外送平台串接或 POS「外送單」登錄')
    elif payment == PaymentMethod.PLATFORM:
        raise AppError('平台代收只用於外送平台訂單')
    elif dining_table is None and not is_hub:
        allowed = payment_service.allowed_methods('pos' if is_pos else 'kiosk')
        wallet_ok = payment == PaymentMethod.STORED_VALUE and settings_service.get('member', tenant_id).get('wallet_enabled')
        gift_ok = payment == PaymentMethod.GIFT_CARD and is_pos and settings_service.get('gift_card', tenant_id).get('enabled')
        split_ok = payment == PaymentMethod.SPLIT and is_pos   # 各筆付款方式在 parse_tenders 檢查
        if payment not in allowed and not wallet_ok and not gift_ok and not split_ok:
            raise AppError(f'此點餐管道未開放「{payment}」付款，請改用其他付款方式')

    home = None
    if order_type == OrderType.HOME_DELIVERY:   # 自有外送 (外送到府)
        if dining_table is not None or not (is_hub or is_pos):
            raise AppError('外送到府請由線上點餐或 POS 電話訂餐')
        from app.services import own_delivery_service
        home = own_delivery_service.validate(delivery, 'online' if is_hub else 'pos', tenant_id)

    pos_cfg = settings_service.get('pos', tenant_id)
    shift_open = payment_service.open_shift_id(tenant_id)
    if is_pos and pos_cfg.get('require_shift') and shift_open is None:
        raise AppError('尚未開班：請先在 POS 按「交班」輸入開班零用金後再結帳', 409, need_shift=True)

    user = db.session.get(User, safe_int(user_id, 0)) if user_id else None
    if user is not None and user.tenant_id != tenant_id:
        user = None
    price_channel = _price_channel(order_source, order_type, is_hub)
    # 分店價格 / 上下架：分店 (與桌邊、點餐機) 用本分店的設定；線上端用顧客選擇的取餐分店
    from app.services import branch_service
    branch_service.activate(tenant_id, node_uuid if is_hub else sync_runtime.local_node_uuid())
    lines = _parse_lines(items, tenant_id, price_channel, now, allow_override=is_pos)
    approved_by = ''
    if any(ln.original_price is not None for ln in lines):
        if not pos_cfg.get('allow_price_override', True):
            raise AppError('目前設定不允許收銀改價', 403)
        if not approve:
            raise AppError('改價需要主管核准', 403, need_approval=True)
        approved_by = approve('discount', '收銀改價')

    if any(ln.is_reward for ln in lines) and user is None:
        raise AppError('請先綁定會員才可使用紅利點數兌換商品！')
    if payment == PaymentMethod.STORED_VALUE and user is None:
        raise AppError('使用儲值金付款請先登入 / 綁定會員！')
    pass_lines = []
    if any(ln.is_pass for ln in lines):   # 寄杯兌換：必須是會員自己的寄杯、餐點相符 (次數於交易中條件式扣除)
        from app.services import pass_service
        if is_delivery:
            raise AppError('外送平台訂單不能使用寄杯')
        pass_lines = [(ln, pass_service.find_for_line(tenant_id, ln.pass_id, user, ln.menu_item))
                      for ln in lines if ln.is_pass]
    tenders = None
    if payment == PaymentMethod.SPLIT:   # 混合付款 (POS)：各筆付款先檢查格式，金額合計於交易中比對應付金額
        if not is_pos or dining_table is not None:
            raise AppError('混合付款只能在 POS 櫃檯結帳使用')
        tenders = payment_service.parse_tenders(tenant_id, (payment_info or {}).get('tenders'), user)
    gift_code = ''
    if payment == PaymentMethod.GIFT_CARD:
        from app.services import giftcard_service
        gift_code = giftcard_service.find(tenant_id, (payment_info or {}).get('gift_code')).code
    quota_error = _validate_addon_quota(
        [(ln.menu_item, ln.quantity) for ln in lines
         if not ln.is_add_on and not ln.is_reward and not ln.is_pass and not (ln.combo and ln.combo.can_addon is False)],
        [(ln.menu_item, ln.quantity) for ln in lines if ln.is_add_on],
    )
    if quota_error:
        raise AppError(quota_error)

    # 促銷活動 (外送平台訂單以平台價格為準，不套用本店促銷)
    promo_result = promotion_service.PromotionResult() if is_delivery else promotion_service.evaluate(
        lines, promotion_service.channel_for(order_source, order_type, is_hub), has_member=user is not None, now=now)
    for gift, qty, promo_name in promo_result.gifts:
        lines.append(CheckoutLine(gift, qty, 0, f'[{promo_name}] 贈品', is_gift=True))

    demands, gift_demands = {}, {}
    for ln in lines:
        target = gift_demands if ln.is_gift else demands
        target[ln.menu_item.id] = target.get(ln.menu_item.id, 0) + ln.quantity
        for side_id in ln.side_item_ids:
            demands[side_id] = demands.get(side_id, 0) + ln.quantity
    subtotal = sum(ln.unit_price * ln.quantity for ln in lines)
    stock_node = None
    payment_status = ''
    if is_hub:
        if user is None:
            raise AppError('線上點餐請先登入會員 (訪客請至門市點餐)！', 401)
        stock_node = online_pickup_node(tenant_id, node_uuid)
        node_uuid, order_source = stock_node.uuid, OrderSource.ONLINE
        if current_app.config.get('ONLINE_PAYMENT', True):
            payment_status = 'pending'  # 付款完成前不送分店 (見 payment_ecpay)
    else:
        node_uuid = sync_runtime.local_node_uuid()
        if dining_table is not None:
            payment_status = 'unpaid'

    # 付款細節 (交易外先檢查，避免取號後才發現資料不完整)
    card_auth, card_last4, one_time_key = '', '', ''
    if payment == PaymentMethod.CREDIT_CARD and not is_hub and dining_table is None:
        card_auth, card_last4 = payment_service.validate_card_details(payment_info, is_pos)
    if payment == PaymentMethod.LINE_PAY and is_pos:
        from app.services import linepay
        if linepay.is_enabled(tenant_id):
            one_time_key = linepay.normalize_one_time_key((payment_info or {}).get('one_time_key'))
            payment_status = 'authorizing'   # 建單後立即向 LINE Pay 扣款，失敗則取消
    if invoice_info:
        from app.services import einvoice_ecpay
        einvoice_ecpay.normalize_invoice_info(invoice_info, settings_service.get('einvoice', tenant_id))
    # 線上點餐的預約取餐時間 (空白 = 盡快)
    scheduled_at = None
    if isinstance(pickup_at, datetime):   # 系統內部建立的預約單 (團體預購，時間已在建立時驗證)
        scheduled_at = pickup_at
    elif is_hub and pickup_at:
        from app.services import pickup_service
        scheduled_at = pickup_service.parse(pickup_at, tenant_id)

    try:
        # ---- 交易開始：先取號 (鎖住今日計數列)，之後的寫入都在同一筆交易 ----
        pickup = 0 if stock_node else next_pickup_number(tenant_id)

        short_id, dropped_gifts = _deduct_stock(tenant_id, demands, gift_demands,
                                                node_id=stock_node.id if stock_node else None)
        if short_id is not None:
            db.session.rollback()
            item = db.session.get(MenuItem, short_id)
            name = item.name if item else ''
            stock, sold_out = (item.stock, item.is_sold_out) if item else (0, True)
            if stock_node is not None:
                snap = db.session.get(NodeStock, (stock_node.id, short_id))
                stock, sold_out = (snap.stock, snap.is_sold_out) if snap else (0, True)
            msg = (f'餐點或配餐【{name}】已售完，請移除後再結帳！' if sold_out or stock <= 0
                   else f'餐點或配餐【{name}】庫存剩 {stock} 份，庫存不足！')
            raise AppError(msg, insufficient_stock=True, sold_out_items=[name])

        if dropped_gifts:   # 促銷贈品剛好被別的訂單拿走最後一份：訂單照常成立，只是不附贈
            gift_names = '、'.join(sorted({ln.menu_item.name for ln in lines if ln.is_gift and ln.menu_item.id in dropped_gifts}))
            lines = [ln for ln in lines if not (ln.is_gift and ln.menu_item.id in dropped_gifts)]
            note = f"{note or ''} 【贈品已送完：{gift_names}】".strip()
        for gift_id, qty in gift_demands.items():
            if gift_id not in dropped_gifts:
                demands[gift_id] = demands.get(gift_id, 0) + qty

        # 同一位會員同時在多個通路 / 多張線上訂單結帳：之後的點數、券、集點卡、推薦獎勵依序處理
        # (鎖的順序固定為「庫存 → 會員」，與取消訂單相同，避免交叉鎖死)
        if user is not None:
            _lock_member(user.id)

        # 交易中再讀一次開著的班別 (FOR SHARE)：剛好在關班的那一刻結帳，不會掛在已算完報表的班別上
        if not stock_node:
            shift_open = payment_service.open_shift_id(tenant_id, lock=True)
            if is_pos and pos_cfg.get('require_shift') and shift_open is None:
                raise AppError('班別剛關閉：請先在 POS 按「交班」輸入開班零用金後再結帳', 409, need_shift=True)

        # 紅利兌換品：先核銷會員已兌換的憑證，不足的份數才當場扣點
        reward_points_spent = 0
        claimed_vouchers, new_vouchers = [], []
        for ln in (ln for ln in lines if ln.is_reward):
            got = claim_vouchers_for_item(tenant_id, user.id, ln.menu_item.id, ln.quantity)
            claimed_vouchers += got
            missing = ln.quantity - len(got)
            if missing:
                cost = ln.menu_item.redeem_points
                if change_points(tenant_id, user.id, -cost * missing, 'reward_item') is None:
                    raise AppError(f'會員紅利點數不足（需 {cost * missing} 點），無法兌換【{ln.menu_item.name}】！')
                reward_points_spent += cost * missing
                new_vouchers += [RewardRedemption(tenant_id=tenant_id, user_id=user.id, menu_item_id=ln.menu_item.id,
                                                  points_cost=cost, status=VoucherStatus.USED) for _ in range(missing)]

        promotion_discount = min(promo_result.discount, subtotal)
        remaining = max(0, subtotal - promotion_discount)
        if promo_result.blocks_coupon and promo_code:
            promo_code = ''   # 此促銷活動不可與優惠券併用
        promo_discount, applied_code, applied_uc_id = _apply_promo(
            tenant_id, normalize_code(promo_code), remaining, user.id if user else None,
            guard=None if is_pos or is_delivery else (tenant_id, client_ip or '-'))
        remaining = max(0, remaining - promo_discount)

        # 會員等級折扣
        member_discount = 0
        tier = user.tier if user is not None and not is_delivery else None
        if tier is not None and (tier.discount_percent or 0) > 0 \
                and settings_service.get('member', tenant_id).get('tiers_enabled', True):
            member_discount = remaining * min(100, tier.discount_percent) / 100
            remaining = max(0, remaining - member_discount)

        # 整單折扣 (POS)：超過門檻或沒有權限時需主管核准
        manual_discount = 0
        if order_discount and is_pos:
            manual_discount, pct = _manual_discount(order_discount, remaining)
            if manual_discount > 0:
                limit = pos_cfg.get('discount_approval_percent') or 0
                from app.services.approval_service import has_permission
                if not has_permission('discount') and (limit == 0 or pct > limit):
                    if not approve:
                        raise AppError('整單折扣需要主管核准', 403, need_approval=True)
                    approved_by = approve('discount', '整單折扣')
                remaining = max(0, remaining - manual_discount)

        # 點數折抵 (受單筆上限限制，只能折整數元)
        setting = get_or_create_reward_setting(tenant_id, commit=False)
        points_used = points_discount = 0
        if user and setting.is_enabled and safe_int(use_points, 0) > 0:
            ppd = max(1, setting.points_per_dollar or 1)
            db.session.refresh(user, ['points'])
            max_cash = int(math.floor(remaining))
            if setting.max_discount_per_order:
                max_cash = min(max_cash, setting.max_discount_per_order)
            points_discount = min(safe_int(use_points, 0) // ppd, max_cash)
            points_used = points_discount * ppd
            # 送出時看到的點數已被其他通路 (點餐機、POS、線上、紅利商城) 用掉：結帳失敗並說明，不可只折一部分、用較高金額成立
            if points_used > (user.points or 0) or (
                    points_used and change_points(tenant_id, user.id, -points_used, 'redeem') is None):
                db.session.refresh(user, ['points'])
                raise AppError(f'會員點數已變動 (目前 {user.points or 0} 點，不足折抵 {points_used} 點)，'
                               f'可能剛在其他裝置、門市或線上使用，請重新確認後再結帳！', 409,
                               points_changed=True, remaining_points=user.points or 0)
        after_discounts = max(0, remaining - points_discount)

        # 服務費 (以折扣後金額計算)
        service_charge = 0
        pct = pos_cfg.get('service_charge_percent') or 0
        if pct > 0 and _service_charge_applies(order_source, order_type, pos_cfg):
            service_charge = int(math.floor(after_discounts * pct / 100 + 0.5))

        # 外送到府：依配送區域加外送費 (最低消費以商品小計計算)
        delivery_fee = 0
        if home is not None:
            from app.services import own_delivery_service
            delivery_fee = own_delivery_service.fee_for(home['zone'], int(subtotal), tenant_id)

        # 應付金額四捨五入到整數元，折扣總額以「小計 + 服務費 + 外送費 - 應付」反推，確保金額一致
        final_price = int(math.floor(after_discounts + 0.5)) + service_charge + delivery_fee
        if expected_total not in (None, '') and safe_int(expected_total, -1) != final_price:
            raise AppError(f'金額已變動：畫面顯示 ${safe_int(expected_total, 0)}，目前應付 ${final_price} '
                           f'(可能剛調整價格、優惠或點數)，已更新為最新金額，請確認後再送出！', 409, price_changed=True,
                           expected_total=safe_int(expected_total, 0), server_total=final_price,
                           line_prices=[ln.unit_price for ln in lines if not ln.is_gift])
        total_discount = max(0, subtotal + service_charge + delivery_fee - final_price)
        multiplier = tier.points_multiplier if tier is not None and (tier.points_multiplier or 0) > 0 else 1
        points_earned = int((final_price - delivery_fee) // max(1, setting.spend_per_point or 100) * multiplier) \
            if user and not is_delivery else 0
        if points_earned:
            change_points(tenant_id, user.id, points_earned, 'earn')

        cash_in = max(0, safe_int(cash_received, 0))
        pays_cash_at_pos = is_pos and payment == PaymentMethod.CASH
        if pays_cash_at_pos and 0 < cash_in < final_price:
            raise AppError(f'實收現金 (${cash_in}) 小於應付總額 (${final_price})！')
        actual_received = cash_in if (pays_cash_at_pos and cash_in >= final_price) else final_price
        cash_change = actual_received - final_price if pays_cash_at_pos else 0
        if tenders is not None:
            split_total = sum(t['amount'] for t in tenders)
            if split_total != final_price:
                raise AppError(f'混合付款金額合計 ${split_total} 與應付 ${final_price} 不符，請重新分配金額',
                               split_mismatch=True, server_total=final_price)
            cash_t = next((t for t in tenders if t['method'] == PaymentMethod.CASH), None)
            if cash_t is not None:   # cash_received 為現金這一筆實收 (0 = 剛好)
                given = cash_in or cash_t['amount']
                if given < cash_t['amount']:
                    raise AppError(f'現金實收 (${given}) 小於現金應付 (${cash_t["amount"]})！')
                cash_t['received'], cash_change = given, given - cash_t['amount']
            actual_received = final_price + cash_change

        table_input = str(table_input or '').strip()[:40]
        if table_input:
            table_display = f"{table_input} ({user.name})" if user and user.name not in table_input else table_input
        elif user:
            table_display = f"POS-{user.name}" if is_pos else user.name
        else:
            table_display = '櫃檯POS' if is_pos else '訪客'

        summary = ', '.join(f"{ln.menu_item.name} x{ln.quantity}" for ln in lines)
        log = db.session.get(CustomerLog, customer_log_id) if customer_log_id else None
        if log is None:
            log = CustomerLog(
                tenant_id=tenant_id,
                user_type=('POS會員' if user else 'POS訪客') if is_pos else ('會員' if user else '訪客'),
                identifier=(f"{user.name} ({user.phone})" if is_pos else user.name) if user else table_display[:100],
                login_at=now, logout_at=now, ip_address=client_ip,
                ordered_items=f"[{'POS ' if is_pos else ''}取餐#{pickup}] {summary}",
            )
            db.session.add(log)
        else:
            log.ordered_items, log.logout_at = summary, now

        order = Order(
            tenant_id=tenant_id, user_id=user.id if user else None, table_number=table_display[:50],
            total_price=final_price, payment_method=payment, order_type=order_type, order_source=order_source,
            cashier_username=cashier_username, cash_received=actual_received, cash_change=cash_change,
            need_cutlery=bool(need_cutlery), note=str(note or '')[:200], status=OrderStatus.PENDING,
            points_used=points_used, points_earned=points_earned, discount_amount=total_discount,
            coupon_code=applied_code, applied_user_coupon_id=applied_uc_id, pickup_number=pickup,
            customer_log=log, created_at=now, node_uuid=node_uuid, payment_status=payment_status,
            dining_table_id=dining_table_id, service_charge=service_charge,
            manual_discount=int(math.floor(manual_discount + 0.5)), promotion_discount=promotion_discount,
            member_discount=int(math.floor(member_discount + 0.5)), promotion_names='、'.join(promo_result.names)[:200],
            approved_by=approved_by, payment_reference=card_auth, card_last4=card_last4, is_rush=bool(is_rush),
            external_ref=str(external_ref or '')[:60], shift_id=shift_open, pickup_at=scheduled_at,
            delivery_fee=delivery_fee, delivery_address=home['address'] if home else '',
            delivery_phone=home['phone'] if home else '', delivery_zone=home['zone'] if home else '',
            delivery_status='pending' if home else '',
        )
        if client_order_id:
            order.uuid = client_order_id
        order.items = [
            OrderItem(tenant_id=tenant_id, menu_item_id=ln.menu_item.id, combo_option_id=ln.combo.id if ln.combo else None,
                      item_name=ln.menu_item.name, price=ln.unit_price, quantity=ln.quantity,
                      customization=ln.customization, is_reward=ln.is_reward, is_gift=ln.is_gift,
                      original_price=ln.original_price, combo_sides=_combo_sides_snapshot(ln))
            for ln in lines
        ]
        db.session.add(order)
        db.session.add_all(new_vouchers)
        db.session.flush()
        for v in new_vouchers:
            v.order_id = order.id
        if user is not None:  # 本次結帳的點數異動連結到訂單
            db.session.execute(
                update(PointLedger).where(PointLedger.user_id == user.id, PointLedger.order_id.is_(None),
                                          PointLedger.sync_rev < 0)
                .values(order_id=order.id).execution_options(synchronize_session=False))
        if pass_lines:   # 寄杯兌換扣次數 (條件式：同時在兩個通路兌換同一張不會扣成負數)
            from app.services import pass_service
            for ln, mp in pass_lines:
                pass_service.redeem(mp, ln.quantity, order.id, cashier_username)
        if claimed_vouchers:
            db.session.execute(
                update(RewardRedemption).where(RewardRedemption.id.in_(claimed_vouchers))
                .values(order_id=order.id).execution_options(synchronize_session=False))

        # 儲值金扣款
        if payment == PaymentMethod.STORED_VALUE and final_price > 0:
            if wallet_service.change_balance(tenant_id, user.id, -final_price, 'pay', order_id=order.id,
                                             note=f'訂單 #{order.id}') is None:
                raise AppError(f'儲值金餘額不足 (需 ${final_price})，請先加值或改用其他付款方式！', insufficient_wallet=True)
            order.wallet_used = final_price

        # 混合付款：儲值金 / 禮物卡依各自金額扣款
        if tenders is not None:
            from app.services import giftcard_service
            for t in tenders:
                if t['method'] == PaymentMethod.STORED_VALUE:
                    if wallet_service.change_balance(tenant_id, user.id, -t['amount'], 'pay', order_id=order.id,
                                                     note=f'訂單 #{order.id} (混合付款)') is None:
                        raise AppError(f'儲值金餘額不足 (需 ${t["amount"]})，請調整混合付款金額！', insufficient_wallet=True)
                    order.wallet_used = t['amount']
                elif t['method'] == PaymentMethod.GIFT_CARD:
                    giftcard_service.charge(tenant_id, t['gift_code'], t['amount'], order.id, cashier_username)
                    order.payment_reference = t['gift_code']   # 退款時退回這張卡
                elif t['method'] == PaymentMethod.CREDIT_CARD and not order.card_last4:
                    order.card_last4 = t['last4']

        # 禮物卡扣款
        if payment == PaymentMethod.GIFT_CARD:
            from app.services import giftcard_service
            if final_price > 0:
                giftcard_service.charge(tenant_id, gift_code, final_price, order.id, cashier_username)
            order.payment_reference = gift_code
            card_auth = f'{gift_code[:4]}****{gift_code[-4:]}'   # 收款紀錄只留遮罩後的卡號

        # 原物料扣料
        if not stock_node:
            inventory_service.consume(tenant_id, order.id, demands)

        # 收款紀錄 / 發票 / 列印 (當場收款的訂單；桌邊點餐於桌位結帳、線上於付款完成、LINE Pay 於扣款成功時處理)
        if payment_status == '' and tenders is not None:   # 混合付款：每一種付款方式各一筆收款紀錄
            for t in tenders:
                payment_service.record_payment(
                    order, PaymentKind.SALE, t['method'], t['amount'], received=t.get('received', t['amount']),
                    change=(t.get('received', t['amount']) - t['amount']), reference=t['reference'],
                    card_last4=t['last4'], staff=cashier_username, note='混合付款')
            payment_service.after_paid(order, invoice_info)
        elif payment_status == '':
            payment_service.record_payment(
                order, PaymentKind.SALE, payment, final_price, received=actual_received, change=cash_change,
                reference=card_auth, card_last4=card_last4, staff=cashier_username or 'kiosk')
            payment_service.after_paid(order, invoice_info)
        elif payment_status == 'pending':
            payment_service.maybe_create_invoice(order, invoice_info, status=InvoiceStatus.WAITING)
        elif payment_status == 'unpaid':
            from app.services import deferred, print_service
            deferred.defer(print_service.print_order, order.id, tenant_id, False)   # 桌邊點餐：只印廚房單

        if user is not None and payment_status in ('', 'paid', 'settled'):
            user.total_spent = (user.total_spent or 0) + final_price
            wallet_service.refresh_tier(user)

        # 集點卡、推薦好友 (會員訂單)
        if user is not None and payment_status != 'authorizing':
            from app.services import loyalty_service
            loyalty_service.on_checkout(tenant_id, user, order, lines, final_price)

        if is_pos:
            record_staff_audit(
                'POS_CREATE_ORDER', target_order_id=order.id, target_user_id=order.user_id, tenant_id=tenant_id,
                points_delta=points_earned - points_used - reward_points_spent,
                details=(f"POS 結帳單號 #{order.id} (取餐 #{pickup:03d}) | 顧客:{table_display} | 付款:{payment} "
                         f"應收:${final_price} 實收:${actual_received} 找零:${cash_change} | 兌換扣點:{reward_points_spent} "
                         f"折抵扣點:{points_used} 贈點:{points_earned} 優惠碼:{applied_code or '無'}"
                         + (f" | 整單折扣:${order.manual_discount}" if order.manual_discount else '')
                         + (f" | 服務費:${service_charge}" if service_charge else '')
                         + (f" | 核准:{approved_by}" if approved_by and approved_by != cashier_username else '')),
            )
        db.session.commit()
    except IntegrityError:
        # 同一個 client_order_id 同時送出兩次：後到的撞上唯一索引，回傳先完成的那張
        db.session.rollback()
        existing = Order.query.filter_by(uuid=client_order_id).first() if client_order_id else None
        if existing is None:
            if dining_table_id is not None and db.session.get(DiningTable, dining_table_id) is None:
                raise AppError('此桌位剛被店家移除，請洽服務人員或掃描其他桌的 QR code！', 409)
            raise
        return _replay(existing, **owner), True
    except Exception:
        db.session.rollback()
        raise

    if one_time_key:
        order = payment_service.charge_linepay(order.id, one_time_key)
    invalidate_ai_advice(tenant_id)
    order_event_bus.notify(tenant_id)
    return order, False
