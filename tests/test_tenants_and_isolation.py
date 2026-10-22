"""多店家：註冊新店家、店家入口網址、跨店資料隔離 (他店資料一律 404 且不被修改)"""
import threading
from urllib.parse import urlparse

from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.menu import ComboOption, MenuItem, ModifierOption
from app.models.order import Order
from app.models.tenant import Tenant
from app.models.user import RewardSetting, User
from app.services.reward_service import get_or_create_reward_setting
from tests.support import default_tenant_id


def test_tenant_register(app):
    """公開註冊新店家，並自動初始化預設加料與紅利設定"""
    c = app.test_client()
    resp = c.post('/tenant/register', data={'shop_name': '星光二號店', 'slug': 'branch-two',
                                            'username': 'owner_b2', 'password': 'Branch2Pass!2026'})
    assert resp.status_code == 302
    with app.app_context():
        t2 = Tenant.query.filter_by(slug='branch-two').first()
        assert t2 is not None
        assert ModifierOption.query.filter_by(tenant_id=t2.id).count() >= 2
        assert RewardSetting.query.filter_by(tenant_id=t2.id).first() is not None


def test_shop_entry(app):
    with app.app_context():
        on, off = Tenant(name='啟用店13', slug='on-shop-13'), Tenant(name='停用店13', slug='off-shop-13', is_active=False)
        db.session.add_all([on, off])
        db.session.commit()
        on_id = on.id
    c = app.test_client()
    r = c.get('/shop/on-shop-13')
    assert r.status_code == 302 and urlparse(r.headers['Location']).path == '/'
    with c.session_transaction() as sess:
        assert sess['tenant_id'] == on_id
    assert app.test_client().get('/shop/no-such-shop-13').status_code == 404
    assert app.test_client().get('/shop/off-shop-13').status_code == 404


def test_toggle_user_coupon_and_tenant_isolation(app, admin_client, photo_member):
    """切換會員持券的使用狀態：他店的券回 404 且不變，本店的券可來回切換"""
    with app.app_context():
        own = Coupon(tenant_id=default_tenant_id(app), code='TOGGLE-OWN', title='本店切換券', discount_type='fixed',
                     discount_value=10, min_spend=0, reward_points=1, is_reward=True)
        db.session.add(own)
        db.session.flush()
        db.session.add(UserCoupon(tenant_id=own.tenant_id, user_id=photo_member, coupon_id=own.id, code='TOGGLE-OWN-A',
                                  is_used=False))
        tb = Tenant(name='跨店B13', slug='tenant-b-13')
        db.session.add(tb)
        db.session.flush()
        ub = User(tenant_id=tb.id, name='B店會員', phone='0933130099', photo_path='b13.jpg', points=5)
        cb = Coupon(tenant_id=tb.id, code='X13B', title='B店券', discount_type='fixed', discount_value=5, min_spend=0,
                    reward_points=1, is_reward=True)
        db.session.add_all([ub, cb])
        db.session.flush()
        ucb = UserCoupon(tenant_id=tb.id, user_id=ub.id, coupon_id=cb.id, code='X13B-1', is_used=False)
        db.session.add(ucb)
        db.session.commit()
        ucb_id = ucb.id
        own_id = UserCoupon.query.filter_by(code='TOGGLE-OWN-A').first().id
    assert admin_client.post(f'/admin/toggle_user_coupon/{ucb_id}').status_code == 404
    with app.app_context():
        assert db.session.get(UserCoupon, ucb_id).is_used is False
    j1 = admin_client.post(f'/admin/toggle_user_coupon/{own_id}').get_json()
    assert any(c['code'] == 'TOGGLE-OWN-A' and c['is_used'] for c in j1['coupons'])
    j2 = admin_client.post(f'/admin/toggle_user_coupon/{own_id}').get_json()
    assert any(c['code'] == 'TOGGLE-OWN-A' and not c['is_used'] for c in j2['coupons'])


def test_cross_tenant_access_is_404(app, admin_client):
    """預設店家的負責人操作 B 店資料：全部 404 且 B 店資料不變"""
    with app.app_context():
        tb = Tenant(name='跨店B13X', slug='tenant-b-13x')
        db.session.add(tb)
        db.session.flush()
        item = MenuItem(tenant_id=tb.id, name='B店紅利品', category='主餐', price=50, stock=9, total_stock=9,
                        is_reward=True, reward_points=10)
        cp = Coupon(tenant_id=tb.id, code='X13BSWEEP', title='B店掃描券', discount_type='fixed', discount_value=5, min_spend=0,
                    reward_points=0, is_reward=False)
        usr = User(tenant_id=tb.id, name='B店掃描會員', phone='0933130198', photo_path='b13x.jpg', points=50)
        db.session.add_all([item, cp, usr])
        db.session.flush()
        combo = ComboOption(tenant_id=tb.id, main_item_id=item.id, name='B店掃描套餐', additional_price=5)
        orders = [Order(tenant_id=tb.id, table_number='B店', total_price=10, status=s, pickup_number=n)
                  for n, s in enumerate(['Pending', 'Pending', 'Completed'], 1)]
        db.session.add_all([combo, *orders])
        db.session.commit()
        ids = dict(item=item.id, cb=combo.id, cp=cp.id, usr=usr.id, orders=[o.id for o in orders])
    probes = [
        (f"/admin/edit/{ids['item']}", {'data': {'name': '被入侵', 'price': '1'}}),
        (f"/admin/edit_combo/{ids['cb']}", {'data': {'main_item_id': ids['item'], 'name': '被入侵'}}),
        (f"/admin/edit_coupon/{ids['cp']}", {'data': {'code': 'HACKED13', 'title': '被入侵'}}),
        (f"/admin/edit_user/{ids['usr']}", {'data': {'name': '被入侵', 'phone': '0933130197', 'points': '9999'}}),
        (f"/admin/delete/{ids['item']}", {}), (f"/admin/delete_combo/{ids['cb']}", {}),
        (f"/admin/remove_reward/{ids['item']}", {}), (f"/admin/delete_coupon/{ids['cp']}", {}),
        *[(f'/api/orders/{oid}/status', {'json': {'status': s}}) for oid, s in zip(ids['orders'], ['Completed', 'Completed', 'PickedUp'])],
    ]
    offenders = [f'{path}={r.status_code}' for path, kw in probes for r in [admin_client.post(path, **kw)] if r.status_code != 404]
    assert not offenders, offenders
    with app.app_context():
        it = db.session.get(MenuItem, ids['item'])
        assert (it.name, it.price, it.is_reward) == ('B店紅利品', 50, True)
        assert db.session.get(ComboOption, ids['cb']).name == 'B店掃描套餐'
        assert db.session.get(Coupon, ids['cp']).code == 'X13BSWEEP'
        assert (db.session.get(User, ids['usr']).name, db.session.get(User, ids['usr']).points) == ('B店掃描會員', 50)
        assert [db.session.get(Order, i).status for i in ids['orders']] == ['Pending', 'Pending', 'Completed']


def test_reward_setting_unique_per_tenant(app):
    with app.app_context():
        t = Tenant(name='M15設定店', slug='m15-rs')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    errors = []

    def create():
        try:
            with app.app_context():
                get_or_create_reward_setting(tid)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
    threads = [threading.Thread(target=create) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    with app.app_context():
        assert RewardSetting.query.filter_by(tenant_id=tid).count() == 1
