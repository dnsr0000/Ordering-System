from app.extensions import db
from app.models.tenant import TenantScoped


class MenuItem(TenantScoped, db.Model):
    """
    菜單品項資料表:儲存店家餐點、單價、即時庫存、特價規則、紅利商城兌換屬性與加購條件。
    """
    __tablename__ = 'menu_item'

    # 主鍵：餐點唯一識別碼 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 餐點名稱 (VARCHAR(100)，不可為空)
    name = db.Column(db.String(100), nullable=False)

    # 分類名稱 (VARCHAR(50)，預設 '主餐'，如：主餐、飲料、副餐、點心)
    category = db.Column(db.String(50), default='主餐')

    # 基準原價 (INTEGER，單位 NTD)
    price = db.Column(db.Integer, nullable=False)

    # 當前剩餘即時庫存數量 (INTEGER，預設 50，併發扣減之原子判斷欄位)
    stock = db.Column(db.Integer, default=50)

    # 每日初始總庫存量 (INTEGER，預設 50，作為補貨參考)
    total_stock = db.Column(db.Integer, default=50)

    # 售罄標記 (BOOLEAN，True 代表已售完)
    is_sold_out = db.Column(db.Boolean, default=False)

    # 特價啟用標記 (BOOLEAN，True 代表套用 discount_price)
    is_discount = db.Column(db.Boolean, default=False)

    # 特價優惠金額 (INTEGER，單位 NTD)
    discount_price = db.Column(db.Integer, default=0)

    # 系統演算法推薦標記 (BOOLEAN)
    is_recommended = db.Column(db.Boolean, default=False)

    # 後台手動強制熱門推薦標記 (BOOLEAN)
    is_manual_popular = db.Column(db.Boolean, default=False)

    # 客製化規格類型 (VARCHAR(50)，可選：none, ice_sugar, spicy, addons)
    modifiers = db.Column(db.String(50), default='none')

    # 商品文案描述 (VARCHAR(200))
    description = db.Column(db.String(200), default='')

    # 圖片路徑/檔名 (VARCHAR(200)，存放於 static/menu/ 目錄)
    image_path = db.Column(db.String(200), default='')

    # 新品推薦標記 (BOOLEAN)
    is_new = db.Column(db.Boolean, default=False)

    # 是否上架至紅利商城開放點數兌換 (BOOLEAN)
    is_reward = db.Column(db.Boolean, default=False)

    # 紅利兌換基準所需點數 (INTEGER)
    reward_points = db.Column(db.Integer, default=0)

    # 紅利兌換促銷/特惠所需點數 (INTEGER，若大於 0 優先以此計算)
    reward_discount_points = db.Column(db.Integer, default=0)

    # 是否可作為購物車專屬加購商品 (BOOLEAN)
    can_be_add_on = db.Column(db.Boolean, default=False)

    # 加購優惠價格 (INTEGER，單位 NTD)
    add_on_price = db.Column(db.Integer, default=0)

    # 加購觸發規則類型 (VARCHAR(20)，可選：any 全單主餐觸發, category 指定分類觸發, item 指定單品觸發)
    addon_trigger_type = db.Column(db.String(20), default='any')

    # 加購觸發指定目標值 (VARCHAR(100)，如特定分類名稱或特定餐點 ID)
    addon_trigger_target = db.Column(db.String(100), default='')


class ComboOption(TenantScoped, db.Model):
    """
    套餐升級選項資料表
    定義主餐可升級之套餐組合、加價幅度以及附屬配餐
    """
    __tablename__ = 'combo_option'

    # 主鍵：套餐選項識別碼 (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 外鍵：所屬主餐餐點 ID (關聯 menu_item.id)
    main_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=False)

    # 套餐名稱 (VARCHAR(100)，例如：A套餐-經典雙饗)
    name = db.Column(db.String(100), nullable=False)

    # 套餐升級加價金額 (INTEGER，單位 NTD)
    additional_price = db.Column(db.Integer, nullable=False)

    # 套餐組合說明文字 (VARCHAR(200))
    description = db.Column(db.String(200), default='')

    # 外鍵：配餐 1 之餐點 ID (可為空)
    item1_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)

    # 外鍵：配餐 2 之餐點 ID (可為空)
    item2_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)

    # 外鍵：配餐 3 之餐點 ID (可為空)
    item3_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)

    # 配餐 1 是否開放客製化規格選擇 (BOOLEAN)
    item1_customizable = db.Column(db.Boolean, default=True)

    # 配餐 2 是否開放客製化規格選擇 (BOOLEAN)
    item2_customizable = db.Column(db.Boolean, default=True)

    # 配餐 3 是否開放客製化規格選擇 (BOOLEAN)
    item3_customizable = db.Column(db.Boolean, default=True)

    # 點購此套餐後是否仍允許享有加購優惠 (BOOLEAN)
    can_addon = db.Column(db.Boolean, default=True)

    # 關聯：主餐與 3 項附屬配餐之 ORM 關聯
    main_item = db.relationship('MenuItem', foreign_keys=[main_item_id], backref=db.backref('combo_options', lazy=True, cascade="all, delete-orphan"))
    item1 = db.relationship('MenuItem', foreign_keys=[item1_id])
    item2 = db.relationship('MenuItem', foreign_keys=[item2_id])
    item3 = db.relationship('MenuItem', foreign_keys=[item3_id])


class ModifierOption(TenantScoped, db.Model):
    """
    客製化細項選項資料表:例如加料選項（加起司、加蛋）之單價與啟用狀態
    """
    __tablename__ = 'modifier_option'

    # 主鍵：客製化選項 ID (Integer)
    id = db.Column(db.Integer, primary_key=True)

    # 對應 MenuItem.modifiers 之群組名稱 (VARCHAR(50)，預設 'addons')
    category = db.Column(db.String(50), default='addons')

    # 選項名稱 (VARCHAR(50)，例如：加荷包蛋、起司片)
    name = db.Column(db.String(50), nullable=False)

    # 加價金額 (INTEGER，單位 NTD，預設 0)
    price = db.Column(db.Integer, nullable=False, default=0)

    # 啟用狀態 (BOOLEAN，False 代表已停用該加料品項)
    is_active = db.Column(db.Boolean, default=True)
