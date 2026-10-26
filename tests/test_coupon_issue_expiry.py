"""會員優惠券：會員專屬券、批量發放 (對象、張數、每人上限、到期、LINE 通知、撤回)、使用期限與過期、
後台核銷畫面分開未使用 / 已使用 (只顯示近 7 天)"""
from datetime import date, datetime, time, timedelta

from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.user import User
from app.services import coupon_service, line_service
from tests.support import (assert_flash, checkout, default_tenant_id, guest_client, line, login_staff, make_item,
                           make_member, member_client)
from tests.test_members_line_sms import line_api  # noqa: F401  (共用假 LINE API)

POS = '/api/pos/checkout'


def _add_coupon(admin_client, app, code, **fields):
    form = {'title': f'測試券 {code}', 'code': code, 'discount_type': 'fixed', 'discount_value': '50', 'min_spend': '0',
            'per_user_limit': '無上限', 'reward_points': '0', 'reward_discount_points': '0'}
    form.update({k: str(v) for k, v in fields.items()})
    admin_client.post('/admin/add_coupon', data=form)
    with app.app_context():
        c = Coupon.query.filter_by(code=code).first()
        assert c is not None, f'建立優惠券 {code} 失敗'
        return c.id


def _held(app, phone, code=None):
    with app.app_context():
        u = User.query.filter_by(phone=phone).first()
        q = UserCoupon.query.filter_by(user_id=u.id)
        if code:
            q = q.filter_by(code=code)
        return [(uc.id, uc.is_used, uc.expires_at, uc.source, uc.batch_id) for uc in q.order_by(UserCoupon.id).all()]


def _set(app, model, pk, **values):
    with app.app_context():
        obj = db.session.get(model, pk)
        for k, v in values.items():
            setattr(obj, k, v)
        db.session.commit()


def _verify(client, code, subtotal=300):
    return client.post('/api/verify_promo', json={'promo_code': code, 'subtotal': subtotal}).get_json()


# ---------------------------------------------------------------------------
# 會員專屬券 + 批量發放
# ---------------------------------------------------------------------------
def test_member_only_coupon_and_batch_issue_by_phones(app, admin_client):
    make_member(app, '發券甲', '0961000001', pin='1357')
    make_member(app, '發券乙', '0961000002', pin='1357')
    make_member(app, '沒發到', '0961000003', pin='1357')
    cid = _add_coupon(admin_client, app, 'VIPONLY50', member_only='1', reward_points='30', valid_days='7',
                      per_user_limit='3')
    with app.app_context():
        c = db.session.get(Coupon, cid)
        assert c.member_only and c.reward_points == 0 and not c.is_reward and c.valid_days == 7, '會員專屬券不在紅利商城'
    admin_client.post(f'/admin/coupons/{cid}/issue', data={
        'target': 'phones', 'phones': '0961000001\n0961000002, 0960999999 abc', 'quantity': '2', 'expire_mode': 'coupon'})
    msg = assert_flash(admin_client, '2 位會員，共 4 張')[-1]
    assert '0960999999' in msg and 'abc' in msg, '找不到 / 格式錯誤的手機要列出來'
    held = _held(app, '0961000001')
    expected = datetime.combine(date.today() + timedelta(days=7), time(23, 59, 59))
    assert len(held) == 2 and all(h[2] == expected and h[3] == 'batch' and h[4] == held[0][4] for h in held)
    assert _held(app, '0961000003') == []

    # 每人上限 3：再發 2 張，每人只補到 1 張
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0961000001 0961000002',
                                                           'quantity': '2', 'expire_mode': 'date',
                                                           'expires_on': (date.today() + timedelta(days=3)).isoformat()})
    assert_flash(admin_client, '共 2 張')
    held = _held(app, '0961000001')
    assert len(held) == 3 and held[-1][2].date() == date.today() + timedelta(days=3)
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0961000001', 'quantity': '1'})
    assert_flash(admin_client, '已達每人上限')

    # 使用：持有的會員可折抵，沒持有的會員、訪客、POS 公開代碼清單都不能用
    a = member_client(app, '0961000001', '1357', ip='10.61.0.1')
    assert _verify(a, 'VIPONLY50')['valid'] is True
    c3 = member_client(app, '0961000003', '1357', ip='10.61.0.3')
    assert '尚未持有' in _verify(c3, 'VIPONLY50')['message']
    g = guest_client(app)
    assert '會員專屬' in _verify(g, 'VIPONLY50')['message']
    iid = make_item(app, '發券測試餐', price=200, stock=50)
    r = checkout(a, [line(iid)], promo_code='VIPONLY50')
    assert r.status_code == 200 and r.get_json()['total_price'] == 150
    # 先用最快到期的那張 (3 天後到期)
    used = [h for h in _held(app, '0961000001') if h[1]]
    assert len(used) == 1 and used[0][2].date() == date.today() + timedelta(days=3)
    staff = login_staff(app, 'coupon_pos', 'staff')
    assert 'VIPONLY50' not in staff.get('/pos').get_data(as_text=True), 'POS 公開代碼清單不列會員專屬券'


