from datetime import date, datetime

from app.extensions import db
from app.models.enums import OrderSource, OrderStatus, ORDER_STATUS_LABELS
from app.models.mixins import TenantScoped

REWARD_LABEL = '[點數兌換]'


class Order(TenantScoped, db.Model):
    """
    訂單。uuid 由前端在送單時產生 (client_order_id)：同一個 uuid 重送只會建立一張訂單，
    之後同步到線上時也以 uuid 作為跨資料庫的訂單識別。
    status：Pending (製作中) → Completed (已出餐) → PickedUp (已取餐)；任一階段可 Cancelled。
    """
    __tablename__ = 'order'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    table_number = db.Column(db.String(50), default='訪客')
    pickup_number = db.Column(db.Integer, default=1)
    total_price = db.Column(db.Integer, nullable=False)
    payment_method = db.Column(db.String(50), default='Cash')
    order_type = db.Column(db.String(50), default='內用')
    order_source = db.Column(db.String(30), default=OrderSource.KIOSK)
    cashier_username = db.Column(db.String(100), default='')
    cash_received = db.Column(db.Integer, default=0)
    cash_change = db.Column(db.Integer, default=0)
    need_cutlery = db.Column(db.Boolean, default=True)
    note = db.Column(db.String(200), default='')
    status = db.Column(db.String(50), default=OrderStatus.PENDING, index=True)
    points_used = db.Column(db.Integer, default=0)
    points_earned = db.Column(db.Integer, default=0)
    discount_amount = db.Column(db.Integer, default=0)
    # 實際套用成功的優惠代碼與核銷的會員持有券；取消時只還原這一張
    coupon_code = db.Column(db.String(50), default='')
    applied_user_coupon_id = db.Column(db.Integer, db.ForeignKey('user_coupon.id', ondelete='SET NULL'), nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    customer_log_id = db.Column(db.Integer, db.ForeignKey('customer_log.id', ondelete='SET NULL'), nullable=True)
    # 負責出餐的分店 (sync_node.uuid)：分店自己建立的訂單為本分店；線上點餐為顧客選的取餐分店；未啟用同步時為空
    node_uuid = db.Column(db.String(36), nullable=True, index=True)
    # 付款狀態：'' 已在點餐時處理 (點餐機 / POS) / unpaid 掃碼點餐尚未結帳 / settled 已於櫃檯結帳 /
    # 線上付款 (線上端的線上點餐)：pending 待付款 / paid 已付款 / expired 逾時未付 /
    # refund_required 已付款但訂單取消 (需人工退款)。待付款的訂單不會送到分店。
    payment_status = db.Column(db.String(20), nullable=False, default='', server_default='')
    paid_at = db.Column(db.DateTime, nullable=True)
    payment_trade_no = db.Column(db.String(30), nullable=True)   # 金流商交易編號 (綠界 TradeNo)
    payment_ref = db.Column(db.String(20), nullable=True, index=True)  # 最近一次送到金流商的特店訂單編號
    # 掃桌上 QR code 點餐的桌位 (本機資料，不同步)；這類訂單 payment_status 為 unpaid (餐後至櫃檯結帳) → settled
    dining_table_id = db.Column(db.Integer, db.ForeignKey('dining_table.id', ondelete='SET NULL', name='fk_order_dining_table_id'),
                                nullable=True, index=True)

    items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan', passive_deletes=True)
    user = db.relationship('User', lazy='joined')
    customer_log = db.relationship('CustomerLog')

    @property
    def subtotal(self):
        return (self.total_price or 0) + (self.discount_amount or 0)

    @property
    def display_pickup_number(self):
        return self.pickup_number if (self.pickup_number or 0) > 0 else self.id

    @property
    def status_label(self):
        return ORDER_STATUS_LABELS.get(self.status, self.status)

    def to_receipt(self, remaining_points=None):
        """結帳回應 / 收據 (重送同一張訂單時也回傳同一份內容)"""
        return {
            'success': True,
            'order_id': self.id,
            'order_uuid': self.uuid,
            'pickup_number': self.pickup_number,
            'table_number': self.table_number,
            'user_name': self.user.name if self.user else self.table_number,
            'subtotal': self.subtotal,
            'discount_amount': self.discount_amount or 0,
            'points_used': self.points_used or 0,
            'points_earned': self.points_earned or 0,
            'total_price': self.total_price,
            'cash_received': self.cash_received,
            'cash_change': self.cash_change or 0,
            'payment_method': self.payment_method,
            'order_type': self.order_type,
            'order_source': self.order_source,
            'need_cutlery': bool(self.need_cutlery),
            'note': self.note or '',
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else '',
            'cashier': self.cashier_username or '',
            'items': [i.to_dict() for i in self.items],
            'remaining_points': remaining_points if remaining_points is not None else (self.user.points if self.user else 0),
        }


class OrderItem(TenantScoped, db.Model):
    """訂單明細：item_name / price 為下單當下的快照；is_reward 為紅利兌換品 (金額只含加料)"""
    __tablename__ = 'order_item'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='CASCADE'), nullable=False, index=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='SET NULL'), nullable=True)
    combo_option_id = db.Column(db.Integer, db.ForeignKey('combo_option.id', ondelete='SET NULL'), nullable=True)
    item_name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, default=1)
    customization = db.Column(db.String(200), default='')
    is_reward = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def display_name(self):
        return f"{REWARD_LABEL} {self.item_name}" if self.is_reward else self.item_name

    def to_dict(self):
        return {
            'name': self.display_name,
            'item_name': self.item_name,
            'is_reward': bool(self.is_reward),
            'price': self.price,
            'quantity': self.quantity,
            'subtotal': self.price * self.quantity,
            'customization': self.customization or '',
        }


class DailyCounter(db.Model):
    """
    每店每日的流水號計數 (目前用於取餐號 1~999 循環)。
    以單一列的條件式 UPDATE 取號：SQLite 與 PostgreSQL 都會在交易內鎖住這一列，
    同時結帳的請求自然排隊，不需要 Redis 或行程內的鎖。
    """
    __tablename__ = 'daily_counter'

    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant.id'), primary_key=True)
    business_date = db.Column(db.Date, primary_key=True, default=date.today)
    name = db.Column(db.String(30), primary_key=True)
    value = db.Column(db.Integer, nullable=False, default=0)
