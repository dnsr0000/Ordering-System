"""
修正小組 wallet-loyalty-giftcards-2 的回歸測試：付款後才發的回饋 (作廢份數 / 寄杯判斷 / 贈點)、紅利商城兌換與退點、
禮物卡鎖定與作廢後退款、點數到期校正、集點卡獎勵券、禮物卡搜尋、寄杯售出去重。
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.member import GiftCard, GiftCardLedger, MemberPass, StampLedger
from app.models.sync import PointLedger
from app.models.user import RewardRedemption, User
from tests.support import (cancel_order, default_tenant_id, line, make_item, make_member, member_client,
                           tenant_request)


def _points(app, uid):
    with app.app_context():
        return db.session.get(User, uid).points


def _item_line(menu_item_id, quantity, price, voided=0, customization='', is_reward=False, is_gift=False):
    return SimpleNamespace(menu_item_id=menu_item_id, quantity=quantity, voided_qty=voided, price=price,
                           customization=customization, is_reward=is_reward, is_gift=is_gift)


# ===========================================================================
# B-087 / B-094：付款完成才集點的品項份數與寄杯判斷
# ===========================================================================
def test_paid_reward_lines_use_remaining_quantity_and_pass_tag(app):
    from app.services import member_rewards
    coffee = make_item(app, '修正集點咖啡', price=60, category='咖啡')
    latte = make_item(app, '修正寄杯拿鐵', price=80, category='咖啡')
    tea = make_item(app, '修正改價紅茶', price=40, category='咖啡')
    order = SimpleNamespace(items=[
        _item_line(coffee, 2, 60, voided=1),                                   # 點 3 杯作廢 1 杯：quantity 已是 2
        _item_line(latte, 1, 10, customization='[寄杯]、燕麥奶(+10)'),           # 寄杯兌換加料：單價 > 0
        _item_line(tea, 1, 0, customization='改價 $40→$0'),                      # 一般品項改價成 0 元
    ])
    with app.app_context():
        lines = {ln.menu_item.id: ln for ln in member_rewards._order_lines(order)}
    assert lines[coffee].quantity == 2, '作廢的份數不可再扣一次'
    assert lines[latte].is_pass is True, '有加料的寄杯兌換仍是寄杯'
    assert lines[tea].is_pass is False, '改價成 0 元的一般品項不是寄杯'


# ===========================================================================
# B-088：桌邊未結帳時作廢品項，贈點依剩下的比例縮減
# ===========================================================================
def test_points_scaled_down_after_unpaid_voids(app):
    from app.services import member_rewards
    order = SimpleNamespace(points_earned=10, items=[_item_line(None, 1, 100, voided=9)])
    assert member_rewards._points_after_voids(order) == 1
    order = SimpleNamespace(points_earned=10, items=[_item_line(None, 10, 100)])
    assert member_rewards._points_after_voids(order) == 10, '沒有作廢不變'
    order = SimpleNamespace(points_earned=6, items=[_item_line(None, 2, 100, voided=1), _item_line(None, 1, 0)])
    assert member_rewards._points_after_voids(order) == 4


# ===========================================================================
# B-098：0 點的紅利券不可兌換
# ===========================================================================
def test_zero_point_reward_coupon_cannot_be_redeemed(app):
    from app.models.coupon import Coupon, UserCoupon
    from app.services import reward_service
    tid = default_tenant_id(app)
    uid = make_member(app, '零點券會員', '0977602001', points=50)
    with app.app_context():
        c = Coupon(tenant_id=tid, code='ZEROPTFIX2', title='零點券', discount_type='fixed', discount_value=30,
                   min_spend=0, reward_points=0, is_reward=True)
        db.session.add(c)
        db.session.commit()
        cid = c.id
    with tenant_request(app, tid):
        with pytest.raises(AppError):
            reward_service.redeem_coupon(tid, uid, cid)
        assert UserCoupon.query.filter_by(user_id=uid, coupon_id=cid).count() == 0
    assert _points(app, uid) == 50


# ===========================================================================
# R2-034：停售 / 不在供應時段的兌換品不可兌換 (不扣點)
# ===========================================================================
def test_reward_item_out_of_service_cannot_be_redeemed(app):
    from app.services import reward_service
    tid = default_tenant_id(app)
    item = make_item(app, '修正停售兌換品', price=50, is_reward=True, reward_points=20,
                     sale_until=date.today() - timedelta(days=1))
    uid = make_member(app, '停售兌換會員', '0977602002', points=100)
    with tenant_request(app, tid):
        with pytest.raises(AppError) as exc:
            reward_service.redeem_item(tid, uid, item)
        assert '目前不供應' in exc.value.message
    assert _points(app, uid) == 100


# ===========================================================================
# R2-039：取消紅利兌換 / 刪除商品後，已兌換的憑證仍可退回
# ===========================================================================
def test_open_voucher_refundable_after_item_delisted_or_deleted(app):
    from app.models.enums import VoucherStatus
    from app.models.menu import MenuItem
    from app.routes.rewards import _open_voucher_summary
    from app.services import reward_service
    tid = default_tenant_id(app)
    item = make_item(app, '修正下架兌換蛋糕', price=80, is_reward=True, reward_points=30)
    uid = make_member(app, '下架退點會員', '0977602003', points=100)
    with tenant_request(app, tid):
        reward_service.redeem_item(tid, uid, item)
    assert _points(app, uid) == 70
    with app.app_context():
        db.session.get(MenuItem, item).is_reward = False
        db.session.commit()
    with tenant_request(app, tid):
        assert reward_service.refund_item(tid, uid, item) == 30
    assert _points(app, uid) == 100

    # 兌換商品已被刪除 (menu_item_id 為 NULL)：彙總成 item_id 0 一列，仍可退回
    with tenant_request(app, tid):
        db.session.add(RewardRedemption(tenant_id=tid, user_id=uid, menu_item_id=None, points_cost=25,
                                        status=VoucherStatus.OPEN))
        db.session.execute(db.update(User).where(User.id == uid).values(points=User.points - 25))
        # 以 UPDATE 直接扣點不會自動補寫異動帳：一併寫入，維持 點數 = 異動帳合計 (test_sync_invariants)
        from app.models.sync import PointLedger
        db.session.add(PointLedger(tenant_id=tid, user_id=uid, delta=-25, reason='reward_item'))
        db.session.commit()
        rows = _open_voucher_summary(uid)
        assert [(r['item_id'], r['points']) for r in rows] == [(0, 25)]
        assert reward_service.refund_item(tid, uid, 0) == 25
        with pytest.raises(AppError):
            reward_service.refund_item(tid, uid, 0)


# ===========================================================================
# B-091 / R2-035：兌換 API 的輸入驗證與成功訊息翻譯欄位
# ===========================================================================
def test_redeem_api_validates_modifier_ids_and_returns_message_i18n(app):
    item = make_item(app, '修正兌換美式', price=50, is_reward=True, reward_points=10)
    make_member(app, '兌換訊息會員', '0977602004', points=100, pin='7391')
    c = member_client(app, '0977602004', '7391')
    r = c.post('/api/redeem_reward', json={'item_id': item, 'modifier_ids': 5})
    assert r.status_code == 400
    r = c.post('/api/redeem_reward', json={'item_id': item})
    assert r.status_code == 200, r.get_json()
    assert 'message_i18n' in r.get_json()
    r = c.post('/api/refund_reward_item', json={'item_id': item, 'quantity': 1})
    assert r.status_code == 200 and 'message_i18n' in r.get_json()


# ===========================================================================
# B-085：禮物卡扣款的鎖不更新同步時間戳 / 不標記同步
# ===========================================================================
def test_gift_card_charge_lock_does_not_touch_card_row(app):
    from app.services import giftcard_service
    tid = default_tenant_id(app)
    stamp = datetime.now() - timedelta(days=3)
    with app.app_context():
        card = GiftCard(tenant_id=tid, code='5512340000009999', initial_amount=300, created_at=datetime.now())
        db.session.add(card)
        db.session.flush()
        db.session.add(GiftCardLedger(tenant_id=tid, gift_card_id=card.id, delta=300, reason='issue'))
        db.session.commit()
        db.session.execute(db.update(GiftCard.__table__).where(GiftCard.__table__.c.id == card.id)
                           .values(updated_at=stamp))
        db.session.commit()
        cid = card.id
        rev = db.session.execute(db.select(GiftCard.__table__.c.sync_rev).where(GiftCard.__table__.c.id == cid)).scalar()
    with tenant_request(app, tid):
        giftcard_service.charge(tid, '5512340000009999', 100, None)
        db.session.commit()
        row = db.session.execute(db.select(GiftCard.__table__.c.updated_at, GiftCard.__table__.c.sync_rev)
                                 .where(GiftCard.__table__.c.id == cid)).one()
        assert row.updated_at == stamp, '扣款不可更新卡片的 updated_at (LWW 會把作廢蓋回)'
        assert row.sync_rev == rev, '扣款不可把卡片列標記為同步變更'
        assert giftcard_service.balance(db.session.get(GiftCard, cid)) == 200


# ===========================================================================
# B-093：禮物卡作廢後取消用該卡付款的訂單，不可把退款加到已作廢的卡
# ===========================================================================
def test_cancel_order_paid_by_voided_gift_card_is_refused(app, staff_client, admin_client, store_settings):
    store_settings('gift_card', enabled=True)
    r = staff_client.post('/api/pos/gift_cards/sell', json={'amount': 300, 'payment_method': 'Cash'})
    assert r.status_code == 200, r.get_json()
    code = r.get_json()['card']['code']
    item = make_item(app, '修正作廢卡餐點', price=180)
    r = staff_client.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': '禮物卡',
                                                    'order_type': '內用', 'payment_info': {'gift_code': code}})
    assert r.status_code == 200, r.get_json()
    oid = r.get_json()['order_id']
    with app.app_context():
        card_id = GiftCard.query.filter_by(code=code).one().id
    admin_client.post('/admin/gift_cards', data={'action': 'void', 'card_id': str(card_id), 'reason': '遺失'})
    r = cancel_order(admin_client, oid)
    with app.app_context():
        assert GiftCardLedger.query.filter_by(gift_card_id=card_id, reason='refund').count() == 0, '不可退回已作廢的卡'
        if r.status_code == 200:   # 呼叫端改記人工退款時：退款紀錄不可顯示「已完成」
            from app.models.payment import Refund
            assert Refund.query.filter_by(order_id=oid).one().status == 'manual'
        else:
            assert r.status_code == 409 and '作廢' in r.get_json()['message']


# ===========================================================================
# B-086：點數到期校正以當次的有效期限重算，修改設定不溯及既往
# ===========================================================================
def test_expiry_true_up_uses_months_recorded_at_expiry(app, store_settings):
    from app.services import loyalty_service
    tid = default_tenant_id(app)
    uid = make_member(app, '到期期限會員', '0977602005')
    with app.app_context():
        db.session.add(PointLedger(tenant_id=tid, user_id=uid, delta=100, reason='earn',
                                   created_at=datetime.now() - timedelta(days=400)))
        db.session.commit()
        from app.services.points_service import recompute_points
        recompute_points([uid])
        db.session.commit()
    store_settings('member', points_expire_months=12)
    with app.app_context():
        loyalty_service.expire_points(tid)
        entry = PointLedger.query.filter(PointLedger.user_id == uid, PointLedger.delta < 0).one()
        assert entry.reason == 'expire:12' and entry.delta == -100
        assert loyalty_service.true_up_expiry(tid, uid) == 0
    store_settings('member', points_expire_months=24)
    with app.app_context():
        assert loyalty_service.true_up_expiry(tid, uid) == 0, '改長有效期限不可把當年已到期的點數退回'
        db.session.commit()
    assert _points(app, uid) == 0
    store_settings('member', points_expire_months=0)


# ===========================================================================
# R2-032：集點卡獎勵券已過期 / 不存在時保留章數並通知店家
# ===========================================================================
def test_stamp_card_keeps_stamps_when_reward_coupon_invalid(app, store_settings, monkeypatch):
    from app.models.coupon import Coupon, UserCoupon
    from app.services import loyalty_service, monitor_service
    tid = default_tenant_id(app)
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda message, **kw: alerts.append(message) or True)
    with app.app_context():
        db.session.add(Coupon(tenant_id=tid, code='STAMPOLDFIX2', title='過期集點券', discount_type='fixed',
                              discount_value=50, min_spend=0, reward_points=0, is_reward=False, member_only=True,
                              valid_until=date.today() - timedelta(days=1)))
        db.session.commit()
    store_settings('stamps', enabled=True, card_size=3, mode='order', min_spend=0, reward_points=0,
                   reward_coupon_code='STAMPOLDFIX2')
    uid = make_member(app, '集點過期券會員', '0977602006')
    with tenant_request(app, tid):
        db.session.add(StampLedger(tenant_id=tid, user_id=uid, delta=2, reason='earn'))
        db.session.commit()
        user = db.session.get(User, uid)
        earned, completed = loyalty_service.earn_stamps(tid, user, SimpleNamespace(id=None), [], 100)
        db.session.commit()
        assert (earned, completed) == (1, 0)
        assert loyalty_service.stamp_count(uid) == 3, '獎勵券無法發放：保留章數'
        assert UserCoupon.query.filter_by(user_id=uid).count() == 0
    assert any('STAMPOLDFIX2' in a for a in alerts), alerts
    store_settings('stamps', enabled=False, reward_coupon_code='')


# ===========================================================================
# R2-028：後台禮物卡搜尋只比對完整卡號或看得到的前 4 / 末 4 碼
# ===========================================================================
def test_gift_card_search_does_not_match_masked_digits(app, admin_client):
    tid = default_tenant_id(app)
    with app.app_context():
        card = GiftCard(tenant_id=tid, code='8812345678904321', initial_amount=100, created_at=datetime.now())
        db.session.add(card)
        db.session.commit()
        cid = card.id
    marker = f'data-card-id="{cid}"'
    assert marker not in admin_client.get('/admin/gift_cards?q=88123').get_data(as_text=True), '不可子字串比對'
    assert marker not in admin_client.get('/admin/gift_cards?q=2345').get_data(as_text=True), '中間的遮罩碼不可比對'
    assert marker in admin_client.get('/admin/gift_cards?q=8812').get_data(as_text=True)
    assert marker in admin_client.get('/admin/gift_cards?q=4321').get_data(as_text=True)
    assert marker in admin_client.get('/admin/gift_cards?q=88124321').get_data(as_text=True)
    assert marker in admin_client.get('/admin/gift_cards?q=8812-3456-7890-4321').get_data(as_text=True)


# ===========================================================================
# R2-029：POS 售出寄杯以 request_id 去重
# ===========================================================================
def test_pass_sell_with_same_request_id_is_not_duplicated(app, staff_client, admin_client):
    from app.models.member import PassProduct
    item = make_item(app, '修正寄杯美式', price=80)
    r = admin_client.post('/admin/passes', data={'action': 'save', 'name': '修正寄杯去重', 'menu_item_id': item,
                                                 'uses': 3, 'price': 200})
    assert r.status_code == 302
    with app.app_context():
        product = PassProduct.query.filter_by(name='修正寄杯去重').one().id
    uid = make_member(app, '寄杯去重會員', '0977602007')
    payload = dict(user_id=uid, product_id=product, payment_method='Cash', cash_received=200,
                   request_id='fix2-pass-sell-0001')
    first = staff_client.post('/api/pos/passes/sell', json=payload)
    assert first.status_code == 200, first.get_json()
    again = staff_client.post('/api/pos/passes/sell', json=payload)
    assert again.status_code == 200, again.get_json()
    assert again.get_json()['order_id'] == first.get_json()['order_id'] and again.get_json()['replayed'] is True
    with app.app_context():
        assert MemberPass.query.filter_by(user_id=uid).count() == 1
