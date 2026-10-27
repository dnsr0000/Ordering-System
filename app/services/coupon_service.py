"""
會員優惠券：批量發放、到期、撤回與後台核銷畫面。

- 發放 (issue)：依對象 (全部會員 / 會員等級 / 本月壽星 / 已綁 LINE / 手機清單) 一次發給多位會員，
  每位可發多張；遵守「每位會員上限」(已持有的張數 + 本次 ≤ 上限，超過的人略過)。
  同一次發放的券共用 batch_id，可整批撤回「尚未使用」的券。
- 到期：UserCoupon.expires_at (發放 / 兌換時依「領取後 N 天」與活動結束日計算，發放時也可另外指定)；
  過期或活動期間外的券不能使用，也不會出現在點餐頁 / POS 的可用清單。
- 後台核銷畫面只顯示未使用的券，以及近 7 天內已使用 / 已過期的券 (資料不刪除，次數上限與報表照常計算)。
"""
import re
import uuid
from datetime import date, datetime, time

from sqlalchemy import func, or_

from app.errors import AppError
from app.extensions import db
from app.models.coupon import HISTORY_DAYS, Coupon, UserCoupon
from app.models.user import User
from app.utils import validate_phone

# 代碼格式：3~30 碼英文大寫、數字、- 或 _ (第一碼為英數字)，避免空白、相似字元與過短易猜的代碼
CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9_-]{2,29}$')
CODE_RULE = '優惠代碼需為 3~30 碼英文、數字、- 或 _ (例如 NEW50、VIP-2026)'
MAX_RECIPIENTS = 5000
MAX_PER_MEMBER = 10
TARGET_LABELS = {'all': '全部會員', 'tier': '會員等級', 'birthday_month': '本月壽星', 'line': '已綁定 LINE 的會員',
                 'phones': '指定手機號碼'}


def normalize_code(code):
    return str(code or '').strip().upper()


def validate_code(code):
    """後台建立 / 匯入時檢查代碼格式，回傳正規化後的代碼；不合格拋 AppError"""
    code = normalize_code(code)
    if not CODE_RE.match(code):
        raise AppError(f'❌ {CODE_RULE}')
    return code


def member_uses(user_id, code):
    """會員已使用某個公開代碼的次數 (未取消的訂單)"""
    from app.models.enums import OrderStatus
    from app.models.order import Order
    return Order.query.filter(Order.user_id == user_id, Order.coupon_code == code,
                              Order.status != OrderStatus.CANCELLED).count()


# ---------------------------------------------------------------------------
# 查詢
# ---------------------------------------------------------------------------
def usable_coupons(user_id, now=None):
    """可用的會員券 (未使用、未過期、活動期間內)，最快到期的排前面"""
    now = now or datetime.now()
    rows = UserCoupon.query.filter(UserCoupon.user_id == user_id, UserCoupon.is_used.is_(False),
                                   or_(UserCoupon.expires_at.is_(None), UserCoupon.expires_at >= now)).all()
    rows = [uc for uc in rows if uc.coupon is not None and uc.status(now) == 'available']
    return sorted(rows, key=lambda uc: (uc.expired_at is None, uc.expired_at or now, uc.id))


def find_usable(user_id, code, now=None):
    """會員持有的指定代碼可用券 (多張時用最快到期的那張)；回傳 (券, 不可用原因)"""
    now = now or datetime.now()
    held = UserCoupon.query.filter_by(user_id=user_id, code=code, is_used=False).all()
    if not held:
        return None, None
    usable = [uc for uc in held if uc.coupon is not None and uc.status(now) == 'available']
    if usable:
        return sorted(usable, key=lambda uc: (uc.expired_at is None, uc.expired_at or now, uc.id))[0], None
    statuses = {uc.status(now) for uc in held}
    if 'upcoming' in statuses:
        cp = held[0].coupon
        return None, cp.period_error(now.date()) if cp else '活動尚未開始'
    return None, '此優惠券已過期'


