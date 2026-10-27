"""
第四批：點數到期 (先進先出) 與提醒、集點卡、推薦好友、禮物卡 (售出 / 扣款 / 退回 / 作廢 / 跨店同步)、
會員自助下載資料與刪除帳號。
"""
import json
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.member import GiftCard, GiftCardLedger, StampLedger
from app.models.order import Order
from app.models.payment import PaymentRecord
from app.models.sync import PointLedger
from app.models.user import User
from tests.support import cancel_order, checkout, default_tenant_id, line, make_item, make_member, member_client

CARD = {'auth_code': 'A12345', 'card_last4': '4242'}


def _pos(client, items, user_id=None, method='Cash', **extra):
    payload = {'items': items, 'payment_method': method, 'order_type': '內用', 'user_id': user_id, **extra}
    if method == 'Cash':
        payload.setdefault('cash_received', 100000)
    if method == 'Credit Card':
        payload['payment_info'] = CARD
    return client.post('/api/pos/checkout', json=payload)


def _points(app, uid):
    with app.app_context():
        return db.session.get(User, uid).points


# ===========================================================================
# 點數到期
# ===========================================================================
def test_points_expire_first_in_first_out(app, store_settings, monkeypatch):
    from app.services import line_service, loyalty_service
    from app.services.points_service import change_points
    tid = default_tenant_id(app)
    uid = make_member(app, '到期會員', '0966500001', line_user_id='U-expire-1')
    now = datetime.now()
    with app.app_context():
        for delta, days in ((100, 400), (50, 200), (-30, 100)):
            change_points(tid, uid, delta, 'test')
            db.session.flush()
            entry = PointLedger.query.filter_by(user_id=uid).order_by(PointLedger.id.desc()).first()
            entry.created_at = now - timedelta(days=days)
        db.session.commit()
        assert loyalty_service.expire_points(tid, now) == 0, '未設定有效期限不到期'
    assert _points(app, uid) == 120
    store_settings('member', points_expire_months=12, points_expiry_notice_days=200)
    with app.app_context():
        assert loyalty_service.expire_points(tid, now) >= 1
        assert db.session.get(User, uid).points == 50, '最舊的 100 點扣掉已用的 30 點後，剩 70 點到期'
        assert PointLedger.query.filter_by(user_id=uid, reason='expire').one().delta == -70
        loyalty_service.expire_points(tid, now)
        assert db.session.get(User, uid).points == 50, '不重複到期'
        up = loyalty_service.upcoming_expiry(db.session.get(User, uid), now)
        assert up['amount'] == 50 and up['date'] == loyalty_service.add_months(now - timedelta(days=200), 12).date()

    pushed = []
    monkeypatch.setattr(line_service, 'push_text', lambda tid_, to, text: pushed.append((to, text)) or True)
    with app.app_context():
        assert loyalty_service.notify_expiring(tid, now) >= 1
        mine = [t for to, t in pushed if to == 'U-expire-1']
        assert mine and '50 點' in mine[0]
        pushed.clear()
        loyalty_service.notify_expiring(tid, now)
        assert not [t for to, t in pushed if to == 'U-expire-1'], '同一提醒期間只通知一次'
    assert loyalty_service.add_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
    assert loyalty_service.add_months(datetime(2026, 3, 15), -14) == datetime(2025, 1, 15)


def test_points_expiry_shown_on_rewards_page(app, store_settings):
    from app.services.points_service import change_points
    store_settings('member', points_expire_months=6)
    uid = make_member(app, '到期顯示', '0966500002', pin='7391')
    with app.app_context():
        change_points(default_tenant_id(app), uid, 40, 'test')
        db.session.commit()
    page = member_client(app, '0966500002', '7391').get('/rewards').get_data(as_text=True)
    assert 'id="pointsExpiry"' in page and '40 點將於' in page