def test_expired_coupon_cannot_be_used(app, admin_client, staff_client):
    uid = make_member(app, '過期券會員', '0961000010', pin='1357')
    cid = _add_coupon(admin_client, app, 'EXPIRE30', member_only='1', discount_value='30')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0961000010', 'quantity': '2'})
    (first, *_), (second, *_) = _held(app, '0961000010')
    assert _held(app, '0961000010')[0][2] is None, '券沒有設定期限：不會過期'
    _set(app, UserCoupon, first, expires_at=datetime.now() - timedelta(minutes=1))
    c = member_client(app, '0961000010', '1357', ip='10.61.0.10')
    assert _verify(c, 'EXPIRE30')['valid'] is True, '另一張還沒過期'
    _set(app, UserCoupon, second, expires_at=datetime.now() - timedelta(days=1))
    res = _verify(c, 'EXPIRE30')
    assert res['valid'] is False and '已過期' in res['message']
    # 結帳照常完成但不折抵；POS 會員資料與點餐頁可用清單都不列過期券
    iid = make_item(app, '過期券測試餐', price=100, stock=10)
    r = checkout(c, [line(iid)], promo_code='EXPIRE30')
    assert r.status_code == 200 and r.get_json()['total_price'] == 100
    m = staff_client.post('/api/pos/member_lookup', json={'member_id': uid}).get_json()['member']
    assert 'EXPIRE30' not in [x['code'] for x in m['coupons']]
    assert c.get('/api/user_available_coupons').get_json()['coupons'] == []
    # POS 結帳帶過期券：不折抵
    r = checkout(staff_client, [line(iid)], url=POS, user_id=uid, promo_code='EXPIRE30', cash_received=100)
    assert r.get_json()['total_price'] == 100


def test_coupon_period_window(app, admin_client):
    yesterday, tomorrow = date.today() - timedelta(days=1), date.today() + timedelta(days=1)
    pid = _add_coupon(admin_client, app, 'ENDEDCODE', valid_until=yesterday.isoformat())
    g = guest_client(app)
    assert '活動已結束' in _verify(g, 'ENDEDCODE')['message']
    _add_coupon(admin_client, app, 'SOONCODE', valid_from=tomorrow.isoformat())
    assert '活動尚未開始' in _verify(g, 'SOONCODE')['message']
    admin_client.post('/admin/add_coupon', data={'title': '錯誤期間', 'code': 'BADPERIOD', 'discount_type': 'fixed',
                                                 'discount_value': '10', 'reward_points': '0',
                                                 'valid_from': tomorrow.isoformat(), 'valid_until': yesterday.isoformat()})
    assert_flash(admin_client, '結束日不可早於開始日')
    # 活動已結束：不能發放
    make_member(app, '期間會員', '0961000020', pin='1357')
    admin_client.post(f'/admin/coupons/{pid}/issue', data={'target': 'phones', 'phones': '0961000020', 'quantity': '1'})
    assert_flash(admin_client, '活動已於')
    # 會員已持有的券：活動結束後一併失效
    mid = _add_coupon(admin_client, app, 'HELDENDS', member_only='1')
    admin_client.post(f'/admin/coupons/{mid}/issue', data={'target': 'phones', 'phones': '0961000020', 'quantity': '1'})
    c = member_client(app, '0961000020', '1357', ip='10.61.0.20')
    assert _verify(c, 'HELDENDS')['valid'] is True
    _set(app, Coupon, mid, valid_until=yesterday)
    assert '已過期' in _verify(c, 'HELDENDS')['message']


