"""跨檔請求 (order_lifecycle.py) 的回歸測試：R2-081 已出餐未結帳取消需核准、R2-016 / B-037 線上端不可替分店出餐 / 作廢已收款訂單、
B-073 一鍵出餐不含稍晚的預約單、R2-196 升級前舊訂單取消追回贈點、B-498 / B-012 桌邊未結帳作廢品項的折扣與贈點倍數"""
from datetime import datetime, timedelta

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderSource, OrderStatus
from app.models.member import MemberTier
from app.models.order import Order, OrderItem
from app.models.sync import PointLedger
from app.models.tenant import StaffAccount
from app.models.user import User
from app.services import order_lifecycle
from tests.support import default_tenant_id, get_or_create_tenant, login_staff, make_item, make_member, tenant_request


def _order(app, tenant_id=None, **kw):
    items = kw.pop('items', [])
    with app.app_context():
        o = Order(tenant_id=tenant_id or default_tenant_id(app), table_number=kw.pop('table_number', 'OLKR'),
                  pickup_number=kw.pop('pickup_number', 1), **kw)
        o.items = [OrderItem(tenant_id=o.tenant_id, menu_item_id=mid, item_name=name, price=price, quantity=qty)
                   for mid, name, price, qty in items]
        db.session.add(o)
        db.session.commit()
        return o.id, [i.id for i in o.items]


def _staff(username):
    return StaffAccount.query.filter_by(username=username).first()


# ---------------------------------------------------------------------------
# R2-081：已出餐但未結帳的訂單取消需 refund 權限或主管核准；製作中的未結帳訂單仍可直接取消
# ---------------------------------------------------------------------------
def test_cancel_served_unpaid_order_needs_approval(app):
    tid = default_tenant_id(app)
    login_staff(app, 'olkr_staff', 'staff')
    served, _ = _order(app, total_price=300, status='Completed', payment_status='unpaid')
    cooking, _ = _order(app, total_price=300, status='Pending', payment_status='unpaid')
    with tenant_request(app, tid):
        staff = _staff('olkr_staff')
        with pytest.raises(AppError) as exc:
            order_lifecycle.change_order_status(served, 'Cancelled', staff, tid)
        assert exc.value.status == 403 and exc.value.extra.get('need_approval')
        db.session.rollback()
        asked = []
        order_lifecycle.change_order_status(served, 'Cancelled', staff, tid,
                                            approve=lambda perm, label: asked.append((perm, label)) or 'boss')
        assert asked == [('refund', '作廢訂單')]
        assert db.session.get(Order, served).status == OrderStatus.CANCELLED
        order_lifecycle.change_order_status(cooking, 'Cancelled', staff, tid)
        assert db.session.get(Order, cooking).status == OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# R2-016 / B-037：線上端不可替分店的訂單出餐 / 一鍵出餐，也不可作廢分店已收款的訂單
# ---------------------------------------------------------------------------
def test_hub_rejects_serving_and_cancelling_paid_branch_orders(app, monkeypatch):
    tid = default_tenant_id(app)
    branch_paid, _ = _order(app, total_price=200, status='Pending', payment_status='',
                            order_source=OrderSource.POS, node_uuid='olkr-node-uuid')
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    with tenant_request(app, tid):
        owner = StaffAccount(tenant_id=tid, username='olkr_hub_owner', role='owner', password_hash='-')
        with pytest.raises(AppError, match='分店'):
            order_lifecycle.change_order_status(branch_paid, 'Completed', owner, tid)
        db.session.rollback()
        with pytest.raises(AppError, match='出餐分店作廢退款'):
            order_lifecycle.change_order_status(branch_paid, 'Cancelled', owner, tid)
        db.session.rollback()
        assert order_lifecycle.complete_all_pending_orders(tid, [branch_paid]) == 0
        assert db.session.get(Order, branch_paid).status == OrderStatus.PENDING


