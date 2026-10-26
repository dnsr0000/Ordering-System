"""會員等級、儲值金異動帳、簡訊驗證碼"""
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


class MemberTier(TenantScoped, db.Model):
    """
    會員等級：累積消費達 min_spend 自動升級。points_multiplier 為贈點倍數，discount_percent 為會員折扣 (%)。
    與菜單一樣在總店與各分店之間同步。
    """
    __tablename__ = 'member_tier'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'name', name='uq_member_tier_tenant_name'),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), nullable=False)
    min_spend = db.Column(db.Integer, nullable=False, default=0)
    points_multiplier = db.Column(db.Float, nullable=False, default=1.0)
    discount_percent = db.Column(db.Integer, nullable=False, default=0)
    color = db.Column(db.String(20), nullable=False, default='secondary')


class WalletLedger(TenantScoped, db.Model):
    """
    儲值金異動帳 (只新增、不修改)；user.wallet_balance 為合計快取 (與點數異動帳相同的設計，跨分店同步後重算)。
    reason：topup 加值 / bonus 加值贈送 / pay 消費 / refund 退回 / adjust 後台調整
    """
    __tablename__ = 'wallet_ledger'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    delta = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(20), nullable=False, default='')
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True)
    note = db.Column(db.String(200), nullable=False, default='')
    staff_username = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class OtpCode(TenantScoped, db.Model):
    """簡訊驗證碼 (只存雜湊)。purpose：register 註冊 / reset_pin 忘記 PIN"""
    __tablename__ = 'otp_code'

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    purpose = db.Column(db.String(20), nullable=False)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    consumed_at = db.Column(db.DateTime, nullable=True)
    verified_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    ip_address = db.Column(db.String(50), nullable=False, default='')


class StampLedger(TenantScoped, db.Model):
    """
    集點卡章數異動帳 (只新增)：earn 消費得章 / cancel 訂單取消扣回 / complete 集滿兌換 (負數) / adjust 後台調整。
    會員目前章數 = 合計；跨分店同步後自然合併 (與點數異動帳相同)。
    """
    __tablename__ = 'stamp_ledger'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    delta = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(20), nullable=False, default='')
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class GiftCard(TenantScoped, db.Model):
    """
    禮物卡 (商品禮券)：卡號 16 碼 (自然鍵，各分店通用)，餘額 = 異動帳合計。
    依「商品 (服務) 禮券定型化契約應記載及不得記載事項」：不得記載使用期限，售出時需提供履約保證 (見 DEPLOY.txt)。
    """
    __tablename__ = 'gift_card'
    __table_args__ = (db.UniqueConstraint('tenant_id', 'code', name='uq_gift_card_tenant_code'),)

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False)
    initial_amount = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(10), nullable=False, default='active')   # active / void
    note = db.Column(db.String(100), nullable=False, default='')
    sold_by = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    voided_at = db.Column(db.DateTime, nullable=True)

    @property
    def masked_code(self):
        return f'{self.code[:4]}-****-****-{self.code[-4:]}' if len(self.code or '') >= 8 else self.code


class PassProduct(TenantScoped, db.Model):
    """
    寄杯 / 多次券商品：例如「美式咖啡 10 杯 $450」。會員購買後得到 MemberPass，每次取一杯 (份) 扣一次。
    屬預收款 (商品禮券)，上線前請確認履約保證 (DEPLOY.txt)；依規定不設使用期限。
    """
    __tablename__ = 'pass_product'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='CASCADE'), nullable=False)
    uses = db.Column(db.Integer, nullable=False, default=10)
    price = db.Column(db.Integer, nullable=False, default=0)
    description = db.Column(db.String(200), nullable=False, default='')
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    menu_item = db.relationship('MenuItem')

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'uses': self.uses, 'price': self.price, 'description': self.description,
                'menu_item_id': self.menu_item_id, 'item_name': self.menu_item.name if self.menu_item else ''}


class MemberPass(TenantScoped, db.Model):
    """會員持有的寄杯：剩餘次數 remaining = 異動帳 (pass_ledger) 合計 (本機欄位，同步後重算，與點數相同)"""
    __tablename__ = 'member_pass'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('pass_product.id', ondelete='SET NULL'), nullable=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id', ondelete='SET NULL'), nullable=True)
    name = db.Column(db.String(60), nullable=False, default='')
    total_uses = db.Column(db.Integer, nullable=False, default=0)
    price_paid = db.Column(db.Integer, nullable=False, default=0)
    sale_order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True)
    status = db.Column(db.String(10), nullable=False, default='active')   # active / void
    remaining = db.Column(db.Integer, nullable=False, default=0)          # 本機：異動帳合計
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    menu_item = db.relationship('MenuItem')
    user = db.relationship('User')

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'remaining': self.remaining, 'total_uses': self.total_uses,
                'menu_item_id': self.menu_item_id, 'item_name': self.menu_item.name if self.menu_item else '',
                'status': self.status, 'created_at': self.created_at.strftime('%Y-%m-%d') if self.created_at else ''}


class PassLedger(TenantScoped, db.Model):
    """寄杯異動帳 (只新增)：sale 購買 (+次數) / redeem 兌換 (負數) / cancel 兌換訂單取消退回 / void 作廢"""
    __tablename__ = 'pass_ledger'

    id = db.Column(db.Integer, primary_key=True)
    member_pass_id = db.Column(db.Integer, db.ForeignKey('member_pass.id', ondelete='CASCADE'), nullable=False, index=True)
    delta = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(20), nullable=False, default='')
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True, index=True)
    staff_username = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class GiftCardLedger(TenantScoped, db.Model):
    """禮物卡異動帳 (只新增)：issue 售出 / redeem 消費 (負數) / refund 訂單退款退回 / void 作廢 (扣為 0)"""
    __tablename__ = 'gift_card_ledger'

    id = db.Column(db.Integer, primary_key=True)
    gift_card_id = db.Column(db.Integer, db.ForeignKey('gift_card.id', ondelete='CASCADE'), nullable=False, index=True)
    delta = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(20), nullable=False, default='')
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True, index=True)
    staff_username = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    gift_card = db.relationship('GiftCard', backref=db.backref('entries', lazy='dynamic', passive_deletes=True))
