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
PASS_TAG = '[寄杯]'   # order_pricing._parse_lines 寫在寄杯兌換品項 customization 開頭的標記


def _order_lines(order):
    """
    已建立訂單的品項 → 集點計算需要的欄位 (付款完成時才集點的訂單用)。
    - 單品作廢時 quantity 已經扣成剩下的份數 (voided_qty 只是紀錄)，不可再減一次。
    - 寄杯兌換的品項以結帳時寫在 customization 開頭的「[寄杯]」標記判斷 (有加料時單價不是 0；
      改價成 0 元的一般品項也不是寄杯)，與現場結帳看 pass_id 的結果一致。
    """
    from types import SimpleNamespace
    menu = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_({it.menu_item_id for it in order.items}))
            .execution_options(skip_tenant_filter=True).all()}
    return [SimpleNamespace(quantity=max(0, it.quantity or 0), is_reward=bool(it.is_reward),
                            is_gift=bool(it.is_gift),
                            is_pass=(it.customization or '').startswith(PASS_TAG) and not it.is_reward and not it.is_gift,
                            menu_item=menu[it.menu_item_id])
            for it in order.items if it.menu_item_id in menu]


def _points_after_voids(order):
    """
    訂單記錄的贈點 (下單時依當時應付計算)，扣掉付款前作廢的份數：桌邊未結帳時作廢品項，應付金額依
    「剩下的商品小計 / 作廢前的商品小計」等比例降低 (order_lifecycle.void_item)，贈點也依同樣比例縮減，
    被作廢、從未付款的品項不給點。沒有作廢份數時原樣回傳。
    """
    earned = order.points_earned or 0
    items = list(order.items)
    if earned <= 0 or not any((it.voided_qty or 0) > 0 for it in items):
        return earned
    left = sum((it.price or 0) * max(0, it.quantity or 0) for it in items)
    before = left + sum((it.price or 0) * max(0, it.voided_qty or 0) for it in items)
    if before <= 0:
        return earned
    return max(0, min(earned, earned * left // before))


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
        points = _points_after_voids(order)
        if points != order.points_earned:
            order.points_earned = points   # 收據 / 訂單紀錄顯示實際給的點數
        if points:
            change_points(order.tenant_id, user.id, points, 'earn', order_id=order.id)
    user.total_spent = (user.total_spent or 0) + (order.total_price or 0)
    wallet_service.refresh_tier(user)
    loyalty_service.on_checkout(order.tenant_id, user, order, lines if lines is not None else _order_lines(order),
                                order.total_price or 0)
    receipt_service.auto_on_checkout(order)
