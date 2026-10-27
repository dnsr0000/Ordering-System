"""
原物料庫存：原料、配方 (每份餐點用量)、進貨 / 扣料 / 盤點異動、供應商與採購單、批號效期 (門市本機資料，不同步)
"""
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


class Ingredient(TenantScoped, db.Model):
    __tablename__ = 'ingredient'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'name', name='uq_ingredient_tenant_name'),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    unit = db.Column(db.String(20), nullable=False, default='g')
    stock = db.Column(db.Float, nullable=False, default=0)
    low_threshold = db.Column(db.Float, nullable=False, default=0)
    unit_cost = db.Column(db.Float, nullable=False, default=0)     # 每單位成本 (元)，報表計算毛利用
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    low_alerted_at = db.Column(db.DateTime, nullable=True)
    # 採購：預設供應商、目標庫存 (建議採購補到這個量；0 = 安全量的 2 倍)、採購單位 (例如一箱 1000 g，建議量進位到整箱)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id', ondelete='SET NULL', name='fk_ingredient_supplier_id'),
                            nullable=True)
    par_level = db.Column(db.Float, nullable=False, default=0, server_default='0')
    pack_size = db.Column(db.Float, nullable=False, default=1, server_default='1')

    supplier = db.relationship('Supplier')

    @property
    def is_low(self):
        return (self.low_threshold or 0) > 0 and (self.stock or 0) <= self.low_threshold


class RecipeLine(TenantScoped, db.Model):
    __tablename__ = 'recipe_line'
    __table_args__ = (db.UniqueConstraint('menu_item_id', 'ingredient_id', name='uq_recipe_item_ingredient'),)

    id = db.Column(db.Integer, primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='CASCADE'), nullable=False, index=True)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id', ondelete='CASCADE'), nullable=False, index=True)
    quantity = db.Column(db.Float, nullable=False, default=0)

    ingredient = db.relationship('Ingredient')
    menu_item = db.relationship('MenuItem')


class StockMovement(TenantScoped, db.Model):
    """原料異動：purchase 進貨 / consume 銷售扣料 / restore 取消回補 / stocktake 盤點調整 / waste 報廢 / adjust 手動調整"""
    __tablename__ = 'stock_movement'

    id = db.Column(db.Integer, primary_key=True)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id', ondelete='CASCADE'), nullable=False, index=True)
    delta = db.Column(db.Float, nullable=False, default=0)
    reason = db.Column(db.String(20), nullable=False, default='adjust')
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True)
    unit_cost = db.Column(db.Float, nullable=False, default=0)
    note = db.Column(db.String(200), nullable=False, default='')
    staff_username = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)

    ingredient = db.relationship('Ingredient')


class Supplier(TenantScoped, db.Model):
    """供應商 (各門市自己的進貨廠商)"""
    __tablename__ = 'supplier'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'name', name='uq_supplier_tenant_name'),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    contact = db.Column(db.String(60), nullable=False, default='')
    phone = db.Column(db.String(30), nullable=False, default='')
    email = db.Column(db.String(120), nullable=False, default='')
    lead_days = db.Column(db.Integer, nullable=False, default=1)       # 下單到到貨天數
    note = db.Column(db.String(200), nullable=False, default='')
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class PurchaseOrder(TenantScoped, db.Model):
    """採購單：draft 草稿 / sent 已送出 / received 已收貨 / cancelled 已取消"""
    __tablename__ = 'purchase_order'

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id', ondelete='SET NULL'), nullable=True, index=True)
    status = db.Column(db.String(12), nullable=False, default='draft', index=True)
    expected_on = db.Column(db.Date, nullable=True)
    note = db.Column(db.String(200), nullable=False, default='')
    created_by = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    sent_at = db.Column(db.DateTime, nullable=True)
    received_at = db.Column(db.DateTime, nullable=True)
    received_by = db.Column(db.String(120), nullable=False, default='')

    supplier = db.relationship('Supplier')
    lines = db.relationship('PurchaseOrderLine', backref='order', cascade='all, delete-orphan', passive_deletes=True,
                            order_by='PurchaseOrderLine.id')

    @property
    def number(self):
        return f'PO{self.created_at:%y%m%d}-{self.id:04d}' if self.created_at and self.id else ''

    @property
    def total(self):
        return round(sum((ln.quantity or 0) * (ln.unit_cost or 0) for ln in self.lines))


class PurchaseOrderLine(TenantScoped, db.Model):
    __tablename__ = 'purchase_order_line'

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey('purchase_order.id', ondelete='CASCADE'), nullable=False, index=True)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id', ondelete='CASCADE'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    unit_cost = db.Column(db.Float, nullable=False, default=0)
    received_qty = db.Column(db.Float, nullable=True)

    ingredient = db.relationship('Ingredient')


class IngredientLot(TenantScoped, db.Model):
    """
    原料批號 (每次進貨一批，含有效期限)：銷售扣料、報廢、盤虧依「先到期先出」扣減各批剩餘量；
    原料總量仍以 ingredient.stock 為準，批號用於效期管理與到期提醒。
    """
    __tablename__ = 'ingredient_lot'

    id = db.Column(db.Integer, primary_key=True)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id', ondelete='CASCADE'), nullable=False, index=True)
    quantity = db.Column(db.Float, nullable=False, default=0)          # 進貨量
    remaining = db.Column(db.Float, nullable=False, default=0)         # 剩餘量
    expires_on = db.Column(db.Date, nullable=True, index=True)
    received_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    unit_cost = db.Column(db.Float, nullable=False, default=0)
    source = db.Column(db.String(20), nullable=False, default='purchase')   # purchase / po / restore / stocktake
    note = db.Column(db.String(100), nullable=False, default='')
    alerted_at = db.Column(db.DateTime, nullable=True)

    ingredient = db.relationship('Ingredient')
