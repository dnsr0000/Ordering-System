"""
跨檔請求 (order-pricing-checkout-1，order_checkout.py) 的回歸測試：
預約單以取餐時間判斷供應 (B-017 / B-443)、團購不套用本店促銷 (R2-005)、會員等級折扣先取整數 (R2-024)、
預約單依取餐營業日取號 (B-449 / B-190)、CustomerLog.identifier 不超過 100 字 (B-118)。
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.enums import OrderSource
from app.models.menu import MenuItem
from app.models.order import DailyCounter
from app.models.promotion import Promotion
from app.models.member import MemberTier
from app.models.user import User
from app.services.order_checkout import process_checkout
from app.services.order_stock import business_day
from tests.support import get_or_create_tenant, make_member, tenant_request


def _item(tid, name, price=100, **kw):
    m = MenuItem(tenant_id=tid, name=name, price=price, stock=50, total_stock=50, category='xreq', **kw)
    db.session.add(m)
    db.session.commit()
    return m


def test_group_order_uses_pickup_time_no_promo_and_pickup_day_counter(app):
    tid = get_or_create_tenant(app, 'xreq-opc1-group')
    pickup = (datetime.now() + timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
    with tenant_request(app, tid):
        # 只在取餐那天開始販售：送單當下不供應，但取餐時供應
        item = _item(tid, 'XREQ 取餐日便當', price=120, sale_from=pickup.date())
        db.session.add(Promotion(tenant_id=tid, name='XREQ 全店折50', kind='threshold_discount', amount=50, threshold=0))
        db.session.commit()
        order, replayed = process_checkout(
            tenant_id=tid, items=[{'menu_item_id': item.id, 'quantity': 1}], payment_method='Cash', order_type='外帶',
            table_input='團購 主揪', client_order_id='xreq-opc1-group-1', external_ref='group:xreqtoken',
            cashier_username='主揪', pickup_at=pickup, skip_intake=True)
        assert not replayed
        assert order.total_price == 120 and order.promotion_discount == 0 and order.promotion_names == ''
        assert order.pickup_at == pickup
        # 取號用取餐營業日的計數器 (不是今天)
        row = DailyCounter.query.filter_by(tenant_id=tid, business_date=business_day(pickup), name='pickup').first()
        assert row is not None and row.value == order.pickup_number
        assert DailyCounter.query.filter_by(tenant_id=tid, business_date=business_day(), name='pickup').first() is None


def test_member_tier_discount_rounded_before_total(app):
    tid = get_or_create_tenant(app, 'xreq-opc1-tier')
    uid = make_member(app, 'XREQ 五趴會員', '0968024024', tenant_id=tid)
    with tenant_request(app, tid):
        tier = MemberTier(tenant_id=tid, name='XREQ 5%', min_spend=999999, discount_percent=5)
        db.session.add(tier)
        db.session.flush()
        user = db.session.get(User, uid)
        user.tier_id = tier.id
        item = _item(tid, 'XREQ 130 餐', price=130)
        order, _ = process_checkout(tenant_id=tid, items=[{'menu_item_id': item.id, 'quantity': 1}], payment_method='Cash',
                                    order_source=OrderSource.POS, user_id=uid, cashier_username='xreq',
                                    client_order_id='xreq-opc1-tier-1')
        # 6.5 → 7，應付 123；各項折扣加總 = 折扣總額
        assert order.member_discount == 7 and order.total_price == 123 and order.discount_amount == 7
        # 識別字串截到 100 字 (B-118)
        user = db.session.get(User, uid)
        user.name = '長' * 100
        db.session.commit()
        order2, _ = process_checkout(tenant_id=tid, items=[{'menu_item_id': item.id, 'quantity': 1}], payment_method='Cash',
                                     order_source=OrderSource.POS, user_id=uid, cashier_username='xreq',
                                     client_order_id='xreq-opc1-tier-2')
        assert len(order2.customer_log.identifier) <= 100
        User.query.filter_by(id=uid).update({'tier_id': None})
        db.session.commit()


def test_promotion_details_saved_per_promotion(app):
    import json
    tid = get_or_create_tenant(app, 'xreq-opc1-promo')
    with tenant_request(app, tid):
        item = _item(tid, 'XREQ 促銷餐', price=200)
        gift = _item(tid, 'XREQ 贈品', price=30)
        db.session.add_all([
            Promotion(tenant_id=tid, name='XREQ 折30', kind='threshold_discount', amount=30, threshold=0),
            Promotion(tenant_id=tid, name='XREQ 滿額贈', kind='threshold_gift', threshold=100, gift_item_uuid=gift.uuid),
        ])
        db.session.commit()
        order, _ = process_checkout(tenant_id=tid, items=[{'menu_item_id': item.id, 'quantity': 1}], payment_method='Cash',
                                    order_source=OrderSource.POS, cashier_username='xreq', client_order_id='xreq-opc1-promo-1')
        details = {d['name']: d['amount'] for d in json.loads(order.promotion_details)}
        assert details.get('XREQ 折30') == 30
        assert details.get('XREQ 滿額贈', 0) == 0 and set(details) <= {'XREQ 折30', 'XREQ 滿額贈'}


def test_online_home_delivery_uses_pickup_node_reported_config():
    """R2-002：線上端外送到府依取餐分店回報的本店外送設定 (是否開放、配送區域、免運門檻) 驗證與計費"""
    import json

    import pytest
    from app.errors import AppError
    from app.models.order import Order
    from app.models.sync import SyncNode
    from app.services import settings_service
    from tests.support import line
    from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, _member, _register_member, tenant_ctx
    addr = {'address': '台南市東區大學路 1 號 3 樓', 'phone': '0912-345-678', 'zone': '近'}
    env = Env()
    try:
        burger = _add_item(env.b, 'XREQ 外送堡', price=220, stock=20)
        _register_member(env.b, 'XREQ 外送會員', '0966750019')
        env.sync_all()
        with tenant_ctx(env.hub) as tenant:
            settings_service.put('own_delivery', {'enabled': True, 'zones': '近:30:200', 'channels': ['online']},
                                 tenant_id=tenant.id)
            db.session.commit()
        env.hub.test_client().get('/?shop=default')
        uid = _member(env.hub, '0966750019')['id']
        item_id = _item(env.hub, burger)['id']

        def report(summary):
            with env.hub.app_context():
                node = SyncNode.query.filter_by(uuid=env.node_b_uuid).one()
                node.own_delivery_json = json.dumps(summary, ensure_ascii=False)
                db.session.commit()

        report({'enabled': False, 'channels': ['online'], 'zones': '近:30:200'})   # 分店自訂關閉外送
        with pytest.raises(AppError):
            _checkout(env.hub, [line(item_id)], node_uuid=env.node_b_uuid, user_id=uid,
                      order_type='外送到府', delivery=addr)
        # 分店自訂配送區域與免運門檻：外送費依分店的設定計算 (220 >= 200 免運)
        report({'enabled': True, 'channels': ['online'], 'zones': '近:50:100', 'free_over': 200})
        order = _checkout(env.hub, [line(item_id)], node_uuid=env.node_b_uuid, user_id=uid,
                          order_type='外送到府', delivery=addr)
        with env.hub.app_context():
            o = Order.query.filter_by(uuid=order['uuid']).one()
            assert o.delivery_fee == 0 and o.total_price == 220
    finally:
        env.close()