# ===========================================================================
# 集點卡
# ===========================================================================
def test_stamp_card_collect_complete_and_cancel(app, staff_client, admin_client, store_settings):
    from app.models.coupon import Coupon, UserCoupon
    tid = default_tenant_id(app)
    with app.app_context():
        db.session.add(Coupon(tenant_id=tid, code='STAMPFREE', title='集點兌換券', discount_type='fixed',
                              discount_value=60, min_spend=0, reward_points=0, is_reward=False, member_only=True))
        db.session.commit()
    store_settings('stamps', enabled=True, card_size=3, mode='item', categories='飲料, 咖啡', reward_points=30,
                   reward_coupon_code='stampfree', card_title='咖啡卡')
    drink = make_item(app, '集點拿鐵', price=60, category='咖啡')
    food = make_item(app, '集點貝果', price=50, category='點心')
    uid = make_member(app, '集章會員', '0966500003', pin='7391')
    r = _pos(staff_client, [line(drink, 2), line(food, 1)], user_id=uid)
    assert r.status_code == 200, r.get_json()
    with app.app_context():
        from app.services import loyalty_service
        assert loyalty_service.stamp_count(uid) == 2
    before = _points(app, uid)
    second = _pos(staff_client, [line(drink, 2)], user_id=uid).get_json()['order_id']
    with app.app_context():
        assert loyalty_service.stamp_count(uid) == 1, '集滿 3 章兌換後剩 1 章'
        assert StampLedger.query.filter_by(user_id=uid, reason='complete').one().delta == -3
        assert UserCoupon.query.filter_by(user_id=uid, source='stamp').count() == 1
        earned_pts = PointLedger.query.filter_by(user_id=uid, reason='stamp_card').one().delta
    assert earned_pts == 30 and _points(app, uid) >= before + 30
    lookup = staff_client.post('/api/pos/member_lookup', json={'member_id': uid}).get_json()['member']
    assert lookup['stamps'] == {'enabled': True, 'title': '咖啡卡', 'count': 1, 'size': 3}
    page = member_client(app, '0966500003', '7391').get('/rewards').get_data(as_text=True)
    assert 'id="stampCard"' in page and '咖啡卡' in page

    assert cancel_order(admin_client, second).status_code == 200
    with app.app_context():
        assert loyalty_service.stamp_count(uid) == 0, '取消扣回 (最多扣到 0；已兌換的獎勵不追回)'

    store_settings('stamps', mode='order', min_spend=100)
    _pos(staff_client, [line(food, 1)], user_id=uid)
    _pos(staff_client, [line(food, 2)], user_id=uid)
    with app.app_context():
        assert loyalty_service.stamp_count(uid) == 1, '每筆消費滿 $100 得 1 章'


# ===========================================================================
# 推薦好友
# ===========================================================================
def _register(app, phone, referral=''):
    c = app.test_client()
    c.get('/?shop=default')
    return c, c.post('/register', data={'name': f'新會員{phone[-3:]}', 'phone': phone, 'pin': '7391',
                                        'agree_privacy': '1', 'referral_code': referral})


def test_referral_rewards_both_on_first_order(app, staff_client, admin_client, store_settings):
    from tests.support import flashes
    store_settings('member', referral_enabled=True, referrer_points=100, referee_points=50, referral_min_spend=100)
    a = make_member(app, '推薦人', '0966500004', pin='7391')
    page = member_client(app, '0966500004', '7391').get('/rewards').get_data(as_text=True)
    assert 'id="referralBox"' in page
    with app.app_context():
        code = db.session.get(User, a).referral_code
    assert len(code) == 8
    reg = app.test_client().get(f'/register?ref={code}').get_data(as_text=True)
    assert f'value="{code}"' in reg

    c, r = _register(app, '0966500005', 'NOPE1234')
    assert r.status_code in (302, 400)
    with app.app_context():
        assert User.query.filter_by(phone='0966500005').first() is None, '推薦碼錯誤不建立帳號'
    c, r = _register(app, '0966500005', code.lower())
    assert r.status_code == 302
    with app.app_context():
        b = User.query.filter_by(phone='0966500005').first()
        assert b.referred_by_uuid == db.session.get(User, a).uuid
        bid = b.id
    item = make_item(app, '推薦套餐', price=120)
    cheap = make_item(app, '推薦小點', price=30)
    a_before, b_before = _points(app, a), _points(app, bid)
    _pos(staff_client, [line(cheap)], user_id=bid)
    assert _points(app, a) == a_before, '未達門檻不發'
    first = _pos(staff_client, [line(item)], user_id=bid).get_json()['order_id']
    assert _points(app, a) == a_before + 100
    with app.app_context():
        assert PointLedger.query.filter_by(user_id=bid, reason='referred').one().delta == 50
    _pos(staff_client, [line(item)], user_id=bid)
    assert _points(app, a) == a_before + 100, '只發一次'
    assert cancel_order(admin_client, first).status_code == 200
    assert _points(app, a) == a_before, '第一筆訂單取消扣回'