# ---------------------------------------------------------------------------
# B-073：一鍵出餐不完成今天稍晚 (取餐前 15 分鐘以外) 的預約單
# ---------------------------------------------------------------------------
def test_complete_all_skips_later_scheduled_orders(app):
    tid = default_tenant_id(app)
    later, _ = _order(app, total_price=100, status='Pending', payment_status='',
                      pickup_at=datetime.now() + timedelta(hours=2))
    soon, _ = _order(app, total_price=100, status='Pending', payment_status='',
                     pickup_at=datetime.now() + timedelta(minutes=10))
    with tenant_request(app, tid):
        assert order_lifecycle.complete_all_pending_orders(tid, [later, soon]) == 1
        assert db.session.get(Order, later).status == OrderStatus.PENDING
        assert db.session.get(Order, soon).status == OrderStatus.COMPLETED


# ---------------------------------------------------------------------------
# R2-196：升級前 (期初點數異動之前) 的會員訂單取消時，以訂單上的贈點追回；升級後的訂單不受影響
# ---------------------------------------------------------------------------
def test_cancel_pre_ledger_order_claws_back_points_earned(app):
    tid = get_or_create_tenant(app, 'olkr-r2196')
    uid = make_member(app, '舊會員', '0955196001', tenant_id=tid)
    with app.app_context():
        now = datetime.now()
        db.session.add(PointLedger(tenant_id=tid, user_id=uid, delta=50, reason='opening', created_at=now))
        db.session.execute(db.update(User).where(User.id == uid).values(points=50))
        db.session.commit()
    old, _ = _order(app, tenant_id=tid, user_id=uid, total_price=500, points_earned=10, status='Completed',
                    payment_status='', created_at=now - timedelta(days=30))
    new, _ = _order(app, tenant_id=tid, user_id=uid, total_price=500, points_earned=7, status='Completed',
                    payment_status='', created_at=now + timedelta(seconds=1))
    with tenant_request(app, tid):
        owner = StaffAccount(tenant_id=tid, username='olkr_owner', role='owner', password_hash='-')
        order_lifecycle.change_order_status(old, 'Cancelled', owner, tid)
        assert db.session.get(User, uid).points == 40
        order_lifecycle.change_order_status(new, 'Cancelled', owner, tid)
        assert db.session.get(User, uid).points == 40, '升級後的訂單沒有 earn 異動：不以訂單上的贈點追回'


# ---------------------------------------------------------------------------
# B-498 / B-012：桌邊未結帳訂單作廢品項，折扣等比例調降 (小計 = 剩下品項)；贈點倍數 0 = 不贈點
# ---------------------------------------------------------------------------
def test_void_unpaid_item_scales_discount_and_respects_zero_multiplier(app):
    tid = default_tenant_id(app)
    a = make_item(app, 'OLKR 牛肉麵', price=2000)
    b = make_item(app, 'OLKR 滷味', price=1000)
    with app.app_context():
        tier = MemberTier(tenant_id=tid, name='OLKR 零倍', min_spend=999999, points_multiplier=0)
        db.session.add(tier)
        db.session.commit()
        tier_id = tier.id
    uid = make_member(app, 'OLKR 會員', '0955498001', tier_id=tier_id)
    # 小計 3000，折扣 600 (促銷)，應付 2400，訂單上記的贈點 24 點 (結帳後等級才改為 0 倍)
    oid, (ia, _) = _order(app, user_id=uid, total_price=2400, discount_amount=600, promotion_discount=600,
                          points_earned=24, status='Pending', payment_status='unpaid',
                          items=[(a, 'OLKR 牛肉麵', 2000, 1), (b, 'OLKR 滷味', 1000, 1)])
    with tenant_request(app, tid):
        result = order_lifecycle.void_item(tid, ia, 1, '客人不要了')
        db.session.commit()
        o = db.session.get(Order, oid)
        assert result['amount'] == 1600
        assert o.total_price == 800
        assert o.subtotal == 1000, '小計應等於剩下品項'
        assert o.discount_amount == 200 and o.promotion_discount == 200
        assert o.points_earned == 0, '倍數 0 的等級不贈點'