def test_reward_redeem_sets_expiry(app, admin_client, reward_setting_snapshot):
    uid = make_member(app, '兌換到期會員', '0961000030', points=500, pin='1357')
    cid = _add_coupon(admin_client, app, 'SHOP14', reward_points='10', valid_days='14')
    c = member_client(app, '0961000030', '1357', ip='10.61.0.30')
    r = c.post('/api/redeem_reward', json={'coupon_id': cid})
    assert r.status_code == 200, r.get_data(as_text=True)
    (_, _, expires_at, source, batch_id), = _held(app, '0961000030', 'SHOP14')
    assert expires_at.date() == date.today() + timedelta(days=14) and source == 'reward' and batch_id == ''
    assert 'SHOP14' in c.get('/rewards').get_data(as_text=True)
    _set(app, Coupon, cid, valid_until=date.today() - timedelta(days=1))
    assert 'SHOP14' not in c.get('/rewards').get_data(as_text=True), '活動結束的券不在商城'
    assert c.post('/api/redeem_reward', json={'coupon_id': cid}).status_code == 400
    with app.app_context():
        assert db.session.get(User, uid).points == 490


# ---------------------------------------------------------------------------
# 發放對象 / LINE 通知 / 撤回
# ---------------------------------------------------------------------------
def test_targets_line_notify_and_revoke(app, admin_client, line_api):
    posts, _ = line_api
    from app.models.member import MemberTier
    tid = default_tenant_id(app)
    with app.app_context():
        tier = MemberTier(tenant_id=tid, name='發券等級', min_spend=999999)
        db.session.add(tier)
        db.session.commit()
        tier_id = tier.id
    today = date.today()
    u1 = make_member(app, '等級壽星', '0961000040', tier_id=tier_id, birthday=today.replace(year=1990, day=1),
                     line_user_id='U' + 'd' * 32)
    make_member(app, '一般會員', '0961000041', birthday=today.replace(year=1991, month=(today.month % 12) + 1, day=1))
    with app.app_context():
        users, desc, _ = coupon_service.resolve_targets(tid, 'tier', tier_id)
        assert [u.id for u in users] == [u1] and '發券等級' in desc
        assert u1 in [u.id for u in coupon_service.resolve_targets(tid, 'birthday_month')[0]]
        assert '0961000041' not in [u.phone for u in coupon_service.resolve_targets(tid, 'birthday_month')[0]]
        assert u1 in [u.id for u in coupon_service.resolve_targets(tid, 'line')[0]]
        assert len(coupon_service.resolve_targets(tid, 'all')[0]) == User.query.filter_by(tenant_id=tid).count()

    cid = _add_coupon(admin_client, app, 'TIERGIFT', member_only='1', valid_days='10')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'tier', 'tier_id': tier_id, 'quantity': '3',
                                                           'expire_mode': 'days', 'valid_days': '5', 'notify_line': '1'})
    assert_flash(admin_client, '已 LINE 通知 1 位')
    multi = [p for p in posts if p[0] == line_service.MULTICAST_URL][-1][1]['json']
    assert multi['to'] == ['U' + 'd' * 32] and 'TIERGIFT' in multi['messages'][0]['text']
    held = _held(app, '0961000040', 'TIERGIFT')
    assert len(held) == 3 and held[0][2].date() == today + timedelta(days=5)

    # 用掉一張後撤回整批：只撤回未使用的
    _set(app, UserCoupon, held[0][0], is_used=True, used_at=datetime.now())
    page = admin_client.get('/admin').get_data(as_text=True)
    assert 'couponBatchTable' in page and '撤回未使用' in page
    admin_client.post(f'/admin/coupon_batches/{held[0][4]}/revoke')
    assert_flash(admin_client, '已撤回 2 張')
    assert [h[1] for h in _held(app, '0961000040', 'TIERGIFT')] == [True]
    with app.app_context():
        MemberTier.query.filter_by(id=tier_id).delete()
        User.query.filter_by(tier_id=tier_id).update({'tier_id': None})
        db.session.commit()


