"""
寄杯 / 多次券：會員一次買 N 杯 (份)，之後每次來店或線上點餐扣一杯。

- 購買：POS 綁定會員後售出，建立一張「寄杯購買」訂單 (收款、發票、交班、作廢退款都沿用訂單流程，不送廚房)，
  並建立會員寄杯 MemberPass 與 +N 的異動。售出的訂單作廢時：寄杯未使用才可作廢 (寄杯一併作廢)。
- 兌換：結帳品項帶 pass_id (會員自己的寄杯、餐點相符) → 該品項免費 (只收加料)，條件式扣剩餘次數；訂單取消時退回。
- 剩餘次數 = pass_ledger 合計 (同點數：跨分店各自扣、同步合併後重算；兩家分店同時用完會變負數並通知店家)。
"""
from datetime import datetime

from sqlalchemy import func, select, update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderSource, OrderStatus, OrderType, PaymentMethod, normalize_payment_method
from app.models.member import MemberPass, PassLedger, PassProduct
from app.models.menu import MenuItem
from app.utils import safe_int

SALE_LABEL = '寄杯購買'


# ---------------------------------------------------------------------------
# 商品
# ---------------------------------------------------------------------------
def save_product(product, form):
    """後台新增 / 編輯寄杯商品 (由呼叫端 commit)"""
    name = str(form.get('name') or '').strip()[:60]
    item = db.session.get(MenuItem, safe_int(form.get('menu_item_id'), 0))
    uses, price = safe_int(form.get('uses'), 0), safe_int(form.get('price'), -1)
    if not name:
        raise AppError('請輸入寄杯名稱')
    if item is None or item.tenant_id != product.tenant_id:
        raise AppError('請選擇可兌換的餐點')
    if not 2 <= uses <= 100:
        raise AppError('次數需介於 2 ~ 100')
    if price < 0 or price > 100000:
        raise AppError('售價不正確')
    product.name, product.menu_item_id, product.uses, product.price = name, item.id, uses, price
    product.description = str(form.get('description') or '').strip()[:200]
    product.is_active = form.get('is_active', '1') not in ('0', '', None)
    return product


def active_products():
    return PassProduct.query.filter_by(is_active=True).order_by(PassProduct.id).all()


# ---------------------------------------------------------------------------
# 查詢 / 重算
# ---------------------------------------------------------------------------
def member_passes(user_id, include_empty=False):
    q = MemberPass.query.filter(MemberPass.user_id == user_id, MemberPass.status == 'active')
    if not include_empty:
        q = q.filter(MemberPass.remaining > 0)
    return q.order_by(MemberPass.created_at).all()


def recompute(pass_ids):
    """以異動帳重算剩餘次數 (同步合併後呼叫)；回傳剩餘為負數的寄杯 (兩家分店同時用完)"""
    ids = sorted({i for i in pass_ids if i})
    if not ids:
        return []
    total = select(func.coalesce(func.sum(PassLedger.delta), 0)).where(PassLedger.member_pass_id == MemberPass.id) \
        .scalar_subquery()
    db.session.execute(update(MemberPass).where(MemberPass.id.in_(ids)).values(remaining=total)
                       .execution_options(synchronize_session=False))
    return MemberPass.query.filter(MemberPass.id.in_(ids), MemberPass.remaining < 0) \
        .execution_options(skip_tenant_filter=True).all()


def _add(member_pass, delta, reason, order_id=None, staff=''):
    db.session.add(PassLedger(tenant_id=member_pass.tenant_id, member_pass_id=member_pass.id, delta=delta,
                              reason=reason, order_id=order_id, staff_username=staff or ''))


