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

def find_by_client_order_id(tenant_id, client_order_id):
    """同店內以前端送單識別找訂單 (舊資料的 uuid 即當時的 client_order_id，遷移時已補到新欄位)"""
    client_order_id = str(client_order_id or '').strip()[:36]
    if not client_order_id:
        return None
    return Order.query.filter_by(tenant_id=tenant_id, client_order_id=client_order_id).first()


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


class Checkout:
    """
    一次結帳的所有輸入與中間結果 (各階段函式共用)。欄位在各階段依序填入：
    _prepare (正規化輸入) → _validate (付款方式 / 通路 / 品項 / 會員 / 核准) → _reserve (取號、扣庫存、鎖會員、班別)
    → _price (憑證 / 促銷 / 券 / 等級 / 整單折扣 / 點數 / 服務費 / 外送費 / 應付金額) → _persist (建單、扣款、收款紀錄、稽核)
    → _after_commit (LINE Pay 扣款、通知)。
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


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
    c = Checkout(**{k: v for k, v in locals().items() if k != 'c'})
    replayed = _prepare(c)
    if replayed is not None:
        return replayed, True
    _validate(c)
    try:
        # ---- 交易開始：先取號 (鎖住今日計數列)，之後的寫入都在同一筆交易 ----
        _reserve(c)
        _price(c)
        _persist(c)
    except IntegrityError:
        # 同一個 client_order_id 同時送出兩次：後到的撞上唯一索引，回傳先完成的那張
        db.session.rollback()
        existing = find_by_client_order_id(c.tenant_id, c.client_order_id) if c.client_order_id else None
        if existing is None:
            if c.dining_table_id is not None and db.session.get(DiningTable, c.dining_table_id) is None:
                raise AppError('此桌位剛被店家移除，請洽服務人員或掃描其他桌的 QR code！', 409)
            raise
        return _replay(existing, **c.owner), True
    except Exception:
        db.session.rollback()
        raise

    return _after_commit(c), False


def _prepare(c):
    """正規化輸入 (桌邊點餐固定內用 / 現金)；同一個 client_order_id 已結帳過時回傳原訂單 (重送)"""
    c.now = c.now or datetime.now()
    c.dining_table_id = c.dining_table.id if c.dining_table is not None else None
    if c.dining_table is not None:
        c.order_type, c.need_cutlery, c.table_input = OrderType.DINE_IN, False, c.dining_table.label
        c.order_source = OrderSource.TABLE
        c.payment_method = PaymentMethod.CASH  # 實際付款方式在櫃檯結帳時記錄
    c.client_order_id = str(c.client_order_id or '').strip()[:36] or None
    c.owner = dict(user_id=c.user_id, customer_log_id=c.customer_log_id, cashier_username=c.cashier_username)
    if c.client_order_id:
        existing = find_by_client_order_id(c.tenant_id, c.client_order_id)
        if existing is not None:
            return _replay(existing, **c.owner)
    return None


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
    if c.is_delivery:
        c.order_type, c.payment = OrderType.DELIVERY, PaymentMethod.PLATFORM
    elif c.order_type == OrderType.DELIVERY:
        raise AppError('外送訂單請由外送平台串接或 POS「外送單」登錄')
    elif c.payment == PaymentMethod.PLATFORM:
        raise AppError('平台代收只用於外送平台訂單')
    elif c.dining_table is None and not c.is_hub:
        allowed = payment_service.allowed_methods('pos' if c.is_pos else 'kiosk')
        wallet_ok = c.payment == PaymentMethod.STORED_VALUE and settings_service.get('member', c.tenant_id).get('wallet_enabled')
        gift_ok = c.payment == PaymentMethod.GIFT_CARD and c.is_pos and settings_service.get('gift_card', c.tenant_id).get('enabled')
        split_ok = c.payment == PaymentMethod.SPLIT and c.is_pos   # 各筆付款方式在 parse_tenders 檢查
        if c.payment not in allowed and not wallet_ok and not gift_ok and not split_ok:
            raise AppError(f'此點餐管道未開放「{c.payment}」付款，請改用其他付款方式')

    c.home = None
    if c.order_type == OrderType.HOME_DELIVERY:   # 自有外送 (外送到府)
        if c.dining_table is not None or not (c.is_hub or c.is_pos):
            raise AppError('外送到府請由線上點餐或 POS 電話訂餐')
        from app.services import own_delivery_service
        c.home = own_delivery_service.validate(c.delivery, 'online' if c.is_hub else 'pos', c.tenant_id)

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
    c.lines = _parse_lines(c.items, c.tenant_id, c.price_channel, c.now, allow_override=c.is_pos)
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

    # 促銷活動 (外送平台訂單以平台價格為準，不套用本店促銷)
    c.promo_result = promotion_service.PromotionResult() if c.is_delivery else promotion_service.evaluate(
        c.lines, promotion_service.channel_for(c.order_source, c.order_type, c.is_hub), has_member=c.user is not None, now=c.now)
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
        if current_app.config.get('ONLINE_PAYMENT', True):
            from app.services import payment_ecpay
            payment_ecpay.require_keys_allowed()   # 測試金鑰未被允許時不成立待付款訂單 (付不了，只會逾時取消)
            c.payment_status = 'pending'  # 付款完成前不送分店 (見 payment_ecpay)
    else:
        c.node_uuid = sync_runtime.local_node_uuid()
        if c.dining_table is not None:
            c.payment_status = 'unpaid'

    # 付款細節 (交易外先檢查，避免取號後才發現資料不完整)
    c.card_auth, c.card_last4, c.one_time_key = '', '', ''
    if c.payment == PaymentMethod.CREDIT_CARD and not c.is_hub and c.dining_table is None:
        c.card_auth, c.card_last4 = payment_service.validate_card_details(c.payment_info, c.is_pos)
    if c.payment == PaymentMethod.LINE_PAY and c.is_pos:
        from app.services import linepay
        if linepay.is_enabled(c.tenant_id):
            c.one_time_key = linepay.normalize_one_time_key((c.payment_info or {}).get('one_time_key'))
            c.payment_status = 'authorizing'   # 建單後立即向 LINE Pay 扣款，失敗則取消
    if c.invoice_info:
        from app.services import einvoice_ecpay
        einvoice_ecpay.normalize_invoice_info(c.invoice_info, settings_service.get('einvoice', c.tenant_id))
    # 線上點餐的預約取餐時間 (空白 = 盡快)
    c.scheduled_at = None
    if isinstance(c.pickup_at, datetime):   # 系統內部建立的預約單 (團體預購，時間已在建立時驗證)
        c.scheduled_at = c.pickup_at
    elif c.is_hub and c.pickup_at:
        from app.services import pickup_service
        c.scheduled_at = pickup_service.parse(c.pickup_at, c.tenant_id)


def _reserve(c):
    """交易內：取號 (鎖住今日計數列)、扣庫存 (不足時整筆失敗)、贈品售完處理、鎖會員列、重讀開著的班別"""
    c.pickup = 0 if c.stock_node else next_pickup_number(c.tenant_id)

    short_id, c.dropped_gifts = _deduct_stock(c.tenant_id, c.demands, c.gift_demands,
                                            node_id=c.stock_node.id if c.stock_node else None)
    if short_id is not None:
        db.session.rollback()
        item = db.session.get(MenuItem, short_id)
        name = item.name if item else ''
        stock, sold_out = (item.stock, item.is_sold_out) if item else (0, True)
        if c.stock_node is not None:
            snap = db.session.get(NodeStock, (c.stock_node.id, short_id))
            stock, sold_out = (snap.stock, snap.is_sold_out) if snap else (0, True)
        msg = (f'餐點或配餐【{name}】已售完，請移除後再結帳！' if sold_out or stock <= 0
               else f'餐點或配餐【{name}】庫存剩 {stock} 份，庫存不足！')
        raise AppError(msg, insufficient_stock=True, sold_out_items=[name])

    if c.dropped_gifts:   # 促銷贈品剛好被別的訂單拿走最後一份：訂單照常成立，只是不附贈
        gift_names = '、'.join(sorted({ln.menu_item.name for ln in c.lines if ln.is_gift and ln.menu_item.id in c.dropped_gifts}))
        c.lines = [ln for ln in c.lines if not (ln.is_gift and ln.menu_item.id in c.dropped_gifts)]
        c.note = f"{c.note or ''} 【贈品已送完：{gift_names}】".strip()
    for gift_id, qty in c.gift_demands.items():
        if gift_id not in c.dropped_gifts:
            c.demands[gift_id] = c.demands.get(gift_id, 0) + qty

    # 同一位會員同時在多個通路 / 多張線上訂單結帳：之後的點數、券、集點卡、推薦獎勵依序處理
    # (鎖的順序固定為「庫存 → 會員」，與取消訂單相同，避免交叉鎖死)
    if c.user is not None:
        _lock_member(c.user.id)

    # 交易中再讀一次開著的班別 (FOR SHARE)：剛好在關班的那一刻結帳，不會掛在已算完報表的班別上
    if not c.stock_node:
        c.shift_open = payment_service.open_shift_id(c.tenant_id, lock=True)
        if c.is_pos and c.pos_cfg.get('require_shift') and c.shift_open is None:
            raise AppError('班別剛關閉：請先在 POS 按「交班」輸入開班零用金後再結帳', 409, need_shift=True)


def _price(c):
    """
    交易內的金額計算：紅利憑證 / 扣點 → 促銷 → 優惠券 → 會員等級 → 整單折扣 (核准) → 點數折抵 → 服務費 → 外送費
    → 應付金額 (與畫面金額比對) → 贈點 → 現金 / 混合付款金額。
    """
    # 紅利兌換品：先核銷會員已兌換的憑證，不足的份數才當場扣點
    c.reward_points_spent = 0
    c.claimed_vouchers, c.new_vouchers = [], []
    for ln in (ln for ln in c.lines if ln.is_reward):
        got = claim_vouchers_for_item(c.tenant_id, c.user.id, ln.menu_item.id, ln.quantity)
        c.claimed_vouchers += got
        missing = ln.quantity - len(got)
        if missing:
            cost = ln.menu_item.redeem_points
            if change_points(c.tenant_id, c.user.id, -cost * missing, 'reward_item') is None:
                raise AppError(f'會員紅利點數不足（需 {cost * missing} 點），無法兌換【{ln.menu_item.name}】！')
            c.reward_points_spent += cost * missing
            c.new_vouchers += [RewardRedemption(tenant_id=c.tenant_id, user_id=c.user.id, menu_item_id=ln.menu_item.id,
                                              points_cost=cost, status=VoucherStatus.USED) for _ in range(missing)]

    c.promotion_discount = min(c.promo_result.discount, c.subtotal)
    c.remaining = max(0, c.subtotal - c.promotion_discount)
    if c.promo_result.blocks_coupon and c.promo_code:
        c.promo_code = ''   # 此促銷活動不可與優惠券併用
    c.promo_discount, c.applied_code, c.applied_uc_id = _apply_promo(
        c.tenant_id, normalize_code(c.promo_code), c.remaining, c.user.id if c.user else None,
        guard=None if c.is_pos or c.is_delivery else (c.tenant_id, c.client_ip or '-'))
    c.remaining = max(0, c.remaining - c.promo_discount)

    # 會員等級折扣
    c.member_discount = 0
    c.tier = c.user.tier if c.user is not None and not c.is_delivery else None
    if c.tier is not None and (c.tier.discount_percent or 0) > 0 \
            and settings_service.get('member', c.tenant_id).get('tiers_enabled', True):
        c.member_discount = c.remaining * min(100, c.tier.discount_percent) / 100
        c.remaining = max(0, c.remaining - c.member_discount)

    # 整單折扣 (POS)：超過門檻或沒有權限時需主管核准
    c.manual_discount = 0
    if c.order_discount and c.is_pos:
        c.manual_discount, pct = _manual_discount(c.order_discount, c.remaining)
        if c.manual_discount > 0:
            limit = c.pos_cfg.get('discount_approval_percent') or 0
            from app.services.approval_service import has_permission
            if not has_permission('discount') and (limit == 0 or pct > limit):
                if not c.approve:
                    raise AppError('整單折扣需要主管核准', 403, need_approval=True)
                c.approved_by = c.approve('discount', '整單折扣')
            c.remaining = max(0, c.remaining - c.manual_discount)

    # 點數折抵 (受單筆上限限制，只能折整數元)
    c.setting = get_or_create_reward_setting(c.tenant_id, commit=False)
    c.points_used = c.points_discount = 0
    if c.user and c.setting.is_enabled and safe_int(c.use_points, 0) > 0:
        ppd = max(1, c.setting.points_per_dollar or 1)
        db.session.refresh(c.user, ['points'])
        max_cash = int(math.floor(c.remaining))
        if c.setting.max_discount_per_order:
            max_cash = min(max_cash, c.setting.max_discount_per_order)
        c.points_discount = min(safe_int(c.use_points, 0) // ppd, max_cash)
        c.points_used = c.points_discount * ppd
        # 送出時看到的點數已被其他通路 (點餐機、POS、線上、紅利商城) 用掉：結帳失敗並說明，不可只折一部分、用較高金額成立
        if c.points_used > (c.user.points or 0) or (
                c.points_used and change_points(c.tenant_id, c.user.id, -c.points_used, 'redeem') is None):
            db.session.refresh(c.user, ['points'])
            raise AppError(f'會員點數已變動 (目前 {c.user.points or 0} 點，不足折抵 {c.points_used} 點)，'
                           f'可能剛在其他裝置、門市或線上使用，請重新確認後再結帳！', 409,
                           points_changed=True, remaining_points=c.user.points or 0)
    c.after_discounts = max(0, c.remaining - c.points_discount)

    # 服務費 (以折扣後金額計算)
    c.service_charge = 0
    pct = c.pos_cfg.get('service_charge_percent') or 0
    if pct > 0 and _service_charge_applies(c.order_source, c.order_type, c.pos_cfg):
        c.service_charge = int(math.floor(c.after_discounts * pct / 100 + 0.5))

    # 外送到府：依配送區域加外送費 (最低消費以商品小計計算)
    c.delivery_fee = 0
    if c.home is not None:
        from app.services import own_delivery_service
        c.delivery_fee = own_delivery_service.fee_for(c.home['zone'], int(c.subtotal), c.tenant_id)

    # 應付金額四捨五入到整數元，折扣總額以「小計 + 服務費 + 外送費 - 應付」反推，確保金額一致
    c.final_price = int(math.floor(c.after_discounts + 0.5)) + c.service_charge + c.delivery_fee
    if c.expected_total not in (None, '') and safe_int(c.expected_total, -1) != c.final_price:
        raise AppError(f'金額已變動：畫面顯示 ${safe_int(c.expected_total, 0)}，目前應付 ${c.final_price} '
                       f'(可能剛調整價格、優惠或點數)，已更新為最新金額，請確認後再送出！', 409, price_changed=True,
                       expected_total=safe_int(c.expected_total, 0), server_total=c.final_price,
                       line_prices=[ln.unit_price for ln in c.lines if not ln.is_gift])
    c.total_discount = max(0, c.subtotal + c.service_charge + c.delivery_fee - c.final_price)
    multiplier = c.tier.points_multiplier if c.tier is not None and (c.tier.points_multiplier or 0) > 0 else 1
    c.points_earned = int((c.final_price - c.delivery_fee) // max(1, c.setting.spend_per_point or 100) * multiplier) \
        if c.user and not c.is_delivery else 0
    if c.points_earned:
        change_points(c.tenant_id, c.user.id, c.points_earned, 'earn')

    c.cash_in = max(0, safe_int(c.cash_received, 0))
    c.pays_cash_at_pos = c.is_pos and c.payment == PaymentMethod.CASH
    if c.pays_cash_at_pos and 0 < c.cash_in < c.final_price:
        raise AppError(f'實收現金 (${c.cash_in}) 小於應付總額 (${c.final_price})！')
    c.actual_received = c.cash_in if (c.pays_cash_at_pos and c.cash_in >= c.final_price) else c.final_price
    c.cash_change = c.actual_received - c.final_price if c.pays_cash_at_pos else 0
    if c.tenders is not None:
        split_total = sum(t['amount'] for t in c.tenders)
        if split_total != c.final_price:
            raise AppError(f'混合付款金額合計 ${split_total} 與應付 ${c.final_price} 不符，請重新分配金額',
                           split_mismatch=True, server_total=c.final_price)
        cash_t = next((t for t in c.tenders if t['method'] == PaymentMethod.CASH), None)
        if cash_t is not None:   # c.cash_received 為現金這一筆實收 (0 = 剛好)
            given = c.cash_in or cash_t['amount']
            if given < cash_t['amount']:
                raise AppError(f'現金實收 (${given}) 小於現金應付 (${cash_t["amount"]})！')
            cash_t['received'], c.cash_change = given, given - cash_t['amount']
        c.actual_received = c.final_price + c.cash_change


def _persist(c):
    """
    交易內的寫入：顧客紀錄、訂單與明細、憑證 / 點數帳連結、寄杯扣次、儲值金 / 禮物卡扣款、原料扣料、
    收款紀錄 / 發票 / 列印、累積消費與等級、集點 / 推薦、稽核，最後 commit。
    """
    c.table_input = str(c.table_input or '').strip()[:40]
    if c.table_input:
        c.table_display = f"{c.table_input} ({c.user.name})" if c.user and c.user.name not in c.table_input else c.table_input
    elif c.user:
        c.table_display = f"POS-{c.user.name}" if c.is_pos else c.user.name
    else:
        c.table_display = '櫃檯POS' if c.is_pos else '訪客'

    c.summary = ', '.join(f"{ln.menu_item.name} x{ln.quantity}" for ln in c.lines)
    c.log = db.session.get(CustomerLog, c.customer_log_id) if c.customer_log_id else None
    if c.log is None:
        c.log = CustomerLog(
            tenant_id=c.tenant_id,
            user_type=('POS會員' if c.user else 'POS訪客') if c.is_pos else ('會員' if c.user else '訪客'),
            identifier=(f"{c.user.name} ({c.user.phone})" if c.is_pos else c.user.name) if c.user else c.table_display[:100],
            login_at=c.now, logout_at=c.now, ip_address=c.client_ip,
            ordered_items=f"[{'POS ' if c.is_pos else ''}取餐#{c.pickup}] {c.summary}",
        )
        db.session.add(c.log)
    else:
        c.log.ordered_items, c.log.logout_at = c.summary, c.now

    c.order = Order(
        tenant_id=c.tenant_id, user_id=c.user.id if c.user else None, table_number=c.table_display[:50],
        total_price=c.final_price, payment_method=c.payment, order_type=c.order_type, order_source=c.order_source,
        cashier_username=c.cashier_username, cash_received=c.actual_received, cash_change=c.cash_change,
        need_cutlery=bool(c.need_cutlery), note=str(c.note or '')[:200], status=OrderStatus.PENDING,
        points_used=c.points_used, points_earned=c.points_earned, discount_amount=c.total_discount,
        coupon_code=c.applied_code, applied_user_coupon_id=c.applied_uc_id, pickup_number=c.pickup,
        customer_log=c.log, created_at=c.now, node_uuid=c.node_uuid, payment_status=c.payment_status,
        dining_table_id=c.dining_table_id, service_charge=c.service_charge,
        manual_discount=int(math.floor(c.manual_discount + 0.5)), promotion_discount=c.promotion_discount,
        member_discount=int(math.floor(c.member_discount + 0.5)), promotion_names='、'.join(c.promo_result.names)[:200],
        approved_by=c.approved_by, payment_reference=c.card_auth, card_last4=c.card_last4, is_rush=bool(c.is_rush),
        external_ref=str(c.external_ref or '')[:60], shift_id=c.shift_open, pickup_at=c.scheduled_at,
        delivery_fee=c.delivery_fee, delivery_address=c.home['address'] if c.home else '',
        delivery_phone=c.home['phone'] if c.home else '', delivery_zone=c.home['zone'] if c.home else '',
        delivery_status='pending' if c.home else '',
    )
    if c.client_order_id:   # 前端去重識別存獨立欄位；uuid 由伺服器產生 (追蹤頁憑證不可由前端自訂)
        c.order.client_order_id = c.client_order_id
    c.order.items = [
        OrderItem(tenant_id=c.tenant_id, menu_item_id=ln.menu_item.id, combo_option_id=ln.combo.id if ln.combo else None,
                  item_name=ln.menu_item.name, price=ln.unit_price, quantity=ln.quantity,
                  customization=ln.customization, is_reward=ln.is_reward, is_gift=ln.is_gift,
                  original_price=ln.original_price, combo_sides=_combo_sides_snapshot(ln))
        for ln in c.lines
    ]
    db.session.add(c.order)
    db.session.add_all(c.new_vouchers)
    db.session.flush()
    for v in c.new_vouchers:
        v.order_id = c.order.id
    if c.user is not None:  # 本次結帳的點數異動連結到訂單
        db.session.execute(
            update(PointLedger).where(PointLedger.user_id == c.user.id, PointLedger.order_id.is_(None),
                                      PointLedger.sync_rev < 0)
            .values(order_id=c.order.id).execution_options(synchronize_session=False))
    if c.pass_lines:   # 寄杯兌換扣次數 (條件式：同時在兩個通路兌換同一張不會扣成負數)
        from app.services import pass_service
        for ln, mp in c.pass_lines:
            pass_service.redeem(mp, ln.quantity, c.order.id, c.cashier_username)
    if c.claimed_vouchers:
        db.session.execute(
            update(RewardRedemption).where(RewardRedemption.id.in_(c.claimed_vouchers))
            .values(order_id=c.order.id).execution_options(synchronize_session=False))

    # 儲值金扣款
    if c.payment == PaymentMethod.STORED_VALUE and c.final_price > 0:
        if wallet_service.change_balance(c.tenant_id, c.user.id, -c.final_price, 'pay', order_id=c.order.id,
                                         note=f'訂單 #{c.order.id}') is None:
            raise AppError(f'儲值金餘額不足 (需 ${c.final_price})，請先加值或改用其他付款方式！', insufficient_wallet=True)
        c.order.wallet_used = c.final_price

    # 混合付款：儲值金 / 禮物卡依各自金額扣款
    if c.tenders is not None:
        from app.services import giftcard_service
        for t in c.tenders:
            if t['method'] == PaymentMethod.STORED_VALUE:
                if wallet_service.change_balance(c.tenant_id, c.user.id, -t['amount'], 'pay', order_id=c.order.id,
                                                 note=f'訂單 #{c.order.id} (混合付款)') is None:
                    raise AppError(f'儲值金餘額不足 (需 ${t["amount"]})，請調整混合付款金額！', insufficient_wallet=True)
                c.order.wallet_used = t['amount']
            elif t['method'] == PaymentMethod.GIFT_CARD:
                giftcard_service.charge(c.tenant_id, t['gift_code'], t['amount'], c.order.id, c.cashier_username)
                c.order.payment_reference = t['gift_code']   # 退款時退回這張卡
            elif t['method'] == PaymentMethod.CREDIT_CARD and not c.order.card_last4:
                c.order.card_last4 = t['last4']

    # 禮物卡扣款
    if c.payment == PaymentMethod.GIFT_CARD:
        from app.services import giftcard_service
        if c.final_price > 0:
            giftcard_service.charge(c.tenant_id, c.gift_code, c.final_price, c.order.id, c.cashier_username)
        c.order.payment_reference = c.gift_code
        c.card_auth = f'{c.gift_code[:4]}****{c.gift_code[-4:]}'   # 收款紀錄只留遮罩後的卡號

    # 原物料扣料
    if not c.stock_node:
        inventory_service.consume(c.tenant_id, c.order.id, c.demands)

    # 收款紀錄 / 發票 / 列印 (當場收款的訂單；桌邊點餐於桌位結帳、線上於付款完成、LINE Pay 於扣款成功時處理)
    if c.payment_status == '' and c.tenders is not None:   # 混合付款：每一種付款方式各一筆收款紀錄
        for t in c.tenders:
            payment_service.record_payment(
                c.order, PaymentKind.SALE, t['method'], t['amount'], received=t.get('received', t['amount']),
                change=(t.get('received', t['amount']) - t['amount']), reference=t['reference'],
                card_last4=t['last4'], staff=c.cashier_username, note='混合付款')
        payment_service.after_paid(c.order, c.invoice_info)
    elif c.payment_status == '':
        payment_service.record_payment(
            c.order, PaymentKind.SALE, c.payment, c.final_price, received=c.actual_received, change=c.cash_change,
            reference=c.card_auth, card_last4=c.card_last4, staff=c.cashier_username or 'kiosk')
        payment_service.after_paid(c.order, c.invoice_info)
    elif c.payment_status == 'pending':
        payment_service.maybe_create_invoice(c.order, c.invoice_info, status=InvoiceStatus.WAITING)
    elif c.payment_status == 'unpaid':
        from app.services import deferred, print_service
        deferred.defer(print_service.print_order, c.order.id, c.tenant_id, False)   # 桌邊點餐：只印廚房單

    if c.user is not None and c.payment_status in ('', 'paid', 'settled'):
        c.user.total_spent = (c.user.total_spent or 0) + c.final_price
        wallet_service.refresh_tier(c.user)

    # 集點卡、推薦好友 (會員訂單)
    if c.user is not None and c.payment_status != 'authorizing':
        from app.services import loyalty_service
        loyalty_service.on_checkout(c.tenant_id, c.user, c.order, c.lines, c.final_price)

    if c.is_pos:
        record_staff_audit(
            'POS_CREATE_ORDER', target_order_id=c.order.id, target_user_id=c.order.user_id, tenant_id=c.tenant_id,
            points_delta=c.points_earned - c.points_used - c.reward_points_spent,
            details=(f"POS 結帳單號 #{c.order.id} (取餐 #{c.pickup:03d}) | 顧客:{c.table_display} | 付款:{c.payment} "
                     f"應收:${c.final_price} 實收:${c.actual_received} 找零:${c.cash_change} | 兌換扣點:{c.reward_points_spent} "
                     f"折抵扣點:{c.points_used} 贈點:{c.points_earned} 優惠碼:{c.applied_code or '無'}"
                     + (f" | 整單折扣:${c.order.manual_discount}" if c.order.manual_discount else '')
                     + (f" | 服務費:${c.service_charge}" if c.service_charge else '')
                     + (f" | 核准:{c.approved_by}" if c.approved_by and c.approved_by != c.cashier_username else '')),
        )
    db.session.commit()


def _after_commit(c):
    """交易提交後：LINE Pay 當場扣款 (失敗則取消訂單)、AI 建議快取失效、即時通知；回傳訂單"""
    if c.one_time_key:
        c.order = payment_service.charge_linepay(c.order.id, c.one_time_key)
    invalidate_ai_advice(c.tenant_id)
    order_event_bus.notify(c.tenant_id)
    return c.order
