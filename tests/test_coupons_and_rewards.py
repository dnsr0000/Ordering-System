"""優惠券與紅利：優惠券管理與使用限制、紅利商品與兌換憑證 (核銷 / 退點原子性)、紅利設定"""
import threading

from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.enums import VoucherStatus
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.user import (
    RewardRedemption,
    User,
)
from app.services.voucher_service import try_claim_voucher, try_refund_voucher
from tests.support import (
    PHOTO_MEMBER_PHONE,
    PHOTO_MEMBER_PIN,
    assert_flash,
    checkout,
    default_tenant_id,
    line,
    make_item,
    make_member,
    member_client,
    reward_setting_values,
)


def test_reward_listing(app, admin_client, ctx):
    """紅利商城上架 (含特惠點數) 與下架"""
    ids = ctx['menu_ids']
    admin_client.post('/admin/add_reward', data=dict(item_id=ids['黃金雞塊'], reward_points='50', reward_discount_points='0'))
    admin_client.post('/admin/add_reward', data=dict(item_id=ids['招牌和牛堡'], reward_points='100', reward_discount_points='80'))
    assert admin_client.post('/admin/add_reward', data=dict(item_id=ids['美式脆薯'], reward_points='45',
                                                            reward_discount_points='35')).status_code == 302
    with app.app_context():
        fries = db.session.get(MenuItem, ids['美式脆薯'])
        assert (fries.is_reward, fries.reward_points, fries.reward_discount_points) == (True, 45, 35)
    admin_client.post('/admin/add_reward', data=dict(item_id=ids['錫蘭紅茶'], reward_points='999'))
    assert_flash(admin_client, '不得高於原單價')
    assert admin_client.post(f"/admin/remove_reward/{ids['美式脆薯']}").status_code == 302
    with app.app_context():
        assert db.session.get(MenuItem, ids['美式脆薯']).is_reward is False


def test_coupon_crud(app, admin_client):
    """公開促銷碼、紅利兌換券 (85 折存為 85)、編輯與刪除"""
    admin_client.post('/admin/add_coupon', data=dict(title='全館滿百折20公開碼', code='OPEN20', discount_type='fixed',
                                                     discount_value='20', min_spend='100', reward_points='0'))
    admin_client.post('/admin/add_coupon', data=dict(title='VIP 85折神券', code='VIP85', discount_type='percent',
                                                     discount_value='85', min_spend='100', reward_points='30',
                                                     reward_discount_points='20', per_user_limit='1'))
    admin_client.post('/admin/add_coupon', data=dict(title='臨時測試券', code='TMP10', discount_type='fixed',
                                                     discount_value='10', min_spend='50', reward_points='10'))
    with app.app_context():
        assert Coupon.query.filter_by(code='OPEN20').first().is_reward is False
        vip = Coupon.query.filter_by(code='VIP85').first()
        assert vip.is_reward is True and vip.discount_value == 85 and vip.discount_label == '85 折'
        tmp_id = Coupon.query.filter_by(code='TMP10').first().id
    r = admin_client.post(f'/admin/edit_coupon/{tmp_id}', data=dict(title='已編輯測試券', code='TMP15', discount_type='fixed',
                                                                    discount_value='15', min_spend='80', reward_points='15',
                                                                    per_user_limit='2'))
    assert r.status_code == 302
    with app.app_context():
        tmp = db.session.get(Coupon, tmp_id)
        assert (tmp.code, tmp.discount_value, tmp.min_spend) == ('TMP15', 15, 80)
    assert admin_client.post(f'/admin/delete_coupon/{tmp_id}').status_code == 302
    with app.app_context():
        assert db.session.get(Coupon, tmp_id) is None


