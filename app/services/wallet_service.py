"""
會員儲值金與會員等級、生日禮。

儲值金與點數相同設計：wallet_ledger 只新增，user.wallet_balance 為合計快取；扣款以條件式 UPDATE (餘額足夠才扣)。
會員等級：累積消費 (user.total_spent，本機已收款訂單合計) 達門檻自動升級 (只升不降，避免跨分店資料不同步時降級)。
"""
import logging
import re
from datetime import date, datetime

from sqlalchemy import func, select, update

from app.errors import AppError
from app.extensions import db
from app.models.member import MemberTier, WalletLedger
from app.models.user import User
from app.services import settings_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 儲值金
# ---------------------------------------------------------------------------
def change_balance(tenant_id, user_id, delta, reason, order_id=None, note='', staff='', clamp=False):
    """原子加減儲值金並記帳；扣款時餘額不足回傳 None。clamp=True 時最多扣到 0。"""
    delta = int(delta or 0)
    if delta == 0:
        return 0
    if clamp and delta < 0:
        current = db.session.execute(select(User.wallet_balance).where(User.id == user_id)).scalar() or 0
        delta = max(delta, -max(0, current))
        if delta == 0:
            return 0
    stmt = update(User).where(User.id == user_id, User.tenant_id == tenant_id)
    if delta < 0:
        stmt = stmt.where(User.wallet_balance >= -delta)
    if db.session.execute(stmt.values(wallet_balance=User.wallet_balance + delta)
                          .execution_options(synchronize_session=False)).rowcount != 1:
        return None
    db.session.add(WalletLedger(tenant_id=tenant_id, user_id=user_id, delta=delta, reason=reason[:20], order_id=order_id,
                                note=str(note or '')[:200], staff_username=staff or ''))
    return delta


def parse_bonus_rules(text):
    """'1000:100, 3000:400' → [(3000, 400), (1000, 100)] (金額由大到小)"""
    rules = []
    for part in re.split(r'[,，;\s]+', str(text or '').strip()):
        if ':' in part:
            a, _, b = part.partition(':')
            if a.strip().isdigit() and b.strip().isdigit():
                rules.append((int(a), int(b)))
    return sorted(rules, reverse=True)


def bonus_for(amount, tenant_id=None):
    for threshold, bonus in parse_bonus_rules(settings_service.get('member', tenant_id).get('topup_bonus')):
        if amount >= threshold:
            return bonus
    return 0


def topup(tenant_id, user, amount, method, staff, payment_info=None):
    """POS 加值：收款紀錄 + 儲值金異動 (含加值贈送)。回傳 (加值金額, 贈送)"""
    from app.models.enums import PaymentMethod, normalize_payment_method
    from app.models.payment import PaymentKind
    from app.services import payment_service
    cfg = settings_service.get('member', tenant_id)
    if not cfg.get('wallet_enabled'):
        raise AppError('尚未啟用儲值金 (系統設定 → 會員等級 / 生日禮 / 儲值金)')
    amount = int(amount or 0)
    if amount <= 0 or amount > 100000:
        raise AppError('加值金額需介於 1 ~ 100,000 元')
    method = normalize_payment_method(method)
    if method is None or method in (PaymentMethod.STORED_VALUE, PaymentMethod.PLATFORM):
        raise AppError('加值的付款方式不正確')
    bonus = bonus_for(amount, tenant_id)
    limit = cfg.get('wallet_max_balance') or 0
    db.session.refresh(user, ['wallet_balance'])
    if limit and (user.wallet_balance or 0) + amount + bonus > limit:
        raise AppError(f'加值後餘額將超過上限 ${limit:,}')
    reference, last4 = '', ''
    if method == PaymentMethod.CREDIT_CARD:
        reference, last4 = payment_service.validate_card_details(payment_info, True)
    payment_service.record_payment(None, PaymentKind.TOPUP, method, amount, reference=reference, card_last4=last4,
                                   note=f'會員 {user.name} 儲值加值', staff=staff, tenant_id=tenant_id)
    change_balance(tenant_id, user.id, amount, 'topup', note=f'{method} 加值', staff=staff)
    if bonus:
        change_balance(tenant_id, user.id, bonus, 'bonus', note=f'加值 ${amount} 贈送', staff=staff)
    return amount, bonus


