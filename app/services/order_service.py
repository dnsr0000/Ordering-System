"""
結帳與訂單狀態機 (Kiosk 與 POS 共用)。

併發控制完全交給資料庫，SQLite (本機) 與 PostgreSQL (線上) 行為一致：
- 取餐號：每店每日一列計數，條件式 UPDATE 取號 (同時結帳自然排隊)。
- 庫存 / 點數 / 憑證 / 優惠券：皆為「WHERE 剩餘量足夠」的條件式 UPDATE，rowcount 為 0 即失敗。
- 訂單狀態：WHERE status = 目前狀態 的 CAS 更新，同一張單同時操作只有一方成功。
- 重送：前端每次結帳產生一個 client_order_id (存為 Order.uuid，具唯一索引)，重送時直接回傳同一張訂單。
"""
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from flask import current_app
from sqlalchemy import case, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.enums import (
    OrderSource, OrderStatus, OrderType, PaymentMethod, VoucherStatus, normalize_payment_method,
)
from app.models.menu import ComboOption, MenuItem, ModifierOption
from app.models.order import DailyCounter, Order, OrderItem
from app.models.sync import NodeStock, PointLedger, SyncNode
from app.models.table import DiningTable
from app.models.user import CustomerLog, RewardRedemption, User
from app.services.ai_service import invalidate_ai_advice
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.reward_service import get_or_create_reward_setting
from app.services.points_service import change_points
from app.services.voucher_service import claim_vouchers_for_item
from app.sync import runtime as sync_runtime
from app.tenant_context import tenant_bypass
from app.utils import safe_int

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

PICKUP_COUNTER = 'pickup'
MAX_PICKUP_NUMBER = 999
NON_MAIN_ADDON_CATEGORIES = ('飲料', '點心', '副餐', '配餐')
_PRICE_TAG = re.compile(r'\(\+\d+\)')
_COMBO_TAG = re.compile(r'\[套餐:[^\]]*\]')


# ============================================================================
# 優惠碼
# ============================================================================
@dataclass
class PromoResult:
    valid: bool
    discount: float
    message: str
    coupon: Coupon | None = None
    user_coupon: UserCoupon | None = None

    def to_dict(self):
        return {'valid': self.valid, 'discount': self.discount, 'message': self.message}


def normalize_code(code):
    return str(code or '').strip().upper()


def find_promo(code, subtotal, user_id=None, is_pos=False):
    """驗證優惠碼 (會員持有券優先，其次為公開促銷碼)"""
    code = normalize_code(code)
    subtotal = max(0, safe_int(subtotal, 0))
    if not code:
        return PromoResult(False, 0, '請輸入優惠代碼！')

    user_coupon = None
    if user_id:
        user_coupon = UserCoupon.query.filter_by(user_id=user_id, code=code, is_used=False).first()
    coupon = user_coupon.coupon if user_coupon else None
    if coupon is None:
        public = Coupon.query.filter_by(code=code).first()
        if public is None:
            return PromoResult(False, 0, '無效的優惠代碼！')
        if public.is_reward:
            if not user_id:
                return PromoResult(False, 0, '此為會員專屬紅利券，請先於 POS 綁定會員！' if is_pos else '此為會員專屬紅利券，請先登入會員！')
            return PromoResult(False, 0, '該會員尚未持有此優惠券，或該券已核銷！')
        coupon = public

    if subtotal < coupon.min_spend:
        return PromoResult(False, 0, f'未達使用門檻！需消費滿 ${coupon.min_spend} 元才可折抵。', coupon, user_coupon)
    discount = coupon.discount_for(subtotal)
    return PromoResult(True, discount, f'已套用「{coupon.discount_label}」，折抵 ${int(discount)} 元！', coupon, user_coupon)


# ============================================================================
# 加購配額
# ============================================================================
def _addon_qualifies(main_item, addon_item):
    """正價餐點 main_item 是否符合加購品 addon_item 的觸發條件 (與前端同一套規則)"""
    trigger = addon_item.addon_trigger_type or 'any'
    target = (addon_item.addon_trigger_target or '').strip().lower()
    main_cat = (main_item.category or '未分類').strip()
    if trigger == 'category':
        return main_cat.lower() == (target or (addon_item.category or '').strip().lower())
    if trigger == 'item':  # 後台存餐點名稱，舊資料可能存 ID，兩種都接受
        return str(main_item.id) == target or (main_item.name or '').strip().lower() == target
    return main_cat not in NON_MAIN_ADDON_CATEGORIES


