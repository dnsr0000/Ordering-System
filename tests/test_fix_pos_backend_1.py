"""修正小組 pos-backend-1 的回歸測試：POS 會員驗證 / 主管核准 / 交班 (app/routes/pos.py、shift_service、approval_service、member_verify_service)"""
import pytest

from app.extensions import db
from app.models.shift import CashShift
from tests.support import checkout, line, login_staff, make_item, make_member

POS = '/api/pos/checkout'
ADMIN = {'approver_username': 'admin_test', 'approver_password': 'AdminPass!2026'}


@pytest.fixture
def no_open_shift(app):
    """確保測試開始與結束時沒有開著的班 (其他測試不受影響)"""
    def close_all():
        with app.app_context():
            for s in CashShift.query.filter_by(status='open').all():
                s.status = 'closed'
            db.session.commit()
    close_all()
    yield
    close_all()


@pytest.fixture
def verify_setup(app, store_settings):
    """開啟儲值金與 POS 會員驗證；回傳 (品項, 有 PIN 的會員)"""
    store_settings('member', wallet_enabled=True, pos_verify=True)
    from app.models.menu import MenuItem
    from app.models.user import User
    from app.tenant_context import tenant_bypass
    with app.app_context():
        with tenant_bypass():
            item = MenuItem.query.filter_by(name='pb1 驗證餐點').first()
            item = item.id if item else None
            user = User.query.filter_by(phone='0911300001').first()
            if user is not None:
                user.wallet_balance = 1000
                db.session.commit()
            uid = user.id if user else None
    item = item or make_item(app, 'pb1 驗證餐點', price=100, stock=500)
    uid = uid or make_member(app, 'pb1 儲值會員', '0911300001', pin='8264', wallet_balance=1000)
    return item, uid


def _wallet(app, uid):
    from app.models.user import User
    with app.app_context():
        return db.session.get(User, uid).wallet_balance


# ---------------------------------------------------------------------------
# B-103：付款方式前後空白不能繞過會員驗證
# ---------------------------------------------------------------------------
def test_padded_stored_value_still_requires_verification(app, verify_setup):
    item, uid = verify_setup
    staff = login_staff(app, 'pb1_cashier_b103', 'staff')
    before = _wallet(app, uid)
    for method in (' 儲值金', '儲值金 ', '\t儲值金'):
        r = staff.post(POS, json={'items': [line(item)], 'payment_method': method, 'user_id': uid, 'order_type': '內用'})
        assert r.status_code == 403 and r.get_json().get('need_member_pin'), method
    r = staff.post(POS, json={'items': [line(item)], 'payment_method': '混合付款', 'user_id': uid, 'order_type': '內用',
                              'payment_info': {'tenders': [{'method': '儲值金 ', 'amount': 50},
                                                           {'method': 'Cash', 'amount': 50}]}})
    assert r.status_code == 403 and r.get_json().get('need_member_pin')
    assert _wallet(app, uid) == before


def test_non_string_approver_username_is_not_500(app, verify_setup):
    """B-113：approver_username 不是字串時不再 AttributeError (回 403 要求主管核准)"""
    item, uid = verify_setup
    staff = login_staff(app, 'pb1_cashier_b113', 'staff')
    r = staff.post(POS, json={'items': [line(item)], 'payment_method': '儲值金', 'user_id': uid, 'order_type': '內用',
                              'approver_username': 123})
    assert r.status_code == 403 and r.get_json().get('need_approval')


# ---------------------------------------------------------------------------
# B-104：覆寫會員既有 PIN 需主管核准
# ---------------------------------------------------------------------------
def test_reset_existing_member_pin_needs_approval(app):
    staff = login_staff(app, 'pb1_cashier_b104', 'staff')
    uid = make_member(app, 'pb1 有PIN會員', '0911300104', pin='8264')
    r = staff.post('/api/pos/set_member_pin', json={'user_id': uid, 'pin': '3816'})
    assert r.status_code == 403 and r.get_json().get('need_approval')
    from app.models.user import User
    with app.app_context():
        assert db.session.get(User, uid).check_pin('8264')   # 沒被覆寫
    r = staff.post('/api/pos/set_member_pin', json={'user_id': uid, 'pin': '3816', **ADMIN})
    assert r.status_code == 200, r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, uid).check_pin('3816')
    # 尚未設定 PIN 的會員：店員可直接協助設定
    uid2 = make_member(app, 'pb1 無PIN會員', '0911300105')
    assert staff.post('/api/pos/set_member_pin', json={'user_id': uid2, 'pin': '3816'}).status_code == 200


