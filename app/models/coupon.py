from datetime import date, datetime, time, timedelta

from app.extensions import db
from app.models.enums import DiscountType
from app.models.mixins import TenantScoped


def parse_discount_value(discount_type, raw):
    """
    把後台/匯入輸入的折扣值轉成存檔格式。
    百分比折扣接受店家習慣的三種寫法：85、8.5、0.85 都代表「85 折」(存為 85，範圍 1~99)。
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0
    if val != val or val in (float('inf'), float('-inf')):
        return 0
    if discount_type == DiscountType.PERCENT:
        if val <= 1:
            val *= 100
        elif val <= 10:
            val *= 10
        return int(min(99, max(1, round(val))))
    return max(0, int(round(val)))


class Coupon(TenantScoped, db.Model):
    """
    優惠券範本。
    - discount_type = fixed：discount_value 為折抵元數。
    - discount_type = percent：discount_value 為應付百分比 (85 代表 85 折)。
    - reward_points > 0 (is_reward) 為紅利商城兌換券，0 點為公開促銷碼。
    金額一律以整數元保存。
    """
    __tablename__ = 'coupon'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'code', name='uq_coupon_tenant_code'),)

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    discount_type = db.Column(db.String(20), nullable=False, default=DiscountType.FIXED)
    discount_value = db.Column(db.Integer, nullable=False, default=0)
    min_spend = db.Column(db.Integer, nullable=False, default=0)
    reward_points = db.Column(db.Integer, default=0)
    reward_discount_points = db.Column(db.Integer, default=0)
    is_reward = db.Column(db.Boolean, default=True)
    per_user_limit = db.Column(db.Integer, default=0)
    # 會員專屬券：只能由後台發放 (批量 / 生日禮)，必須持有才能使用；不在紅利商城、不能當公開代碼
    member_only = db.Column(db.Boolean, nullable=False, default=False)
    # 活動期間 (含兩端)：期間外不能兌換、發放或使用；會員已持有的券也一併失效
    valid_from = db.Column(db.Date, nullable=True)
    valid_until = db.Column(db.Date, nullable=True)
    # 領取 (兌換 / 發放) 後幾天到期；0 = 不限 (只受活動期間限制)
    valid_days = db.Column(db.Integer, nullable=False, default=0)
    # 公開代碼的防濫用：限登入會員使用、每位會員可使用次數 (0 = 不限)
    login_required = db.Column(db.Boolean, nullable=False, default=False)
    uses_per_member = db.Column(db.Integer, nullable=False, default=0)
    # 單筆最高折抵金額 (0 = 不限)，主要用於百分比折扣
    max_discount = db.Column(db.Integer, nullable=False, default=0)

    user_coupons = db.relationship('UserCoupon', back_populates='coupon', cascade='all, delete-orphan', passive_deletes=True)

    @property
    def requires_holding(self):
        """必須是會員持有的券才能使用 (紅利兌換券、會員專屬券)"""
        return bool(self.is_reward or self.member_only)

    def period_error(self, today=None):
        """活動期間檢查：期間內回傳 None，否則回傳原因"""
        today = today or date.today()
        if self.valid_from and today < self.valid_from:
            return f'活動尚未開始 ({self.valid_from:%Y/%m/%d} 起)'
        if self.valid_until and today > self.valid_until:
            return f'活動已結束 ({self.valid_until:%Y/%m/%d} 止)'
        return None

    def expires_for(self, issued_at, valid_days=None):
        """領取時間 issued_at 的到期時間 (到期日當天 23:59:59)：取「領取後 N 天」與活動結束日較早者；都沒有時回傳 None"""
        days = self.valid_days if valid_days is None else valid_days
        candidates = []
        if days and days > 0:
            candidates.append(issued_at.date() + timedelta(days=days))
        if self.valid_until:
            candidates.append(self.valid_until)
        if not candidates:
            return None
        return datetime.combine(min(candidates), time(23, 59, 59))

    @property
    def usage_label(self):
        """公開代碼的使用限制 (顯示用)"""
        parts = []
        if self.login_required or self.uses_per_member:
            parts.append('限會員')
        if self.uses_per_member:
            parts.append(f'每人限用 {self.uses_per_member} 次')
        if self.max_discount:
            parts.append(f'最高折 ${self.max_discount}')
        return '，'.join(parts)

    @property
    def period_label(self):
        parts = []
        if self.valid_from or self.valid_until:
            parts.append(f"{self.valid_from:%m/%d}" if self.valid_from else '即日')
            parts[-1] += f" ~ {self.valid_until:%m/%d}" if self.valid_until else ' 起'
        if self.valid_days:
            parts.append(f'領取後 {self.valid_days} 天')
        return '，'.join(parts) or '不限'

    @property
    def redeem_points(self):
        return self.reward_discount_points if (self.reward_discount_points or 0) > 0 else (self.reward_points or 0)

    @property
    def discount_label(self):
        """顯示用：『現折 $20』或『85 折』"""
        if self.discount_type == DiscountType.PERCENT:
            pct = self.discount_value  # 應付百分比：90 → 9 折、85 → 85 折、5 → 0.5 折
            return f"{pct // 10 if pct % 10 == 0 else (pct if pct > 10 else pct / 10)} 折"
        return f"現折 ${self.discount_value}"

    @property
    def limit_display(self):
        return "無上限" if not self.per_user_limit or self.per_user_limit <= 0 else f"{self.per_user_limit}次"

    def discount_for(self, subtotal):
        """套用到小計後的折抵金額 (百分比折扣可能有小數，最後在結帳時統一四捨五入)；不超過小計與最高折抵金額"""
        if subtotal <= 0:
            return 0
        if self.discount_type == DiscountType.PERCENT:
            amount = subtotal * (100 - self.discount_value) / 100
        else:
            amount = min(self.discount_value, subtotal)
        if self.max_discount and self.max_discount > 0:
            amount = min(amount, self.max_discount)
        return amount


USER_COUPON_SOURCES = {'reward': '點數兌換', 'batch': '批量發放', 'birthday': '生日禮', 'admin': '後台發放', 'campaign': '自動行銷'}
HISTORY_DAYS = 7   # 後台核銷畫面只顯示近 7 天已使用 / 已過期的券 (資料保留)


class UserCoupon(TenantScoped, db.Model):
    """
    會員持有的優惠券 (紅利商城兌換、批量發放、生日禮)。
    狀態：available 可用 / upcoming 活動尚未開始 / used 已使用 / expired 已過期 (到期時間已過或活動已結束)。
    batch_id：同一次批量發放的券共用一個代碼，可整批撤回未使用的券。
    """
    __tablename__ = 'user_coupon'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon.id', ondelete='CASCADE'), nullable=False)
    code = db.Column(db.String(50), nullable=False)
    is_used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    used_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    source = db.Column(db.String(20), nullable=False, default='reward')
    batch_id = db.Column(db.String(36), nullable=False, default='', index=True)

    coupon = db.relationship('Coupon', back_populates='user_coupons')
    user = db.relationship('User', backref=db.backref('user_coupons', lazy=True, cascade='all, delete-orphan', passive_deletes=True))

    def status(self, now=None):
        now = now or datetime.now()
        if self.is_used:
            return 'used'
        if self.expires_at is not None and self.expires_at < now:
            return 'expired'
        cp = self.coupon
        if cp is not None:
            if cp.valid_until and now.date() > cp.valid_until:
                return 'expired'
            if cp.valid_from and now.date() < cp.valid_from:
                return 'upcoming'
        return 'available'

    @property
    def expired_at(self):
        """實際失效時間 (到期時間與活動結束日較早者)；不會過期時為 None"""
        cp = self.coupon
        ends = [t for t in (self.expires_at,
                            datetime.combine(cp.valid_until, time(23, 59, 59)) if cp is not None and cp.valid_until else None) if t]
        return min(ends) if ends else None

    def visible_in_history(self, now=None):
        """後台核銷畫面：未使用的一律顯示；已使用 / 已過期的只顯示近 HISTORY_DAYS 天"""
        now = now or datetime.now()
        st = self.status(now)
        if st in ('available', 'upcoming'):
            return True
        when = self.used_at if st == 'used' else self.expired_at
        return when is None or when >= now - timedelta(days=HISTORY_DAYS)

    def to_dict(self, now=None):
        cp = self.coupon
        end = self.expired_at
        title = f"[{cp.discount_label}] {cp.title}" if cp else self.code
        if end is not None and not self.is_used:
            title += f" (至 {end:%m/%d})"
        return {
            'id': self.id,
            'user_coupon_id': self.id,
            'code': self.code,
            'title': title,
            'min_spend': cp.min_spend if cp else 0,
            'is_used': bool(self.is_used),
            'status': self.status(now),
            'expires_at': end.strftime('%Y-%m-%d %H:%M') if end else '',
            'used_at': self.used_at.strftime('%Y-%m-%d %H:%M') if self.used_at else '',
            'created_at': self.created_at.strftime('%Y-%m-%d') if self.created_at else '',
            'source': USER_COUPON_SOURCES.get(self.source or 'reward', self.source or ''),
        }
