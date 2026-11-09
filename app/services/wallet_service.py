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
from app.services.points_service import atomic_change, change_points
from app.utils import safe_int

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 儲值金
# ---------------------------------------------------------------------------
def change_balance(tenant_id, user_id, delta, reason, order_id=None, note='', staff='', clamp=False):
    """原子加減儲值金並記帳 (與點數同一套 points_service.atomic_change)；扣款時餘額不足回傳 None。clamp=True 時最多扣到 0。"""
    return atomic_change(User.wallet_balance,
                         lambda d: WalletLedger(tenant_id=tenant_id, user_id=user_id, delta=d, reason=reason[:20],
                                                order_id=order_id, note=str(note or '')[:200], staff_username=staff or ''),
                         tenant_id, user_id, delta, clamp)


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


TOPUP_METHODS = ('Cash', 'Credit Card', 'Line Pay', '街口支付', 'Apple Pay')   # 加值可收的付款方式 (實際收款)


def topup(tenant_id, user, amount, method, staff, payment_info=None, request_id=None):
    """
    POS 加值：收款紀錄 + 儲值金異動 (含加值贈送)。回傳 (加值金額, 贈送)。
    request_id：前端產生的冪等鍵 (同內容重送時相同)；同一個 request_id 已加值過時不再收款 / 加值，直接回傳第一次的結果。
    """
    from app.models.enums import PaymentMethod, normalize_payment_method
    from app.models.payment import PaymentKind, PaymentRecord
    from app.services import payment_service
    cfg = settings_service.get('member', tenant_id)
    if not cfg.get('wallet_enabled'):
        raise AppError('尚未啟用儲值金 (系統設定 → 會員等級 / 生日禮 / 儲值金)')
    amount = safe_int(amount, 0)
    if amount <= 0 or amount > 100000:
        raise AppError('加值金額需介於 1 ~ 100,000 元')
    method = normalize_payment_method(method)
    if method not in TOPUP_METHODS:   # 禮物卡 / ATM / 超商代碼 / 混合付款 / 儲值金 / 平台代收 都不是實際收款
        raise AppError('加值的付款方式不正確')
    if payment_service.open_shift_id(tenant_id, lock=True) is None \
            and settings_service.get('pos', tenant_id).get('require_shift'):
        raise AppError('尚未開班：請先在 POS 按「交班」輸入開班零用金後再收款', 409, need_shift=True)
    bonus = bonus_for(amount, tenant_id)
    limit = cfg.get('wallet_max_balance') or 0
    # 先鎖住會員資料列再讀餘額：兩台 POS 同時加值時依序檢查上限 (PostgreSQL；SQLite 寫入本來就依序)
    db.session.execute(select(User.id).where(User.id == user.id).with_for_update())
    db.session.refresh(user, ['wallet_balance'])
    # 冪等：已鎖住會員資料列，同一個 request_id 的重送 (連點 / 逾時重按) 依序到這裡時看得到第一次的收款紀錄
    rid = str(request_id or '').strip()[:64]
    rid_col = getattr(PaymentRecord, 'request_id', None) if rid else None   # 欄位由遷移新增；舊結構時略過冪等檢查
    if rid_col is not None:
        done = PaymentRecord.query.filter(PaymentRecord.tenant_id == tenant_id, PaymentRecord.kind == PaymentKind.TOPUP,
                                          rid_col == rid).execution_options(skip_tenant_filter=True).first()
        if done is not None:
            return done.amount, bonus_for(done.amount, tenant_id)
    if limit and (user.wallet_balance or 0) + amount + bonus > limit:
        raise AppError(f'加值後餘額將超過上限 ${limit:,}')
    reference, last4 = '', ''
    if method == PaymentMethod.CREDIT_CARD:
        reference, last4 = payment_service.validate_card_details(payment_info, True)
    rec = payment_service.record_payment(None, PaymentKind.TOPUP, method, amount, reference=reference, card_last4=last4,
                                         note=f'會員 {user.name} 儲值加值', staff=staff, tenant_id=tenant_id)
    if rid_col is not None:
        rec.request_id = rid
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
    from app.services.report_service import REVENUE_STATUSES
    total = db.session.execute(
        select(func.coalesce(func.sum(Order.total_price - Order.refunded_amount), 0))
        .where(Order.user_id == user.id, Order.status != OrderStatus.CANCELLED,
               Order.payment_status.in_(REVENUE_STATUSES))).scalar() or 0
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
    cfg = settings_service.get('member', tenant_id)
    if not cfg.get('birthday_enabled'):
        return 0
    today = today or date.today()
    coupon = None
    if cfg.get('birthday_coupon_code'):
        coupon = Coupon.query.filter_by(tenant_id=tenant_id, code=cfg['birthday_coupon_code'].strip().upper()) \
            .execution_options(skip_tenant_filter=True).first()
    coupon_err = coupon.period_error(today) if coupon is not None else None
    if coupon_err:
        # 券不在活動期間 (已結束 / 尚未開始)：不發一張不能用的券，通知店家延長活動
        from app.services import monitor_service
        monitor_service.alert(f'生日禮優惠券「{coupon.code}」{coupon_err}，今天未發券；請到行銷活動調整券的活動期間',
                              key=f'birthday-coupon-{tenant_id}', tenant_id=tenant_id, level='warning', source='member')
        coupon = None
        if not cfg.get('birthday_points'):
            return 0   # 只設定了券：先不佔用今年的生日禮，券修好後還能補發
    shop = db.session.get(Tenant, tenant_id)
    users = User.query.filter(User.tenant_id == tenant_id, User.birthday.isnot(None),
                              User.birthday_gift_year < today.year).execution_options(skip_tenant_filter=True).all()
    sent, pushes = 0, []
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
        # 生日點數 / 券是會員權益照發；店家自訂的生日文字屬行銷推播，拒絕行銷的會員不推
        if u.line_user_id and not u.marketing_opt_out and cfg.get('birthday_message'):
            text = cfg['birthday_message'].replace('{name}', u.name).replace('{shop}', shop.name if shop else '')
            pushes.append((u.line_user_id, text))
    db.session.commit()
    # 提交後才推播：呼叫 LINE API 期間不持有未提交的寫入 (SQLite 寫入鎖)
    for line_user_id, text in pushes:
        line_service.push_text(tenant_id, line_user_id, text)
    if sent:
        logger.info('店家 %s 送出 %d 份生日禮', tenant_id, sent)
    return sent
