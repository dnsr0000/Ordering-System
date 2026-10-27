import re
from datetime import datetime

from app.extensions import db
from app.models.branch import active_overrides
from app.models.mixins import TenantScoped

MODIFIER_GROUPS = ('none', 'ice_sugar', 'spicy', 'addons')

# 食品過敏原標示 (依衛福部公告須標示的過敏原)
ALLERGENS = {
    'shellfish': '甲殼類', 'mango': '芒果', 'peanut': '花生', 'milk': '牛奶/羊奶', 'egg': '蛋',
    'nuts': '堅果', 'sesame': '芝麻', 'gluten': '含麩質穀物', 'soy': '大豆', 'fish': '魚類', 'sulfite': '亞硫酸鹽',
}
ALLERGENS_I18N = {
    'en': {'shellfish': 'Crustacean', 'mango': 'Mango', 'peanut': 'Peanut', 'milk': 'Milk', 'egg': 'Egg',
           'nuts': 'Tree nuts', 'sesame': 'Sesame', 'gluten': 'Gluten', 'soy': 'Soy', 'fish': 'Fish',
           'sulfite': 'Sulfites'},
    'ja': {'shellfish': '甲殻類', 'mango': 'マンゴー', 'peanut': '落花生', 'milk': '乳', 'egg': '卵',
           'nuts': 'ナッツ類', 'sesame': 'ごま', 'gluten': '小麦(グルテン)', 'soy': '大豆', 'fish': '魚',
           'sulfite': '亜硫酸塩'},
}
# 素食標示 (依素食標示規定)
DIETS = {'vegan': '全素', 'egg_veg': '蛋素', 'milk_veg': '奶素', 'ovo_lacto': '蛋奶素', 'five_pungent': '植物五辛素'}
DIETS_I18N = {
    'en': {'vegan': 'Vegan', 'egg_veg': 'Ovo-vegetarian', 'milk_veg': 'Lacto-vegetarian',
           'ovo_lacto': 'Lacto-ovo vegetarian', 'five_pungent': 'Vegetarian (with five pungent)'},
    'ja': {'vegan': 'ヴィーガン', 'egg_veg': '卵菜食', 'milk_veg': '乳菜食', 'ovo_lacto': '乳卵菜食',
           'five_pungent': '五葷入り菜食'},
}
# 通路：價格可依通路不同 (0 = 與內用同價)
CHANNELS = ('dine_in', 'takeout', 'online', 'delivery')
WEEKDAY_LABELS = '一二三四五六日'