def _validate_addon_quota(main_units, addon_units):
    """
    每一份合格正價餐點最多搭配一份加購，且加購品必須符合觸發條件。
    以二分圖最大流量判斷是否存在合法配對 (依品項分組帶入數量)。合法回傳 None，否則回傳錯誤訊息。
    """
    mains, addons = {}, {}
    for item, qty in main_units:
        mains.setdefault(item.id, [item, 0])[1] += qty
    for item, qty in addon_units:
        addons.setdefault(item.id, [item, 0])[1] += qty
    total_addons = sum(q for _, q in addons.values())
    if total_addons == 0:
        return None
    msg = '加購品數量超過可搭配的合格餐點數量，請調整購物車後重新結帳！'
    if total_addons > sum(q for _, q in mains.values()):
        return msg

    src, snk = ('s',), ('t',)
    cap, adj = {}, {}

    def add_edge(u, v, c):
        cap[(u, v)] = cap.get((u, v), 0) + c
        cap.setdefault((v, u), 0)
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)

    for aid, (a_item, a_qty) in addons.items():
        add_edge(src, ('a', aid), a_qty)
        for mid, (m_item, _m_qty) in mains.items():
            if _addon_qualifies(m_item, a_item):
                add_edge(('a', aid), ('m', mid), a_qty)
    for mid, (_m_item, m_qty) in mains.items():
        add_edge(('m', mid), snk, m_qty)

    flow = 0
    while True:
        parent, frontier = {src: None}, [src]
        for u in frontier:
            for v in adj.get(u, ()):
                if v not in parent and cap[(u, v)] > 0:
                    parent[v] = u
                    frontier.append(v)
        if snk not in parent:
            break
        path, v = [], snk
        while parent[v] is not None:
            path.append((parent[v], v))
            v = parent[v]
        bottleneck = min(cap[e] for e in path)
        for u, v in path:
            cap[(u, v)] -= bottleneck
            cap[(v, u)] += bottleneck
        flow += bottleneck
    return None if flow >= total_addons else msg


# ============================================================================
# 結帳
# ============================================================================
@dataclass
class CheckoutLine:
    menu_item: MenuItem
    quantity: int
    unit_price: int
    customization: str
    combo: ComboOption | None = None
    is_add_on: bool = False
    is_reward: bool = False


def _clean_customization(raw, modifier_names):
    """移除前端送來的價格標籤 (+N)、套餐標籤與加料名稱 (加料由後端依 modifier_ids 重新產生)"""
    parts = []
    for part in _COMBO_TAG.sub('', str(raw or '')).split('、'):
        part = _PRICE_TAG.sub('', part).strip()
        if part and not any(name in part for name in modifier_names):
            parts.append(part)
    return parts