# ---------------------------------------------------------------------------
# B-105：關閉會員等級時 POS 不顯示等級折扣
# ---------------------------------------------------------------------------
def test_member_dict_hides_tier_when_tiers_disabled(app, staff_client, store_settings):
    from app.models.member import MemberTier
    from app.models.user import User
    from tests.support import default_tenant_id
    uid = make_member(app, 'pb1 等級會員', '0911300106')
    with app.app_context():
        tier = MemberTier(tenant_id=default_tenant_id(app), name='pb1 金卡', min_spend=999999, discount_percent=10)
        db.session.add(tier)
        db.session.flush()
        db.session.get(User, uid).tier_id = tier.id
        db.session.commit()
    store_settings('member', tiers_enabled=True)
    m = staff_client.post('/api/pos/member_lookup', json={'member_id': uid}).get_json()['member']
    assert (m['tier'], m['tier_discount']) == ('pb1 金卡', 10)
    store_settings('member', tiers_enabled=False)
    m = staff_client.post('/api/pos/member_lookup', json={'member_id': uid}).get_json()['member']
    assert (m['tier'], m['tier_discount']) == ('', 0)


# ---------------------------------------------------------------------------
# B-106：必須先開班時，禮物卡售出與退款也要有開著的班
# ---------------------------------------------------------------------------
def test_require_shift_blocks_gift_card_sale_and_refund(app, staff_client, admin_client, store_settings, no_open_shift):
    iid = make_item(app, 'pb1 退款餐', price=100, stock=50)
    order = checkout(staff_client, [line(iid)], url=POS).get_json()
    store_settings('pos', require_shift=True)
    r = staff_client.post('/api/pos/gift_cards/sell', json={'amount': 500, 'payment_method': 'Cash'})
    assert r.status_code == 409 and r.get_json().get('need_shift')
    r = admin_client.post(f"/api/pos/orders/{order['order_id']}/refund", json={'amount': 10, 'reason': '測試'})
    assert r.status_code == 409 and r.get_json().get('need_shift')
    assert staff_client.post('/api/pos/shift/open', json={'opening_cash': 1000}).status_code == 200
    r = admin_client.post(f"/api/pos/orders/{order['order_id']}/refund", json={'amount': 10, 'reason': '測試'})
    assert r.status_code == 200, r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# B-107：啟用 2FA 的主管核准成功時不清除錯誤次數
# ---------------------------------------------------------------------------
def _make_manager(app, username, totp):
    from app.models.tenant import StaffAccount
    from tests.support import default_tenant_id
    with app.app_context():
        acc = StaffAccount.query.filter_by(username=username).first()
        if acc is None:
            acc = StaffAccount(tenant_id=default_tenant_id(app), username=username, role='manager', is_active=True)
            acc.set_password('MgrPass!2026')
            db.session.add(acc)
        acc.totp_enabled = totp
        db.session.commit()


def _throttle_exists(app, key):
    from app.models.system import AuthThrottle
    with app.app_context():
        return db.session.get(AuthThrottle, key) is not None


