from app.extensions import db
from app.models.mixins import CreatedAtMixin, TenantScoped, new_public_token

new_table_token = new_public_token   # QR code 網址中的桌位代碼 (table_service 以此名稱取用)


class DiningTable(CreatedAtMixin, TenantScoped, db.Model):
    """
    門市桌位 (手機掃碼點餐)。每桌一個 QR code：/t/<token>，掃描後以該桌身分點餐，餐後到櫃檯結帳。
    桌位屬於各門市自己 (不同步到線上端或其他分店)。
    """
    __tablename__ = 'dining_table'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), nullable=False)
    token = db.Column(db.String(32), nullable=False, unique=True, index=True, default=new_table_token)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    # ---- 桌位平面圖：座位數、區域 (例：室內 / 戶外 / 二樓)、形狀、位置 (平面圖寬高的千分比，未擺放時為空) ----
    seats = db.Column(db.Integer, nullable=False, default=4, server_default='4')
    area = db.Column(db.String(20), nullable=False, default='', server_default='')
    shape = db.Column(db.String(10), nullable=False, default='square', server_default='square')   # square / round / long
    pos_x = db.Column(db.Integer, nullable=True)
    pos_y = db.Column(db.Integer, nullable=True)

    tenant = db.relationship('Tenant')

    __table_args__ = (db.UniqueConstraint('tenant_id', 'name', name='uq_dining_table_tenant_name'),)

    @property
    def label(self):
        return f'{self.name} 桌'


class TablePayment(CreatedAtMixin, TenantScoped, db.Model):
    """
    桌邊掃碼點餐的「線上結帳」(顧客用手機付清這一桌未結帳的訂單)。門市本機資料 (不同步)。
    pending → processing (結帳中) → paid (綠界通知付款成功後完成桌位結帳) /
    conflict (付款時訂單已在櫃檯結帳或有異動，需退款) / failed。
    """
    __tablename__ = 'table_payment'

    id = db.Column(db.Integer, primary_key=True)
    # 刪除桌位時保留付款紀錄 (桌位設為空)：付款中 / 待人工退款的紀錄不能跟著消失；
    # 之後才進來的付款通知會因找不到桌位而轉為 conflict 並發警示 (含綠界交易編號)，供人工退款
    dining_table_id = db.Column(db.Integer, db.ForeignKey('dining_table.id', ondelete='SET NULL',
                                                          name='fk_table_payment_dining_table_id'),
                                nullable=True, index=True)
    order_ids = db.Column(db.Text, nullable=False, default='')   # 逗號分隔 (同桌累積的未結帳訂單可能很多，不限長度)
    amount = db.Column(db.Integer, nullable=False, default=0)
    # 這筆線上結帳的編號 (付款頁網址用；送綠界時 CustomField1 為 T:<此編號>，每次開付款頁的特店訂單編號另外產生)
    trade_no = db.Column(db.String(20), nullable=False, unique=True)
    status = db.Column(db.String(12), nullable=False, default='pending', index=True)
    provider_trade_no = db.Column(db.String(30), nullable=False, default='')
    note = db.Column(db.String(200), nullable=False, default='')
    paid_at = db.Column(db.DateTime, nullable=True)

    dining_table = db.relationship('DiningTable')

    @property
    def order_id_list(self):
        return [int(x) for x in self.order_ids.split(',') if x.strip().isdigit()]
