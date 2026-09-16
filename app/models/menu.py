from app.extensions import db

class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), default='主餐')
    price = db.Column(db.Integer, nullable=False)
    stock = db.Column(db.Integer, default=50)
    total_stock = db.Column(db.Integer, default=50)
    is_sold_out = db.Column(db.Boolean, default=False)
    is_discount = db.Column(db.Boolean, default=False)
    discount_price = db.Column(db.Integer, default=0)
    is_recommended = db.Column(db.Boolean, default=False)
    is_manual_popular = db.Column(db.Boolean, default=False)
    modifiers = db.Column(db.String(50), default='none')
    description = db.Column(db.String(200), default='')
    image_path = db.Column(db.String(200), default='')
    is_new = db.Column(db.Boolean, default=False)
    is_reward = db.Column(db.Boolean, default=False)
    reward_points = db.Column(db.Integer, default=0)
    reward_discount_points = db.Column(db.Integer, default=0)
    can_be_add_on = db.Column(db.Boolean, default=False)
    add_on_price = db.Column(db.Integer, default=0)
    addon_trigger_type = db.Column(db.String(20), default='any')
    addon_trigger_target = db.Column(db.String(100), default='')

class ComboOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    main_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    additional_price = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(200), default='')
    
    item1_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)
    item2_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)
    item3_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)

    item1_customizable = db.Column(db.Boolean, default=True)
    item2_customizable = db.Column(db.Boolean, default=True)
    item3_customizable = db.Column(db.Boolean, default=True)
    can_addon = db.Column(db.Boolean, default=True)

    main_item = db.relationship('MenuItem', foreign_keys=[main_item_id], backref=db.backref('combo_options', lazy=True, cascade="all, delete-orphan"))
    item1 = db.relationship('MenuItem', foreign_keys=[item1_id])
    item2 = db.relationship('MenuItem', foreign_keys=[item2_id])
    item3 = db.relationship('MenuItem', foreign_keys=[item3_id])

class ModifierOption(db.Model):
    __tablename__ = 'modifier_option'
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), default='addons')   # 對應 MenuItem.modifiers 群組名稱
    name = db.Column(db.String(50), nullable=False)          # 選項名稱
    price = db.Column(db.Integer, nullable=False, default=0) # 加價金額 (NTD)
    is_active = db.Column(db.Boolean, default=True)          # 啟用狀態