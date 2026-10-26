"""
進階功能的同步：促銷 / 會員等級 / 分類翻譯由線上端下發；儲值金異動在兩家分店離線使用後合併；
收款紀錄、退款、發票上傳線上端彙整；本機設定 (StoreSetting) 不同步。
"""
import pytest

from tests.support import line
from tests.test_sync_hub_node import Env, _member, _register_member, tenant_ctx


@pytest.fixture(scope='module')
def env():
    e = Env()
    yield e
    e.close()


def _put_setting(app, key, **values):
    from app.extensions import db
    from app.services import settings_service
    with tenant_ctx(app) as tenant:
        settings_service.put(key, values, tenant_id=tenant.id)
        db.session.commit()


def _add_item(app, name, price):
    from app.extensions import db
    from app.models.menu import MenuItem
    with tenant_ctx(app):
        item = MenuItem(name=name, category='同步進階', price=price, stock=50, total_stock=50)
        db.session.add(item)
        db.session.commit()
        return item.uuid


def _local_id(app, model, **filters):
    with app.app_context():
        return model.query.filter_by(**filters).first().id


def _wallet(app, phone):
    from app.models.member import WalletLedger
    from app.models.user import User
    with app.app_context():
        u = User.query.filter_by(phone=phone).first()
        return u.wallet_balance, sum(w.delta for w in WalletLedger.query.filter_by(user_id=u.id).all())


def test_hub_config_tables_reach_branches(env):
    from app.extensions import db
    from app.models.member import MemberTier
    from app.models.menu import CategoryTranslation, MenuItem
    from app.models.promotion import Promotion
    _register_member(env.a, '儲值同步會員', '0922000001', points=0)
    env.sync(env.a)
    env.sync(env.b)
    item_uuid = _add_item(env.hub, '同步促銷飲料', 60)
    with tenant_ctx(env.hub) as tenant:
        db.session.add(MemberTier(tenant_id=tenant.id, name='同步金卡', min_spend=1000, discount_percent=5))
        db.session.add(Promotion(tenant_id=tenant.id, name='同步買一送一', kind='bogo', target_type='items',
                                 target_value=item_uuid, buy_qty=1, get_qty=1))
        db.session.add(CategoryTranslation(tenant_id=tenant.id, category='同步進階', name_en='Sync Drinks'))
        db.session.commit()
    env.sync_all()
    for node in (env.a, env.b):
        with node.app_context():
            assert MemberTier.query.filter_by(name='同步金卡').count() == 1
            promo = Promotion.query.filter_by(name='同步買一送一').one()
            assert promo.target_value == item_uuid
            assert CategoryTranslation.query.filter_by(category='同步進階').one().name_en == 'Sync Drinks'
            assert MenuItem.query.filter_by(uuid=item_uuid).count() == 1


def test_promotion_applies_at_branch(env):
    from app.models.menu import MenuItem
    from app.services.order_service import process_checkout
    with env.a.app_context():
        iid = MenuItem.query.filter_by(name='同步促銷飲料').one().id
        MenuItem.query.filter_by(id=iid).update({'stock': 20, 'total_stock': 20})
        from app.extensions import db
        db.session.commit()
    with tenant_ctx(env.a) as tenant:
        order, _ = process_checkout(tenant_id=tenant.id, items=[line(iid, 2)], payment_method='Cash', order_type='內用')
        assert order.total_price == 60 and order.promotion_names == '同步買一送一'


def test_wallet_used_offline_at_two_branches_merges(env):
    from app.models.user import User
    from app.services import wallet_service
    from app.services.order_service import process_checkout
    for node in (env.a, env.b):
        _put_setting(node, 'member', wallet_enabled=True)
    with tenant_ctx(env.a) as tenant:
        user = User.query.filter_by(phone='0922000001').one()
        wallet_service.topup(tenant.id, user, 1000, 'Cash', 'pos_a')
        from app.extensions import db
        db.session.commit()
    env.sync(env.a)
    env.sync(env.b)
    assert _wallet(env.b, '0922000001') == (1000, 1000)
    # 兩家分店離線時各自用儲值金付款
    drink_uuid = _add_item(env.hub, '同步儲值餐', 300)
    env.sync_all()
    from app.extensions import db
    from app.models.menu import MenuItem
    for node in (env.a, env.b):
        with node.app_context():
            MenuItem.query.filter_by(uuid=drink_uuid).update({'stock': 10, 'total_stock': 10})
            db.session.commit()
        with tenant_ctx(node) as tenant:
            iid = MenuItem.query.filter_by(uuid=drink_uuid).one().id
            uid = User.query.filter_by(phone='0922000001').one().id
            order, _ = process_checkout(tenant_id=tenant.id, items=[line(iid)], payment_method='儲值金', user_id=uid,
                                        order_type='內用')
            assert order.wallet_used == 300
    assert _wallet(env.a, '0922000001') == (700, 700) and _wallet(env.b, '0922000001') == (700, 700)
    env.sync_all()
    for app in (env.hub, env.a, env.b):
        assert _wallet(app, '0922000001') == (400, 400)