def test_redeem_coupon_with_per_user_limit(app, staff_client, member_client):
    """以點數兌換優惠券 (特惠 20 點)，超過每人兌換上限時擋下"""
    with app.app_context():
        chen_id = User.query.filter_by(phone='0912345678').first().id
        vip_id = Coupon.query.filter_by(code='VIP85').first().id
    # 櫃檯協助重設 PIN，同時解除 5.4 的鎖定
    assert staff_client.post('/api/pos/set_member_pin', json={'user_id': chen_id, 'pin': '1357'}).status_code == 200
    member_client.get('/?shop=default')
    r = member_client.post('/phone_login', json={'phone': '0912345678', 'pin': '1357'}, headers={'X-Forwarded-For': '10.40.1.1'})
    assert r.status_code == 200 and r.get_json()['success'] is True

    r1 = member_client.post('/api/redeem_reward', json={'coupon_id': vip_id})
    assert r1.status_code == 200 and r1.get_json()['remaining_points'] == 280
    r2 = member_client.post('/api/redeem_reward', json={'coupon_id': vip_id})
    assert r2.status_code == 400 and '兌換上限' in r2.get_json()['message']


def test_reward_voucher_refund_cannot_be_repeated(app, member_client, ctx):
    """沒有憑證不能退點；兌換後可退一次，重複退點擋下"""
    nugget = ctx['menu_ids']['黃金雞塊']
    assert member_client.post('/api/refund_reward_item', json={'item_id': nugget}).status_code == 400
    r = member_client.post('/api/redeem_reward', json={'item_id': nugget})
    assert r.status_code == 200 and r.get_json()['remaining_points'] == 230
    assert r.get_json()['redeemed_item']['is_reward'] is True
    r = member_client.post('/api/refund_reward_item', json={'item_id': nugget, 'quantity': 1})
    assert r.status_code == 200 and r.get_json()['current_points'] == 280
    assert member_client.post('/api/refund_reward_item', json={'item_id': nugget}).status_code == 400
    with app.app_context():
        assert User.query.filter_by(phone='0912345678').first().points == 280


def test_member_composite_checkout(app, admin_client, member_client, ctx):
    """
    小計 240 (和牛堡特價 120 x2) + 兌換品 (核銷既有憑證，不再扣點)
    → VIP85 折 36 → 點數折抵受上限 30 → 實付 174 → 每 50 元送 1 點 = 3 點 → 餘 230 - 30 + 3 = 203
    """
    assert admin_client.post('/admin/update_reward_setting', data=dict(is_enabled='1', points_per_dollar='1',
                                                                       max_discount_per_order='30', spend_per_point='50')).status_code == 302
    ids = ctx['menu_ids']
    r = member_client.post('/api/redeem_reward', json={'item_id': ids['黃金雞塊']})
    assert r.get_json()['remaining_points'] == 230
    r = checkout(member_client, [line(ids['招牌和牛堡'], 2), line(ids['黃金雞塊'], is_reward=True)],
                 payment_method='Credit Card', promo_code='VIP85', use_points=100, ip='10.40.1.2')
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert (d['total_price'], d['points_used'], d['points_earned'], d['remaining_points']) == (174, 30, 3, 203)
    assert d['payment_method'] == 'Credit Card'
    reward_line = next(i for i in d['items'] if i['is_reward'])
    assert reward_line['price'] == 0 and reward_line['name'].startswith('[點數兌換]')


def test_user_available_coupons(app, photo_member):
    tid = default_tenant_id(app)
    anon = app.test_client()
    anon.get('/?shop=default')
    assert anon.get('/api/user_available_coupons').get_json() == {'success': False, 'coupons': [], 'points': 0}
    with app.app_context():
        cp = Coupon(tenant_id=tid, code='X13OWN', title='模組13專屬券', discount_type='fixed', discount_value=30, min_spend=0,
                    reward_points=10, is_reward=True)
        db.session.add(cp)
        db.session.flush()
        db.session.add_all([UserCoupon(tenant_id=tid, user_id=photo_member, coupon_id=cp.id, code='X13OWN-A', is_used=False),
                            UserCoupon(tenant_id=tid, user_id=photo_member, coupon_id=cp.id, code='X13OWN-B', is_used=True)])
        db.session.commit()
    data = member_client(app, PHOTO_MEMBER_PHONE, PHOTO_MEMBER_PIN).get('/api/user_available_coupons').get_json()
    assert data['success'] is True and data['points'] == 77
    assert [c['code'] for c in data['coupons']] == ['X13OWN-A']
    assert '現折 $30' in data['coupons'][0]['title']


