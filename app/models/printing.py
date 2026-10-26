"""出單印表機、列印佇列與廚房工作站 (門市本機資料，不同步)"""
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


class KitchenStation(TenantScoped, db.Model):
    """
    廚房工作站 (例如飲料站、熱食站)：負責哪些分類 (逗號分隔)；品項也可個別指定工作站 (menu_item.station_id)。
    KDS 開啟 /kitchen?station=<id> 只看到這一站的品項；各站完成後訂單才轉為已出餐。
    """
    __tablename__ = 'kitchen_station'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    categories = db.Column(db.String(300), nullable=False, default='')
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def category_list(self):
        return [c.strip() for c in (self.categories or '').replace('，', ',').split(',') if c.strip()]


class Printer(TenantScoped, db.Model):
    """網路熱感印表機 (ESC/POS，TCP 9100)。role：receipt 收據 / kitchen 廚房單 (可指定工作站)"""
    __tablename__ = 'printer'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    host = db.Column(db.String(100), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=9100)
    role = db.Column(db.String(20), nullable=False, default='receipt')
    station_id = db.Column(db.Integer, db.ForeignKey('kitchen_station.id', ondelete='SET NULL'), nullable=True)
    paper_width = db.Column(db.Integer, nullable=False, default=80)    # 58 / 80 mm
    encoding = db.Column(db.String(20), nullable=False, default='big5')
    copies = db.Column(db.Integer, nullable=False, default=1)
    cut = db.Column(db.Boolean, nullable=False, default=True)
    # 這台收據印表機有接錢櫃 (RJ11)：現金結帳時送出開錢櫃指令
    drawer = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    station = db.relationship('KitchenStation')


class PrintJob(TenantScoped, db.Model):
    __tablename__ = 'print_job'

    id = db.Column(db.Integer, primary_key=True)
    printer_id = db.Column(db.Integer, db.ForeignKey('printer.id', ondelete='CASCADE'), nullable=False, index=True)
    title = db.Column(db.String(100), nullable=False, default='')
    payload = db.Column(db.LargeBinary, nullable=False)
    status = db.Column(db.String(10), nullable=False, default='queued', index=True)   # queued / done / failed
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(300), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    printed_at = db.Column(db.DateTime, nullable=True)

    printer = db.relationship('Printer')
