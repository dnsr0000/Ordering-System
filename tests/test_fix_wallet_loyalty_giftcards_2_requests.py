"""
跨檔請求處理者 wallet-loyalty-giftcards-2 的回歸測試：人臉登入兌換紅利要 PIN (B-010)、兌換憑證記入工作階段 (B-128)、
禮物卡售出的金額 / 付款方式 / 開班檢查 (B-099 / B-100 / B-095 / B-113)、點數到期提醒先 commit 再推播 (B-342)。
"""
from datetime import datetime, timedelta

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.user import RewardRedemption, User
from tests.support import default_tenant_id, make_item, make_member, member_client, tenant_request


def _reward_item(app, name, cost=10):
    return make_item(app, name, price=50, is_reward=True, reward_points=cost)


# ===========================================================================
# B-010：人臉登入 (弱因子) 兌換紅利前要再輸入 PIN
# ===========================================================================
def test_face_login_redeem_reward_requires_pin(app):
    uid = make_member(app, '人臉兌換會員', '0977612001', points=100, pin='2580')
    item_id = _reward_item(app, '人臉兌換雞塊')
    c = member_client(app, '0977612001', '2580', ip='10.98.12.1')
    with c.session_transaction() as sess:
        sess['auth_factor'] = 'face'
    r = c.post('/api/redeem_reward', json={'item_id': item_id})
    assert r.status_code == 403 and r.get_json().get('need_pin'), r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, uid).points == 100, '未確認 PIN 不可扣點'
    r = c.post('/api/redeem_reward', json={'item_id': item_id, 'pin': '0000'}, headers={'X-Forwarded-For': '10.98.12.1'})
    assert r.status_code == 403
    r = c.post('/api/redeem_reward', json={'item_id': item_id, 'pin': '2580'}, headers={'X-Forwarded-For': '10.98.12.1'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, uid).points == 90


def test_pin_login_redeem_reward_needs_no_pin(app):
    make_member(app, 'PIN 兌換會員', '0977612002', points=100, pin='2580')
    item_id = _reward_item(app, 'PIN 兌換雞塊')
    c = member_client(app, '0977612002', '2580', ip='10.98.12.2')
    assert c.post('/api/redeem_reward', json={'item_id': item_id}).status_code == 200


# ===========================================================================
# B-128：兌換的憑證記入工作階段 (分店 / 線上端登出時只自動退還這些)
# ===========================================================================
def test_redeemed_voucher_remembered_in_session(app):
    uid = make_member(app, '憑證記錄會員', '0977612003', points=100, pin='2580')
    item_id = _reward_item(app, '憑證記錄雞塊')
    c = member_client(app, '0977612003', '2580', ip='10.98.12.3')
    r = c.post('/api/redeem_reward', json={'item_id': item_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    with app.app_context():
        voucher = RewardRedemption.query.filter_by(user_id=uid).one()
        vuuid = voucher.uuid
    with c.session_transaction() as sess:
        assert vuuid in (sess.get('voucher_uuids') or [])


# ===========================================================================
# B-099 / B-113 / B-100 / B-095：禮物卡售出
# ===========================================================================
@pytest.fixture
def giftcards_on(app, monkeypatch):
    from app.services import giftcard_service
    monkeypatch.setattr(giftcard_service, 'enabled', lambda tenant_id: True)
    return default_tenant_id(app)


@pytest.mark.parametrize('amount', ['abc', '1,000', '', None])
def test_giftcard_sell_non_numeric_amount_is_400(app, giftcards_on, amount):
    from app.services import giftcard_service
    with tenant_request(app, giftcards_on):
        with pytest.raises(AppError) as exc:
            giftcard_service.sell(giftcards_on, amount, 'Cash', 'tester')
        assert exc.value.status == 400
        db.session.rollback()


@pytest.mark.parametrize('method', ['混合付款', '儲值金', '禮物卡', '平台代收', 'ATM 轉帳', '超商代碼', 'Bitcoin'])
def test_giftcard_sell_rejects_non_cash_like_methods(app, giftcards_on, method):
    from app.services import giftcard_service
    with tenant_request(app, giftcards_on):
        with pytest.raises(AppError) as exc:
            giftcard_service.sell(giftcards_on, 100, method, 'tester')
        assert '付款方式' in str(exc.value)
        db.session.rollback()


def test_giftcard_sell_requires_open_shift_when_configured(app, giftcards_on, monkeypatch):
    from app.services import giftcard_service, payment_service, settings_service
    real_get = settings_service.get

    def fake_get(section, tenant_id=None, *a, **kw):
        cfg = real_get(section, tenant_id, *a, **kw)
        return {**cfg, 'require_shift': True} if section == 'pos' else cfg

    monkeypatch.setattr(settings_service, 'get', fake_get)
    monkeypatch.setattr(payment_service, 'open_shift_id', lambda tenant_id, lock=False: None)
    with tenant_request(app, giftcards_on):
        with pytest.raises(AppError) as exc:
            giftcard_service.sell(giftcards_on, 100, 'Cash', 'tester')
        assert exc.value.status == 409
        db.session.rollback()


# ===========================================================================
# B-342：點數到期提醒先 commit 再推播
# ===========================================================================
def test_notify_expiring_commits_before_push(app, monkeypatch):
    from app.models.sync import PointLedger
    from app.services import line_service, loyalty_service, settings_service
    tid = default_tenant_id(app)
    # 點數只來自一年前的贈點 (make_member 帶 points 會再自動寫一筆 initial 異動，合計會變成 60)
    uid = make_member(app, '到期提醒會員', '0977612004', line_user_id='U-fix-expire-2')
    now = datetime.now()
    with app.app_context():
        from app.services.points_service import recompute_points
        db.session.add(PointLedger(tenant_id=tid, user_id=uid, delta=30, reason='earn',
                                   created_at=now - timedelta(days=360)))
        db.session.commit()
        recompute_points([uid])
        db.session.commit()
    real_get = settings_service.get

    def fake_get(section, tenant_id=None, *a, **kw):
        cfg = real_get(section, tenant_id, *a, **kw)
        return {**cfg, 'points_expiry_notice_days': 30} if section == 'member' else cfg

    monkeypatch.setattr(settings_service, 'get', fake_get)
    monkeypatch.setattr(loyalty_service, '_expire_months', lambda tenant_id: 12)
    states = []

    def fake_push(tid_, to, text):
        if to == 'U-fix-expire-2':
            states.append((db.session.new or db.session.dirty, text))
        return True

    monkeypatch.setattr(line_service, 'push_text', fake_push)
    with app.app_context():
        assert loyalty_service.notify_expiring(tid, now) >= 1
        assert states, '應推播提醒'
        pending, text = states[0]
        assert not pending, '推播時不可仍有未 commit 的變更 (持有寫入鎖)'
        assert '30 點' in text
        assert db.session.get(User, uid).points_notice_on == now.date()
