"""
付款完成才給的會員回饋 (贈點、累積消費 / 等級、集點卡與推薦、自動電子收據)。
結帳當下已付款的訂單在結帳時給 (order_checkout)；線上付款 (payment_ecpay)、桌邊結帳 (table_service)、
POS LINE Pay (payment_service.charge_linepay) 在付款完成時才給。
"""
from app.models.menu import MenuItem
from app.models.sync import PointLedger
from app.services import wallet_service
from app.services.points_service import change_points

PAID_STATUSES = ('', 'paid', 'settled')


def _order_lines(order):
    """已建立訂單的品項 → 集點計算需要的欄位 (付款完成時才集點的訂單用；寄杯兌換是 0 元且非贈品 / 兌換)"""
    from types import SimpleNamespace
    menu = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_({it.menu_item_id for it in order.items}))
            .execution_options(skip_tenant_filter=True).all()}
    return [SimpleNamespace(quantity=max(0, (it.quantity or 0) - (it.voided_qty or 0)), is_reward=bool(it.is_reward),
                            is_gift=bool(it.is_gift), is_pass=(it.price or 0) == 0 and not it.is_reward and not it.is_gift,
                            menu_item=menu[it.menu_item_id])
            for it in order.items if it.menu_item_id in menu]


def grant_paid_rewards(order, lines=None):
    """
    付款完成才給的會員回饋：贈點、累積消費 / 等級、集點卡與推薦獎勵、自動電子收據。由呼叫端 commit。
    結帳當下已付款的訂單在結帳時呼叫；線上付款、桌邊結帳、POS LINE Pay 在付款完成時呼叫 (各自只會發生一次)。
    未付款就給的話，顧客可以先把點數用掉、訂單逾時取消後又追不回。贈點以點數異動帳 (earn + 訂單) 防止重複入帳。
    """
    user = order.user
    if user is None:
        return
    from app.services import loyalty_service, receipt_service
    if order.points_earned and not PointLedger.query.filter_by(order_id=order.id, reason='earn') \
            .execution_options(skip_tenant_filter=True).first():
        change_points(order.tenant_id, user.id, order.points_earned, 'earn', order_id=order.id)
    user.total_spent = (user.total_spent or 0) + (order.total_price or 0)
    wallet_service.refresh_tier(user)
    loyalty_service.on_checkout(order.tenant_id, user, order, lines if lines is not None else _order_lines(order),
                                order.total_price or 0)
    receipt_service.auto_on_checkout(order)