def _parse_lines(items_data, tenant_id):
    if not isinstance(items_data, list) or not items_data:
        raise AppError('點餐清單為空！')
    modifiers = {m.id: m for m in ModifierOption.query.filter_by(is_active=True).all()}
    modifier_names = {m.name for m in modifiers.values()}
    lines = []
    for raw in items_data:
        if not isinstance(raw, dict):
            continue
        item_id = safe_int(raw.get('menu_item_id') or raw.get('id'), 0)
        menu_item = db.session.get(MenuItem, item_id) if item_id > 0 else None
        if menu_item is None or menu_item.tenant_id != tenant_id:
            raise AppError(f"餐點【{raw.get('name') or item_id}】不存在於系統中！")
        if menu_item.is_sold_out:
            raise AppError(f'餐點【{menu_item.name}】已售完，請移除後再結帳！',
                           insufficient_stock=True, sold_out=True, sold_out_items=[menu_item.name])

        quantity = max(1, safe_int(raw.get('quantity'), 1))
        is_reward = bool(raw.get('is_reward'))
        is_add_on = bool(raw.get('is_add_on')) and not is_reward
        combo = None
        combo_id = safe_int(raw.get('combo_option_id'), 0)
        if combo_id and not (is_reward or is_add_on):
            combo = db.session.get(ComboOption, combo_id)
            if combo is None or combo.tenant_id != tenant_id or combo.main_item_id != menu_item.id:
                raise AppError(f'餐點【{menu_item.name}】的套餐選項不存在，請重新選擇！')

        mod_ids = [safe_int(m, 0) for m in raw.get('modifier_ids') or [] if str(m).isdigit()]
        if mod_ids and combo is None and menu_item.modifiers != 'addons' and not menu_item.can_be_add_on:
            raise AppError(f'餐點【{menu_item.name}】不支援客製化加料！')
        if any(m not in modifiers for m in mod_ids):
            raise AppError('包含無效或已停用的加料品項！')
        extra = sum(modifiers[m].price for m in mod_ids)

        parts = _clean_customization(raw.get('customization'), modifier_names)
        parts += [f"{modifiers[m].name}(+{modifiers[m].price})" for m in mod_ids]
        if combo is not None:
            parts.insert(0, f'[套餐: {combo.name}]')

        if is_reward:
            if not menu_item.is_reward or menu_item.redeem_points <= 0:
                raise AppError(f'餐點【{menu_item.name}】未開放紅利兌換！')
            unit_price = extra
            parts = parts or ['紅利免費兌換']
        elif is_add_on:
            if not menu_item.can_be_add_on:
                raise AppError(f'餐點【{menu_item.name}】非合法加購品項！')
            unit_price = (menu_item.add_on_price or 0) + extra
        else:
            unit_price = menu_item.unit_price + (combo.additional_price if combo else 0) + extra

        lines.append(CheckoutLine(menu_item, quantity, int(unit_price), '、'.join(parts)[:200],
                                  combo, is_add_on, is_reward))
    if not lines:
        raise AppError('無有效結帳品項！')
    return lines


def next_pickup_number(tenant_id):
    """取得今日下一個取餐號 (1~999 循環)。必須在結帳交易內呼叫：計數列會鎖到交易結束。"""
    today = date.today()
    key = dict(tenant_id=tenant_id, business_date=today, name=PICKUP_COUNTER)
    stmt = (
        update(DailyCounter)
        .where(*(getattr(DailyCounter, k) == v for k, v in key.items()))
        .values(value=DailyCounter.value % MAX_PICKUP_NUMBER + 1)
        .returning(DailyCounter.value)
        .execution_options(synchronize_session=False)
    )
    value = db.session.execute(stmt).scalar()
    if value is None:
        # 今天第一張：建立計數列 (並接續今天已存在訂單的最後號碼，例如升級當天)。
        # 兩個請求同時建立時，後到的會撞上主鍵，改為直接更新即可。
        last = db.session.execute(
            select(Order.pickup_number)
            .where(Order.tenant_id == tenant_id, Order.created_at >= datetime.combine(today, datetime.min.time()))
            .order_by(Order.id.desc()).limit(1)
        ).scalar() or 0
        try:
            with db.session.begin_nested():
                db.session.execute(insert(DailyCounter).values(**key, value=last))
        except IntegrityError:
            pass
        value = db.session.execute(stmt).scalar()
    return value


def _change_stock(tenant_id, demands, sign, node_id=None):
    """
    依品項 ID 排序後逐一增減庫存 (固定順序避免 PostgreSQL 交叉鎖死)；扣庫存失敗回傳該品項。
    node_id 有值 (線上端的線上點餐) 時改用該分店回報的庫存快照。
    """
    if node_id is not None:
        return _change_node_stock(node_id, demands, sign)
    for item_id in sorted(demands):
        qty = demands[item_id]
        stmt = update(MenuItem).where(MenuItem.id == item_id, MenuItem.tenant_id == tenant_id)
        if sign < 0:
            stmt = stmt.where(MenuItem.stock >= qty, MenuItem.is_sold_out.is_not(True)).values(
                stock=MenuItem.stock - qty,
                is_sold_out=case((MenuItem.stock - qty <= 0, True), else_=MenuItem.is_sold_out),
            )
        else:
            # 回補庫存：庫存原本歸零而被自動標為售完者解除售完；店員手動標示的售完 (庫存 > 0) 保持不變
            stmt = stmt.values(
                stock=MenuItem.stock + qty,
                is_sold_out=case((MenuItem.stock <= 0, False), else_=MenuItem.is_sold_out),
            )
        if db.session.execute(stmt.execution_options(synchronize_session=False)).rowcount == 0 and sign < 0:
            return item_id
    return None


