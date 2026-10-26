"""
費用與損益 (第四輪)：營業費用登錄 (房租、水電、雜支…)；原料成本 / 餐點售價的變動歷史。
"""
from datetime import date, datetime

from app.extensions import db
from app.models.mixins import TenantScoped

EXPENSE_CATEGORIES = {
    'rent': '租金', 'utilities': '水電瓦斯', 'payroll': '薪資 (未打卡部分 / 勞健保)', 'supplies': '耗材包材',
    'marketing': '行銷廣告', 'repair': '修繕設備', 'fees': '手續費 / 規費', 'tax': '稅捐', 'other': '其他',
}


class Expense(TenantScoped, db.Model):
    """
    營業費用 (一筆一筆登錄；固定費用可勾選「每月固定」，下個月一鍵複製)。
    分店登錄的費用上傳到線上端 (損益表可依分店比較)；node_uuid 為登錄的分店 (線上端 / 單機為空)。
    """
    __tablename__ = 'expense'

    id = db.Column(db.Integer, primary_key=True)
    spent_on = db.Column(db.Date, nullable=False, default=date.today, index=True)
    category = db.Column(db.String(20), nullable=False, default='other')
    name = db.Column(db.String(80), nullable=False)
    amount = db.Column(db.Integer, nullable=False, default=0)
    vendor = db.Column(db.String(60), nullable=False, default='')
    invoice_no = db.Column(db.String(20), nullable=False, default='')        # 進項發票號碼 (選填)
    is_fixed = db.Column(db.Boolean, nullable=False, default=False)          # 每月固定 (下月可一鍵複製)
    note = db.Column(db.String(200), nullable=False, default='')
    node_uuid = db.Column(db.String(36), nullable=True, index=True)
    created_by = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)        # 刪除改為標記 (上傳到線上端的也一併刪除)

    @property
    def category_label(self):
        return EXPENSE_CATEGORIES.get(self.category, self.category)


class CostHistory(TenantScoped, db.Model):
    """
    原料單位成本 / 餐點售價的變動紀錄 (任何途徑修改都會記錄：後台編輯、進貨加權平均、調撥收貨、匯入、同步)。
    kind：ingredient / menu；field：unit_cost / price / discount_price / takeout_price / online_price / delivery_price
    """
    __tablename__ = 'cost_history'

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(12), nullable=False)
    ref_id = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False, default='')
    field = db.Column(db.String(20), nullable=False)
    old_value = db.Column(db.Float, nullable=False, default=0)
    new_value = db.Column(db.Float, nullable=False, default=0)
    changed_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    changed_by = db.Column(db.String(120), nullable=False, default='')

    @property
    def change_pct(self):
        return round((self.new_value - self.old_value) / self.old_value * 100, 1) if self.old_value else None