# ===========================================================================
# 禮物卡
# ===========================================================================
def _sell(client, amount=500, method='Cash', **extra):
    return client.post('/api/pos/gift_cards/sell', json={'amount': amount, 'payment_method': method, **extra})


def test_gift_card_sell_redeem_refund_void(app, staff_client, admin_client, store_settings):
    assert _sell(staff_client).status_code == 400, '未啟用'
    store_settings('gift_card', enabled=True, max_amount=5000)
    assert _sell(staff_client, 6000).status_code == 400
    assert _sell(staff_client, 500, '禮物卡').status_code == 400
    r = _sell(staff_client, 500)
    assert r.status_code == 200, r.get_json()
    code = r.get_json()['card']['code']
    assert len(code) == 16 and code.isdigit()
    with app.app_context():
        rec = PaymentRecord.query.filter_by(kind='gift_sale').order_by(PaymentRecord.id.desc()).first()
        assert rec.amount == 500 and code not in rec.note
    assert staff_client.post('/api/pos/gift_cards/lookup', json={'code': code}).get_json()['card']['balance'] == 500
    spaced = f'{code[:4]} {code[4:8]}-{code[8:12]} {code[12:]}'
    assert staff_client.post('/api/pos/gift_cards/lookup', json={'code': spaced}).status_code == 200
    assert staff_client.post('/api/pos/gift_cards/lookup', json={'code': '1' * 16}).status_code == 404
    assert '禮物卡' in staff_client.get('/pos').get_data(as_text=True)

    item = make_item(app, '禮物卡餐', price=180)
    r = _pos(staff_client, [line(item)], method='禮物卡', payment_info={'gift_code': code})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['payment_reference'] == f'{code[:4]}****{code[-4:]}', '收據只顯示遮罩卡號'
    oid = body['order_id']
    big = make_item(app, '禮物卡大餐', price=400)
    r = _pos(staff_client, [line(big)], method='禮物卡', payment_info={'gift_code': code})
    assert r.status_code == 409 and r.get_json().get('insufficient_gift')
    assert staff_client.post('/api/pos/gift_cards/lookup', json={'code': code}).get_json()['card']['balance'] == 320

    assert cancel_order(admin_client, oid).status_code == 200
    assert staff_client.post('/api/pos/gift_cards/lookup', json={'code': code}).get_json()['card']['balance'] == 500

    # 自助點餐機不能用禮物卡
    from tests.support import guest_client
    r = checkout(guest_client(app), [line(item)], payment_method='禮物卡', payment_info={'gift_code': code})
    assert r.status_code == 400

    page = admin_client.get('/admin/gift_cards').get_data(as_text=True)
    assert f'{code[:4]}-****-****-{code[-4:]}' in page and code not in page
    with app.app_context():
        card_id = GiftCard.query.filter_by(code=code).one().id
    admin_client.post('/admin/gift_cards', data={'action': 'void', 'card_id': str(card_id), 'reason': '遺失'})
    with app.app_context():
        card = db.session.get(GiftCard, card_id)
        assert card.status == 'void'
        assert sum(e.delta for e in GiftCardLedger.query.filter_by(gift_card_id=card_id).all()) == 0
    r = _pos(staff_client, [line(item)], method='禮物卡', payment_info={'gift_code': code})
    assert r.status_code == 409 and '作廢' in r.get_json()['message']


def test_gift_card_partial_refund_returns_to_card(app, staff_client, admin_client, store_settings):
    store_settings('gift_card', enabled=True)
    code = _sell(staff_client, 300).get_json()['card']['code']
    item = make_item(app, '部分退禮物卡', price=200)
    oid = _pos(staff_client, [line(item)], method='禮物卡', payment_info={'gift_code': code}).get_json()['order_id']
    r = admin_client.post(f'/api/pos/orders/{oid}/refund', json={'amount': 50, 'reason': '少一份', 'mode': 'partial'})
    assert r.status_code == 200, r.get_json()
    assert staff_client.post('/api/pos/gift_cards/lookup', json={'code': code}).get_json()['card']['balance'] == 150


