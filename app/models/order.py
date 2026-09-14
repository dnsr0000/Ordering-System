from datetime import datetime
from app.extensions import db
from app.models.user import User

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    table_number = db.Column(db.String(50), default='訪客')
    pickup_number = db.Column(db.Integer, default=1)
    total_price = db.Column(db.Integer, nullable=False)
    payment_method = db.Column(db.String(50), default='Cash')
    order_type = db.Column(db.String(50), default='內用')
    need_cutlery = db.Column(db.Boolean, default=True)
    note = db.Column(db.String(200), default='')
    status = db.Column(db.String(50), default='Pending')
    points_used = db.Column(db.Integer, default=0)
    points_discount_amount = db.Column(db.Integer, default=0)
    points_earned = db.Column(db.Integer, default=0)
    discount_amount = db.Column(db.Integer, default=0)
    coupon_code = db.Column(db.String(50), default='')
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    customer_log_id = db.Column(db.Integer, nullable=True)
    items = db.relationship('OrderItem', backref='order', lazy=True, cascade="all, delete-orphan")

    @property
    def user(self):
        if self.user_id:
            return db.session.get(User, self.user_id)
        return None

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    item_name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, default=1)
    customization = db.Column(db.String(200), default='')