def admin_view(user, now=None):
    """後台會員票券視窗：未使用 (含尚未開始) 在前，已使用 / 已過期只列近 7 天"""
    now = now or datetime.now()
    rows = [uc for uc in user.user_coupons if uc.visible_in_history(now)]
    order = {'available': 0, 'upcoming': 1, 'used': 2, 'expired': 3}

    def key(uc):
        st = uc.status(now)
        when = uc.used_at if st == 'used' else (uc.expired_at or uc.created_at)
        return order[st], -(when or uc.created_at or now).timestamp(), -uc.id
    return [dict(uc.to_dict(now), title=uc.coupon.title if uc.coupon else '優惠券', label=uc.coupon.discount_label if uc.coupon else '')
            for uc in sorted(rows, key=key)]


def unused_count(user, now=None):
    now = now or datetime.now()
    return sum(1 for uc in user.user_coupons if uc.status(now) in ('available', 'upcoming'))


# ---------------------------------------------------------------------------
# 發放對象
# ---------------------------------------------------------------------------
def parse_phones(text):
    """貼上的手機清單 (換行、逗號、空白分隔)；回傳 (有效號碼, 格式錯誤的字串)"""
    import re
    good, bad = [], []
    for raw in re.split(r'[\s,，;；]+', str(text or '')):
        raw = raw.strip().replace('-', '')
        if not raw:
            continue
        (good if validate_phone(raw) else bad).append(raw)
    return list(dict.fromkeys(good)), bad


def resolve_targets(tenant_id, target, tier_id=None, phones_text='', today=None):
    """回傳 (會員清單, 說明, 找不到的手機)"""
    today = today or date.today()
    q = User.query.filter(User.tenant_id == tenant_id)
    missing = []
    if target == 'all':
        users = q.all()
        desc = TARGET_LABELS['all']
    elif target == 'tier':
        from app.models.member import MemberTier
        tier = db.session.get(MemberTier, int(tier_id or 0))
        if tier is None or tier.tenant_id != tenant_id:
            raise AppError('請選擇會員等級')
        users = q.filter(User.tier_id == tier.id).all()
        desc = f'會員等級「{tier.name}」'
    elif target == 'birthday_month':
        users = [u for u in q.filter(User.birthday.isnot(None)).all() if u.birthday.month == today.month]
        desc = f'{today.month} 月壽星'
    elif target == 'line':
        users = q.filter(User.line_user_id != '').all()
        desc = TARGET_LABELS['line']
    elif target == 'phones':
        phones, bad = parse_phones(phones_text)
        if not phones:
            raise AppError('請貼上至少一個手機號碼 (09 開頭 10 碼)')
        users = q.filter(User.phone.in_(phones)).all()
        found = {u.phone for u in users}
        missing = [p for p in phones if p not in found] + bad
        desc = f'指定手機 {len(phones)} 個'
    else:
        raise AppError('請選擇發放對象')
    return users, desc, missing


# ---------------------------------------------------------------------------
# 發放 / 撤回
# ---------------------------------------------------------------------------
def parse_date(value):
    value = str(value or '').strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AppError(f'日期格式不正確：{value}')


