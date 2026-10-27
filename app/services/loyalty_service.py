"""
會員忠誠度：點數到期 (先進先出)、集點卡、推薦好友。

點數到期
- 系統設定「會員等級 / 生日禮 / 儲值金」的點數有效期限 (月)。先得到的點數先使用、先到期：
  到期量 = 有效期限以前得到的點數合計 − 至今所有扣點合計 (含已到期扣除的)，不超過目前點數。
- 只在線上端或單機執行 (點數異動帳會同步，分店不執行，避免同一筆點數被多台主機各扣一次)。
- 到期前 N 天以 LINE 提醒 (每個提醒期間只通知一次)。

集點卡
- 每購買 1 份指定分類的餐點 (或每筆消費滿額) 得 1 章，集滿自動扣章並送點數 / 優惠券；訂單取消時扣回。
- 章數記在 stamp_ledger (只新增、跨分店同步合併)，目前章數 = 合計。

推薦好友
- 會員有自己的推薦碼；新會員註冊時填寫，完成第一筆 (達門檻) 消費時雙方各得點數 (point_ledger reason referral / referred)，
  該訂單取消時扣回。每位新會員只會觸發一次。
"""
import logging
import secrets
from calendar import monthrange
from datetime import date, datetime, timedelta

from sqlalchemy import func

from app.errors import AppError
from app.extensions import db
from app.models.member import StampLedger
from app.models.sync import PointLedger
from app.models.user import User
from app.services import settings_service
from app.services.points_service import change_points

logger = logging.getLogger(__name__)
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, monthrange(year, month)[1]))


# ---------------------------------------------------------------------------
# 點數到期
# ---------------------------------------------------------------------------
def _expire_months(tenant_id):
    return int(settings_service.get('member', tenant_id).get('points_expire_months') or 0)


def _lots(user_id):
    """[(得點時間, 尚未用掉的點數)] 由舊到新 (先進先出扣除所有扣點)"""
    # 到期校正 (expire_fix) 不是新得到的點數，而是修正「到期扣了多少」：從已用掉的量扣回
    positives = db.session.query(PointLedger.created_at, PointLedger.delta) \
        .filter(PointLedger.user_id == user_id, PointLedger.delta > 0, PointLedger.reason != 'expire_fix') \
        .order_by(PointLedger.created_at, PointLedger.id).all()
    used = -(db.session.query(func.coalesce(func.sum(PointLedger.delta), 0))
             .filter(PointLedger.user_id == user_id, PointLedger.delta < 0, PointLedger.reason != 'expire_fix').scalar() or 0)
    used -= db.session.query(func.coalesce(func.sum(PointLedger.delta), 0)) \
        .filter(PointLedger.user_id == user_id, PointLedger.reason == 'expire_fix').scalar() or 0
    lots = []
    for at, delta in positives:
        take = min(used, delta)
        used -= take
        if delta - take > 0:
            lots.append((at, delta - take))
    return lots


def expiring_amount(user_id, cutoff):
    """cutoff 以前得到、尚未用掉的點數"""
    return sum(n for at, n in _lots(user_id) if at < cutoff)


def upcoming_expiry(user, now=None):
    """最近一批要到期的點數：{'amount', 'date'}；未設定有效期限或沒有點數時為 None"""
    months = _expire_months(user.tenant_id)
    if not months or (user.points or 0) <= 0:
        return None
    now = now or datetime.now()
    lots = _lots(user.id)
    if not lots:
        return None
    first_day = add_months(lots[0][0], months).date()
    amount = sum(n for at, n in lots if add_months(at, months).date() == first_day)
    return {'amount': min(amount, user.points or 0), 'date': first_day, 'days': (first_day - now.date()).days}


def expire_points(tenant_id, now=None):
    """執行到期扣點；回傳扣點的會員數 (由排程每日呼叫，只在線上端 / 單機)"""
    months = _expire_months(tenant_id)
    if not months:
        return 0
    now = now or datetime.now()
    cutoff = add_months(now, -months)
    candidates = db.session.query(User.id).filter(
        User.tenant_id == tenant_id, User.points > 0,
        User.id.in_(db.session.query(PointLedger.user_id).filter(PointLedger.delta > 0, PointLedger.created_at < cutoff))
    ).execution_options(skip_tenant_filter=True).all()
    done = 0
    for (uid,) in candidates:
        amount = expiring_amount(uid, cutoff)
        if amount > 0 and change_points(tenant_id, uid, -amount, 'expire', clamp=True):
            done += 1
    db.session.commit()
    if done:
        logger.info('店家 %s：%d 位會員的點數到期扣除', tenant_id, done)
    return done


