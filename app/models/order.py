from datetime import datetime
from app.extensions import db
from app.models.user import User
from app.models.tenant import TenantScoped


class Order(TenantScoped, db.Model):
    """
    訂單主表:持久化紀錄點餐交易、取餐叫號、POS收銀金流明細、點數異動與生命週期狀態。
    """
    __tablename__ = 'order'

    # 主鍵：系統訂單唯一流水編號 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 會員 ID (INTEGER，訪客點餐時為 Null)
    user_id = db.Column(db.Integer, nullable=True)

    # 桌號或顧客識別資訊 (VARCHAR(50)，預設 '訪客'，外帶時可標記取餐代號)
    table_number = db.Column(db.String(50), default='訪客')

    # 當日循環取餐號碼 (INTEGER，1~999 遞增循環)
    pickup_number = db.Column(db.Integer, default=1)

    # 訂單實收總金額 (INTEGER，扣除所有優惠折抵後的應付金額，單位 NTD)
    total_price = db.Column(db.Integer, nullable=False)

    # 付款方式 (VARCHAR(50)，受白名單約束，如：Cash, Credit Card, Line Pay, 街口支付)
    payment_method = db.Column(db.String(50), default='Cash')

    # 用餐模式 (VARCHAR(50)，受白名單約束：'內用' 或 '外帶')
    order_type = db.Column(db.String(50), default='內用')

    # 訂單來源通道 (VARCHAR(30)，例如：'Kiosk' 自助機 或 'POS' 櫃檯收銀機)
    order_source = db.Column(db.String(30), default='Kiosk')

    # 收銀經手人員帳號 (VARCHAR(100)，POS 訂單專用，用於結帳責任追溯)
    cashier_username = db.Column(db.String(100), default='')

    # 現金實收金額 (INTEGER，單位 NTD，非現金結帳時等於 total_price)
    cash_received = db.Column(db.Integer, default=0)

    # 現金找零金額 (INTEGER，單位 NTD)
    cash_change = db.Column(db.Integer, default=0)

    # 是否需要免洗餐具 (BOOLEAN，外帶點餐時由顧客或櫃檯指定)
    need_cutlery = db.Column(db.Boolean, default=True)

    # 整單備註說明 (VARCHAR(200)，例如：少油、分開包裝)
    note = db.Column(db.String(200), default='')

    # 訂單狀態機 (VARCHAR(50)，狀態：Pending 製作中 -> Completed 待取餐 -> PickedUp 已取餐 / Cancelled 已取消)
    status = db.Column(db.String(50), default='Pending')

    # 本單使用折抵之紅利點數 (INTEGER)
    points_used = db.Column(db.Integer, default=0)

    # 本單消費回饋獲得之紅利點數 (INTEGER)
    points_earned = db.Column(db.Integer, default=0)

    # 優惠券與紅利點數合併折抵總金額 (INTEGER，單位 NTD)
    discount_amount = db.Column(db.Integer, default=0)

    # 本單套用之優惠代碼 (VARCHAR(50))
    coupon_code = db.Column(db.String(50), default='')

    # 廚房出餐/出單完成時間 (DATETIME，可為空)
    completed_at = db.Column(db.DateTime, nullable=True)

    # 訂單送出建立時間 (DATETIME，預設為當前系統時間)
    created_at = db.Column(db.DateTime, default=datetime.now)

    # 關聯之顧客訪問日誌 ID (INTEGER，關聯 customer_log.id)
    customer_log_id = db.Column(db.Integer, nullable=True)

    # 關聯：此筆訂單包含的所有餐點細項，級聯刪除
    items = db.relationship('OrderItem', backref='order', lazy=True, cascade="all, delete-orphan")

    @property
    def user(self):
        """動態載入訂單關聯之會員 User 物件"""
        if self.user_id:
            return db.session.get(User, self.user_id)
        return None


class OrderItem(TenantScoped, db.Model):
    """
    訂單餐點細項資料表:記錄各品項之實際計價、購買數量、客製化字串與關聯套餐。
    """
    __tablename__ = 'order_item'

    # 主鍵：細項唯一識別碼 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 外鍵：所屬訂單 ID (關聯 order.id)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)

    # 對應原始菜單品項 ID (INTEGER，可為空，避免刪除品項後破壞歷史訂單)
    menu_item_id = db.Column(db.Integer, nullable=True)

    # 對應升級之套餐選項 ID (INTEGER，單點為 Null)
    combo_option_id = db.Column(db.Integer, nullable=True)

    # 餐點快照名稱 (VARCHAR(100)，包含套餐或點數兌換標註，不可為空)
    item_name = db.Column(db.String(100), nullable=False)

    # 結帳當下之單品計價金額 (INTEGER，含加價客製化與套餐加價，單位 NTD)
    price = db.Column(db.Integer, nullable=False)

    # 購買份數 (INTEGER，預設 1)
    quantity = db.Column(db.Integer, default=1)

    # 客製化規格文字記錄 (VARCHAR(200)，如：少冰半糖)
    customization = db.Column(db.String(200), default='')