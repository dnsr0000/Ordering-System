"""
訂單狀態機與取消 / 作廢 / 換日結案 (由 order_service 拆出)：合法流轉與角色權限、取消回補 (庫存、點數、券、贈點)、
單品作廢、廚房一鍵出餐、換日自動結案、「今天的訂單」條件。
"""
import json
import logging
import math
from datetime import datetime, time, timedelta

from flask import current_app, has_app_context
from sqlalchemy import and_, case, func, or_, select, update

from app.domain.lifecycle.service import LifecycleDomainService
from app.errors import AppError
from app.extensions import db
from app.models.coupon import UserCoupon
from app.models.enums import ORDER_STATUS_LABELS, OrderSource, OrderStatus, VoucherStatus
from app.models.menu import ComboOption, MenuItem
from app.models.order import Order, OrderItem
from app.models.sync import PointLedger, SyncNode
from app.models.user import RewardRedemption, User
from app.services import inventory_service, payment_service, settings_service, wallet_service
from app.services.ai_service import invalidate_ai_advice
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.order_stock import _change_node_stock, _change_stock, order_stock_demands, restock_demands
from app.services.points_service import change_points
from app.sync import runtime as sync_runtime
from app.tenant_context import tenant_bypass
from app.utils import safe_int
from app.core.errors import ValidationError

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

# 合法狀態流轉 (不可倒退)
LEGAL_TRANSITIONS = {
    OrderStatus.PENDING: {OrderStatus.COMPLETED, OrderStatus.CANCELLED},
    OrderStatus.COMPLETED: {OrderStatus.PICKED_UP, OrderStatus.CANCELLED},
    OrderStatus.PICKED_UP: {OrderStatus.CANCELLED},
    OrderStatus.CANCELLED: set(),
}
_ALL_TRANSITIONS = {(cur, tgt) for cur, tgts in LEGAL_TRANSITIONS.items() for tgt in tgts}
# 各角色可執行的流轉
ROLE_TRANSITIONS = {
    'owner': _ALL_TRANSITIONS,
    'manager': _ALL_TRANSITIONS,
    'staff': _ALL_TRANSITIONS - {(OrderStatus.PICKED_UP, OrderStatus.CANCELLED)},
    'kitchen': {(OrderStatus.PENDING, OrderStatus.COMPLETED)},
}
# 尚未收款的線上 / LINE Pay 訂單：不可出餐、核銷取餐，也不換日結案 (付款完成後才進廚房)
UNPAID_PAYMENT_STATUSES = ('pending', 'authorizing', 'expired')
# 這些付款狀態不可作廢單一品項 (請改用取消訂單)
NO_VOID_PAYMENT_STATUSES = UNPAID_PAYMENT_STATUSES + ('refund_required', 'refunded')