# ---------------------------------------------------------------------------
# 會員等級
# ---------------------------------------------------------------------------
def tiers(tenant_id=None):
    q = MemberTier.query
    if tenant_id is not None:
        q = q.filter_by(tenant_id=tenant_id)
    return q.order_by(MemberTier.min_spend).all()


def tier_for_spend(tier_list, spent):
    chosen = None
    for t in tier_list:
        if spent >= (t.min_spend or 0):
            chosen = t
    return chosen


def recompute_spent(user):
    """本機已收款、未取消訂單的金額合計 (扣除已退款)"""
    from app.models.enums import OrderStatus
    from app.models.order import Order
    total = db.session.execute(
        select(func.coalesce(func.sum(Order.total_price - Order.refunded_amount), 0))
        .where(Order.user_id == user.id, Order.status != OrderStatus.CANCELLED,
               Order.payment_status.in_(('', 'settled', 'paid')))).scalar() or 0
    user.total_spent = int(total) + int(user.archived_spent or 0)   # 加上已封存訂單的金額
    return user.total_spent


def refresh_tier(user):
    """依累積消費升級 (只升不降)；回傳是否升級"""
    if not settings_service.get('member', user.tenant_id).get('tiers_enabled', True):
        return False
    tier_list = tiers(user.tenant_id)
    if not tier_list:
        return False
    target = tier_for_spend(tier_list, recompute_spent(user))
    current = user.tier
    if target is not None and (current is None or (target.min_spend or 0) > (current.min_spend or 0)):
        user.tier_id = target.id
        return True
    return False


# ---------------------------------------------------------------------------
# 生日禮
# ---------------------------------------------------------------------------
def grant_birthday_gifts(tenant_id, today=None):
    """
    生日當月的會員各送一次 (birthday_gift_year 記錄已送的年份，會同步到其他分店)。
    只在線上端或單機執行 (分店不執行，避免同一位會員在多家分店各送一次)。回傳送出的人數。
    """
    from app.models.coupon import Coupon, UserCoupon
    from app.models.tenant import Tenant
    from app.services import line_service
    from app.services.points_service import change_points
    cfg = settings_service.get('member', tenant_id)
    if not cfg.get('birthday_enabled'):
        return 0
    today = today or date.today()
    coupon = None
    if cfg.get('birthday_coupon_code'):
        coupon = Coupon.query.filter_by(tenant_id=tenant_id, code=cfg['birthday_coupon_code'].strip().upper()) \
            .execution_options(skip_tenant_filter=True).first()
    shop = db.session.get(Tenant, tenant_id)
    users = User.query.filter(User.tenant_id == tenant_id, User.birthday.isnot(None),
                              User.birthday_gift_year < today.year).execution_options(skip_tenant_filter=True).all()
    sent = 0
    for u in users:
        if u.birthday.month != today.month:
            continue
        claimed = db.session.execute(update(User).where(User.id == u.id, User.birthday_gift_year < today.year)
                                     .values(birthday_gift_year=today.year)
                                     .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
        if not claimed:
            continue
        if cfg.get('birthday_points'):
            change_points(tenant_id, u.id, int(cfg['birthday_points']), 'birthday')
        if coupon is not None:
            now = datetime.now()
            db.session.add(UserCoupon(tenant_id=tenant_id, user_id=u.id, coupon_id=coupon.id, code=coupon.code,
                                      created_at=now, expires_at=coupon.expires_for(now), source='birthday'))
        sent += 1
        if u.line_user_id and cfg.get('birthday_message'):
            text = cfg['birthday_message'].replace('{name}', u.name).replace('{shop}', shop.name if shop else '')
            line_service.push_text(tenant_id, u.line_user_id, text)
    db.session.commit()
    if sent:
        logger.info('店家 %s 送出 %d 份生日禮', tenant_id, sent)
    return sent