def consume_stock_unchecked(tenant_id, demands):
    """
    分店收到線上訂單時扣庫存：訂單已在線上成立，不因本機庫存不足而失敗 (最多扣到 0 並標示售完)，
    店員可視情況取消訂單 (取消時會回補)。
    """
    for item_id in sorted(demands):
        qty = demands[item_id]
        db.session.execute(
            update(MenuItem).where(MenuItem.id == item_id, MenuItem.tenant_id == tenant_id)
            .values(stock=case((MenuItem.stock - qty < 0, 0), else_=MenuItem.stock - qty),
                    is_sold_out=case((MenuItem.stock - qty <= 0, True), else_=MenuItem.is_sold_out))
            .execution_options(synchronize_session=False))


def _change_node_stock(node_id, demands, sign):
    """線上端：以分店回報的庫存預扣 (分店下次回報時會以實際庫存覆蓋)；沒有回報過的品項視為不供應"""
    for item_id in sorted(demands):
        qty = demands[item_id]
        stmt = update(NodeStock).where(NodeStock.node_id == node_id, NodeStock.menu_item_id == item_id)
        if sign < 0:
            stmt = stmt.where(NodeStock.stock >= qty, NodeStock.is_sold_out.is_(False)).values(stock=NodeStock.stock - qty)
        else:
            stmt = stmt.values(stock=NodeStock.stock + qty)
        if db.session.execute(stmt.execution_options(synchronize_session=False)).rowcount == 0 and sign < 0:
            return item_id
    return None


def online_pickup_node(tenant_id, node_uuid):
    """線上端：顧客選擇的取餐分店 (需啟用且開放線上點餐)"""
    node = SyncNode.query.filter_by(tenant_id=tenant_id, uuid=str(node_uuid or ''), is_active=True,
                                    accepts_online_orders=True).first() if node_uuid else None
    if node is None:
        raise AppError('請先選擇取餐分店！')
    if not node.is_online(current_app.config['SYNC_NODE_OFFLINE_AFTER']):
        raise AppError(f'【{node.name}】目前離線，暫停線上點餐，請稍後再試或改選其他分店！')
    return node


