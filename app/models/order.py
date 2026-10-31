from datetime import date, datetime

from app.extensions import db
from app.models.enums import OrderSource, OrderStatus, ORDER_STATUS_LABELS
from app.models.mixins import TenantScoped

REWARD_LABEL = '[點數兌換]'


class Order(TenantScoped, db.Model):
    """
    訂單。uuid 由伺服器產生 (同步到線上時作為跨資料庫的訂單識別，也是 /track 追蹤頁的憑證)；
    前端送單時另外帶 client_order_id：同一個 client_order_id 重送只會建立一張訂單 (本機去重，不同步)。
    舊版把 client_order_id 直接當 uuid，前端可自訂 (非 https 的點餐機還是用 Math.random 產生)，追蹤頁網址因此可能被猜到。
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
    # 前端送單時產生的去重識別 (同店唯一；沒有帶時為 NULL)
    client_order_id = db.Column(db.String(36), nullable=True)
    __table_args__ = (db.Index('ix_order_tenant_client_order_id', 'tenant_id', 'client_order_id', unique=True),)
    # 付款狀態：'' 已在點餐時處理 (點餐機 / POS) / unpaid 掃碼點餐尚未結帳 / settled 已於櫃檯結帳 /
    # 線上付款 (線上端的線上點餐)：pending 待付款 / paid 已付款 / expired 逾時未付 /
    # refund_required 已付款但訂單取消 (需人工退款)。待付款的訂單不會送到分店。
    payment_status = db.Column(db.String(20), nullable=False, default='', server_default='')
    paid_at = db.Column(db.DateTime, nullable=True)
    payment_trade_no = db.Column(db.String(30), nullable=True)   # 金流商交易編號 (綠界 TradeNo)
    payment_ref = db.Column(db.String(20), nullable=True, index=True)  # 最近一次送到金流商的特店訂單編號
    # ATM 轉帳 / 超商代碼等非即時付款：繳費期限 (期限前不會因逾時被取消) 與繳費資訊 (銀行代碼帳號、超商繳費代碼)
    payment_due_at = db.Column(db.DateTime, nullable=True)
    payment_note = db.Column(db.String(120), nullable=False, default='', server_default='')
    # 線上點餐預約取餐時間 (空白 = 盡快)；廚房看板與 POS 顯示「預約 HH:MM」
    pickup_at = db.Column(db.DateTime, nullable=True, index=True)   # 預約取餐 (「今天的訂單」條件會用到，需索引)
    # ---- 自有外送 (外送到府)：地址、電話、配送區域、外送費、外送員、配送狀態 (pending → assigned → out → delivered / failed) ----
    delivery_fee = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    delivery_address = db.Column(db.String(200), nullable=False, default='', server_default='')
    delivery_phone = db.Column(db.String(30), nullable=False, default='', server_default='')
    delivery_zone = db.Column(db.String(30), nullable=False, default='', server_default='')
    driver_name = db.Column(db.String(60), nullable=False, default='', server_default='')
    delivery_status = db.Column(db.String(12), nullable=False, default='', server_default='')
    dispatched_at = db.Column(db.DateTime, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)
    # 掃桌上 QR code 點餐的桌位 (本機資料，不同步)；這類訂單 payment_status 為 unpaid (餐後至櫃檯結帳) → settled
    dining_table_id = db.Column(db.Integer, db.ForeignKey('dining_table.id', ondelete='SET NULL', name='fk_order_dining_table_id'),
                                nullable=True, index=True)
    # ---- 金額明細：discount_amount 為所有折扣合計 (優惠券 + 點數 + 促銷 + 會員 + 整單折扣)；服務費另計 ----
    service_charge = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    manual_discount = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    promotion_discount = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    member_discount = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    promotion_names = db.Column(db.String(200), nullable=False, default='', server_default='')
    approved_by = db.Column(db.String(120), nullable=False, default='', server_default='')  # 改價 / 折扣的核准主管
    # ---- 付款細節：刷卡授權碼或 LINE Pay 交易序號、卡號末四碼、儲值金扣款、已退款金額 ----
    payment_reference = db.Column(db.String(60), nullable=False, default='', server_default='')
    card_last4 = db.Column(db.String(4), nullable=False, default='', server_default='')
    wallet_used = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    refunded_amount = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    # ---- 廚房：加急 ----
    is_rush = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    # ---- 外送平台訂單編號 ----
    external_ref = db.Column(db.String(60), nullable=False, default='', server_default='')
    # ---- 本機資料 (不同步)：交班、桌位結帳 ----
    shift_id = db.Column(db.Integer, nullable=True, index=True)
    settlement_id = db.Column(db.Integer, nullable=True, index=True)

    items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan', passive_deletes=True)
    user = db.relationship('User', lazy='joined')
    customer_log = db.relationship('CustomerLog')

    @property
    def subtotal(self):
        """商品小計 = 應付 + 折扣 - 服務費 - 外送費"""
        return (self.total_price or 0) + (self.discount_amount or 0) - (self.service_charge or 0) - (self.delivery_fee or 0)

    @property
    def is_paid(self):
        """已收款 (點餐機 / POS 當場付款、桌位已結帳、線上已付款)"""
        return (self.payment_status or '') in ('', 'settled', 'paid')

    @property
    def refundable_amount(self):
        return max(0, (self.total_price or 0) - (self.refunded_amount or 0))

    @property
    def display_pickup_number(self):
        return self.pickup_number if (self.pickup_number or 0) > 0 else self.id

    @property
    def status_label(self):
        return ORDER_STATUS_LABELS.get(self.status, self.status)

    @property
    def delivery_status_label(self):
        from app.models.enums import DELIVERY_STATUS_LABELS
        return DELIVERY_STATUS_LABELS.get(self.delivery_status or '', '')

    @property
    def masked_payment_reference(self):
        """收據 / 列表顯示的付款參考：禮物卡卡號遮罩 (完整卡號只留在訂單供退款)"""
        ref = self.payment_reference or ''
        if self.payment_method == '禮物卡' and len(ref) >= 8:
            return f'{ref[:4]}****{ref[-4:]}'
        return ref

    @property
    def pickup_at_label(self):
        """預約取餐時間 (今天 → HH:MM，其他日期 → MM/DD HH:MM)；沒有預約為空字串"""
        if self.pickup_at is None:
            return ''
        fmt = '%H:%M' if self.pickup_at.date() == date.today() else '%m/%d %H:%M'
        return self.pickup_at.strftime(fmt)

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
            'service_charge': self.service_charge or 0,
            'delivery_fee': self.delivery_fee or 0,
            'delivery_address': self.delivery_address or '',
            'delivery_status': self.delivery_status or '',
            'promotion_discount': self.promotion_discount or 0,
            'promotion_names': self.promotion_names or '',
            'member_discount': self.member_discount or 0,
            'manual_discount': self.manual_discount or 0,
            'wallet_used': self.wallet_used or 0,
            'wallet_balance': self.user.wallet_balance if self.user else 0,
            'is_rush': bool(self.is_rush),
            'pickup_at': self.pickup_at_label,
            'payment_reference': self.masked_payment_reference,
            'card_last4': self.card_last4 or '',
            'invoice': self.invoice_summary(),
            'track_url': f'/track/{self.uuid}',
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

    def invoice_summary(self):
        """最近一張電子發票 (收據顯示用)"""
        inv = max(self.invoices, key=lambda i: i.id) if self.invoices else None
        if inv is None:
            return None
        return {'id': inv.id, 'status': inv.status, 'invoice_no': inv.invoice_no, 'carrier': inv.carrier_label,
                'random_number': inv.random_number, 'amount': inv.amount}


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
    is_gift = db.Column(db.Boolean, nullable=False, default=False, server_default='0')   # 促銷贈品 (金額 0)
    original_price = db.Column(db.Integer, nullable=True)   # 收銀改價前的單價 (沒有改價為空)
    # 賣出當下的套餐配餐 (餐點 uuid 的 JSON 清單)：取消回補與分店扣庫存以此為準，不受之後修改 / 刪除套餐影響
    combo_sides = db.Column(db.Text, nullable=False, default='', server_default='')
    # 單品作廢：已作廢的份數 (quantity 為剩下的份數) 與原因；同步時只增不減
    voided_qty = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    void_note = db.Column(db.String(120), nullable=False, default='', server_default='')
    prepared_at = db.Column(db.DateTime, nullable=True)     # 廚房工作站完成時間 (本機)

    @property
    def display_name(self):
        if self.is_gift:
            return f"[贈品] {self.item_name}"
        return f"{REWARD_LABEL} {self.item_name}" if self.is_reward else self.item_name

    def to_dict(self):
        return {
            'name': self.display_name,
            'item_name': self.item_name,
            'is_reward': bool(self.is_reward),
            'is_gift': bool(self.is_gift),
            'original_price': self.original_price,
            'price': self.price,
            'quantity': self.quantity,
            'subtotal': self.price * self.quantity,
            'customization': self.customization or '',
            'id': self.id,
            'voided_qty': self.voided_qty or 0,
            'void_note': self.void_note or '',
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


class ServiceCall(TenantScoped, db.Model):
    """
    桌邊「呼叫服務」(加水、結帳、需要協助)：顧客在掃碼點餐頁按下，POS 顯示待處理。
    門市本機資料 (不同步)；同一桌同一種呼叫未處理前不重複建立。
    """
    __tablename__ = 'service_call'

    id = db.Column(db.Integer, primary_key=True)
    dining_table_id = db.Column(db.Integer, db.ForeignKey('dining_table.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, default='help')     # water / bill / help
    note = db.Column(db.String(100), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    handled_at = db.Column(db.DateTime, nullable=True)
    handled_by = db.Column(db.String(120), nullable=False, default='')

    dining_table = db.relationship('DiningTable')


class OrderFeedback(TenantScoped, db.Model):
    """餐後滿意度 (1~5 星與意見)：顧客在出餐追蹤頁填寫；分店上傳到線上端彙整 (後台「顧客評價」)"""
    __tablename__ = 'order_feedback'

    id = db.Column(db.Integer, primary_key=True)
    # 每台主機每張訂單一筆 (不設唯一限制：同一張線上訂單可能在線上端與分店的追蹤頁各填一次，同步彙整時以最新一筆為準)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='CASCADE'), nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=False, default=5)
    comment = db.Column(db.String(500), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)

    order = db.relationship('Order')