# ============================================================================
# 訂單狀態機
# ============================================================================
def change_order_status(order_id, target_status, staff, tenant_id, approve=None, reason='', trusted_delivery_pickup=False):
    """
    變更訂單狀態。判斷順序刻意先驗權限 (403) 再查訂單 (404 / 409)，
    無權限者不論訂單存在與否、狀態為何，回應都相同，無法藉此探測訂單資訊。
    作廢 (取消) 已收款的訂單，或已出餐但未結帳的訂單 (桌邊掃碼點餐吃完未付，取消等於整單免收)：
    沒有「作廢 / 退款」權限的員工需主管核准 (approve 回呼，後台可關閉此規則)；製作中的未結帳訂單仍可直接取消。
    線上端 (hub)：分店的訂單不可在線上端出餐 / 核銷取餐；分店建立且已收款的訂單須在出餐分店作廢退款。
    trusted_delivery_pickup 僅供已通過派車路由角色驗證的內部送達流程使用。
    """
    try:
        target = OrderStatus(str(target_status or '').strip())
    except ValueError:
        raise AppError(f'不支援的訂單狀態：{target_status}', 409)
    reachable = {t for tgts in LEGAL_TRANSITIONS.values() for t in tgts}
    if target not in reachable:
        raise AppError(f'非法狀態轉換：沒有任何流程可以將訂單變更為【{ORDER_STATUS_LABELS.get(target, target)}】！', 409)

    role = getattr(staff, 'role', '')
    allowed = ROLE_TRANSITIONS.get(role, set())
    internal_pickup = trusted_delivery_pickup and target == OrderStatus.PICKED_UP
    forbidden = AppError(f'權限不足：角色 [{role}] 無權將訂單變更為【{ORDER_STATUS_LABELS.get(target, target)}】！', 403)
    if not internal_pickup and target not in {t for _, t in allowed}:
        raise forbidden

    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != tenant_id:
        raise AppError('查無此訂單！', 404)
    current = OrderStatus(order.status)

    # 1. 先做原本的重複操作與合法狀態機中文檢查 (滿足 test_status_error_message_uses_chinese_labels)
    if current == target:
        raise AppError(f'訂單已處於【{ORDER_STATUS_LABELS.get(current, current)}】狀態，請勿重複操作！', 409)
    if target not in LEGAL_TRANSITIONS.get(current, set()):
        raise AppError(f'非法狀態轉換：不可從【{ORDER_STATUS_LABELS.get(current, current)}】'
                       f'轉換為【{ORDER_STATUS_LABELS.get(target, target)}】！', 409)

    # 2. 領域層驗證
    try:
        LifecycleDomainService.validate_transition(current.value, target.value)
    except ValidationError as e:
        raise AppError(str(e), 409)


    if not internal_pickup and (current, target) not in allowed:
        raise forbidden
    # 待付款 / LINE Pay 扣款中 / 逾時未付款的訂單不可出餐或核銷 (付款完成才進廚房；否則會先發出取餐通知)
    serving = target in (OrderStatus.COMPLETED, OrderStatus.PICKED_UP)
    if serving and order.payment_status in UNPAID_PAYMENT_STATUSES:
        raise AppError('這張訂單尚未完成付款 (待付款 / 扣款中 / 逾時未付)，不可出餐或核銷取餐', 409)
    # 線上端不可替分店的訂單出餐 / 核銷 (分店還沒做的餐會被標成已出餐並通知顧客取餐，下發後分店廚房也看不到)
    if serving and sync_runtime.is_hub() and order.node_uuid:
        raise AppError('分店的訂單請在分店出餐 / 取餐', 409)
    # 分店建立且已收款的訂單：退款紀錄只上傳不下發，線上端記的退款分店看不到，須在出餐分店作廢退款
    # (未收款的分店訂單，例如桌邊未結帳，仍可在線上端取消)
    if target == OrderStatus.CANCELLED and sync_runtime.is_hub() and order.node_uuid \
            and order.order_source != OrderSource.ONLINE and order.is_paid and (order.total_price or 0) > 0:
        raise AppError('這張訂單由分店建立且已收款，請在出餐分店作廢退款', 409)
    approved_by = ''
    # 已出餐但未結帳 (桌邊掃碼點餐吃完未付) 的訂單取消等於整單免收，與作廢已收款的訂單一樣需要核准
    served_unpaid = current == OrderStatus.COMPLETED and order.payment_status == 'unpaid'
    if target == OrderStatus.CANCELLED and (order.is_paid or served_unpaid) and (order.total_price or 0) > 0 \
            and getattr(staff, 'id', None) is not None and not staff.has_permission('refund'):
        refund_cfg = settings_service.get('refund', tenant_id)
        if refund_cfg.get('void_requires_approval', True):
            if approve is None:
                raise AppError('作廢已收款或已出餐未結帳的訂單需要主管核准', 403, need_approval=True, permission='refund')
            approved_by = approve('refund', '作廢訂單')

    # CAS：只有在狀態仍是 current 時才更新，同時操作只有一方成功
    completed_at = datetime.now() if target == OrderStatus.COMPLETED else order.completed_at
    cas = [Order.id == order.id, Order.tenant_id == tenant_id, Order.status == current]
    if serving:   # 檢查與更新之間付款狀態被改回未付款 (例如逾時) 時同樣不更新
        cas.append(Order.payment_status.notin_(UNPAID_PAYMENT_STATUSES))
    swapped = db.session.execute(
        update(Order).where(*cas)
        .values(status=target, completed_at=completed_at).execution_options(synchronize_session=False)
    ).rowcount
    if not swapped:
        db.session.rollback()
        raise AppError('訂單狀態已被其他操作者變更，請重新整理！', 409)
    db.session.expire(order, ['status', 'completed_at'])

    if target == OrderStatus.CANCELLED:
        _rollback_cancelled_order(order, tenant_id, reason=reason, approved_by=approved_by, previous_status=current)
    elif target == OrderStatus.PICKED_UP:
        record_staff_audit('MARK_PICKED_UP', target_order_id=order.id, target_user_id=order.user_id, tenant_id=tenant_id,
                           details=f"核銷完成取餐單號 #{order.id} (取餐 #{order.pickup_number:03d})")
    elif target == OrderStatus.COMPLETED:
        from app.services import notify_service
        notify_service.order_ready(order)
    db.session.commit()
    invalidate_ai_advice(tenant_id)
    order_event_bus.notify(tenant_id)
    return order


