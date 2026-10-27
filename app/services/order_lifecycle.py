"""
訂單狀態機與取消 / 作廢 / 換日結案 (由 order_service 拆出)：合法流轉與角色權限、取消回補 (庫存、點數、券、贈點)、
單品作廢、廚房一鍵出餐、換日自動結案、「今天的訂單」條件。
"""
import json
import logging
import math
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update

from app.errors import AppError
from app.extensions import db
from app.models.coupon import UserCoupon
from app.models.enums import OrderStatus, VoucherStatus
from app.models.menu import ComboOption, MenuItem
from app.models.order import Order, OrderItem
from app.models.sync import SyncNode
from app.models.user import RewardRedemption, User
from app.services import inventory_service, payment_service, settings_service, wallet_service
from app.services.ai_service import invalidate_ai_advice
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.order_stock import _change_node_stock, _change_stock, order_stock_demands
from app.services.points_service import change_points
from app.sync import runtime as sync_runtime
from app.tenant_context import tenant_bypass
from app.utils import safe_int

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


# ============================================================================
# 訂單狀態機
# ============================================================================
def change_order_status(order_id, target_status, staff, tenant_id, approve=None, reason=''):
    """
    變更訂單狀態。判斷順序刻意先驗權限 (403) 再查訂單 (404 / 409)，
    無權限者不論訂單存在與否、狀態為何，回應都相同，無法藉此探測訂單資訊。
    作廢 (取消) 已收款的訂單：沒有「作廢 / 退款」權限的員工需主管核准 (approve 回呼，後台可關閉此規則)。
    """
    try:
        target = OrderStatus(str(target_status or '').strip())
    except ValueError:
        raise AppError(f'不支援的訂單狀態：{target_status}')
    reachable = {t for tgts in LEGAL_TRANSITIONS.values() for t in tgts}
    if target not in reachable:
        raise AppError(f'非法狀態轉換：沒有任何流程可以將訂單變更為【{target}】！', 409)

    role = getattr(staff, 'role', '')
    allowed = ROLE_TRANSITIONS.get(role, set())
    forbidden = AppError(f'權限不足：角色 [{role}] 無權將訂單變更為【{target}】！', 403)
    if target not in {t for _, t in allowed}:
        raise forbidden

    order = db.session.get(Order, order_id)
    if order is None or order.tenant_id != tenant_id:
        raise AppError('查無此訂單！', 404)
    current = OrderStatus(order.status)
    if current == target:
        raise AppError(f'訂單已處於【{current}】狀態，請勿重複操作！', 409)
    if target not in LEGAL_TRANSITIONS[current]:
        raise AppError(f'非法狀態轉換：不可從【{current}】轉換為【{target}】！', 409)
    if (current, target) not in allowed:
        raise forbidden
    approved_by = ''
    if target == OrderStatus.CANCELLED and order.is_paid and (order.total_price or 0) > 0 \
            and getattr(staff, 'id', None) is not None and not staff.has_permission('refund'):
        refund_cfg = settings_service.get('refund', tenant_id)
        if refund_cfg.get('void_requires_approval', True):
            if approve is None:
                raise AppError('作廢已收款的訂單需要主管核准', 403, need_approval=True, permission='refund')
            approved_by = approve('refund', '作廢訂單')

    # CAS：只有在狀態仍是 current 時才更新，同時操作只有一方成功
    completed_at = datetime.now() if target == OrderStatus.COMPLETED else order.completed_at
    swapped = db.session.execute(
        update(Order).where(Order.id == order.id, Order.tenant_id == tenant_id, Order.status == current)
        .values(status=target, completed_at=completed_at).execution_options(synchronize_session=False)
    ).rowcount
    if not swapped:
        db.session.rollback()
        raise AppError('訂單狀態已被其他操作者變更，請重新整理！', 409)
    db.session.expire(order, ['status', 'completed_at'])

    if target == OrderStatus.CANCELLED:
        _rollback_cancelled_order(order, tenant_id, reason=reason, approved_by=approved_by)
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
    if order.payment_status in ('pending', 'authorizing', 'expired', 'refund_required', 'refunded'):
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
    claimed = db.session.execute(
        update(OrderItem).where(OrderItem.id == item.id, OrderItem.quantity >= qty)
        .values(quantity=OrderItem.quantity - qty, voided_qty=OrderItem.voided_qty + qty,
                void_note=func.substr(OrderItem.void_note + ' ' + reason, 1, 120))
        .execution_options(synchronize_session=False)).rowcount
    if not claimed:
        db.session.rollback()
        raise AppError('可作廢的份數不足 (可能剛被其他人作廢)，請重新整理', 409)
    left, left_sum = db.session.execute(
        select(func.coalesce(func.sum(OrderItem.quantity), 0),
               func.coalesce(func.sum(func.coalesce(OrderItem.price, 0) * OrderItem.quantity), 0))
        .where(OrderItem.order_id == order.id)).one()
    if (left or 0) <= 0:
        db.session.rollback()
        raise AppError('整張訂單的品項都要作廢時請改用「取消訂單」', 409)
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
    if settings_service.get('refund', tenant_id).get('restock_on_cancel', True):
        _change_stock(tenant_id, demands, +1)
        inventory_service.restore_items(tenant_id, order.id, demands, f'作廢訂單 #{order.id} 的 {item.item_name} x{qty}')

    # 金額：這幾份占「作廢前剩下的商品小計」的比例 × (應付 - 外送費 - 已退款)
    # (已收款的訂單應付金額不變、改記已退款；桌邊未結帳的訂單則是應付金額直接降低)
    share = (item.price or 0) * qty
    before_sum = int(left_sum or 0) + share
    base = max(0, (order.total_price or 0) - (order.delivery_fee or 0) - (order.refunded_amount or 0))
    amount = int(math.floor(share * base / before_sum + 0.5)) if before_sum > 0 else 0
    amount = min(amount, order.refundable_amount)
    refund_status = ''
    if amount > 0 and order.payment_status == 'unpaid':     # 桌邊點餐尚未結帳：直接降低應付金額
        order.total_price = (order.total_price or 0) - amount
    elif amount > 0 and order.is_paid:
        refund = payment_service.partial_refund(order.id, amount, f'作廢 {item.item_name} x{qty}：{reason}',
                                                method=refund_method, approved_by=approved_by, tenant_id=tenant_id)
        refund_status = refund.status
    record_staff_audit('VOID_ITEM', target_order_id=order.id, target_user_id=order.user_id, tenant_id=tenant_id,
                       details=f'訂單 #{order.id} 作廢 {item.item_name} x{qty} (退 ${amount})，原因：{reason}；'
                               f'核准：{approved_by or "-"}')
    from app.services.event_bus import order_event_bus
    order_event_bus.notify(tenant_id)
    return {'amount': amount, 'refund_status': refund_status, 'order_total': order.total_price,
            'remaining_qty': item.quantity}


