from datetime import datetime
from app.extensions import db
from app.models.tenant import TenantScoped

class Coupon(TenantScoped, db.Model):
    """
    優惠券範本資料表:定義公開促銷代碼或紅利商城兌換券之規則、折抵金額與使用限制。
    """
    __tablename__ = 'coupon'

    # 約束條件：同店家下優惠券代碼 (code) 必須具備唯一性
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'code', name='uq_coupon_tenant_code'),
    )

    # 主鍵：優惠券範本識別碼 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 優惠券代碼 (VARCHAR(50)，如：OPEN20, VIP85，比對時自動大寫)
    code = db.Column(db.String(50), nullable=False)

    # 優惠券標題名稱 (VARCHAR(100)，不可為空)
    title = db.Column(db.String(100), nullable=False)

    # 折扣計算類型 (VARCHAR(20)，'fixed' 現金折抵 / 'percent' 折扣比例)
    discount_type = db.Column(db.String(20), default='fixed')

    # 折扣數值 (FLOAT；fixed 為折抵金額，percent 為折數如 0.85 代表 85 折)
    discount_value = db.Column(db.Float, nullable=False)

    # 最低消費門檻金額 (FLOAT，預設 0 代表無門檻限制)
    min_spend = db.Column(db.Float, default=0)

    # 兌換此券所需基準紅利點數 (INTEGER，若為 0 則代表免點數之公開券)
    reward_points = db.Column(db.Integer, default=0)

    # 兌換特惠紅利點數 (INTEGER，大於 0 時優先以特惠點數扣除)
    reward_discount_points = db.Column(db.Integer, default=0)

    # 是否為紅利專屬兌換券 (BOOLEAN，True 代表需於紅利商城以點數兌換)
    is_reward = db.Column(db.Boolean, default=True)

    # 單一會員領取/兌換次數上限 (INTEGER，0 代表無兌換次數上限)
    per_user_limit = db.Column(db.Integer, default=0)

    @property
    def limit_display(self):
        """後台介面展示領取次數上限文字"""
        if not self.per_user_limit or self.per_user_limit <= 0:
            return "無上限"
        return f"{self.per_user_limit}次"


class UserCoupon(TenantScoped, db.Model):
    """
    會員持有優惠券清單資料表:記錄會員透過紅利點數兌換或發送至其帳戶下的優惠券實體與核銷狀態。
    """
    __tablename__ = 'user_coupon'

    # 主鍵：會員持券唯一識別碼 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 外鍵：持有會員 ID (關聯 user.id)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # 外鍵：關聯優惠券範本 ID (關聯 coupon.id)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon.id'), nullable=False)

    # 核銷代碼 (VARCHAR(50)，通常繼承 Coupon.code)
    code = db.Column(db.String(50), nullable=False)

    # 是否已核銷使用 (BOOLEAN，True 代表已於結帳折抵完畢)
    is_used = db.Column(db.Boolean, default=False)

    # 兌換/領取時間 (DATETIME，預設當前系統時間)
    created_at = db.Column(db.DateTime, default=datetime.now)

    # 實際核銷使用時間 (DATETIME，未核銷前為 Null)
    used_at = db.Column(db.DateTime, nullable=True)

    # 關聯：持有會員與來源優惠券，級聯更新/刪除
    user = db.relationship('User', backref=db.backref('user_coupons', lazy=True, cascade="all, delete-orphan"))
    coupon = db.relationship('Coupon', backref=db.backref('user_coupons', lazy=True, cascade="all, delete-orphan"))