# ===========================================================================
# 會員自助：下載資料、刪除帳號
# ===========================================================================
def test_member_downloads_own_data_and_deletes_account(app, staff_client):
    uid = make_member(app, '自助會員', '0966500009', pin='7391', points=12)
    item = make_item(app, '自助資料餐', price=90)
    oid = _pos(staff_client, [line(item)], user_id=uid).get_json()['order_id']
    assert app.test_client().get('/privacy/my_data').status_code == 302
    member = member_client(app, '0966500009', '7391')
    r = member.get('/privacy/my_data')
    assert r.status_code == 200 and 'attachment' in r.headers['Content-Disposition']
    data = json.loads(r.get_data(as_text=True))
    assert data['profile']['phone'] == '0966500009'
    assert [o['order_id'] for o in data['orders']] == [oid]
    assert any(p['delta'] == 12 for p in data['point_ledger'])
    assert 'pin_hash' not in r.get_data(as_text=True) and 'feature' not in data['profile']
    assert 'id="myDataBox"' in member.get('/rewards').get_data(as_text=True)

    assert member.post('/privacy/delete_account', data={'pin': '0000', 'confirm': '刪除'}).status_code in (302, 403)
    assert member.post('/privacy/delete_account', data={'pin': '7391', 'confirm': 'yes'}).status_code in (302, 400)
    with app.app_context():
        assert db.session.get(User, uid) is not None
        db.session.get(User, uid).wallet_balance = 10
        db.session.commit()
    member.post('/privacy/delete_account', data={'pin': '7391', 'confirm': '刪除'})
    with app.app_context():
        assert db.session.get(User, uid) is not None, '儲值金有餘額不可刪除'
        u = db.session.get(User, uid)
        u.wallet_balance = 0
        db.session.commit()
    r = member.post('/privacy/delete_account', data={'pin': '7391', 'confirm': '刪除'})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(User, uid) is None
        o = db.session.get(Order, oid)
        assert o.user_id is None and o.table_number == '已刪除會員'
    assert member.get('/rewards').status_code == 302, '已登出'


# ===========================================================================
# 跨店同步：禮物卡在 A 店售出、B 店使用；集章在兩店合併
# ===========================================================================
def test_gift_card_and_stamps_sync_between_branches():
    from app.services import giftcard_service, loyalty_service, settings_service
    from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, _member, _register_member, tenant_ctx
    env = Env()
    try:
        burger = _add_item(env.a, '同步禮物卡餐', price=150, stock=50)
        _register_member(env.a, '同步集章', '0966500020')
        env.sync_all()
        for node in (env.a, env.b):
            with tenant_ctx(node) as tenant:
                settings_service.put('gift_card', {'enabled': True}, tenant_id=tenant.id)
                settings_service.put('stamps', {'enabled': True, 'card_size': 10, 'mode': 'item', 'categories': ''},
                                     tenant_id=tenant.id)
                db.session.commit()
        with tenant_ctx(env.a) as tenant:
            code = giftcard_service.sell(tenant.id, 1000, 'Cash', 'a_cashier').code
            db.session.commit()
        _checkout(env.a, [line(_item(env.a, burger)['id'], 2)], user_id=_member(env.a, '0966500020')['id'])
        env.sync_all()
        _checkout(env.b, [line(_item(env.b, burger)['id'], 1)], payment_method='禮物卡', order_source='POS',
                  payment_info={'gift_code': code}, user_id=_member(env.b, '0966500020')['id'])
        env.sync_all()
        for app_ in (env.hub, env.a, env.b):
            with tenant_ctx(app_) as tenant:
                card = GiftCard.query.filter_by(code=code).one()
                assert giftcard_service.balance(card) == 850, app_.config['SYNC_ROLE']
                uid = User.query.filter_by(phone='0966500020').one().id
                assert loyalty_service.stamp_count(uid) == 3
    finally:
        env.close()
