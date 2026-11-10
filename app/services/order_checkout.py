"""
原子結帳 process_checkout (由 order_service 拆出；Kiosk / POS / 桌邊 / 線上 / 外送 / 團購 / 斷線補送共用)。
併發控制說明見 app/services/order_service.py。
"""
import json
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.domain.order.service import OrderDomainService
from app.domain.pricing.service import PricingDomainService
from app.errors import AppError
from app.extensions import db
from app.models.enums import (
    OrderSource, OrderStatus, OrderType, PaymentMethod, VoucherStatus,
)
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.payment import InvoiceStatus, PaymentKind
from app.models.sync import NodeStock, PointLedger
from app.models.table import DiningTable
from app.models.user import CustomerLog, RewardRedemption
from app.services import inventory_service, payment_service, settings_service, wallet_service
from app.services.ai_service import invalidate_ai_advice
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.order_pricing import (
    _apply_promo, _combo_sides_snapshot, _lock_member, _manual_discount, _service_charge_applies, normalize_code,
)
from app.services.order_stock import _deduct_stock, business_day, next_pickup_number
from app.services.order_validate import _validate
from app.services.points_service import change_points
from app.services.reward_service import get_or_create_reward_setting
from app.services.member_rewards import PAID_STATUSES, grant_paid_rewards
from app.services.voucher_service import claim_vouchers_for_item
from app.utils import safe_int

logger = logging.getLogger(__name__)

# POS 實收現金最多可超過應付金額多少 (找零上限)：避免誤按 / 條碼槍誤掃出天文數字，
# 存入 Integer 欄位 (PostgreSQL 上限約 21 億) 時結帳失敗或同步卡住
MAX_CASH_CHANGE = 100000