def true_up_expiry(tenant_id, user_id):
    """
    點數到期的事後校正 (線上端收到分店上傳的點數異動後執行)。
    線上端執行到期時，分店可能已經 (離線、尚未同步) 用掉同一批點數；合併後同一批點數被「到期」又被「使用」，
    會員點數被多扣甚至變負數。依時間順序重新模擬先進先出：每一次到期時，有效期限以前得到、當時尚未用掉的點數才算到期；
    與已記錄的到期量 (含先前的校正) 不同時補一筆 expire_fix。回傳校正的點數 (正數 = 退回)。
    有效期限設定為 0 (不到期) 時不校正。
    """
    from collections import deque
    months = _expire_months(tenant_id)
    if not months:
        return 0
    rows = PointLedger.query.filter(PointLedger.user_id == user_id) \
        .order_by(PointLedger.created_at, PointLedger.id).all()
    if not any(r.reason == 'expire' for r in rows):
        return 0
    lots, debt, correct, recorded = deque(), 0, 0, 0
    for r in rows:
        delta = r.delta or 0
        if r.reason == 'expire':
            recorded -= delta
            cutoff = add_months(r.created_at, -months)
            while lots and lots[0][0] < cutoff:
                correct += lots.popleft()[1]
        elif r.reason == 'expire_fix':
            recorded -= delta
        elif delta > 0:
            repay = min(debt, delta)
            debt -= repay
            if delta - repay:
                lots.append([r.created_at, delta - repay])
        elif delta < 0:
            need = -delta
            while need and lots:
                take = min(need, lots[0][1])
                lots[0][1] -= take
                need -= take
                if not lots[0][1]:
                    lots.popleft()
            debt += need
    diff = recorded - correct
    if diff:
        db.session.add(PointLedger(tenant_id=tenant_id, user_id=user_id, delta=diff, reason='expire_fix'))
        logger.info('會員 %s 點數到期校正 %+d 點 (分店同步前已使用同一批點數)', user_id, diff)
    return diff


def notify_expiring(tenant_id, now=None):
    """點數到期前 LINE 提醒 (每位會員每個提醒期間一次)；回傳通知人數"""
    from app.models.tenant import Tenant
    from app.services import line_service
    cfg = settings_service.get('member', tenant_id)
    days = int(cfg.get('points_expiry_notice_days') or 0)
    if not _expire_months(tenant_id) or not days:
        return 0
    now = now or datetime.now()
    today = now.date()
    shop = db.session.get(Tenant, tenant_id)
    users = User.query.filter(User.tenant_id == tenant_id, User.points > 0, User.line_user_id != '') \
        .execution_options(skip_tenant_filter=True).all()
    sent = 0
    for u in users:
        if u.points_notice_on is not None and u.points_notice_on > today - timedelta(days=days):
            continue
        up = upcoming_expiry(u, now)
        if up is None or up['date'] > today + timedelta(days=days):
            continue
        u.points_notice_on = today
        line_service.push_text(tenant_id, u.line_user_id,
                               f"{shop.name if shop else ''}：您有 {up['amount']} 點紅利將於 {up['date']:%Y/%m/%d} 到期，"
                               f"歡迎回來使用！")
        sent += 1
    db.session.commit()
    return sent


# ---------------------------------------------------------------------------
# 集點卡
# ---------------------------------------------------------------------------
def stamp_config(tenant_id=None):
    return settings_service.get('stamps', tenant_id)


def stamp_count(user_id):
    return max(0, db.session.query(func.coalesce(func.sum(StampLedger.delta), 0))
               .filter(StampLedger.user_id == user_id).scalar() or 0)


def stamp_card(user):
    """會員頁 / POS 顯示用：{'enabled', 'title', 'count', 'size'}"""
    cfg = stamp_config(user.tenant_id)
    if not cfg.get('enabled'):
        return {'enabled': False}
    size = int(cfg.get('card_size') or 10)
    return {'enabled': True, 'title': cfg.get('card_title') or '集點卡', 'count': min(stamp_count(user.id), size),
            'size': size}


def _stamps_earned(cfg, lines, final_price):
    if cfg.get('mode') == 'order':
        return 1 if final_price >= int(cfg.get('min_spend') or 0) and final_price > 0 else 0
    cats = {c.strip() for c in str(cfg.get('categories') or '').replace('，', ',').split(',') if c.strip()}
    return sum(ln.quantity for ln in lines
               if not ln.is_reward and not ln.is_gift and not getattr(ln, 'is_pass', False)
               and (not cats or (ln.menu_item.category or '').strip() in cats))


def _grant_card_reward(tenant_id, user, cfg, order_id):
    from app.models.coupon import Coupon, UserCoupon
    if cfg.get('reward_points'):
        change_points(tenant_id, user.id, int(cfg['reward_points']), 'stamp_card', order_id=order_id)
    code = str(cfg.get('reward_coupon_code') or '').strip().upper()
    if code:
        coupon = Coupon.query.filter_by(tenant_id=tenant_id, code=code).execution_options(skip_tenant_filter=True).first()
        if coupon is not None:
            now = datetime.now()
            db.session.add(UserCoupon(tenant_id=tenant_id, user_id=user.id, coupon_id=coupon.id, code=coupon.code,
                                      created_at=now, expires_at=coupon.expires_for(now), source='stamp'))
        else:
            logger.warning('集點卡獎勵優惠券 %s 不存在', code)