def test_payments_refunds_go_up_and_settings_stay_local(env):
    from app.extensions import db
    from app.models.order import Order
    from app.models.payment import PaymentRecord, Refund
    from app.models.settings import StoreSetting
    from app.services import payment_service
    with tenant_ctx(env.a) as tenant:
        order = Order.query.filter_by(payment_method='儲值金').order_by(Order.id.desc()).first()
        payment_service.partial_refund(order.id, 50, '同步退款測試', tenant_id=tenant.id)
        db.session.commit()
        order_uuid = order.uuid
    env.sync(env.a)
    with env.hub.app_context():
        hub_order = Order.query.filter_by(uuid=order_uuid).one()
        kinds = sorted((p.kind, p.amount) for p in PaymentRecord.query.filter_by(order_id=hub_order.id).all())
        assert kinds == [('refund', -50), ('sale', 300)]
        assert Refund.query.filter_by(order_id=hub_order.id).one().amount == 50
        assert hub_order.refunded_amount == 50
        assert StoreSetting.query.filter_by(key='member').count() == 0, '分店的系統設定不上傳'
        topups = PaymentRecord.query.filter_by(kind='topup').all()
        assert [t.amount for t in topups] == [1000]
    assert _wallet(env.a, '0922000001') == (450, 450)
    env.sync_all()
    for app in (env.hub, env.b):
        assert _wallet(app, '0922000001') == (450, 450)
    with env.b.app_context():
        assert PaymentRecord.query.filter_by(kind='topup').count() == 0, '收款紀錄只上傳，不下發到其他分店'


def test_invariants_after_pro_sync(env):
    import sqlalchemy as sa

    from app.extensions import db
    from app.models.member import WalletLedger
    from app.models.user import User
    from app.sync.registry import SPECS
    for node_app in (env.hub, env.a, env.b):
        with node_app.app_context():
            for spec in SPECS:
                t = spec.model.__table__
                assert db.session.execute(sa.select(sa.func.count()).select_from(t).where(t.c.sync_rev < 0)).scalar() == 0
            total = sa.select(sa.func.coalesce(sa.func.sum(WalletLedger.delta), 0)).where(
                WalletLedger.user_id == User.id).scalar_subquery()
            assert not db.session.execute(sa.select(User.id).where(User.wallet_balance != total)).all()


def test_issued_coupons_reach_branches_with_expiry(env):
    """線上端批量發放的會員券 (含到期時間、批次) 同步到分店；分店使用後回報，撤回未使用的券也同步刪除"""
    from datetime import date, timedelta

    from app.extensions import db
    from app.models.coupon import Coupon, UserCoupon
    from app.models.user import User
    from app.services import coupon_service
    with tenant_ctx(env.hub) as tenant:
        c = Coupon(tenant_id=tenant.id, code='SYNCGIFT', title='同步發放券', discount_value=40, member_only=True,
                   is_reward=False, valid_days=5)
        db.session.add(c)
        db.session.flush()
        users = User.query.filter_by(phone='0922000001').all()
        result = coupon_service.issue(tenant.id, c, users, quantity=2)
        db.session.commit()
        batch = result['batch_id']
    env.sync_all()
    for node in (env.a, env.b):
        with node.app_context():
            rows = UserCoupon.query.filter_by(code='SYNCGIFT').all()
            assert len(rows) == 2 and {r.batch_id for r in rows} == {batch}
            assert all(r.expires_at.date() == date.today() + timedelta(days=5) and r.source == 'batch' for r in rows)
    # 分店 A 使用一張
    with tenant_ctx(env.a) as tenant:
        from app.models.menu import MenuItem
        from app.services.order_service import process_checkout
        item = MenuItem.query.filter_by(name='同步儲值餐').one()
        uid = User.query.filter_by(phone='0922000001').one().id
        order, _ = process_checkout(tenant_id=tenant.id, items=[line(item.id)], payment_method='Cash', order_type='內用',
                                    user_id=uid, promo_code='SYNCGIFT')
        assert order.discount_amount == 40
    env.sync_all()
    with tenant_ctx(env.hub) as tenant:
        assert UserCoupon.query.filter_by(code='SYNCGIFT', is_used=True).count() == 1
        assert coupon_service.revoke_batch(tenant.id, batch) == 1
        db.session.commit()
    env.sync_all()
    for node in (env.a, env.b):
        with node.app_context():
            assert [r.is_used for r in UserCoupon.query.filter_by(code='SYNCGIFT').all()] == [True]
