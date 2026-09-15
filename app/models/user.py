from datetime import datetime
from app.extensions import db

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    photo_path = db.Column(db.String(200), nullable=False)
    feature = db.Column(db.PickleType, nullable=True)
    points = db.Column(db.Integer, default=0)
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_logout_at = db.Column(db.DateTime, nullable=True)

class AdminLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    login_at = db.Column(db.DateTime, default=datetime.now)
    logout_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(50), default='')

class CustomerLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_type = db.Column(db.String(20), nullable=False)
    identifier = db.Column(db.String(100), nullable=False)
    login_at = db.Column(db.DateTime, default=datetime.now)
    logout_at = db.Column(db.DateTime, nullable=True)
    ordered_items = db.Column(db.Text, default='')
    ip_address = db.Column(db.String(50), default='')

# app/models/user.py

class RewardSetting(db.Model):
    __tablename__ = 'reward_setting'
    id = db.Column(db.Integer, primary_key=True)
    is_enabled = db.Column(db.Boolean, default=True)               # 啟用點數折抵
    points_per_dollar = db.Column(db.Integer, default=1)           # 每 $1 需要幾點
    max_discount_per_order = db.Column(db.Integer, default=0)      # 單筆最多折抵金額 (0 = 不限)
    spend_per_point = db.Column(db.Integer, default=100)           # 消費滿幾元送 1 點