# ---------------------------------------------------------------------------
# 購買
# ---------------------------------------------------------------------------
def sell(tenant_id, product_id, user, payment_method, staff, cash_received=0, payment_info=None, invoice_info=None):
    """
    POS 售出寄杯：建立寄杯購買訂單 (不送廚房、不扣庫存) 與會員寄杯。由呼叫端 commit。回傳 (MemberPass, Order)。
    """
    from app.models.order import Order, OrderItem
    from app.models.payment import PaymentKind
    from app.services import payment_service
    product = db.session.get(PassProduct, safe_int(product_id, 0))
    if product is None or product.tenant_id != tenant_id or not product.is_active:
        raise AppError('查無此寄杯商品', 404)
    if user is None or user.tenant_id != tenant_id:
        raise AppError('購買寄杯請先綁定會員')
    method = normalize_payment_method(payment_method)
    allowed = payment_service.allowed_methods('pos')
    if method is None or method not in allowed or method in (PaymentMethod.STORED_VALUE, PaymentMethod.GIFT_CARD,
                                                              PaymentMethod.SPLIT, PaymentMethod.PLATFORM):
        raise AppError(f'寄杯不可使用「{payment_method}」付款')
    if method == PaymentMethod.LINE_PAY:
        from app.services import linepay
        if linepay.is_enabled(tenant_id):
            raise AppError('寄杯暫不支援 LINE Pay 串接扣款，請改用其他付款方式')
    reference, last4 = '', ''
    if method == PaymentMethod.CREDIT_CARD:
        reference, last4 = payment_service.validate_card_details(payment_info, True)
    price = product.price
    received = safe_int(cash_received, 0) if method == PaymentMethod.CASH else price
    if method == PaymentMethod.CASH:
        received = received or price
        if received < price:
            raise AppError(f'實收現金 (${received}) 小於售價 (${price})！')
    now = datetime.now()
    from app.sync import runtime as sync_runtime
    order = Order(tenant_id=tenant_id, user_id=user.id, table_number=user.name, pickup_number=0, total_price=price,
                  payment_method=method, order_type=OrderType.TAKE_OUT, order_source=OrderSource.POS,
                  cashier_username=staff, cash_received=received, cash_change=received - price, need_cutlery=False,
                  note=SALE_LABEL, status=OrderStatus.PICKED_UP, completed_at=now, created_at=now,
                  node_uuid=sync_runtime.local_node_uuid(), payment_reference=reference, card_last4=last4,
                  shift_id=payment_service.open_shift_id(tenant_id, lock=True))
    order.items = [OrderItem(tenant_id=tenant_id, menu_item_id=None, item_name=f'{SALE_LABEL}：{product.name}',
                             price=price, quantity=1, customization=f'{product.uses} 次')]
    db.session.add(order)
    db.session.flush()
    mp = MemberPass(tenant_id=tenant_id, user_id=user.id, product_id=product.id, menu_item_id=product.menu_item_id,
                    name=product.name, total_uses=product.uses, price_paid=price, sale_order_id=order.id,
                    remaining=product.uses, created_at=now)
    db.session.add(mp)
    db.session.flush()
    _add(mp, product.uses, 'sale', order.id, staff)
    payment_service.record_payment(order, PaymentKind.SALE, method, price, received=received, change=received - price,
                                   reference=reference, card_last4=last4, staff=staff, note=f'{SALE_LABEL} {product.name}')
    payment_service.after_paid(order, invoice_info)
    from app.services.audit import record_staff_audit
    record_staff_audit('PASS_SELL', target_order_id=order.id, target_user_id=user.id, tenant_id=tenant_id,
                       details=f'售出寄杯 {product.name} ({product.uses} 次) ${price} 給 {user.name}')
    return mp, order


def on_sale_order_cancelled(order):
    """寄杯購買訂單作廢：寄杯還沒用過才可作廢 (寄杯一併作廢)；用過的請改用部分退款"""
    for mp in MemberPass.query.filter_by(sale_order_id=order.id).all():
        used = (mp.total_uses or 0) - (mp.remaining or 0)
        if used > 0:
            raise AppError(f'寄杯「{mp.name}」已使用 {used} 次，無法整筆作廢；請改用部分退款', 409)
        if mp.status == 'active':
            mp.status = 'void'
            _add(mp, -(mp.remaining or 0), 'void', order.id)
            mp.remaining = 0


# ---------------------------------------------------------------------------
# 兌換 (結帳交易中)
# ---------------------------------------------------------------------------
def find_for_line(tenant_id, pass_id, user, menu_item):
    """結帳品項帶 pass_id：必須是會員自己的有效寄杯、餐點相符。回傳 MemberPass"""
    mp = db.session.get(MemberPass, safe_int(pass_id, 0))
    if mp is None or mp.tenant_id != tenant_id or user is None or mp.user_id != user.id or mp.status != 'active':
        raise AppError('找不到這張寄杯 (請先登入 / 綁定持有的會員)')
    if mp.menu_item_id != menu_item.id:
        raise AppError(f'寄杯「{mp.name}」不能兌換【{menu_item.name}】')
    return mp


def redeem(member_pass, quantity, order_id, staff=''):
    """條件式扣剩餘次數 (兩個通路同時兌換同一張寄杯不會扣成負數)"""
    claimed = db.session.execute(
        update(MemberPass).where(MemberPass.id == member_pass.id, MemberPass.remaining >= quantity)
        .values(remaining=MemberPass.remaining - quantity).execution_options(synchronize_session=False)).rowcount
    if not claimed:
        db.session.refresh(member_pass, ['remaining'])
        raise AppError(f'寄杯「{member_pass.name}」剩餘 {member_pass.remaining} 次，不足兌換 {quantity} 份'
                       f' (可能剛在其他裝置或門市使用)', 409, points_changed=True)
    _add(member_pass, -quantity, 'redeem', order_id, staff)


def on_order_cancelled(order):
    """兌換寄杯的訂單取消：退回次數 (只退這張訂單淨扣掉的)；寄杯購買訂單另由 on_sale_order_cancelled 處理"""
    net = {}
    for row in PassLedger.query.filter(PassLedger.order_id == order.id,
                                       PassLedger.reason.in_(('redeem', 'cancel'))).all():
        net[row.member_pass_id] = net.get(row.member_pass_id, 0) + row.delta
    for pass_id, delta in sorted(net.items()):
        if delta < 0:
            mp = db.session.get(MemberPass, pass_id)
            if mp is not None:
                db.session.execute(update(MemberPass).where(MemberPass.id == pass_id)
                                   .values(remaining=MemberPass.remaining - delta)
                                   .execution_options(synchronize_session=False))
                _add(mp, -delta, 'cancel', order.id)