def test_issue_validation_and_permissions(app, admin_client, staff_client):
    cid = _add_coupon(admin_client, app, 'VALIDATE1', member_only='1')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '', 'quantity': '1'})
    assert_flash(admin_client, '至少一個手機號碼')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0961000001', 'quantity': '11'})
    assert_flash(admin_client, '1 ~ 10 張')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0961000001', 'quantity': '1',
                                                           'expire_mode': 'date', 'expires_on': '2020-01-01'})
    assert_flash(admin_client, '不可早於今天')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'nobody'})
    assert_flash(admin_client, '請選擇發放對象')
    assert staff_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'all'}).status_code in (302, 403)
    with app.app_context():
        assert UserCoupon.query.filter_by(coupon_id=cid).count() == 0


# ---------------------------------------------------------------------------
# 後台核銷畫面：未使用 / 已使用分開，已使用與過期只顯示近 7 天
# ---------------------------------------------------------------------------
def test_admin_view_splits_and_keeps_seven_days(app, admin_client):
    uid = make_member(app, '核銷畫面會員', '0961000050')
    cid = _add_coupon(admin_client, app, 'HISTORY7', member_only='1')
    admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0961000050', 'quantity': '6'})
    ids = [h[0] for h in _held(app, '0961000050')]
    now = datetime.now()
    _set(app, UserCoupon, ids[0], is_used=True, used_at=now - timedelta(days=2))     # 近期已使用 → 顯示
    _set(app, UserCoupon, ids[1], is_used=True, used_at=now - timedelta(days=8))     # 8 天前使用 → 隱藏
    _set(app, UserCoupon, ids[2], expires_at=now - timedelta(days=1))                # 昨天過期 → 顯示
    _set(app, UserCoupon, ids[3], expires_at=now - timedelta(days=10))               # 10 天前過期 → 隱藏
    _set(app, UserCoupon, ids[4], expires_at=now + timedelta(days=3))                # 未使用
    with app.app_context():
        view = coupon_service.admin_view(db.session.get(User, uid))
        assert [(v['id'], v['status']) for v in view] == [
            (ids[4], 'available'), (ids[5], 'available'), (ids[0], 'used'), (ids[2], 'expired')]
        assert coupon_service.unused_count(db.session.get(User, uid)) == 2
        # 資料沒有刪除
        assert UserCoupon.query.filter_by(user_id=uid).count() == 6
    page = admin_client.get('/admin').get_data(as_text=True)
    assert '管理票券 (2)' in page
    # 切換已使用 / 未使用的回應也套用同樣規則
    r = admin_client.post(f'/admin/toggle_user_coupon/{ids[4]}').get_json()
    assert r['unused'] == 1 and [c['status'] for c in r['coupons']].count('used') == 2
    r = admin_client.post(f'/admin/toggle_user_coupon/{ids[4]}').get_json()
    assert r['unused'] == 2