def test_reward_setting_input_clamped(app, admin_client, reward_setting_snapshot):
    r = admin_client.post('/admin/update_reward_setting', data={'is_enabled': '1', 'points_per_dollar': 'abc',
                                                                'max_discount_per_order': '-5', 'spend_per_point': '0'})
    assert r.status_code == 302
    assert reward_setting_values(app, reward_setting_snapshot) == (1, 0, 1)


def test_rewards_store_page(app, photo_member):
    make_item(app, '模組13紅利品Z', price=10, stock=5, is_reward=True, reward_points=5)
    make_item(app, '模組13一般品Z', price=10, stock=5)
    anon = app.test_client()
    anon.get('/?shop=default')
    r = anon.get('/rewards')
    assert r.status_code == 302 and 'face_login' in r.headers['Location']
    html = member_client(app, PHOTO_MEMBER_PHONE, PHOTO_MEMBER_PIN).get('/rewards').get_data(as_text=True)
    assert '模組13紅利品Z' in html and '模組13一般品Z' not in html


def test_voucher_claim_and_refund_are_atomic(app):
    """同一張兌換憑證：核銷與退點只有一方能成功 (決定性 + 24 組並行 + 結帳與退點同時發生 8 輪)"""
    tid = default_tenant_id(app)
    item_id = make_item(app, '模組14兌換品', price=100, stock=500, is_reward=True, reward_points=10)
    user_id = make_member(app, '模組14憑證會員', '0933140003', points=1000, pin='2468')

    def new_voucher():
        with app.app_context():
            v = RewardRedemption(tenant_id=tid, user_id=user_id, menu_item_id=item_id, points_cost=10)
            db.session.add(v)
            db.session.commit()
            return v.id

    def run(fn, vid):
        with app.app_context():
            ok = fn(tid, user_id, vid)
            db.session.commit()
            return ok

    v1, v2 = new_voucher(), new_voucher()
    assert run(try_claim_voucher, v1) and not run(try_claim_voucher, v1) and not run(try_refund_voucher, v1)
    assert run(try_refund_voucher, v2) and not run(try_refund_voucher, v2) and not run(try_claim_voucher, v2)

    outcomes = []
    for _ in range(24):
        vid, results, barrier = new_voucher(), [], threading.Barrier(2)

        def race(fn, vid=vid, results=results, barrier=barrier):
            barrier.wait()
            results.append(run(fn, vid))
        threads = [threading.Thread(target=race, args=(fn,)) for fn in (try_claim_voucher, try_refund_voucher)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        outcomes.append((vid, results))
    assert all(sum(r) == 1 for _, r in outcomes), outcomes[:3]
    with app.app_context():
        statuses = {v.status for v in RewardRedemption.query.filter(RewardRedemption.id.in_([v for v, _ in outcomes]))}
        assert statuses <= {VoucherStatus.USED, VoucherStatus.REFUNDED}

    # 端對端：同一會員同時「結帳使用憑證」與「退點」，點數與餐點必須守恆
    leaks = []
    for rnd in range(8):
        a = member_client(app, '0933140003', '2468', ip=f'198.51.101.{rnd * 2 + 1}')
        b = member_client(app, '0933140003', '2468', ip=f'198.51.101.{rnd * 2 + 2}')
        with app.app_context():
            pts_before = db.session.get(User, user_id).points
            orders_before = Order.query.filter_by(user_id=user_id).count()
        new_voucher()
        res, barrier = {}, threading.Barrier(2)

        def order():
            barrier.wait()
            res['order'] = checkout(a, [line(item_id, is_reward=True)])

        def refund():
            barrier.wait()
            res['refund'] = b.post('/api/refund_reward_item', json={'item_id': item_id, 'quantity': 1})
        threads = [threading.Thread(target=order), threading.Thread(target=refund)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with app.app_context():
            pts_after = db.session.get(User, user_id).points
            orders_after = Order.query.filter_by(user_id=user_id).count()
        if res['order'].status_code == 200 and (pts_after != pts_before or orders_after != orders_before + 1):
            leaks.append((rnd, pts_before, pts_after, res['order'].status_code, res['refund'].status_code))
    assert not leaks, leaks
