"""
修正小組 models-b 的回歸測試：
- B-497 混合付款含禮物卡時，payment_reference 的完整卡號在收據 / POS 一律遮罩
- B-498 桌邊未結帳訂單作廢品項後，服務費與折扣同步調降，subtotal 仍等於剩下品項小計
- R2-194 線上訂單尚未由分店編號時 pickup_pending 為 True
- R2-195 桌位合併結帳的發票 (只掛 settlement_id) 也能由 invoice_summary 找到
- B-499 / B-500 admin_log / 稽核 / 收銀員帳號欄位與 staff_account.username 同長
- B-501 / B-502 桌位結帳 order_ids 不限長度；刪除桌位不連帶刪除桌邊線上結帳紀錄
"""
from datetime import datetime

from app.extensions import db
from app.models.enums import OrderSource, PaymentMethod
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus, Settlement
from app.models.table import DiningTable, TablePayment
from tests.support import default_tenant_id

GIFT_CODE = '1234567890123456'


def _order(tid, **kw):
    kw.setdefault('total_price', 100)
    return Order(tenant_id=tid, created_at=datetime.now(), **kw)


# ---------------------------------------------------------------------------
# B-497
# ---------------------------------------------------------------------------
def test_split_payment_gift_code_is_masked(app):
    tid = default_tenant_id(app)
    with app.app_context():
        split = _order(tid, payment_method=PaymentMethod.SPLIT, payment_reference=GIFT_CODE)
        assert split.masked_payment_reference == '1234****3456'
        assert GIFT_CODE not in str(split.to_receipt())
        # 混合付款沒有禮物卡 (例如刷卡授權碼) 時不變
        assert _order(tid, payment_method=PaymentMethod.SPLIT, payment_reference='A12345').masked_payment_reference == 'A12345'
        # 單一禮物卡付款維持遮罩、其他付款方式原樣
        assert _order(tid, payment_method=PaymentMethod.GIFT_CARD, payment_reference=GIFT_CODE).masked_payment_reference \
            == '1234****3456'
        assert _order(tid, payment_method=PaymentMethod.LINE_PAY, payment_reference='2024010112345678').masked_payment_reference \
            == '2024010112345678'


# ---------------------------------------------------------------------------
# B-498
# ---------------------------------------------------------------------------
def test_reduce_unpaid_total_keeps_subtotal_with_service_charge(app):
    tid = default_tenant_id(app)
    with app.app_context():
        o = _order(tid, total_price=220, service_charge=20, discount_amount=0, delivery_fee=0)
        assert o.subtotal == 200
        o.reduce_unpaid_total(110, 100, 200)   # 2 x $100 作廢 1 份
        assert (o.total_price, o.service_charge, o.discount_amount, o.subtotal) == (110, 10, 0, 100)


def test_reduce_unpaid_total_keeps_subtotal_with_discounts(app):
    tid = default_tenant_id(app)
    with app.app_context():
        o = _order(tid, total_price=150, discount_amount=50, promotion_discount=30, member_discount=0, manual_discount=0,
                   service_charge=0, delivery_fee=0)
        o.reduce_unpaid_total(75, 100, 200)
        assert (o.total_price, o.discount_amount, o.subtotal) == (75, 25, 100)
        assert o.promotion_discount == 15
        assert o.promotion_discount + o.member_discount + o.manual_discount <= o.discount_amount


def test_reduce_unpaid_total_rounding_stays_consistent(app):
    tid = default_tenant_id(app)
    with app.app_context():
        # 3 x $100、折 $50 → 應付 250；作廢 1 份 → 退 round(100 x 250 / 300) = 83
        o = _order(tid, total_price=250, discount_amount=50, manual_discount=50, service_charge=0, delivery_fee=0)
        o.reduce_unpaid_total(83, 100, 300)
        assert o.total_price == 167 and o.subtotal == 200
        assert o.manual_discount <= o.discount_amount
        # 3 x $33、服務費 10 → 應付 109；作廢 1 份 → 退 36
        o = _order(tid, total_price=109, discount_amount=0, service_charge=10, delivery_fee=0)
        o.reduce_unpaid_total(36, 33, 99)
        assert o.total_price == 73 and o.subtotal == 66 and o.discount_amount == 0


# ---------------------------------------------------------------------------
# R2-194
# ---------------------------------------------------------------------------
def test_pickup_pending_property(app):
    tid = default_tenant_id(app)
    with app.app_context():
        assert _order(tid, order_source=OrderSource.ONLINE, pickup_number=0).pickup_pending is True
        assert _order(tid, order_source=OrderSource.ONLINE, pickup_number=12).pickup_pending is False
        assert _order(tid, order_source=OrderSource.KIOSK, pickup_number=0).pickup_pending is False


