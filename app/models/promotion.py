"""促銷活動 (買 X 送 Y、第 N 件折扣、滿額折、滿額贈)，計算規則見 app/services/promotion_service.py"""
from app.extensions import db
from app.models.mixins import TenantScoped

PROMOTION_KINDS = {
    'bogo': '買 X 送 Y',
    'nth_discount': '第 N 件折扣',
    'threshold_discount': '滿額折抵',
    'threshold_gift': '滿額贈品',
}


class Promotion(TenantScoped, db.Model):
    __tablename__ = 'promotion'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    kind = db.Column(db.String(20), nullable=False, default='bogo')
    # 適用品項：all 全部 / category 指定分類 (target_value = 分類名稱，逗號分隔) / items 指定品項 (menu_item uuid，逗號分隔)
    target_type = db.Column(db.String(10), nullable=False, default='all')
    target_value = db.Column(db.Text, nullable=False, default='')
    buy_qty = db.Column(db.Integer, nullable=False, default=1)       # bogo：買 X
    get_qty = db.Column(db.Integer, nullable=False, default=1)       # bogo：送 Y (最便宜的 Y 件免費)
    nth = db.Column(db.Integer, nullable=False, default=2)           # nth_discount：第 N 件
    percent = db.Column(db.Integer, nullable=False, default=50)      # nth_discount / threshold_discount(百分比)：應付百分比
    amount = db.Column(db.Integer, nullable=False, default=0)        # threshold_discount：折抵金額 (0 時改用 percent)
    threshold = db.Column(db.Integer, nullable=False, default=0)     # 滿額門檻
    gift_item_uuid = db.Column(db.String(36), nullable=False, default='')
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    days = db.Column(db.String(7), nullable=False, default='1234567')   # 星期一=1 ... 星期日=7
    start_time = db.Column(db.String(5), nullable=False, default='')
    end_time = db.Column(db.String(5), nullable=False, default='')
    channels = db.Column(db.String(60), nullable=False, default='')     # 空白 = 全部；kiosk,pos,table,online,delivery
    member_only = db.Column(db.Boolean, nullable=False, default=False)
    stack_with_coupon = db.Column(db.Boolean, nullable=False, default=True)
    priority = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def kind_label(self):
        return PROMOTION_KINDS.get(self.kind, self.kind)