class _DiscountApprovalNeeded(Exception):
    """交易內發現整單折扣需主管核准：撤銷交易後在交易外核准，再重新結帳 (見 process_checkout)"""


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
    # 原單尚未確認付款 / 已取消 (例如 LINE Pay 扣款中或扣款失敗)：不可回傳「已結帳」的收據
    if (order.payment_status or '') == 'authorizing':
        raise AppError(f'訂單 #{order.id} 的 LINE Pay 付款仍在處理中，請稍候到「當日訂單」確認結果，勿重複收款！', 409,
                       still_authorizing=True, order_id=order.id)
    if order.status == OrderStatus.CANCELLED and order.order_source == OrderSource.POS:
        raise AppError(f'訂單 #{order.id} 已取消 (付款未完成)，請重新結帳！', 409, payment_failed=True,
                       order_id=order.id)
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
                     external_ref='', now=None, pickup_at=None, delivery=None, expected_total=None,
                     skip_intake=False):
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
    skip_intake：不做「暫停接單 / 每時段上限」檢查 (斷線暫存單補送：點餐機當時已收單)。
    payment_info：刷卡授權碼 / 卡號末四碼 / LINE Pay 付款碼；invoice_info：電子發票載具 / 統編 / 捐贈。
    """
    c = Checkout(**{k: v for k, v in locals().items() if k != 'c'})
    replayed = _prepare(c)
    if replayed is not None:
        return replayed, True
    _validate(c)
    # _reserve 會改動品項 (移除送完的贈品)、備註與庫存需求：需要重新結帳時還原
    snapshot = (list(c.lines), c.note, dict(c.demands))
    c.discount_approver = None
    while True:
        try:
            # ---- 交易開始：先取號 (鎖住今日計數列)，之後的寫入都在同一筆交易 ----
            _reserve(c)
            _price(c)
            _persist(c)
            break
        except _DiscountApprovalNeeded:
            # 整單折扣需主管核准：核准會寫入並 commit 錯誤次數 / 清除鎖定，不可在結帳交易中途進行
            # (會把已取號、扣庫存、核銷的券 / 點數一起提交)。先撤銷本次交易，在交易外核准後重新結帳。
            db.session.rollback()
            if c.discount_approver is not None:
                raise AppError('整單折扣核准失敗，請重新結帳', 409) from None
            c.discount_approver = c.approve('discount', '整單折扣') or ''
            c.lines, c.note, c.demands = list(snapshot[0]), snapshot[1], dict(snapshot[2])
            continue
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


def _reserve(c):
    """交易內：取號 (鎖住今日計數列)、扣庫存 (不足時整筆失敗)、贈品售完處理、鎖會員列、重讀開著的班別"""
    # 系統內部建立的預約單 (團體預購) 取餐日晚於今天：用取餐營業日的計數器取號，取餐當天不會和現場訂單同號
    pickup_day = business_day(c.pickup_at) if isinstance(c.pickup_at, datetime) else None
    if pickup_day is not None and pickup_day <= business_day():
        pickup_day = None
    c.pickup = 0 if c.stock_node else next_pickup_number(c.tenant_id, pickup_day)

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
    if c.tier is not None and not settings_service.get('member', c.tenant_id).get('tiers_enabled', True):
        c.tier = None   # 已關閉會員等級：折扣與贈點倍數都不套用
    if c.tier is not None and (c.tier.discount_percent or 0) > 0:
        # 先四捨五入成整數再往下算：存檔的會員折扣與實際扣掉的金額一致 (收據各項折扣加總 = 折扣總額)
        c.member_discount = int(math.floor(c.remaining * min(100, c.tier.discount_percent) / 100 + 0.5))
        c.remaining = max(0, c.remaining - c.member_discount)

    # 整單折扣 (POS)：超過門檻或沒有權限時需主管核准
    c.manual_discount = 0
    if c.order_discount and c.is_pos:
        c.manual_discount, pct = _manual_discount(c.order_discount, c.remaining)
        c.manual_discount = int(math.floor(c.manual_discount + 0.5))   # 同會員折扣：先取整數再扣 (百分比折扣會留下小數)
        if c.manual_discount > 0:
            limit = c.pos_cfg.get('discount_approval_percent') or 0
            from app.services.approval_service import has_permission
            if not has_permission('discount') and (limit == 0 or pct > limit):
                if not c.approve:
                    raise AppError('整單折扣需要主管核准', 403, need_approval=True)
                if getattr(c, 'discount_approver', None) is None:
                    raise _DiscountApprovalNeeded()   # 交易外核准後重新結帳 (見 process_checkout)
                c.approved_by = c.discount_approver
            c.remaining = max(0, c.remaining - c.manual_discount)

    # 點數折抵 (受單筆上限限制，只能折整數元)
    c.setting = get_or_create_reward_setting(c.tenant_id, commit=False)
    c.points_used = c.points_discount = 0
    if c.user and c.setting.is_enabled and safe_int(c.use_points, 0) > 0:
        ppd = max(1, c.setting.points_per_dollar or 1)
        db.session.refresh(c.user, ['points'])
        # 上限 = 四捨五入後的應付金額 (百分比優惠券會留下小數，例如 76.5 → 可折 76 元而非 75；否則永遠剩 $1 折不掉)
        max_cash = int(math.floor(c.remaining + 0.5))
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
        c.service_charge = PricingDomainService.calculate_service_charge(c.after_discounts, pct)

    # 外送到府：依配送區域加外送費 (最低消費以商品小計計算)
    c.delivery_fee = 0
    if c.home is not None:
        from app.services import own_delivery_service
        c.delivery_fee = own_delivery_service.fee_for(c.home['zone'], int(c.subtotal), c.tenant_id,
                                                      node=c.stock_node if c.is_hub else None)

    # 應付金額四捨五入到整數元，折扣總額以「小計 + 服務費 + 外送費 - 應付」反推，確保金額一致
    c.final_price = int(math.floor(c.after_discounts + 0.5)) + c.service_charge + c.delivery_fee
    c.total_discount = max(0, c.subtotal + c.service_charge + c.delivery_fee - c.final_price)
    server_total = OrderDomainService.validate_expected_total(
        c.subtotal,
        c.total_discount,
        service_charge=c.service_charge,
        delivery_fee=c.delivery_fee,
    )
    c.final_price = int(math.floor(server_total + 0.5))
    if c.expected_total not in (None, '') and safe_int(c.expected_total, -1) != c.final_price:
        raise AppError(f'金額已變動：畫面顯示 ${safe_int(c.expected_total, 0)}，目前應付 ${c.final_price} '
                       f'(可能剛調整價格、優惠或點數)，已更新為最新金額，請確認後再送出！', 409, price_changed=True,
                       expected_total=safe_int(c.expected_total, 0), server_total=c.final_price,
                       line_prices=[ln.unit_price for ln in c.lines if not ln.is_gift])
    if c.final_price <= 0 and (c.payment_status in ('pending', 'authorizing')
                               or (c.payment_status == 'unpaid' and c.dining_table is None)):
        # 應付 $0 (點數全額折抵、全額優惠、只兌換紅利商品)：沒有要收的款項，不導向綠界 / 不向 LINE Pay 扣 0 元，直接成立
        c.payment_status, c.one_time_key = '', ''
    # 贈點倍數：0 = 此等級不累積點數 (後台允許設為 0，不可當成沒設定)
    multiplier = max(0, c.tier.points_multiplier) if c.tier is not None and c.tier.points_multiplier is not None else 1
    # 本單可得的贈點先記在訂單上；實際入帳在付款完成時 (grant_paid_rewards)
    c.points_earned = int((c.final_price - c.delivery_fee) // max(1, c.setting.spend_per_point or 100) * multiplier) \
        if c.user and not c.is_delivery else 0

    c.cash_in = max(0, safe_int(c.cash_received, 0))
    c.pays_cash_at_pos = c.is_pos and c.payment == PaymentMethod.CASH
    if c.pays_cash_at_pos and 0 < c.cash_in < c.final_price:
        raise AppError(f'實收現金 (${c.cash_in}) 小於應付總額 (${c.final_price})！')
    if c.pays_cash_at_pos and c.cash_in - c.final_price > MAX_CASH_CHANGE:
        raise AppError(f'實收現金 (${c.cash_in}) 比應付總額 (${c.final_price}) 多出太多，請確認輸入的金額是否正確！')
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
            if given - cash_t['amount'] > MAX_CASH_CHANGE:
                raise AppError(f'現金實收 (${given}) 比現金應付 (${cash_t["amount"]}) 多出太多，請確認輸入的金額是否正確！')
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
            identifier=((f"{c.user.name} ({c.user.phone})" if c.is_pos else c.user.name) if c.user else c.table_display)[:100],
            login_at=c.now, logout_at=c.now, ip_address=c.client_ip,
            ordered_items=f"[{'POS ' if c.is_pos else ''}取餐#{c.pickup}] {c.summary}",
        )
        db.session.add(c.log)
    else:
        c.log.ordered_items, c.log.logout_at = c.summary, c.now

    # 預估完成時間 (線上端建立的訂單由取餐分店收到時估算；預約單不估)
    c.estimated_ready_at = None
    if not c.is_hub and c.scheduled_at is None and c.stock_node is None:
        from app.services import eta_service
        minutes = eta_service.estimate(c.tenant_id, [(ln.menu_item, ln.quantity) for ln in c.lines], c.now)
        if minutes:
            c.estimated_ready_at = c.now + timedelta(minutes=minutes)
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
        # 各活動的折扣明細 (names 與 details 一一對應；滿額贈的 details 名稱帶「(贈 X)」，所以用 names 的名稱)
        promotion_details=json.dumps([{'name': n, 'amount': int(d['amount'])}
                                      for n, d in zip(c.promo_result.names, c.promo_result.details)], ensure_ascii=False)
        if c.promo_result.names else '',
        approved_by=c.approved_by, payment_reference=c.card_auth, card_last4=c.card_last4, is_rush=bool(c.is_rush),
        external_ref=str(c.external_ref or '')[:60], shift_id=c.shift_open, pickup_at=c.scheduled_at,
        estimated_ready_at=c.estimated_ready_at,
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
        c.card_auth = giftcard_service.mask_code(c.gift_code)   # 收款紀錄只留遮罩後的卡號

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
    elif c.payment_status in ('pending', 'authorizing'):
        # 先以「等待付款」建立發票並保存收銀 / 顧客輸入的載具、統編、捐贈；付款完成才改為待開立
        payment_service.maybe_create_invoice(c.order, c.invoice_info, status=InvoiceStatus.WAITING)
    elif c.payment_status == 'unpaid':
        from app.services import deferred, print_service
        deferred.defer(print_service.print_order, c.order.id, c.tenant_id, False)   # 桌邊點餐：只印廚房單

    # 會員回饋 (贈點、累積消費 / 等級、集點卡與推薦、電子收據)：結帳當下已付款才給；
    # 線上待付款 / 桌邊未結帳 / LINE Pay 授權中的訂單在付款完成時才給 (grant_paid_rewards)
    if c.user is not None and c.payment_status in PAID_STATUSES:
        grant_paid_rewards(c.order, c.lines)

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
        c.order = payment_service.charge_linepay(c.order.id, c.one_time_key, c.invoice_info)
        _release_waiting_invoices(c.order)
    invalidate_ai_advice(c.tenant_id)
    order_event_bus.notify(c.tenant_id)
    return c.order


def _release_waiting_invoices(order):
    """LINE Pay 扣款成功：建單時以「等待付款」建立的發票 (含收銀輸入的載具 / 統編) 改為待開立並開立"""
    from app.models.payment import Invoice
    from app.services import deferred, einvoice_ecpay
    invoices = Invoice.query.filter_by(order_id=order.id, status=InvoiceStatus.WAITING).all()
    if not invoices:
        return
    auto_issue = settings_service.get('einvoice', order.tenant_id).get('auto_issue', True)
    for inv in invoices:
        inv.status = InvoiceStatus.PENDING
        db.session.flush()
        if auto_issue:
            deferred.defer(einvoice_ecpay.issue_invoice, inv.id)
    db.session.commit()