def issue(tenant_id, coupon, users, quantity=1, expires_on=None, valid_days=None, staff='', source='batch', now=None):
    """
    發給 users 每人 quantity 張。expires_on (日期) 或 valid_days (天數) 可覆寫券本身的到期規則，
    兩者都沒給時依券的設定 (領取後 N 天 / 活動結束日)。回傳 {'batch_id', 'issued', 'members', 'skipped'}。
    由呼叫端 commit。
    """
    now = now or datetime.now()
    if coupon is None or coupon.tenant_id != tenant_id:
        raise AppError('查無此優惠券', 404)
    quantity = int(quantity or 0)
    if not 1 <= quantity <= MAX_PER_MEMBER:
        raise AppError(f'每人發放張數需為 1 ~ {MAX_PER_MEMBER} 張')
    if not users:
        raise AppError('沒有符合條件的會員')
    if len(users) > MAX_RECIPIENTS:
        raise AppError(f'一次最多發給 {MAX_RECIPIENTS} 位會員，請分批發放')
    if coupon.valid_until and now.date() > coupon.valid_until:
        raise AppError(f'「{coupon.title}」的活動已於 {coupon.valid_until:%Y/%m/%d} 結束，請先延長活動期間')
    if expires_on is not None:
        if expires_on < now.date():
            raise AppError('到期日不可早於今天')
        expires_at = datetime.combine(min(expires_on, coupon.valid_until) if coupon.valid_until else expires_on,
                                      time(23, 59, 59))
    else:
        expires_at = coupon.expires_for(now, valid_days)
    limit = coupon.per_user_limit or 0
    owned = {}
    if limit:
        owned = dict(db.session.query(UserCoupon.user_id, func.count(UserCoupon.id))
                     .filter(UserCoupon.coupon_id == coupon.id, UserCoupon.user_id.in_([u.id for u in users]))
                     .group_by(UserCoupon.user_id).all())
    batch_id = uuid.uuid4().hex
    issued, members, skipped = 0, 0, []
    rows = []
    for u in users:
        n = quantity if not limit else min(quantity, max(0, limit - owned.get(u.id, 0)))
        if n <= 0:
            skipped.append(u.name)
            continue
        members += 1
        for _ in range(n):
            rows.append(UserCoupon(tenant_id=tenant_id, user_id=u.id, coupon_id=coupon.id, code=coupon.code,
                                   created_at=now, expires_at=expires_at, source=source, batch_id=batch_id))
            issued += 1
    db.session.add_all(rows)
    db.session.flush()
    return {'batch_id': batch_id, 'issued': issued, 'members': members, 'skipped': skipped, 'expires_at': expires_at,
            'user_ids': [r.user_id for r in rows]}


def revoke_batch(tenant_id, batch_id):
    """撤回一次發放中尚未使用的券 (已使用的保留)；回傳撤回張數。由呼叫端 commit。"""
    if not batch_id:
        raise AppError('查無此發放紀錄', 404)
    rows = UserCoupon.query.filter_by(tenant_id=tenant_id, batch_id=batch_id, is_used=False).all()
    for uc in rows:
        db.session.delete(uc)
    return len(rows)


def recent_batches(tenant_id, limit=20):
    """最近的發放紀錄：[{batch_id, coupon, created_at, total, used, unused, expired, expires_at}]"""
    now = datetime.now()
    ids = [b for (b,) in db.session.query(UserCoupon.batch_id)
           .filter(UserCoupon.tenant_id == tenant_id, UserCoupon.batch_id != '')
           .group_by(UserCoupon.batch_id).order_by(func.max(UserCoupon.created_at).desc()).limit(limit).all()]
    out = []
    for bid in ids:
        rows = UserCoupon.query.filter_by(tenant_id=tenant_id, batch_id=bid).all()
        if not rows:
            continue
        statuses = [uc.status(now) for uc in rows]
        first = rows[0]
        out.append({
            'batch_id': bid, 'coupon': first.coupon.title if first.coupon else first.code, 'code': first.code,
            'created_at': min(r.created_at for r in rows if r.created_at), 'total': len(rows),
            'members': len({r.user_id for r in rows}), 'used': statuses.count('used'),
            'unused': statuses.count('available') + statuses.count('upcoming'), 'expired': statuses.count('expired'),
            'expires_at': first.expired_at, 'source': first.source,
        })
    return out


def notify_line(tenant_id, user_ids, coupon, expires_at):
    """以 LINE 通知收到券的會員 (有綁定 LINE 且已啟用推播時)；回傳通知人數，失敗只記錄"""
    from app.services import line_service
    if not line_service.messaging_enabled(tenant_id):
        return 0
    ids = [u.line_user_id for u in User.query.filter(User.id.in_(set(user_ids)), User.line_user_id != '').all()]
    if not ids:
        return 0
    text = f'您收到一張優惠券「{coupon.title}」({coupon.discount_label})'
    if coupon.min_spend:
        text += f'，滿 ${coupon.min_spend} 可用'
    if expires_at:
        text += f'，請於 {expires_at:%Y/%m/%d} 前使用'
    text += f'。結帳時選擇或輸入代碼 {coupon.code} 即可折抵！'
    try:
        return line_service.multicast_text(tenant_id, ids, text)
    except AppError:
        return 0