def flag_refund_if_paid(order_id):
    """
    已付款的訂單被取消：標記需退款 (後台顯示「需退款」)。
    線上付款 (paid) 到綠界後台退刷；桌邊掃碼點餐已在櫃檯結帳 (settled) 則由櫃檯退款。
    """
    db.session.execute(update(Order).where(Order.id == order_id, Order.payment_status.in_(('paid', 'settled')))
                       .values(payment_status='refund_required').execution_options(synchronize_session=False))


def _rollback_cancelled_order(order, tenant_id, reason='', approved_by=''):
    """
    取消訂單：回補庫存 (含套餐配餐) 與原物料、退回兌換點數與折抵點數、追回贈點、還原核銷的會員券、
    已付款標記需退款並建立退款紀錄 (儲值金退回、LINE Pay 自動退款、發票作廢 / 折讓)
    """
    flag_refund_if_paid(order.id)
    restock = settings_service.get('refund', tenant_id).get('restock_on_cancel', True)
    if sync_runtime.owns_stock(order) and restock:
        _change_stock(tenant_id, order_stock_demands(order), +1)
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
        net = (order.points_used or 0) + redeemed_points - (order.points_earned or 0)
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
    if order.user_id and order.is_paid:
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


def complete_all_pending_orders(tenant_id):
    """廚房一鍵出餐：所有製作中訂單改為已出餐，回傳張數"""
    from app.services import notify_service
    pending_ids = [o.id for o in Order.query.filter(Order.tenant_id == tenant_id, Order.status == OrderStatus.PENDING,
                                                    Order.payment_status.notin_(('authorizing',))).all()]
    count = db.session.execute(
        update(Order).where(Order.id.in_(pending_ids), Order.status == OrderStatus.PENDING)
        .values(status=OrderStatus.COMPLETED, completed_at=datetime.now())
        .execution_options(synchronize_session=False)
    ).rowcount if pending_ids else 0
    for oid in pending_ids:
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
    把今天 00:00 以前建立、仍在製作中的訂單改為已出餐 (所有店家)。
    completed_at 保持空白 (不是真實出餐時間，不納入平均出餐時間)；每家有結案的店寫一筆系統稽核。
    回傳 {tenant_id: 張數}。
    """
    today_start = datetime.combine(date.today(), datetime.min.time())
    closed = {}
    with tenant_bypass():
        stale = db.session.execute(
            select(Order.tenant_id, Order.id)
            .where(Order.status == OrderStatus.PENDING, Order.created_at < today_start,
                   or_(Order.pickup_at.is_(None), Order.pickup_at < today_start))   # 預約今天以後取餐的不結案
            .order_by(Order.id)
        ).all()
        by_tenant = {}
        for tid, oid in stale:
            by_tenant.setdefault(tid, []).append(oid)
        for tid, ids in by_tenant.items():
            n = db.session.execute(
                update(Order).where(Order.id.in_(ids), Order.status == OrderStatus.PENDING)
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
    """每個行程每天只執行一次 (之後只比較日期)；跨日不重啟時由當天第一個請求觸發"""
    today = date.today()
    if _auto_close_done_for['date'] == today:
        return
    try:
        auto_close_stale_orders()
        _auto_close_done_for['date'] = today
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('換日自動結案失敗 (下一個請求會重試)')


def todays_range():
    start = datetime.combine(date.today(), datetime.min.time())
    return start, start + timedelta(days=1)


def todays_orders_condition(start, end, include_future=True):
    """
    「今天的訂單」：今天建立的，或預約今天取餐的 (前幾天預約的線上訂單)。
    include_future=False (廚房看板)：今天建立但預約明天以後取餐的先不顯示，到當天才出現。
    """
    created_today = and_(Order.created_at >= start, Order.created_at < end)
    if not include_future:
        created_today = and_(created_today, or_(Order.pickup_at.is_(None), Order.pickup_at < end))
    return or_(created_today, and_(Order.pickup_at >= start, Order.pickup_at < end))
