"""優惠券安全性：猜測代碼鎖定、公開代碼限會員 / 每人使用次數、最高折抵金額、代碼格式、跨店隔離；
紅利商城顯示會員目前持有的張數 (剩餘 N 張)"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.user import User
from tests.support import (assert_flash, checkout, guest_client, line, make_item, make_member, member_client,
                           other_shop_owner_client)
from tests.test_coupon_issue_expiry import _add_coupon

POS = '/api/pos/checkout'


def _verify(client, code, subtotal=300, ip=None):
    return client.post('/api/verify_promo', json={'promo_code': code, 'subtotal': subtotal},
                       headers={'X-Forwarded-For': ip} if ip else None).get_json()


def _guest(app, ip):
    c = app.test_client()
    c.get('/?shop=default', headers={'X-Forwarded-For': ip})
    c.post('/guest_login', headers={'X-Forwarded-For': ip})
    return c


# ---------------------------------------------------------------------------
# 猜測代碼
# ---------------------------------------------------------------------------
def test_guessing_codes_locks_public_lookup(app, admin_client):
    _add_coupon(admin_client, app, 'REALCODE1', discount_value='30')
    ip = '10.77.0.1'
    g = _guest(app, ip)
    assert _verify(g, 'REALCODE1', ip=ip)['valid'] is True
    # 未達門檻不算猜錯
    _add_coupon(admin_client, app, 'MINSPEND1', discount_value='30', min_spend='1000')
    for _ in range(5):   # (同一 IP 每分鐘最多 30 次查詢，另有頻率限制)
        assert _verify(g, 'MINSPEND1', ip=ip)['valid'] is False
    assert _verify(g, 'REALCODE1', ip=ip)['valid'] is True
    for i in range(20):
        assert _verify(g, f'GUESS{i:03d}', ip=ip)['message'] == '無效的優惠代碼！'
    locked = _verify(g, 'REALCODE1', ip=ip)
    assert locked['valid'] is False and '次數過多' in locked['message'], '鎖定期間連正確代碼也不回應 (無法繼續試)'
    # 結帳帶代碼也不會折抵
    iid = make_item(app, '猜碼測試餐', price=300, stock=20)
    r = checkout(g, [line(iid)], promo_code='REALCODE1', ip=ip)
    assert r.status_code == 200 and r.get_json()['total_price'] == 300
    # 其他來源不受影響
    assert _verify(_guest(app, '10.77.0.2'), 'REALCODE1', ip='10.77.0.2')['valid'] is True


def test_member_lock_and_held_coupons_still_work(app, admin_client):
    make_member(app, '猜碼會員', '0967700001', pin='1357')
    cid = _add_coupon(admin_client, app, 'HELDSAFE', member_only='1')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0967700001', 'quantity': '1'})
    _add_coupon(admin_client, app, 'PUBLIC77', discount_value='10')
    m = member_client(app, '0967700001', '1357', ip='10.77.1.1')
    for i in range(5):   # 換不同 IP 猜，會員層級仍會累計
        for j in range(2):
            _verify(m, f'NOPE{i}{j}', ip=f'10.77.1.{10 + i}')
    assert '次數過多' in _verify(m, 'PUBLIC77', ip='10.77.1.99')['message']
    # 會員自己持有的券不受影響 (不是猜測)
    assert _verify(m, 'HELDSAFE', ip='10.77.1.99')['valid'] is True
    # 猜會員專屬券的代碼也算失敗
    g = _guest(app, '10.77.2.1')
    assert '會員專屬' in _verify(g, 'HELDSAFE', ip='10.77.2.1')['message']


def test_pos_is_not_throttled(app, admin_client, staff_client):
    _add_coupon(admin_client, app, 'POSOK1', discount_value='10')
    for i in range(30):
        staff_client.post('/api/pos/verify_promo', json={'promo_code': f'POSBAD{i}', 'subtotal': 100})
    r = staff_client.post('/api/pos/verify_promo', json={'promo_code': 'POSOK1', 'subtotal': 100}).get_json()
    assert r['valid'] is True


# ---------------------------------------------------------------------------
# 公開代碼的使用限制
# ---------------------------------------------------------------------------
def test_public_code_member_only_and_uses_per_member(app, admin_client, staff_client):
    uid = make_member(app, '限用會員', '0967700010', pin='1357')
    _add_coupon(admin_client, app, 'ONCEONLY', discount_value='40', uses_per_member='1')
    _add_coupon(admin_client, app, 'LOGINONLY', discount_value='20', login_required='1')
    g = _guest(app, '10.77.3.1')
    assert '限會員使用' in _verify(g, 'ONCEONLY', ip='10.77.3.1')['message']
    assert '限會員使用' in _verify(g, 'LOGINONLY', ip='10.77.3.1')['message']
    iid = make_item(app, '限用測試餐', price=200, stock=20)
    assert checkout(g, [line(iid)], promo_code='LOGINONLY', ip='10.77.3.1').get_json()['total_price'] == 200

    m = member_client(app, '0967700010', '1357', ip='10.77.3.2')
    assert _verify(m, 'LOGINONLY', ip='10.77.3.2')['valid'] is True
    r = checkout(m, [line(iid)], promo_code='ONCEONLY', ip='10.77.3.2')
    assert r.get_json()['total_price'] == 160
    oid = r.get_json()['order_id']
    second = _verify(m, 'ONCEONLY', ip='10.77.3.2')
    assert second['valid'] is False and '限用 1 次' in second['message']
    # 已達使用次數仍送出 (例如另一台裝置剛用掉)：結帳失敗並說明，不會默默用原價成立
    again = checkout(m, [line(iid)], promo_code='ONCEONLY', ip='10.77.3.2')
    assert again.status_code == 409 and again.get_json()['coupon_unavailable'] and '限用 1 次' in again.get_json()['message']
    # POS 也算同一位會員的使用次數；沒綁會員時不能用
    r = checkout(staff_client, [line(iid)], url=POS, promo_code='ONCEONLY', cash_received=200)
    assert r.get_json()['total_price'] == 200
    assert checkout(staff_client, [line(iid)], url=POS, user_id=uid, promo_code='ONCEONLY',
                    cash_received=200).status_code == 409
    # 訂單取消後次數歸還
    assert admin_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled'}).status_code == 200
    assert _verify(m, 'ONCEONLY', ip='10.77.3.2')['valid'] is True


def test_max_discount_caps_percent(app, admin_client):
    _add_coupon(admin_client, app, 'HALFCAP', discount_type='percent', discount_value='50', max_discount='100')
    iid = make_item(app, '上限測試餐', price=1000, stock=20)
    g = _guest(app, '10.77.4.1')
    assert _verify(g, 'HALFCAP', subtotal=1000, ip='10.77.4.1')['discount'] == 100
    assert checkout(g, [line(iid)], promo_code='HALFCAP', ip='10.77.4.1').get_json()['total_price'] == 900
    assert _verify(g, 'HALFCAP', subtotal=120, ip='10.77.4.1')['discount'] == 60


# ---------------------------------------------------------------------------
# 後台：代碼格式、輸入驗證、跨店
# ---------------------------------------------------------------------------
def test_code_format_and_form_validation(app, admin_client):
    base = {'title': '格式測試', 'discount_type': 'fixed', 'discount_value': '10', 'reward_points': '0'}
    for bad in ('AB', 'HAS SPACE', 'SYM$BOL', '-DASHFIRST', 'X' * 31, '優惠碼123'):
        admin_client.post('/admin/add_coupon', data=dict(base, code=bad))
        assert_flash(admin_client, '3~30 碼')
    admin_client.post('/admin/add_coupon', data=dict(base, code='zero-amt', discount_value='0'))
    assert_flash(admin_client, '必須大於 0')
    admin_client.post('/admin/add_coupon', data=dict(base, code='lower-ok_1'))
    with app.app_context():
        assert Coupon.query.filter_by(code='LOWER-OK_1').count() == 1
        assert Coupon.query.filter_by(code='ZERO-AMT').count() == 0


def test_other_shop_cannot_touch_coupons(app, admin_client):
    make_member(app, '隔離會員', '0967700020')
    cid = _add_coupon(admin_client, app, 'ISOLATE1', member_only='1')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0967700020', 'quantity': '1'})
    with app.app_context():
        uc = UserCoupon.query.filter_by(code='ISOLATE1').one()
        ucid, bid = uc.id, uc.batch_id
    other = other_shop_owner_client(app)
    assert other.post(f'/admin/toggle_user_coupon/{ucid}').status_code == 404
    assert other.post(f'/admin/edit_coupon/{cid}', data={'title': 'x', 'code': 'HACKED1', 'discount_value': '999',
                                                         'reward_points': '0'}).status_code == 404
    assert other.post(f'/admin/coupons/{cid}/issue', data={'target': 'all', 'quantity': '1'}).status_code == 404
    other.post(f'/admin/coupon_batches/{bid}/revoke')
    assert other.post(f'/admin/delete_coupon/{cid}').status_code == 404
    with app.app_context():
        c = db.session.get(Coupon, cid)
        assert (c.code, c.discount_value) == ('ISOLATE1', 50)
        uc = db.session.get(UserCoupon, ucid)
        assert uc is not None and uc.is_used is False


# ---------------------------------------------------------------------------
# 紅利商城：剩餘張數
# ---------------------------------------------------------------------------
def test_rewards_store_shows_remaining_held(app, admin_client, reward_setting_snapshot):
    uid = make_member(app, '剩餘張數會員', '0967700030', points=500, pin='1357')
    cid = _add_coupon(admin_client, app, 'REPEAT10', reward_points='10', valid_days='30')
    m = member_client(app, '0967700030', '1357', ip='10.77.5.1')
    page = m.get('/rewards').get_data(as_text=True)
    assert 'held-badge' not in page and '立即兌換' in page
    assert m.post('/api/redeem_reward', json={'coupon_id': cid}).status_code == 200
    page = m.get('/rewards').get_data(as_text=True)
    assert '剩餘 1 張' in page and '再兌換一張' in page and 'data-held="1"' in page
    assert m.post('/api/redeem_reward', json={'coupon_id': cid}).status_code == 200
    assert '剩餘 2 張' in m.get('/rewards').get_data(as_text=True), '可以重複兌換，張數累加'
    # 已使用、已過期的不算
    with app.app_context():
        ids = [uc.id for uc in UserCoupon.query.filter_by(user_id=uid, code='REPEAT10').order_by(UserCoupon.id)]
        db.session.get(UserCoupon, ids[0]).is_used = True
        db.session.commit()
    assert '剩餘 1 張' in m.get('/rewards').get_data(as_text=True)
    with app.app_context():
        db.session.get(UserCoupon, ids[1]).expires_at = datetime.now() - timedelta(minutes=1)
        db.session.commit()
    page = m.get('/rewards').get_data(as_text=True)
    assert 'held-badge' not in page and '立即兌換' in page
    with app.app_context():
        assert db.session.get(User, uid).points == 480
