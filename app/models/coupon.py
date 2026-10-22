from datetime import datetime

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

    user_coupons = db.relationship('UserCoupon', back_populates='coupon', cascade='all, delete-orphan', passive_deletes=True)

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
        """套用到小計後的折抵金額 (百分比折扣可能有小數，最後在結帳時統一四捨五入)"""
        if subtotal <= 0:
            return 0
        if self.discount_type == DiscountType.PERCENT:
            return subtotal * (100 - self.discount_value) / 100
        return min(self.discount_value, subtotal)


class UserCoupon(TenantScoped, db.Model):
    """會員持有的優惠券 (由紅利商城兌換或後台發放)"""
    __tablename__ = 'user_coupon'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon.id', ondelete='CASCADE'), nullable=False)
    code = db.Column(db.String(50), nullable=False)
    is_used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    used_at = db.Column(db.DateTime, nullable=True)

    coupon = db.relationship('Coupon', back_populates='user_coupons')
    user = db.relationship('User', backref=db.backref('user_coupons', lazy=True, cascade='all, delete-orphan', passive_deletes=True))

    def to_dict(self):
        cp = self.coupon
        return {
            'id': self.id,
            'user_coupon_id': self.id,
            'code': self.code,
            'title': f"[{cp.discount_label}] {cp.title}" if cp else self.code,
            'min_spend': cp.min_spend if cp else 0,
            'is_used': bool(self.is_used),
        }