def system_cancel(order_id, tenant_id, reason):
    """系統取消 (LINE Pay 扣款失敗、付款逾時)：以負責人權限執行，回補庫存與點數"""
    from app.models.tenant import StaffAccount
    system = StaffAccount(tenant_id=tenant_id, username='系統', role='owner', password_hash='-')
    try:
        return change_order_status(order_id, OrderStatus.CANCELLED, system, tenant_id, reason=reason)
    except AppError:
        db.session.rollback()
        return None


def void_item(tenant_id, order_item_id, quantity, reason, approved_by='', refund_method=None):
    """
    單品作廢 / 改單：已成立 (可能已送廚房) 的訂單中，作廢某個品項的部分份數 (整張都不要請用取消訂單)。
    - 份數以條件式 UPDATE 扣 (quantity = 剩下份數、voided_qty 累加)，兩台同時作廢不會超過。
    - 回補該品項 (含套餐當時的配餐) 的庫存與原料。
    - 金額：依該品項占商品小計的比例分攤訂單折扣後退款 (已收款 → 部分退款；桌邊未結帳 → 直接降低應付)。
    - 已贈送的點數不追回。回傳 {'amount', 'refund_status', 'order_total'}。
    """
    from app.services import payment_service
    from app.services.audit import record_staff_audit
    item = db.session.get(OrderItem, safe_int(order_item_id, 0))
    order = item.order if item is not None else None
    if order is None or order.tenant_id != tenant_id:
        raise AppError('查無此訂單品項', 404)
    if order.status == OrderStatus.CANCELLED:
        raise AppError('訂單已取消，無法作廢品項', 409)
    if not sync_runtime.owns_stock(order):
        raise AppError('這張訂單由其他分店出餐，請在出餐分店作廢品項 (或取消整張訂單)', 409)
    if order.payment_status in NO_VOID_PAYMENT_STATUSES:
        raise AppError('這張訂單的付款狀態無法作廢單一品項，請改用取消訂單', 409)
    qty = safe_int(quantity, 0)
    reason = str(reason or '').strip()
    if qty <= 0:
        raise AppError('請輸入作廢份數')
    if not reason:
        raise AppError('請填寫作廢原因')
    # 同一張訂單的作廢依序處理 (PostgreSQL 鎖住訂單列；SQLite 在下面第一個寫入時整個資料庫就已依序)，
    # 之後重新讀取剩下的份數與已退金額，連續或同時作廢多次時每次都依「目前剩下的」比例計算
    db.session.execute(select(Order.id).where(Order.id == order.id).with_for_update())
    # 訂單狀態在鎖內 (與扣份數同一個 UPDATE) 再確認一次：上面的檢查之後剛好被取消時，不可再回補庫存 / 降低應付
    still_open = select(Order.id).where(Order.id == order.id, Order.status != OrderStatus.CANCELLED,
                                        Order.payment_status.notin_(NO_VOID_PAYMENT_STATUSES))
    claimed = db.session.execute(
        update(OrderItem).where(OrderItem.id == item.id, OrderItem.quantity >= qty, OrderItem.order_id.in_(still_open))
        .values(quantity=OrderItem.quantity - qty, voided_qty=OrderItem.voided_qty + qty,
                void_note=func.substr(OrderItem.void_note + ' ' + reason, 1, 120))
        .execution_options(synchronize_session=False)).rowcount
    if not claimed:
        db.session.rollback()
        if order.status == OrderStatus.CANCELLED or order.payment_status in NO_VOID_PAYMENT_STATUSES:
            raise AppError('訂單剛被其他操作者取消或變更付款狀態，無法作廢品項，請重新整理', 409)
        raise AppError('可作廢的份數不足 (可能剛被其他人作廢)，請重新整理', 409)
    free_line = or_(OrderItem.is_reward.is_(True), OrderItem.is_gift.is_(True))
    line_total = func.coalesce(OrderItem.price, 0) * OrderItem.quantity
    left, left_sum, paid_left = db.session.execute(
        select(func.coalesce(func.sum(OrderItem.quantity), 0), func.coalesce(func.sum(line_total), 0),
               func.coalesce(func.sum(case((free_line, 0), else_=line_total)), 0))
        .where(OrderItem.order_id == order.id)).one()
    if (left or 0) <= 0:
        db.session.rollback()
        raise AppError('整張訂單的品項都要作廢時請改用「取消訂單」', 409)
    # 只剩贈品 / 兌換品等 0 元明細時也等於整張作廢 (否則會全額退款卻不退折抵點數、不還券、不追回贈點)
    if (item.price or 0) > 0 and not item.is_reward and not item.is_gift and int(paid_left or 0) <= 0:
        db.session.rollback()
        raise AppError('作廢後訂單只剩贈品 / 兌換品，整張不要了請改用「取消訂單」', 409)
    db.session.refresh(item)
    db.session.refresh(order)

    # 庫存與原料回補 (主餐 + 當時賣出的套餐配餐)
    demands = {item.menu_item_id: qty} if item.menu_item_id else {}
    sides = []
    if item.combo_sides:
        try:
            uuids = [str(u) for u in json.loads(item.combo_sides)]
        except (TypeError, ValueError):
            uuids = []
        by_uuid = dict(db.session.execute(select(MenuItem.uuid, MenuItem.id).where(
            MenuItem.tenant_id == tenant_id, MenuItem.uuid.in_(uuids))).all()) if uuids else {}
        sides = [by_uuid[u] for u in uuids if u in by_uuid]
    elif item.combo_option_id:
        combo = db.session.get(ComboOption, item.combo_option_id)
        sides = combo.side_item_ids if combo else []
    for sid in sides:
        demands[sid] = demands.get(sid, 0) + qty
    refund_cfg = settings_service.get('refund', tenant_id)
    # 已取餐 (已被帶走 / 吃掉) 的餐點預設不回補庫存，除非店家設定「已取餐的訂單也回補」
    restock = refund_cfg.get('restock_on_cancel', True) and \
        (order.status != OrderStatus.PICKED_UP or refund_cfg.get('restock_picked_up', False))
    if restock:   # 線上訂單送達分店時因庫存不足沒扣到的份數不回補 (restock_demands)
        _change_stock(tenant_id, restock_demands(order, demands), +1)
    # 取鎖順序與結帳相同 (品項 → 會員 → 原料)，避免與同一會員的結帳交叉鎖死
    if order.user_id:
        db.session.execute(select(User.id).where(User.id == order.user_id).with_for_update())
    redeemed_note = ''
    if order.user_id and item.menu_item_id and item.is_reward:
        # 紅利兌換品：依作廢份數退回兌換憑證的點數 (之後整張取消只會退仍為已使用的憑證)
        vouchers = RewardRedemption.query.filter_by(order_id=order.id, menu_item_id=item.menu_item_id,
                                                    status=VoucherStatus.USED) \
            .order_by(RewardRedemption.id).limit(qty).all()
        back_points = sum(v.points_cost or 0 for v in vouchers)
        for v in vouchers:
            v.status = VoucherStatus.REFUNDED
        if back_points:
            change_points(tenant_id, order.user_id, back_points, 'cancel', order_id=order.id)
            redeemed_note = f'；退回兌換點數 {back_points} 點'
    elif order.user_id and item.menu_item_id and (item.customization or '').startswith('[寄杯]'):
        # 寄杯兌換品：依作廢份數退回次數 (帳與取消訂單相同，之後整張取消只退剩下淨扣的次數)
        from app.models.member import MemberPass, PassLedger
        from app.services import pass_service
        nets = db.session.execute(
            select(PassLedger.member_pass_id, func.sum(PassLedger.delta))
            .join(MemberPass, MemberPass.id == PassLedger.member_pass_id)
            .where(PassLedger.order_id == order.id, PassLedger.reason.in_(('redeem', 'cancel')),
                   MemberPass.menu_item_id == item.menu_item_id)
            .group_by(PassLedger.member_pass_id).order_by(PassLedger.member_pass_id)).all()
        todo = qty
        for pass_id, net in nets:
            n = min(todo, -int(net or 0))
            mp = db.session.get(MemberPass, pass_id) if n > 0 else None
            if mp is None:
                continue
            db.session.execute(update(MemberPass).where(MemberPass.id == pass_id)
                               .values(remaining=MemberPass.remaining + n).execution_options(synchronize_session=False))
            pass_service._add(mp, n, 'cancel', order.id)
            todo -= n
            if todo <= 0:
                break
        if todo < qty:
            redeemed_note = f'；退回寄杯 {qty - todo} 次'
    if restock:
        inventory_service.restore_items(tenant_id, order.id, demands, f'作廢訂單 #{order.id} 的 {item.item_name} x{qty}')

    # 金額：這幾份占「作廢前剩下的商品小計」的比例 × (應付 - 外送費 - 已退款)
    # (已收款的訂單應付金額不變、改記已退款；桌邊未結帳的訂單則是應付金額直接降低)
    share = (item.price or 0) * qty
    before_sum = int(left_sum or 0) + share
    base = max(0, (order.total_price or 0) - (order.delivery_fee or 0) - (order.refunded_amount or 0))
    amount = int(math.floor(share * base / before_sum + 0.5)) if before_sum > 0 else 0
    amount = min(amount, order.refundable_amount)
    refund_status = ''
    if order.payment_status == 'unpaid':     # 桌邊點餐尚未結帳：直接降低應付金額，服務費 / 折扣等比例調降 (小計仍等於剩下品項)
        # amount 為 0 (例如整單全折抵) 時也要調降折扣
        order.reduce_unpaid_total(amount, share, before_sum)
        # 結帳時才入帳的贈點依新應付金額重算 (與結帳相同公式，只減不增)
        if (order.points_earned or 0) > 0 and order.user is not None:
            from app.services.reward_service import get_or_create_reward_setting
            spend_per_point = max(1, get_or_create_reward_setting(tenant_id, commit=False).spend_per_point or 100)
            tier = order.user.tier if settings_service.get('member', tenant_id).get('tiers_enabled', True) else None
            # 倍數與結帳相同：已關閉會員等級 = 1；0 = 此等級不累積點數
            multiplier = max(0, tier.points_multiplier) if tier is not None and tier.points_multiplier is not None else 1
            earned = int((order.total_price - (order.delivery_fee or 0)) // spend_per_point * multiplier)
            order.points_earned = max(0, min(order.points_earned, earned))
    elif amount > 0 and order.is_paid:
        refund = payment_service.partial_refund(order.id, amount, f'作廢 {item.item_name} x{qty}：{reason}',
                                                method=refund_method, approved_by=approved_by, tenant_id=tenant_id)
        refund_status = refund.status
    record_staff_audit('VOID_ITEM', target_order_id=order.id, target_user_id=order.user_id, tenant_id=tenant_id,
                       details=f'訂單 #{order.id} 作廢 {item.item_name} x{qty} (退 ${amount}{redeemed_note})，'
                               f'原因：{reason}；核准：{approved_by or "-"}')
    from app.services import deferred
    deferred.defer(order_event_bus.notify, tenant_id)   # 提交後才通知看板，避免重新抓取時讀到作廢前的資料
    return {'amount': amount, 'refund_status': refund_status, 'order_total': order.total_price,
            'remaining_qty': item.quantity}


def flag_refund_if_paid(order_id):
    """
    已付款的訂單被取消：標記需退款 (後台顯示「需退款」)。
    線上付款 (paid) 到綠界後台退刷；桌邊掃碼點餐已在櫃檯結帳 (settled) 則由櫃檯退款。
    """
    db.session.execute(update(Order).where(Order.id == order_id, Order.payment_status.in_(('paid', 'settled')))
                       .values(payment_status='refund_required').execution_options(synchronize_session=False))


def _is_pre_ledger_order(order, tenant_id):
    """
    升級前 (0003 建立點數帳之前) 成立的訂單：沒有任何帶這張訂單的點數異動，且建立時間早於本店的期初異動
    (0003 在升級當下為每位有點數的會員寫一筆 reason='opening')。以期初時間為界，升級後的新訂單在贈點異動尚未同步到時
    (例如線上端先收到訂單) 不會被誤判。
    """
    if order.created_at is None:
        return False
    if PointLedger.query.filter_by(order_id=order.id).execution_options(skip_tenant_filter=True).first() is not None:
        return False
    cutoff = db.session.query(func.min(PointLedger.created_at)).filter(
        PointLedger.tenant_id == tenant_id, PointLedger.reason == 'opening').scalar()
    return cutoff is not None and order.created_at < cutoff


def _rollback_cancelled_order(order, tenant_id, reason='', approved_by='', previous_status=None):
    """
    取消訂單：回補庫存 (含套餐配餐) 與原物料、退回兌換點數與折抵點數、追回贈點、還原核銷的會員券、
    已付款標記需退款並建立退款紀錄 (儲值金退回、LINE Pay 自動退款、發票作廢 / 折讓)
    """
    # 重新讀取整張訂單：order 是 CAS 更新前讀的，期間可能被同時進行的桌位結帳改為已結帳 (付款狀態、付款方式、settlement_id)；
    # CAS 更新後已握有寫入鎖，這裡讀到的就是最終狀態 (否則現金結帳後作廢會誤判為「線上付款」而停在需退款)
    db.session.refresh(order)
    was_paid = order.is_paid   # 下面會把已收款改為需退款，累積消費的重算要以取消前為準
    flag_refund_if_paid(order.id)
    # 待付款的線上訂單取消：付款狀態改為逾時，顧客不會再看到付款連結；之後才到的付款會被標記需退款
    db.session.execute(update(Order).where(Order.id == order.id, Order.payment_status == 'pending')
                       .values(payment_status='expired').execution_options(synchronize_session=False))
    refund_cfg = settings_service.get('refund', tenant_id)
    # 已取餐 (已被帶走 / 吃掉) 的餐點預設不回補庫存，除非店家設定「已取餐的訂單也回補」
    restock = refund_cfg.get('restock_on_cancel', True) and \
        (previous_status != OrderStatus.PICKED_UP or refund_cfg.get('restock_picked_up', False))
    own_restock = sync_runtime.owns_stock(order) and restock
    if own_restock:   # 線上訂單送達分店時因庫存不足沒扣到的份數不回補 (restock_demands)
        _change_stock(tenant_id, restock_demands(order), +1)
    if order.user_id:   # 取鎖順序與結帳相同 (品項 → 會員 → 原料)，避免與同一會員的結帳交叉鎖死
        db.session.execute(select(User.id).where(User.id == order.user_id).with_for_update())
    if own_restock:
        inventory_service.restore(tenant_id, order.id)
    elif sync_runtime.is_hub() and order.node_uuid:
        node = SyncNode.query.filter_by(tenant_id=tenant_id, uuid=order.node_uuid).first()
        if node is not None:
            _change_node_stock(node.id, order_stock_demands(order), +1)

    from app.services import pass_service
    pass_service.on_sale_order_cancelled(order)   # 寄杯購買訂單：寄杯未使用才可作廢
    pass_service.on_order_cancelled(order)        # 兌換寄杯的訂單：退回次數
    vouchers = RewardRedemption.query.filter_by(order_id=order.id, status=VoucherStatus.USED).all()
    redeemed_points = sum(v.points_cost or 0 for v in vouchers)
    for v in vouchers:
        v.status = VoucherStatus.REFUNDED

    points_delta, note = 0, ''
    if order.user_id:
        # 贈點只追回已入帳的部分 (付款完成才入帳；未付款就取消的訂單沒有給過)
        earned = db.session.query(func.coalesce(func.sum(PointLedger.delta), 0)).filter(
            PointLedger.order_id == order.id, PointLedger.reason == 'earn').scalar() or 0
        if not earned and (order.points_earned or 0) > 0 and was_paid and _is_pre_ledger_order(order, tenant_id):
            earned = order.points_earned   # 升級前的舊訂單：贈點在點數帳建立前入帳 (併入期初)，以訂單上的贈點追回
        net =(order.points_used or 0) + redeemed_points - earned
        if net:
            points_delta = change_points(tenant_id, order.user_id, net, 'cancel', order_id=order.id, clamp=True) or 0
            if points_delta != net:
                note = f"；會員餘額不足，有 {points_delta - net} 點贈點無法追回"
        if order.applied_user_coupon_id:
            uc = db.session.get(UserCoupon, order.applied_user_coupon_id)
            if uc is not None and uc.user_id == order.user_id and uc.is_used:
                uc.is_used, uc.used_at = False, None

    from app.services import loyalty_service
    loyalty_service.on_order_cancelled(order)
    refund = payment_service.on_order_cancelled(order, reason=reason, approved_by=approved_by)
    if order.user_id and was_paid:
        user = db.session.get(User, order.user_id)
        if user is not None:
            wallet_service.recompute_spent(user)
    extra = f"；退款 ${refund.amount} ({refund.method}，{refund.status})" if refund else ''
    extra += f"；原因：{reason}" if reason else ''
    extra += f"；核准：{approved_by}" if approved_by else ''
    record_staff_audit(
        'CANCEL_ORDER', target_order_id=order.id, target_user_id=order.user_id, tenant_id=tenant_id,
        points_delta=points_delta,
        details=f"取消訂單 #{order.id} (取餐 #{order.pickup_number:03d})，已全量回滾庫存、商品兌換點數({redeemed_points}點)、折抵與贈點{note}{extra}",
    )


def complete_all_pending_orders(tenant_id, order_ids):
    """
    廚房一鍵出餐：KDS 畫面上的這幾張製作中訂單改為已出餐，回傳張數。
    只完成指定的訂單 (畫面上看得到的)：原本會連未付款的線上單、晚一點的預約單一起完成並發出取餐通知。
    """
    from app.services import notify_service
    # 只收 ASCII 數字且不超過 18 位 ('²'.isdigit() 為 True 但 int() 會失敗；超長數字綁進 SQLite 查詢會溢位)
    ids = sorted({int(s) for s in (str(i) for i in order_ids or ())
                  if s.isascii() and s.isdigit() and len(s) <= 18})[:500]
    if not ids:
        return 0
    # 預約取餐的訂單到取餐前 15 分鐘 (與 routes/kitchen.py 的 PREP_LEAD_MINUTES 一致) 才可一起出餐，
    # 避免今天稍晚的預約單被一起完成並提早發出取餐通知 (前端已排除，這裡是後端第二道防線)
    conds = [Order.tenant_id == tenant_id, Order.id.in_(ids), Order.status == OrderStatus.PENDING,
             Order.payment_status.notin_(UNPAID_PAYMENT_STATUSES),
             or_(Order.pickup_at.is_(None), Order.pickup_at <= datetime.now() + timedelta(minutes=15))]
    if sync_runtime.is_hub():   # 線上端不可替分店的訂單出餐 (見 change_order_status)
        conds.append(Order.node_uuid.is_(None))
    pending_ids = [o.id for o in Order.query.filter(*conds).all()]
    # 只通知這次真的「搶」到的訂單 (RETURNING)：兩台 KDS 同時按或網路重試時，另一個請求的 UPDATE 會是 0 列，不可重複發出餐通知
    done_ids = db.session.execute(
        update(Order).where(Order.id.in_(pending_ids), Order.status == OrderStatus.PENDING)
        .values(status=OrderStatus.COMPLETED, completed_at=datetime.now())
        .returning(Order.id)
        .execution_options(synchronize_session=False)
    ).scalars().all() if pending_ids else []
    count = len(done_ids)
    for oid in sorted(done_ids):
        notify_service.order_ready(db.session.get(Order, oid))
    if count:
        record_staff_audit('KITCHEN_COMPLETE_ALL', tenant_id=tenant_id,
                           details=f"廚房一鍵全部出餐，共 {count} 張訂單由「製作中」轉為「待取餐」")
    db.session.commit()
    invalidate_ai_advice(tenant_id)
    order_event_bus.notify(tenant_id)
    return count


# ============================================================================
# 換日自動結案
# ============================================================================
def auto_close_stale_orders():
    """
    把本營業日開始 (預設 00:00，見 todays_range) 以前建立、仍在製作中的訂單改為已出餐 (所有店家)。
    尚未付款 (待付款 / 扣款中 / 逾時) 的訂單不結案：付款完成後才送分店 / 進廚房，結案的話付了錢也沒人做。
    completed_at 保持空白 (不是真實出餐時間，不納入平均出餐時間)；每家有結案的店寫一筆系統稽核。
    回傳 {tenant_id: 張數}。
    """
    today_start, _ = todays_range()
    closed = {}
    with tenant_bypass():
        stale = db.session.execute(
            select(Order.tenant_id, Order.id)
            .where(Order.status == OrderStatus.PENDING, Order.created_at < today_start,
                   Order.payment_status.notin_(UNPAID_PAYMENT_STATUSES),
                   or_(Order.pickup_at.is_(None), Order.pickup_at < today_start))   # 預約今天以後取餐的不結案
            .order_by(Order.id)
        ).all()
        by_tenant = {}
        for tid, oid in stale:
            by_tenant.setdefault(tid, []).append(oid)
        for tid, ids in by_tenant.items():
            n = db.session.execute(
                update(Order).where(Order.id.in_(ids), Order.status == OrderStatus.PENDING,
                                    Order.payment_status.notin_(UNPAID_PAYMENT_STATUSES))
                .values(status=OrderStatus.COMPLETED).execution_options(synchronize_session=False)
            ).rowcount
            if not n:
                continue
            closed[tid] = n
            shown = '、'.join(f'#{i}' for i in ids[:20]) + ('…' if len(ids) > 20 else '')
            record_staff_audit('AUTO_CLOSE_STALE_ORDERS', tenant_id=tid, as_system=True,
                               details=f"換日自動結案：{n} 張前一日(含更早)未切換狀態的「製作中」訂單已改為「已出餐」({shown})")
        db.session.commit()
    for tid in closed:
        order_event_bus.notify(tid)
    return closed


_auto_close_done_for = {'date': None}


def ensure_daily_auto_close():
    """每個行程每個營業日只執行一次 (之後只比較日期)；跨日不重啟時由換日後的第一個請求觸發"""
    today = todays_range()[0].date()
    if _auto_close_done_for['date'] == today:
        return
    try:
        auto_close_stale_orders()
        _auto_close_done_for['date'] = today
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('換日自動結案失敗 (下一個請求會重試)')


def todays_range():
    """
    本營業日的半開區間 [換日時間, 隔天換日時間)。預設 00:00 換日；營業跨午夜的店設定 BUSINESS_DAY_START_HOUR
    (例如 4 → 凌晨 04:00 才換日)，深夜的訂單在打烊前不會被換日結案，也不會從各看板消失。
    營業日的日期 (取餐號、候位等) 為 todays_range()[0].date()。
    """
    hour = safe_int(current_app.config.get('BUSINESS_DAY_START_HOUR', 0), 0) if has_app_context() else 0
    now = datetime.now()
    start = datetime.combine(now.date(), time(min(max(hour, 0), 12)))
    if now < start:
        start -= timedelta(days=1)
    return start, start + timedelta(days=1)


def todays_orders_condition(start, end, include_future=True):
    """
    「今天的訂單」：今天建立的，或預約今天取餐的 (前幾天預約的線上訂單)，
    或前幾天建立、今天才完成線上付款的 (ATM / 超商代碼隔天才繳費：付款後才送分店進廚房)。
    include_future=False (廚房看板)：今天建立但預約明天以後取餐的先不顯示，到當天才出現。
    """
    created_today = and_(Order.created_at >= start, Order.created_at < end)
    paid_today = and_(Order.payment_status == 'paid', Order.paid_at >= start, Order.paid_at < end,
                      Order.created_at < start)
    if not include_future:
        created_today = and_(created_today, or_(Order.pickup_at.is_(None), Order.pickup_at < end))
        paid_today = and_(paid_today, or_(Order.pickup_at.is_(None), Order.pickup_at < end))
    return or_(created_today, and_(Order.pickup_at >= start, Order.pickup_at < end), paid_today)
