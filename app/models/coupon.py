from datetime import datetime
from app.extensions import db

class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    title = db.Column(db.String(100), nullable=False)
    discount_type = db.Column(db.String(20), default='fixed')
    discount_value = db.Column(db.Float, nullable=False)
    min_spend = db.Column(db.Float, default=0)
    reward_points = db.Column(db.Integer, default=0)
    reward_discount_points = db.Column(db.Integer, default=0)
    is_reward = db.Column(db.Boolean, default=True)
    per_user_limit = db.Column(db.Integer, default=0)
    @property
    def limit_display(self):
        if not self.per_user_limit or self.per_user_limit <= 0:
            return "無上限"
        return f"{self.per_user_limit}次"
class UserCoupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon.id'), nullable=False)
    code = db.Column(db.String(50), nullable=False)
    is_used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('user_coupons', lazy=True, cascade="all, delete-orphan"))
    coupon = db.relationship('Coupon', backref=db.backref('user_coupons', lazy=True, cascade="all, delete-orphan"))