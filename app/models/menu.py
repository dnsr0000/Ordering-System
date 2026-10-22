from app.extensions import db
from app.models.mixins import TenantScoped

MODIFIER_GROUPS = ('none', 'ice_sugar', 'spicy', 'addons')


class MenuItem(TenantScoped, db.Model):
    """
    菜單品項。
    - 庫存：stock 為剩餘數量、total_stock 為總量；is_sold_out 也可由店員手動標示。
    - 加購品：can_be_add_on + add_on_price，觸發條件 addon_trigger_type = any / category / item。
    - 紅利商城：is_reward + reward_points (reward_discount_points > 0 時為限時特惠點數)。
    """
    __tablename__ = 'menu_item'

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

    combo_options = db.relationship(
        'ComboOption', foreign_keys='ComboOption.main_item_id', back_populates='main_item',
        cascade='all, delete-orphan', passive_deletes=True, order_by='ComboOption.id',
    )

    @property
    def unit_price(self):
        """目前售價 (特價優先)"""
        return self.discount_price if (self.is_discount and (self.discount_price or 0) > 0) else self.price

    @property
    def is_available(self):
        return not self.is_sold_out and (self.stock is None or self.stock > 0)

    @property
    def redeem_points(self):
        """兌換所需點數 (限時特惠點數優先)"""
        return self.reward_discount_points if (self.reward_discount_points or 0) > 0 else (self.reward_points or 0)

    @property
    def has_modifiers(self):
        return bool(self.modifiers) and self.modifiers != 'none'

    def to_dict(self):
        """提供前端 (kiosk / POS) 使用的菜單資料"""
        return {
            'id': self.id,
            'name': self.name,
            'category': (self.category or '未分類').strip(),
            'price': self.unit_price,
            'original_price': self.price,
            'is_discount': bool(self.is_discount and (self.discount_price or 0) > 0),
            'modifiers': self.modifiers or 'none',
            'description': self.description or '',
            'image_path': self.image_path or '',
            'is_new': bool(self.is_new),
            'is_popular': bool(self.is_manual_popular or self.is_recommended),
            'is_available': self.is_available,
            'stock': self.stock,
            'can_be_add_on': bool(self.can_be_add_on),
            'add_on_price': self.add_on_price or 0,
            'addon_trigger_type': self.addon_trigger_type or 'any',
            'addon_trigger_target': (self.addon_trigger_target or '').strip(),
            'is_reward': bool(self.is_reward),
            'redeem_points': self.redeem_points,
            'combos': [c.to_dict() for c in self.combo_options],
        }


class ComboOption(TenantScoped, db.Model):
    """主餐的套餐升級選項：加價 additional_price，附帶 sides 中的配餐 (會一併扣庫存)。"""
    __tablename__ = 'combo_option'

    id = db.Column(db.Integer, primary_key=True)
    main_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    additional_price = db.Column(db.Integer, nullable=False, default=0)
    description = db.Column(db.String(200), default='')
    can_addon = db.Column(db.Boolean, default=True)

    main_item = db.relationship('MenuItem', foreign_keys=[main_item_id], back_populates='combo_options')
    sides = db.relationship(
        'ComboOptionItem', back_populates='combo', cascade='all, delete-orphan',
        passive_deletes=True, order_by='ComboOptionItem.position',
    )

    @property
    def side_item_ids(self):
        return [s.menu_item_id for s in self.sides]

    @property
    def is_available(self):
        return all(s.menu_item is not None and s.menu_item.is_available for s in self.sides)

    def to_dict(self):
        side_names = [s.menu_item.name for s in self.sides if s.menu_item]
        return {
            'id': self.id,
            'name': self.name,
            'price': self.additional_price,
            'description': self.description or ' + '.join(side_names),
            'can_addon': self.can_addon is not False,
            'is_available': self.is_available,
            'sides': [s.to_dict() for s in self.sides if s.menu_item],
        }


class ComboOptionItem(TenantScoped, db.Model):
    """套餐內的配餐 (取代舊版 item1_id / item2_id / item3_id 三組欄位)"""
    __tablename__ = 'combo_option_item'

    id = db.Column(db.Integer, primary_key=True)
    combo_option_id = db.Column(db.Integer, db.ForeignKey('combo_option.id', ondelete='CASCADE'), nullable=False, index=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='CASCADE'), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    customizable = db.Column(db.Boolean, nullable=False, default=False)

    combo = db.relationship('ComboOption', back_populates='sides')
    menu_item = db.relationship('MenuItem')

    def to_dict(self):
        return {
            'menu_item_id': self.menu_item_id,
            'name': self.menu_item.name,
            'modifiers': self.menu_item.modifiers or 'none',
            # 配餐本身沒有客製化群組時，即使設定為可客製也沒有意義
            'customizable': bool(self.customizable and self.menu_item.has_modifiers),
        }


class ModifierOption(TenantScoped, db.Model):
    """加料選項 (加蛋、加起司…)，價格由後端查表，不信任前端傳入的金額"""
    __tablename__ = 'modifier_option'

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), default='addons')
    name = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {'id': self.id, 'category': self.category, 'name': self.name, 'price': self.price}
