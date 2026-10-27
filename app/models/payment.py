"""
收款、退款、桌位分開結帳與電子發票。

- payment_record：每一筆金流 (收款為正、退款為負)。交班的應有現金、報表的付款方式統計都由此計算。
- refund：作廢 (整單取消) 與部分退款的紀錄 (含核准主管、LINE Pay 退款與發票折讓結果)。
- settlement：桌位結帳 (一次結清整桌、或只結其中幾張單、或均分 / 自訂金額分開付款)。
- invoice：電子發票 (綠界加值中心)。開立失敗 (例如斷網) 時保持 pending，背景與後台可重送。
"""
from datetime import datetime

from app.extensions import db
from app.models.mixins import TenantScoped


class PaymentKind:
    SALE = 'sale'          # 結帳收款
    SETTLE = 'settle'      # 桌位結帳收款
    REFUND = 'refund'      # 退款 (金額為負)
    TOPUP = 'topup'        # 儲值金加值收款
    GIFT_SALE = 'gift_sale'  # 禮物卡售出收款 (預收款，與儲值相同列在「預收」)
    PAY_IN = 'pay_in'      # 零用金存入
    PAY_OUT = 'pay_out'    # 零用金支出 (金額為負)


class PaymentRecord(TenantScoped, db.Model):
    __tablename__ = 'payment_record'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True, index=True)
    kind = db.Column(db.String(20), nullable=False, default=PaymentKind.SALE)
    method = db.Column(db.String(50), nullable=False, default='Cash')
    amount = db.Column(db.Integer, nullable=False, default=0)
    # 現金：實收與找零 (amount = 實收 - 找零)
    received = db.Column(db.Integer, nullable=False, default=0)
    change = db.Column(db.Integer, nullable=False, default=0)
    reference = db.Column(db.String(60), nullable=False, default='')   # 刷卡授權碼 / LINE Pay 交易序號
    card_last4 = db.Column(db.String(4), nullable=False, default='')
    note = db.Column(db.String(200), nullable=False, default='')
    staff_username = db.Column(db.String(120), nullable=False, default='')
    shift_id = db.Column(db.Integer, nullable=True, index=True)          # 本機交班 (不同步)
    settlement_id = db.Column(db.Integer, nullable=True, index=True)     # 本機桌位結帳 (不同步)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)


class Refund(TenantScoped, db.Model):
    __tablename__ = 'refund'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True, index=True)
    kind = db.Column(db.String(20), nullable=False, default='partial')   # cancel (作廢整單) / partial (部分退款)
    amount = db.Column(db.Integer, nullable=False, default=0)
    method = db.Column(db.String(50), nullable=False, default='Cash')
    reason = db.Column(db.String(200), nullable=False, default='')
    # done 已退款 / manual 需人工 (刷卡機、綠界後台) / pending 線上退款處理中 / failed 線上退款失敗
    status = db.Column(db.String(20), nullable=False, default='done')
    staff_username = db.Column(db.String(120), nullable=False, default='')
    approved_by = db.Column(db.String(120), nullable=False, default='')
    provider_result = db.Column(db.String(300), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    order = db.relationship('Order')


class Settlement(TenantScoped, db.Model):
    __tablename__ = 'settlement'

    id = db.Column(db.Integer, primary_key=True)
    table_name = db.Column(db.String(50), nullable=False, default='')
    total = db.Column(db.Integer, nullable=False, default=0)
    order_ids = db.Column(db.String(500), nullable=False, default='')
    staff_username = db.Column(db.String(120), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class InvoiceStatus:
    WAITING = 'waiting'      # 等待付款完成 (線上訂單)
    PENDING = 'pending'      # 待開立 (含開立失敗待重送)
    ISSUED = 'issued'
    VOID = 'void'            # 已作廢
    ALLOWANCE = 'allowance'  # 已開折讓 (部分或全部)
    FAILED = 'failed'        # 多次失敗 / 資料錯誤，需人工處理
    SKIPPED = 'skipped'      # 不開發票 (例如外送平台代開)


class Invoice(TenantScoped, db.Model):
    __tablename__ = 'invoice'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id', ondelete='SET NULL'), nullable=True, index=True)
    settlement_id = db.Column(db.Integer, nullable=True, index=True)
    relate_number = db.Column(db.String(30), nullable=False, unique=True)   # 送給加值中心的唯一編號
    status = db.Column(db.String(20), nullable=False, default=InvoiceStatus.PENDING, index=True)
    amount = db.Column(db.Integer, nullable=False, default=0)
    # 載具：print 紙本 / mobile 手機條碼 / citizen 自然人憑證 / member 綠界會員載具 / donate 捐贈 / company 打統編
    carrier_type = db.Column(db.String(20), nullable=False, default='print')
    carrier_num = db.Column(db.String(64), nullable=False, default='')
    love_code = db.Column(db.String(10), nullable=False, default='')
    buyer_identifier = db.Column(db.String(10), nullable=False, default='')
    buyer_name = db.Column(db.String(60), nullable=False, default='')
    buyer_email = db.Column(db.String(100), nullable=False, default='')
    invoice_no = db.Column(db.String(12), nullable=False, default='', index=True)
    invoice_date = db.Column(db.DateTime, nullable=True)
    random_number = db.Column(db.String(4), nullable=False, default='')
    allowance_amount = db.Column(db.Integer, nullable=False, default=0)
    allowance_numbers = db.Column(db.String(300), nullable=False, default='')
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(300), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    order = db.relationship('Order', backref=db.backref('invoices', lazy=True))

    @property
    def carrier_label(self):
        return {
            'print': '紙本證明聯', 'mobile': '手機條碼', 'citizen': '自然人憑證', 'member': '會員載具',
            'donate': f'捐贈 ({self.love_code})', 'company': f'統編 {self.buyer_identifier}',
        }.get(self.carrier_type, self.carrier_type)