def _apply_promo(tenant_id, code, subtotal, user_id):
    """在交易內套用優惠碼：會員持有券以條件式 UPDATE 核銷。回傳 (折抵金額, 實際套用代碼, 會員券 ID)"""
    if not code:
        return 0, '', None
    promo = find_promo(code, subtotal, user_id)
    if not promo.valid:
        return 0, '', None  # 代碼無效或未達門檻：照常結帳，只是不折抵
    if promo.user_coupon is not None:
        claimed = db.session.execute(
            update(UserCoupon)
            .where(UserCoupon.id == promo.user_coupon.id, UserCoupon.tenant_id == tenant_id, UserCoupon.is_used.is_(False))
            .values(is_used=True, used_at=datetime.now())
            .execution_options(synchronize_session=False)
        ).rowcount
        if not claimed:
            raise AppError('該優惠券已被核銷！')
        return promo.discount, promo.coupon.code, promo.user_coupon.id
    return promo.discount, promo.coupon.code, None


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
                     client_ip='', client_order_id=None, node_uuid=None, dining_table=None):
    """
    原子結帳。成功回傳 (Order, replayed)；任何驗證失敗拋出 AppError，整筆交易不留痕跡。
    replayed=True 代表同一個 client_order_id 已經結帳過，回傳的是原本那張訂單。
    線上端 (hub) 為線上點餐：node_uuid 為顧客選擇的取餐分店，庫存以該分店回報的數量檢查，
    取餐號由分店收到訂單時編號 (線上端先存 0)。
    dining_table：門市桌邊掃碼點餐的桌位 (內用、標記桌號、餐後至櫃檯結帳：payment_status='unpaid')。
    """
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

    user = db.session.get(User, safe_int(user_id, 0)) if user_id else None
    if user is not None and user.tenant_id != tenant_id:
        user = None
    lines = _parse_lines(items, tenant_id)

    if any(ln.is_reward for ln in lines) and user is None:
        raise AppError('請先綁定會員才可使用紅利點數兌換商品！')
    quota_error = _validate_addon_quota(
        [(ln.menu_item, ln.quantity) for ln in lines
         if not ln.is_add_on and not ln.is_reward and not (ln.combo and ln.combo.can_addon is False)],
        [(ln.menu_item, ln.quantity) for ln in lines if ln.is_add_on],
    )
    if quota_error:
        raise AppError(quota_error)

    demands = {}
    for ln in lines:
        demands[ln.menu_item.id] = demands.get(ln.menu_item.id, 0) + ln.quantity
        for side_id in (ln.combo.side_item_ids if ln.combo else []):
            demands[side_id] = demands.get(side_id, 0) + ln.quantity
    subtotal = sum(ln.unit_price * ln.quantity for ln in lines)
    is_pos = order_source == OrderSource.POS
    stock_node = None
    payment_status = ''
    if sync_runtime.is_hub():
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

    try:
        # ---- 交易開始：先取號 (鎖住今日計數列)，之後的寫入都在同一筆交易 ----
        pickup = 0 if stock_node else next_pickup_number(tenant_id)

        short_id = _change_stock(tenant_id, demands, -1, node_id=stock_node.id if stock_node else None)
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

        promo_discount, applied_code, applied_uc_id = _apply_promo(
            tenant_id, normalize_code(promo_code), subtotal, user.id if user else None)

        # 點數折抵 (受單筆上限限制，只能折整數元)
        setting = get_or_create_reward_setting(tenant_id, commit=False)
        remaining = max(0, subtotal - promo_discount)
        points_used = points_discount = 0
        if user and setting.is_enabled and safe_int(use_points, 0) > 0:
            ppd = max(1, setting.points_per_dollar or 1)
            db.session.refresh(user, ['points'])
            max_cash = int(math.floor(remaining))
            if setting.max_discount_per_order:
                max_cash = min(max_cash, setting.max_discount_per_order)
            points_discount = min(min(user.points or 0, safe_int(use_points, 0)) // ppd, max_cash)
            points_used = points_discount * ppd
            if points_used and change_points(tenant_id, user.id, -points_used, 'redeem') is None:
                raise AppError('會員紅利點數不足！')

        # 應付金額四捨五入到整數元，折扣總額以「小計 - 應付」反推，確保 小計 = 應付 + 折扣
        final_price = int(math.floor(max(0, remaining - points_discount) + 0.5))
        total_discount = max(0, subtotal - final_price)
        points_earned = final_price // max(1, setting.spend_per_point or 100) if user else 0
        if points_earned:
            change_points(tenant_id, user.id, points_earned, 'earn')

        cash_in = max(0, safe_int(cash_received, 0))
        pays_cash_at_pos = is_pos and payment == PaymentMethod.CASH
        if pays_cash_at_pos and 0 < cash_in < final_price:
            raise AppError(f'實收現金 (${cash_in}) 小於應付總額 (${final_price})！')
        actual_received = cash_in if (pays_cash_at_pos and cash_in >= final_price) else final_price
        cash_change = actual_received - final_price if pays_cash_at_pos else 0

        table_input = str(table_input or '').strip()[:40]
        if table_input:
            table_display = f"{table_input} ({user.name})" if user and user.name not in table_input else table_input
        elif user:
            table_display = f"POS-{user.name}" if is_pos else user.name
        else:
            table_display = '櫃檯POS' if is_pos else '訪客'

        now = datetime.now()
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
            dining_table_id=dining_table_id,
        )
        if client_order_id:
            order.uuid = client_order_id
        order.items = [
            OrderItem(tenant_id=tenant_id, menu_item_id=ln.menu_item.id, combo_option_id=ln.combo.id if ln.combo else None,
                      item_name=ln.menu_item.name, price=ln.unit_price, quantity=ln.quantity,
                      customization=ln.customization, is_reward=ln.is_reward)
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
        if claimed_vouchers:
            db.session.execute(
                update(RewardRedemption).where(RewardRedemption.id.in_(claimed_vouchers))
                .values(order_id=order.id).execution_options(synchronize_session=False))

        if is_pos:
            record_staff_audit(
                'POS_CREATE_ORDER', target_order_id=order.id, target_user_id=order.user_id, tenant_id=tenant_id,
                points_delta=points_earned - points_used - reward_points_spent,
                details=(f"POS 結帳單號 #{order.id} (取餐 #{pickup:03d}) | 顧客:{table_display} | 付款:{payment} "
                         f"應收:${final_price} 實收:${actual_received} 找零:${cash_change} | 兌換扣點:{reward_points_spent} "
                         f"折抵扣點:{points_used} 贈點:{points_earned} 優惠碼:{applied_code or '無'}"),
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

    invalidate_ai_advice(tenant_id)
    order_event_bus.notify(tenant_id)
    return order, False


# ============================================================================
# 訂單狀態機
# ============================================================================
def change_order_status(order_id, target_status, staff, tenant_id):
    """
    變更訂單狀態。判斷順序刻意先驗權限 (403) 再查訂單 (404 / 409)，
    無權限者不論訂單存在與否、狀態為何，回應都相同，無法藉此探測訂單資訊。
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
        _rollback_cancelled_order(order, tenant_id)
    elif target == OrderStatus.PICKED_UP:
        record_staff_audit('MARK_PICKED_UP', target_order_id=order.id, target_user_id=order.user_id, tenant_id=tenant_id,
                           details=f"核銷完成取餐單號 #{order.id} (取餐 #{order.pickup_number:03d})")
    db.session.commit()
    invalidate_ai_advice(tenant_id)
    order_event_bus.notify(tenant_id)
    return order


def order_stock_demands(order):
    """訂單佔用的庫存 (含套餐配餐) {menu_item_id: 數量}"""
    demands = {}
    for item in order.items:
        if item.menu_item_id:
            demands[item.menu_item_id] = demands.get(item.menu_item_id, 0) + item.quantity
        combo = db.session.get(ComboOption, item.combo_option_id) if item.combo_option_id else None
        for side_id in (combo.side_item_ids if combo else []):
            demands[side_id] = demands.get(side_id, 0) + item.quantity
    return demands


def flag_refund_if_paid(order_id):
    """
    已付款的訂單被取消：標記需退款 (後台顯示「需退款」)。
    線上付款 (paid) 到綠界後台退刷；桌邊掃碼點餐已在櫃檯結帳 (settled) 則由櫃檯退款。
    """
    db.session.execute(update(Order).where(Order.id == order_id, Order.payment_status.in_(('paid', 'settled')))
                       .values(payment_status='refund_required').execution_options(synchronize_session=False))


def _rollback_cancelled_order(order, tenant_id):
    """取消訂單：回補庫存 (含套餐配餐)、退回兌換點數與折抵點數、追回贈點、還原核銷的會員券、已付款標記需退款"""
    flag_refund_if_paid(order.id)
    if sync_runtime.owns_stock(order):
        _change_stock(tenant_id, order_stock_demands(order), +1)
    elif sync_runtime.is_hub() and order.node_uuid:
        node = SyncNode.query.filter_by(tenant_id=tenant_id, uuid=order.node_uuid).first()
        if node is not None:
            _change_node_stock(node.id, order_stock_demands(order), +1)

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

    record_staff_audit(
        'CANCEL_ORDER', target_order_id=order.id, target_user_id=order.user_id, tenant_id=tenant_id,
        points_delta=points_delta,
        details=f"取消訂單 #{order.id} (取餐 #{order.pickup_number:03d})，已全量回滾庫存、商品兌換點數({redeemed_points}點)、折抵與贈點{note}",
    )


def complete_all_pending_orders(tenant_id):
    """廚房一鍵出餐：所有製作中訂單改為已出餐，回傳張數"""
    count = db.session.execute(
        update(Order).where(Order.tenant_id == tenant_id, Order.status == OrderStatus.PENDING)
        .values(status=OrderStatus.COMPLETED, completed_at=datetime.now())
        .execution_options(synchronize_session=False)
    ).rowcount
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
            .where(Order.status == OrderStatus.PENDING, Order.created_at < today_start)
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