def earn_stamps(tenant_id, user, order, lines, final_price):
    """結帳時集章並處理集滿兌換；回傳 (本次得章, 集滿張數)"""
    cfg = stamp_config(tenant_id)
    if not cfg.get('enabled') or user is None:
        return 0, 0
    n = _stamps_earned(cfg, lines, final_price)
    if n <= 0:
        return 0, 0
    db.session.add(StampLedger(tenant_id=tenant_id, user_id=user.id, delta=n, reason='earn', order_id=order.id))
    db.session.flush()
    size = int(cfg.get('card_size') or 10)
    completed = 0
    total = stamp_count(user.id)
    while total >= size:
        db.session.add(StampLedger(tenant_id=tenant_id, user_id=user.id, delta=-size, reason='complete', order_id=order.id))
        _grant_card_reward(tenant_id, user, cfg, order.id)
        total -= size
        completed += 1
    return n, completed


# ---------------------------------------------------------------------------
# 推薦好友
# ---------------------------------------------------------------------------
def referral_config(tenant_id=None):
    return settings_service.get('member', tenant_id)


def ensure_referral_code(user):
    """會員的推薦碼 (第一次使用時產生；由呼叫端 commit)"""
    if not user.referral_code:
        for _ in range(20):
            code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            if not User.query.filter_by(tenant_id=user.tenant_id, referral_code=code) \
                    .execution_options(skip_tenant_filter=True).first():
                user.referral_code = code
                break
    return user.referral_code


def find_referrer(tenant_id, code):
    code = str(code or '').strip().upper().replace('-', '').replace(' ', '')
    if not code:
        return None
    referrer = User.query.filter_by(tenant_id=tenant_id, referral_code=code).execution_options(skip_tenant_filter=True).first()
    if referrer is None:
        raise AppError('推薦碼不存在，請確認後再輸入 (或留空)')
    return referrer


def award_referral(tenant_id, user, order, final_price):
    """新會員第一筆消費：雙方得點 (每位新會員一次)。回傳是否發放"""
    cfg = referral_config(tenant_id)
    if not cfg.get('referral_enabled') or user is None or not user.referred_by_uuid:
        return False
    if final_price < int(cfg.get('referral_min_spend') or 0):
        return False
    if PointLedger.query.filter_by(user_id=user.id, reason='referred').first() is not None:
        return False
    referrer = User.query.filter_by(tenant_id=tenant_id, uuid=user.referred_by_uuid) \
        .execution_options(skip_tenant_filter=True).first()
    if referrer is None or referrer.id == user.id:
        return False
    referee_points = int(cfg.get('referee_points') or 0)
    if referee_points:
        change_points(tenant_id, user.id, referee_points, 'referred', order_id=order.id)
    else:   # 新會員不得點時記一筆 0 點的標記，避免之後的訂單重複發給推薦人
        db.session.add(PointLedger(tenant_id=tenant_id, user_id=user.id, delta=0, reason='referred', order_id=order.id))
    change_points(tenant_id, referrer.id, int(cfg.get('referrer_points') or 0), 'referral', order_id=order.id)
    return True


def referral_summary(user):
    return {
        'code': user.referral_code,
        'friends': User.query.filter_by(tenant_id=user.tenant_id, referred_by_uuid=user.uuid)
        .execution_options(skip_tenant_filter=True).count(),
        'points': db.session.query(func.coalesce(func.sum(PointLedger.delta), 0))
        .filter(PointLedger.user_id == user.id, PointLedger.reason == 'referral').scalar() or 0,
    }


# ---------------------------------------------------------------------------
# 結帳 / 取消時呼叫
# ---------------------------------------------------------------------------
def on_checkout(tenant_id, user, order, lines, final_price):
    if user is None:
        return {}
    earned, completed = earn_stamps(tenant_id, user, order, lines, final_price)
    award_referral(tenant_id, user, order, final_price)
    return {'stamps_earned': earned, 'cards_completed': completed}


def on_order_cancelled(order):
    """訂單取消：扣回這張訂單得到的章與推薦獎勵點數 (集滿已兌換的獎勵不追回)"""
    if not order.user_id:
        return
    earned = db.session.query(func.coalesce(func.sum(StampLedger.delta), 0)) \
        .filter(StampLedger.order_id == order.id, StampLedger.reason.in_(('earn', 'cancel'))).scalar() or 0
    if earned > 0:
        back = min(earned, stamp_count(order.user_id))
        if back:
            db.session.add(StampLedger(tenant_id=order.tenant_id, user_id=order.user_id, delta=-back, reason='cancel',
                                       order_id=order.id))
    # 推薦獎勵：扣回這張訂單「淨得」的點數 (跨店重複發放已由線上端沖銷 *_dup 的不再重複扣)
    net = {}
    for entry in PointLedger.query.filter(PointLedger.order_id == order.id, PointLedger.reason.in_(
            ('referral', 'referred', 'referral_dup', 'referred_dup', 'referral_cancel', 'referred_cancel'))).all():
        base = entry.reason.split('_')[0]
        key = (entry.user_id, base)
        net[key] = net.get(key, 0) + (entry.delta or 0)
    for (user_id, base), amount in sorted(net.items()):
        if amount > 0:
            change_points(order.tenant_id, user_id, -amount, f'{base}_cancel', order_id=order.id, clamp=True)