def parse_size_options(text):
    """「小杯:-10,中杯:0,大杯:+15」→ [('小杯', -10), ('中杯', 0), ('大杯', 15)]；格式錯誤的項目略過"""
    out, seen = [], set()
    for part in re.split(r'[,，、\n]+', str(text or '')):
        name, _, delta = part.partition(':')
        name = name.strip()[:20]
        if not name or name in seen:
            continue
        try:
            value = int(str(delta).strip() or 0)
        except ValueError:
            continue
        seen.add(name)
        out.append((name, max(-10000, min(10000, value))))
    return out[:8]

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
    # ---- 時段 / 排程上下架：供應時間 (HH:MM，空白 = 全天)、星期 (1=一 … 7=日)、販售期間 ----
    avail_start = db.Column(db.String(5), nullable=False, default='', server_default='')
    avail_end = db.Column(db.String(5), nullable=False, default='', server_default='')
    avail_days = db.Column(db.String(7), nullable=False, default='1234567', server_default='1234567')
    sale_from = db.Column(db.Date, nullable=True)
    sale_until = db.Column(db.Date, nullable=True)
    # ---- 通路價 (0 = 與內用同價) ----
    takeout_price = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    online_price = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    delivery_price = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    # ---- 過敏原 (ALLERGENS 代碼，逗號分隔)、素食、辣度 0~3 ----
    allergens = db.Column(db.String(200), nullable=False, default='', server_default='')
    diet = db.Column(db.String(20), nullable=False, default='', server_default='')
    spicy_level = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    # ---- 多語系 ----
    name_en = db.Column(db.String(100), nullable=False, default='', server_default='')
    name_ja = db.Column(db.String(100), nullable=False, default='', server_default='')
    description_en = db.Column(db.String(300), nullable=False, default='', server_default='')
    description_ja = db.Column(db.String(300), nullable=False, default='', server_default='')
    # ---- 外送平台 / 條碼對應代碼、單位成本 (沒有設定配方時用來估算毛利)、廚房工作站 (本機) ----
    external_code = db.Column(db.String(50), nullable=False, default='', server_default='')
    unit_cost = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    # 規格 (單選，影響價格)：「小杯:-10,中杯:0,大杯:15」= 名稱:加減價；空白 = 沒有規格
    size_options = db.Column(db.String(200), nullable=False, default='', server_default='')
    # 熱量 (大卡，0 = 未標示) 與營養標示文字 (蛋白質 / 脂肪 / 碳水 / 鈉…)
    calories = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    nutrition = db.Column(db.String(200), nullable=False, default='', server_default='')
    station_id = db.Column(db.Integer, db.ForeignKey('kitchen_station.id', ondelete='SET NULL', name='fk_menu_item_station_id'),
                           nullable=True)

    combo_options = db.relationship(
        'ComboOption', foreign_keys='ComboOption.main_item_id', back_populates='main_item',
        cascade='all, delete-orphan', passive_deletes=True, order_by='ComboOption.id',
    )

    # ---- 分店價格 / 上下架 (見 app/models/branch.py)：點餐頁、POS、結帳時依分店設定，其他頁面為總部價格 ----
    @property
    def branch_delta(self):
        """分店價與總部內用價的差額 (通路價、特價同步加減)"""
        return active_overrides().get(self.id, (0, False))[0]

    @property
    def is_branch_hidden(self):
        return bool(active_overrides().get(self.id, (0, False))[1])

    @property
    def list_price(self):
        """內用價 (含分店價差)"""
        return max(0, (self.price or 0) + self.branch_delta)

    @property
    def sale_price(self):
        """特價 (含分店價差)"""
        return max(0, (self.discount_price or 0) + self.branch_delta)

    @property
    def unit_price(self):
        """目前售價 (特價優先)"""
        return self.sale_price if (self.is_discount and (self.discount_price or 0) > 0) else self.list_price

    @property
    def is_available(self):
        return not self.is_sold_out and (self.stock is None or self.stock > 0) and not self.is_branch_hidden

    def price_for(self, channel='dine_in'):
        """依通路的售價：特價優先；否則該通路有設定價格 (> 0) 時使用，未設定與內用同價 (都含分店價差)"""
        if self.is_discount and (self.discount_price or 0) > 0:
            return self.sale_price
        custom = {'takeout': self.takeout_price, 'online': self.online_price, 'delivery': self.delivery_price}.get(channel)
        return max(0, custom + self.branch_delta) if (custom or 0) > 0 else self.list_price

    def service_status(self, now=None):
        """(是否在供應時段內, 說明)；不在販售期間、非供應星期或時段時不可點"""
        now = now or datetime.now()
        today = now.date()
        if self.sale_from and today < self.sale_from:
            return False, f'{self.sale_from:%m/%d} 起供應'
        if self.sale_until and today > self.sale_until:
            return False, '已停售'
        days = self.avail_days or '1234567'
        if str(now.isoweekday()) not in days:
            return False, '供應日：週' + '、'.join(WEEKDAY_LABELS[int(d) - 1] for d in sorted(set(days)) if d.isdigit() and 1 <= int(d) <= 7)
        start, end = (self.avail_start or '').strip(), (self.avail_end or '').strip()
        if start or end:
            hm = now.strftime('%H:%M')
            start, end = start or '00:00', end or '23:59'
            inside = start <= hm <= end if start <= end else (hm >= start or hm <= end)  # 跨午夜
            if not inside:
                return False, f'供應時段 {start}~{end}'
        return True, ''

    @property
    def sizes(self):
        """[(名稱, 加減價)]；第一個是預設規格"""
        return parse_size_options(self.size_options)

    @property
    def allergen_codes(self):
        return [c for c in (self.allergens or '').split(',') if c in ALLERGENS]

    def translated(self, lang):
        """(名稱, 說明) 的翻譯；沒有翻譯時用中文"""
        if lang in ('en', 'ja'):
            return (getattr(self, f'name_{lang}') or self.name,
                    getattr(self, f'description_{lang}') or self.description or '')
        return self.name, self.description or ''

    @property
    def redeem_points(self):
        """兌換所需點數 (限時特惠點數優先)"""
        return self.reward_discount_points if (self.reward_discount_points or 0) > 0 else (self.reward_points or 0)

    @property
    def has_modifiers(self):
        return bool(self.modifiers) and self.modifiers != 'none'

    def to_dict(self, now=None):
        """提供前端 (kiosk / POS) 使用的菜單資料"""
        in_service, hint = self.service_status(now)
        return {
            'id': self.id,
            'name': self.name,
            'category': (self.category or '未分類').strip(),
            'price': self.unit_price,
            'original_price': self.list_price,
            'is_discount': bool(self.is_discount and (self.discount_price or 0) > 0),
            'modifiers': self.modifiers or 'none',
            'description': self.description or '',
            'image_path': self.image_path or '',
            'is_new': bool(self.is_new),
            'is_popular': bool(self.is_manual_popular or self.is_recommended),
            'is_available': self.is_available and in_service,
            'in_service': in_service,
            'service_hint': hint,
            'prices': {ch: self.price_for(ch) for ch in CHANNELS},
            'allergens': self.allergen_codes,
            'sizes': [{'name': n, 'delta': d} for n, d in self.sizes],
            'calories': self.calories or 0,
            'nutrition': self.nutrition or '',
            'diet': self.diet or '',
            'spicy_level': self.spicy_level or 0,
            'i18n': {lang: {'name': getattr(self, f'name_{lang}') or '', 'description': getattr(self, f'description_{lang}') or ''}
                     for lang in ('en', 'ja')},
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
    def fixed_sides(self):
        return [s for s in self.sides if not s.choice_group]

    @property
    def side_item_ids(self):
        """固定配餐的餐點 ID (任選群組由顧客選擇，見 choice_groups)"""
        return [s.menu_item_id for s in self.fixed_sides]

    @property
    def choice_groups(self):
        """任選群組 [(群組名稱, [配餐選項])]，依設定順序"""
        groups = {}
        for s in self.sides:
            if s.choice_group:
                groups.setdefault(s.choice_group, []).append(s)
        return list(groups.items())

    @property
    def choices_text(self):
        """後台編輯用：每個任選群組一行「飲料: 紅茶, 綠茶, 珍珠奶茶+15」"""
        return '\n'.join(f"{group}: " + ', '.join(
            f"{o.menu_item.name}{f'+{o.extra_price}' if o.extra_price else ''}" for o in options if o.menu_item)
            for group, options in self.choice_groups)

    @property
    def is_available(self):
        """固定配餐都有貨，且每個任選群組至少一個選項有貨"""
        fixed_ok = all(s.menu_item is not None and s.menu_item.is_available for s in self.fixed_sides)
        return fixed_ok and all(any(o.menu_item is not None and o.menu_item.is_available for o in options)
                                for _group, options in self.choice_groups)

    def to_dict(self):
        side_names = [s.menu_item.name for s in self.fixed_sides if s.menu_item]
        side_names += [f'{group}任選' for group, _options in self.choice_groups]
        return {
            'id': self.id,
            'name': self.name,
            'price': self.additional_price,
            'description': self.description or ' + '.join(side_names),
            'can_addon': self.can_addon is not False,
            'is_available': self.is_available,
            'sides': [s.to_dict() for s in self.fixed_sides if s.menu_item],
            'choices': [{'group': group, 'options': [o.to_dict() for o in options if o.menu_item]}
                        for group, options in self.choice_groups],
        }


class ComboOptionItem(TenantScoped, db.Model):
    """套餐內的配餐 (取代舊版 item1_id / item2_id / item3_id 三組欄位)"""
    __tablename__ = 'combo_option_item'

    id = db.Column(db.Integer, primary_key=True)
    combo_option_id = db.Column(db.Integer, db.ForeignKey('combo_option.id', ondelete='CASCADE'), nullable=False, index=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='CASCADE'), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    customizable = db.Column(db.Boolean, nullable=False, default=False)
    # 任選群組：空白 = 固定配餐；同一群組名稱 (例如「飲料」) 的配餐為「任選一項」，extra_price 為選這一項的加價
    choice_group = db.Column(db.String(30), nullable=False, default='', server_default='')
    extra_price = db.Column(db.Integer, nullable=False, default=0, server_default='0')

    combo = db.relationship('ComboOption', back_populates='sides')
    menu_item = db.relationship('MenuItem')

    def to_dict(self):
        return {
            'id': self.id,
            'menu_item_id': self.menu_item_id,
            'name': self.menu_item.name,
            'modifiers': self.menu_item.modifiers or 'none',
            # 配餐本身沒有客製化群組時，即使設定為可客製也沒有意義
            'customizable': bool(self.customizable and self.menu_item.has_modifiers),
            'extra_price': self.extra_price or 0,
            'is_available': self.menu_item.is_available,
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


class CategoryTranslation(TenantScoped, db.Model):
    """菜單分類的英文 / 日文名稱 (點餐頁切換語言時使用)"""
    __tablename__ = 'category_translation'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'category', name='uq_category_translation'),)

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)
    name_en = db.Column(db.String(60), nullable=False, default='')
    name_ja = db.Column(db.String(60), nullable=False, default='')
