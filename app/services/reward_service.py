"""紅利商城：紅利設定、兌換優惠券、兌換商品憑證與退點"""
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.enums import VoucherStatus
from app.models.menu import MenuItem, ModifierOption
from app.models.user import RewardRedemption, RewardSetting, User
from app.services.voucher_service import adjust_points, refund_open_vouchers


def get_or_create_reward_setting(tenant_id, commit=True):
    """
    取得店家的紅利設定，沒有時建立預設值。reward_setting.tenant_id 有唯一索引：
    多個請求同時第一次進來時只有一個建立成功，其餘撞到唯一索引就改讀已存在的那一筆。
    """
    setting = RewardSetting.query.filter_by(tenant_id=tenant_id).first()
    if setting is not None:
        return setting
    try:
        with db.session.begin_nested():
            setting = RewardSetting(tenant_id=tenant_id, is_enabled=True, points_per_dollar=1,
                                    max_discount_per_order=0, spend_per_point=100)
            db.session.add(setting)
    except IntegrityError:
        setting = RewardSetting.query.filter_by(tenant_id=tenant_id).first()
    if commit:
        db.session.commit()
    return setting


def redeem_coupon(tenant_id, user_id, coupon_id):
    """以點數兌換紅利優惠券 (每人兌換上限以「扣點後再數一次」防止同時送出繞過)"""
    coupon = Coupon.query.filter_by(id=coupon_id, tenant_id=tenant_id).first()
    if coupon is None or not coupon.is_reward or coupon.member_only:
        raise AppError('無效的優惠券！')
    period_error = coupon.period_error()
    if period_error:
        raise AppError(f'❌ 此優惠券{period_error}，暫時無法兌換！')
    limit = coupon.per_user_limit or 0
    limit_msg = f'❌ 您已達此優惠券的兌換上限（每位會員限兌換 {limit} 次）！'

    def owned():
        return UserCoupon.query.filter_by(user_id=user_id, coupon_id=coupon.id).count()

    if limit and owned() >= limit:
        raise AppError(limit_msg)
    cost = coupon.redeem_points
    if not adjust_points(tenant_id, user_id, -cost, reason='redeem_coupon'):
        db.session.rollback()
        raise AppError(f'紅利不足！需要 {cost} 點。')
    now = datetime.now()
    db.session.add(UserCoupon(tenant_id=tenant_id, user_id=user_id, coupon_id=coupon.id, code=coupon.code,
                              created_at=now, expires_at=coupon.expires_for(now), source='reward'))
    db.session.flush()
    # 扣點的 UPDATE 已取得寫入鎖，這裡重新計數時其他請求的結果已可見；超過上限就整筆取消
    if limit and owned() > limit:
        db.session.rollback()
        raise AppError(limit_msg)
    db.session.commit()
    return coupon


def redeem_item(tenant_id, user_id, item_id, modifier_ids=(), customization=''):
    """以點數兌換紅利商品，產生一張可在結帳時核銷的憑證；回傳 (MenuItem, 憑證, 購物車用的品項資料)"""
    item = MenuItem.query.filter_by(id=item_id, tenant_id=tenant_id).first()
    if item is None or not item.is_reward or not item.is_available:
        raise AppError('商品不存在或已售罄！')
    cost = item.redeem_points
    if not adjust_points(tenant_id, user_id, -cost, reason='redeem_item'):
        db.session.rollback()
        raise AppError(f'紅利不足！需要 {cost} 點。')
    voucher = RewardRedemption(tenant_id=tenant_id, user_id=user_id, menu_item_id=item.id,
                               points_cost=cost, status=VoucherStatus.OPEN)
    db.session.add(voucher)
    db.session.commit()

    mods = ModifierOption.query.filter(ModifierOption.id.in_(list(modifier_ids)), ModifierOption.is_active.is_(True)).all() \
        if modifier_ids else []
    cart_line = {
        'menu_item_id': item.id,
        'redemption_id': voucher.id,
        'name': item.name,
        'price': sum(m.price for m in mods),
        'points_cost': cost,
        'is_reward': True,
        'customization': str(customization or '').strip(),
        'quantity': 1,
        'modifier_ids': [m.id for m in mods],
    }
    return item, voucher, cart_line


def refund_item(tenant_id, user_id, item_id, quantity=1):
    """退回尚未結帳的兌換商品 (只能退有憑證的份數，憑證以原子 UPDATE 退回，無法重複退點)"""
    item = MenuItem.query.filter_by(id=item_id, tenant_id=tenant_id).first()
    if item is None or not item.is_reward:
        raise AppError('查無此兌換商品或非有效兌換品項！')
    refunded, points = refund_open_vouchers(tenant_id, user_id, item.id, quantity=quantity)
    if refunded < quantity:
        db.session.rollback()
        raise AppError('查無可退還之有效兌換紀錄，無法重複退點！')
    db.session.commit()
    return points


def member_points(user_id):
    db.session.expire_all()
    user = db.session.get(User, user_id)
    return user.points if user else 0