@pytest.mark.parametrize('totp', [True, False])
def test_approval_keeps_throttle_for_2fa_accounts(app, totp):
    from app.services import auth_throttle
    username = f'pb1_mgr_{"totp" if totp else "plain"}'
    _make_manager(app, username, totp)
    keys = auth_throttle.staff_keys(username, '127.0.0.1')
    with app.app_context():
        auth_throttle.record_staff_failure(keys)
    staff = login_staff(app, 'pb1_cashier_b107', 'staff')
    r = staff.post('/api/pos/approval_check', json={'permission': 'discount', 'approver_username': username,
                                                    'approver_password': 'MgrPass!2026'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _throttle_exists(app, keys['account']) is totp
    with app.app_context():
        auth_throttle.clear_keys(*keys.values())


# ---------------------------------------------------------------------------
# B-111：為會員驗證輸入的主管帳密不會默默核准整單折扣
# ---------------------------------------------------------------------------
def test_member_verify_approval_does_not_approve_discount(app, verify_setup):
    item, uid = verify_setup
    staff = login_staff(app, 'pb1_cashier_b111', 'staff')
    payload = {'items': [line(item)], 'payment_method': '儲值金', 'user_id': uid, 'order_type': '內用',
               'order_discount': {'type': 'amount', 'value': 10}, **ADMIN}
    before = _wallet(app, uid)
    r = staff.post(POS, json=payload)
    assert r.status_code == 403 and r.get_json().get('need_approval'), r.get_data(as_text=True)
    assert r.get_json().get('permission') == 'discount' and '折扣' in r.get_json()['message']
    assert _wallet(app, uid) == before
    # 主管看到折扣提示後再次核准：會員已記為驗證過，帳密用於整單折扣
    r = staff.post(POS, json=payload)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _wallet(app, uid) == before - 90


# ---------------------------------------------------------------------------
# B-110 / B-112 / B-113 / R2-042：交班金額驗證、面額、零用金遇到關班
# ---------------------------------------------------------------------------
def test_shift_amount_validation_and_denominations(app, staff_client, no_open_shift, monkeypatch):
    for bad in ('abc', '1,000', [1], 3000000000, -1):
        r = staff_client.post('/api/pos/shift/open', json={'opening_cash': bad})
        assert r.status_code == 400, (bad, r.status_code)
    assert staff_client.post('/api/pos/shift/open', json={'opening_cash': 1000}).status_code == 200
    for bad in ('1,000', 'x', 20000000):
        r = staff_client.post('/api/pos/shift/movement', json={'amount': bad, 'kind': 'pay_out', 'note': '冰塊'})
        assert r.status_code == 400, (bad, r.status_code)
    for body in ({'denominations': [1]}, {'denominations': {'100': 'x'}}, {'counted_cash': 'x'},
                 {'counted_cash': -5}, {'counted_cash': 3000000000}):
        r = staff_client.post('/api/pos/shift/close', json=body)
        assert r.status_code == 400, (body, r.status_code)

    # 零用金登記後剛好被另一台 POS 關班：回 shift: null 而不是 500
    from app.services import shift_service
    real = shift_service.current_shift
    monkeypatch.setattr(shift_service, 'current_shift', lambda tid: None)
    r = staff_client.post('/api/pos/shift/movement', json={'amount': 30, 'kind': 'pay_out', 'note': '冰塊'})
    assert r.status_code == 200 and r.get_json()['shift'] is None
    monkeypatch.setattr(shift_service, 'current_shift', real)

    denoms = staff_client.get('/api/pos/shift').get_json()['denominations']
    assert 2000 in denoms and 20 in denoms
    r = staff_client.post('/api/pos/shift/close', json={'denominations': {'2000': 1, '20': 2}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['report']['counted_cash'] == 2040


# ---------------------------------------------------------------------------
# R2-040 / R2-043：交班報表只算已收款訂單、列出外送費
# ---------------------------------------------------------------------------
def test_shift_report_counts_paid_orders_and_delivery_fee(app, staff_client, no_open_shift):
    from app.models.order import Order
    from app.services import shift_service
    assert staff_client.post('/api/pos/shift/open', json={'opening_cash': 1000}).status_code == 200
    iid = make_item(app, 'pb1 交班餐', price=200, stock=50)
    paid = checkout(staff_client, [line(iid)], url=POS).get_json()['order_id']
    unpaid = checkout(staff_client, [line(iid)], url=POS).get_json()['order_id']
    with app.app_context():
        db.session.get(Order, unpaid).payment_status = 'unpaid'   # 模擬桌邊未結帳的訂單
        o = db.session.get(Order, paid)
        o.delivery_fee, o.total_price = 60, o.total_price + 60     # 外送到府
        db.session.commit()
        shift = shift_service.current_shift(o.tenant_id)
        report = shift_service.shift_report(shift)
        assert report['order_count'] == 1 and report['net_sales'] == 260
        assert (report['unpaid_count'], report['unpaid_total']) == (1, 200)
        assert report['delivery_fee'] == 60
        assert report['gross'] - report['discounts'] + report['service_charge'] + report['delivery_fee'] == report['net_sales']
        labels = [x[0] for x in shift_service.report_lines(report) if isinstance(x, tuple)]
        assert '外送費' in labels and '未結帳' in labels


# ---------------------------------------------------------------------------
# R2-041：線上端的 POS 收銀 / 外送單登錄回明確訊息
# ---------------------------------------------------------------------------
def test_hub_pos_checkout_rejected_with_clear_message(app, staff_client, monkeypatch):
    from app.sync import runtime as sync_runtime
    iid = make_item(app, 'pb1 線上端餐', price=100, stock=50)
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    r = checkout(staff_client, [line(iid)], url=POS)
    assert r.status_code == 404 and '門市 POS' in r.get_json()['message']