# ---------------------------------------------------------------------------
# R2-195
# ---------------------------------------------------------------------------
def test_invoice_summary_finds_settlement_invoice(app):
    tid = default_tenant_id(app)
    with app.app_context():
        st = Settlement(tenant_id=tid, table_name='FX1', total=300, order_ids='')
        db.session.add(st)
        db.session.flush()
        a = _order(tid, total_price=100, payment_status='settled', settlement_id=st.id)
        b = _order(tid, total_price=200, payment_status='settled', settlement_id=st.id)
        lone = _order(tid, total_price=50)
        db.session.add_all([a, b, lone])
        db.session.flush()
        st.order_ids = f'{a.id},{b.id}'
        db.session.add(Invoice(tenant_id=tid, settlement_id=st.id, relate_number=f'FXMB-{st.id}-old',
                               status=InvoiceStatus.VOID, amount=300, invoice_no='ZZ00000001', random_number='1111'))
        db.session.flush()
        inv = Invoice(tenant_id=tid, settlement_id=st.id, relate_number=f'FXMB-{st.id}', status=InvoiceStatus.ISSUED,
                      amount=300, invoice_no='ZZ12345678', random_number='9876')
        db.session.add(inv)
        db.session.commit()
        a_id, b_id, lone_id, inv_id, st_id = a.id, b.id, lone.id, inv.id, st.id
    with app.app_context():
        for oid in (a_id, b_id):
            summary = db.session.get(Order, oid).invoice_summary()
            assert summary['invoice_no'] == 'ZZ12345678' and summary['random_number'] == '9876'
            assert summary['combined'] is True and summary['id'] == inv_id
        assert db.session.get(Order, lone_id).invoice_summary() is None
        # 列表可傳入預先查好的對照表 (不再逐張查詢)
        o = db.session.get(Order, a_id)
        assert o.invoice_summary({st_id: db.session.get(Invoice, inv_id)})['invoice_no'] == 'ZZ12345678'
        assert o.invoice_summary({}) is None
        # 訂單自己的發票優先
        own = Invoice(tenant_id=tid, order_id=a_id, relate_number=f'FXMB-own-{a_id}', status=InvoiceStatus.ISSUED,
                      amount=100, invoice_no='YY11112222', random_number='1234')
        db.session.add(own)
        db.session.commit()
        db.session.expire_all()
        summary = db.session.get(Order, a_id).invoice_summary()
        assert summary['invoice_no'] == 'YY11112222' and summary['combined'] is False


# ---------------------------------------------------------------------------
# B-499 / B-500 / B-501
# ---------------------------------------------------------------------------
def test_staff_username_columns_match_staff_account(app):
    from app.models.tenant import StaffAccount
    from app.models.user import AdminLog, StaffAuditLog
    limit = StaffAccount.__table__.c.username.type.length
    assert AdminLog.__table__.c.username.type.length >= limit
    assert StaffAuditLog.__table__.c.staff_username.type.length >= limit
    assert Order.__table__.c.cashier_username.type.length >= limit


def test_settlement_order_ids_unbounded(app):
    for col in (Settlement.__table__.c.order_ids, TablePayment.__table__.c.order_ids):
        assert getattr(col.type, 'length', None) is None


# ---------------------------------------------------------------------------
# B-502
# ---------------------------------------------------------------------------
def test_table_payment_fk_set_null_in_model():
    fk = next(iter(TablePayment.__table__.c.dining_table_id.foreign_keys))
    assert fk.ondelete == 'SET NULL' and TablePayment.__table__.c.dining_table_id.nullable


def test_deleting_table_keeps_table_payment(app):
    import pytest
    from sqlalchemy import inspect
    tid = default_tenant_id(app)
    with app.app_context():
        fks = [fk for fk in inspect(db.engine).get_foreign_keys('table_payment') if fk['referred_table'] == 'dining_table']
        if not fks or (fks[0].get('options') or {}).get('ondelete', '').upper() != 'SET NULL':
            pytest.skip('資料庫尚未套用 table_payment.dining_table_id 改為 ON DELETE SET NULL 的遷移')
        table = DiningTable(tenant_id=tid, name='FXMB 刪桌')
        db.session.add(table)
        db.session.flush()
        tp = TablePayment(tenant_id=tid, dining_table_id=table.id, amount=100, trade_no='FXMB0000000001', status='pending',
                          order_ids=','.join(str(100000 + i) for i in range(80)))
        db.session.add(tp)
        db.session.commit()
        tp_id = tp.id
        db.session.delete(table)
        db.session.commit()
    with app.app_context():
        kept = db.session.get(TablePayment, tp_id)
        assert kept is not None and kept.dining_table_id is None and kept.status == 'pending'
        assert len(kept.order_id_list) == 80
