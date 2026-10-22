"""
紅利兌換憑證 (reward_redemption) 的原子狀態轉換。

憑證只有三種狀態：open (可用) → used (結帳核銷) 或 refunded (退回點數)。
結帳、手動退點、登入/登出時的自動退還可能同時發生，因此每一次轉換都是
「WHERE status='open' 的單一條件式 UPDATE」：先搶到的一方成功 (rowcount == 1)，其餘一定失敗，
不會出現「餐點拿了、點數也退了」的雙重使用。
"""
from sqlalchemy import select, update

from app.extensions import db
from app.models.enums import VoucherStatus
from app.models.user import RewardRedemption


def list_open_vouchers(tenant_id, user_id, menu_item_id=None, limit=None, newest_first=False):
    """會員目前可用的憑證 [(id, points_cost), ...]；僅為候選清單，實際歸屬以原子 UPDATE 為準"""
    stmt = select(RewardRedemption.id, RewardRedemption.points_cost).where(
        RewardRedemption.tenant_id == tenant_id,
        RewardRedemption.user_id == user_id,
        RewardRedemption.status == VoucherStatus.OPEN,
    )
    if menu_item_id is not None:
        stmt = stmt.where(RewardRedemption.menu_item_id == menu_item_id)
    stmt = stmt.order_by(RewardRedemption.id.desc() if newest_first else RewardRedemption.id.asc())
    if limit is not None:
        stmt = stmt.limit(int(limit))
    return [(vid, int(cost or 0)) for vid, cost in db.session.execute(stmt)]


def _transition(tenant_id, user_id, voucher_id, new_status, order_id=None):
    values = {'status': new_status}
    if order_id is not None:
        values['order_id'] = order_id
    res = db.session.execute(
        update(RewardRedemption)
        .where(
            RewardRedemption.id == voucher_id,
            RewardRedemption.tenant_id == tenant_id,
            RewardRedemption.user_id == user_id,
            RewardRedemption.status == VoucherStatus.OPEN,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1


def try_claim_voucher(tenant_id, user_id, voucher_id, order_id=None):
    """結帳核銷；回傳 True 代表這張憑證確實由本次呼叫取得"""
    return _transition(tenant_id, user_id, voucher_id, VoucherStatus.USED, order_id)


def try_refund_voucher(tenant_id, user_id, voucher_id):
    """退點；回傳 True 代表這張憑證確實由本次呼叫退回 (呼叫端才可加回點數)"""
    return _transition(tenant_id, user_id, voucher_id, VoucherStatus.REFUNDED)


def claim_vouchers_for_item(tenant_id, user_id, menu_item_id, quantity):
    """為指定餐點最多核銷 quantity 張憑證，回傳實際搶到的憑證 ID (可能少於 quantity)"""
    return [
        vid for vid, _cost in list_open_vouchers(tenant_id, user_id, menu_item_id, limit=quantity)
        if try_claim_voucher(tenant_id, user_id, vid)
    ]


def refund_open_vouchers(tenant_id, user_id, menu_item_id=None, quantity=None):
    """
    退回會員尚未使用的憑證點數 (逐張原子 UPDATE)，回傳 (退回張數, 退回點數)。
    quantity 有值時優先退最近兌換的憑證。
    """
    refunded, points = 0, 0
    for vid, cost in list_open_vouchers(tenant_id, user_id, menu_item_id, newest_first=quantity is not None):
        if quantity is not None and refunded >= quantity:
            break
        if try_refund_voucher(tenant_id, user_id, vid):
            refunded += 1
            points += cost
    if points:
        adjust_points(tenant_id, user_id, points, reason='voucher_refund')
    return refunded, points


def adjust_points(tenant_id, user_id, delta, reason='adjust'):
    """原子加減會員點數並記帳 (減點時需餘額足夠)；回傳是否成功"""
    from app.services.points_service import change_points
    return change_points(tenant_id, user_id, delta, reason) is not None
