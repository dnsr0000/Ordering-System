"""跨檔請求處理者 pos-backend-1 的回歸測試 (app/routes/pos.py、member_verify_service)"""
from datetime import datetime

import pytest

from app.extensions import db
from tests.support import default_tenant_id, line, login_staff, make_item, make_member, tenant_request


def _today_orders(client):
    r = client.get('/api/pos/today_orders')
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


# ---------------------------------------------------------------------------
# R2-048：會員驗證以 (id, uuid) 為鍵，被刪除會員的 id 重用時不沿用驗證狀態
# ---------------------------------------------------------------------------
def test_member_verified_key_includes_uuid(app):
    from types import SimpleNamespace

    from app.services import member_verify_service
    with tenant_request(app, default_tenant_id(app)):
        old = SimpleNamespace(id=987654, uuid='old-uuid')
        new = SimpleNamespace(id=987654, uuid='new-uuid')
        member_verify_service.mark_verified(old)
        assert member_verify_service.is_verified(old)
        assert not member_verify_service.is_verified(new)


# ---------------------------------------------------------------------------
# B-358：PIN 驗證先預扣額度，錯一次只計一次
# ---------------------------------------------------------------------------
def test_member_pin_failure_counted_once(app, store_settings):
    from app.models.system import AuthThrottle
    store_settings('member', wallet_enabled=True, pos_verify=True)
    item = make_item(app, 'pbr 驗證餐點', price=50, stock=100)
    uid = make_member(app, 'pbr 會員', '0911377001', pin='8264', wallet_balance=500)
    pos = login_staff(app, 'pbr_cashier_pin', 'staff')
    r = pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': '儲值金', 'user_id': uid,
                                            'order_type': '內用', 'member_pin': '1357'})
    assert r.status_code == 403 and r.get_json().get('need_member_pin')
    with app.app_context():
        rows = AuthThrottle.query.filter(AuthThrottle.key.like('pin:%:0911377001%')).all()
        assert rows and all((row.fails or 0) <= 1 for row in rows)
    r = pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': '儲值金', 'user_id': uid,
                                            'order_type': '內用', 'member_pin': '8264'})
    assert r.status_code == 200, r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# B-021：order_type / payment_info 型別錯誤回 400 而不是 500
# ---------------------------------------------------------------------------
def test_pos_checkout_bad_types_not_500(app):
    item = make_item(app, 'pbr 型別餐點', price=60, stock=100)
    pos = login_staff(app, 'pbr_cashier_types', 'staff')
    r = pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': ['內用']})
    assert r.status_code == 400
    r = pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': '內用',
                                            'payment_info': ['x']})
    assert r.status_code < 500


# ---------------------------------------------------------------------------
# B-226 / B-239：即時同步回傳供應時段；R2-195：當日訂單仍正常回傳發票欄位
# ---------------------------------------------------------------------------
def test_today_orders_menu_items_in_service(app):
    today = str(datetime.now().isoweekday())
    other_days = ''.join(d for d in '1234567' if d != today)
    off = make_item(app, 'pbr 非供應日', price=80, stock=10, avail_days=other_days)
    on = make_item(app, 'pbr 供應中', price=80, stock=10)
    pos = login_staff(app, 'pbr_cashier_live', 'staff')
    data = _today_orders(pos)
    items = {m['id']: m for m in data['menu_items']}
    assert items[off]['in_service'] is False and items[off]['service_hint']
    assert items[on]['in_service'] is True
    assert all('invoice' in o and 'pay_at_pickup' in o for o in data['orders'])


# ---------------------------------------------------------------------------
# B-243：POS 設定帶出能否作廢已取餐訂單
# ---------------------------------------------------------------------------
def test_can_void_picked_up_flag(app):
    from flask_login import login_user

    from app.models.tenant import StaffAccount
    from app.routes.pos import pos_settings_payload
    login_staff(app, 'pbr_staff_void', 'staff')
    login_staff(app, 'pbr_manager_void', 'manager')
    for username, expected in (('pbr_staff_void', False), ('pbr_manager_void', True)):
        with tenant_request(app, default_tenant_id(app)):
            login_user(StaffAccount.query.filter_by(username=username).first())
            assert pos_settings_payload()['canVoidPickedUp'] is expected


# ---------------------------------------------------------------------------
# B-425：手動登錄不同「其他平台」的相同單號不互相擋
# ---------------------------------------------------------------------------
def test_delivery_other_platforms_same_ref(app):
    item = make_item(app, 'pbr 外送餐點', price=90, stock=100)
    pos = login_staff(app, 'pbr_delivery', 'manager')
    r1 = pos.post('/api/pos/delivery_order', json={'platform': 'Lalamove', 'external_ref': 'PBR1001', 'items': [line(item)]})
    assert r1.status_code == 200, r1.get_data(as_text=True)
    r2 = pos.post('/api/pos/delivery_order', json={'platform': 'GrabFood', 'external_ref': 'PBR1001', 'items': [line(item)]})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    r3 = pos.post('/api/pos/delivery_order', json={'platform': 'GrabFood', 'external_ref': 'PBR1001', 'items': [line(item)]})
    assert r3.status_code == 409


# ---------------------------------------------------------------------------
# B-005 / B-440：到店付款訂單由 POS 收款
# ---------------------------------------------------------------------------
def _unpaid_pickup_order(app, pos, item):
    from app.models.order import Order
    r = pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': '外帶',
                                            'cash_received': 1000})
    assert r.status_code == 200, r.get_data(as_text=True)
    oid = r.get_json()['order_id']
    with app.app_context():
        o = db.session.get(Order, oid)
        o.payment_status, o.dining_table_id = 'unpaid', None   # 模擬團體預購 / 不收線上付款的到店付款訂單
        db.session.commit()
        return oid, o.total_price


def test_settle_pay_at_pickup_order(app):
    from app.models.order import Order
    item = make_item(app, 'pbr 到店付款餐點', price=120, stock=100)
    pos = login_staff(app, 'pbr_pickup_pay', 'manager')
    oid, total = _unpaid_pickup_order(app, pos, item)
    listed = {o['id']: o for o in _today_orders(pos)['orders']}
    assert listed[oid]['pay_at_pickup'] is True
    r = pos.post(f'/api/pos/orders/{oid}/settle', json={'payment_method': 'Cash', 'cash_received': total,
                                                         'expected_total': total + 1})
    assert r.status_code == 409
    r = pos.post(f'/api/pos/orders/{oid}/settle', json={'payment_method': 'Cash', 'cash_received': total,
                                                         'expected_total': total})
    assert r.status_code == 200, r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Order, oid).payment_status == 'settled'
    assert pos.post(f'/api/pos/orders/{oid}/settle', json={'payment_method': 'Cash', 'cash_received': total}).status_code == 409


# ---------------------------------------------------------------------------
# B-231：部分退款以 request_id 去重 (Refund.request_id 欄位存在時)
# ---------------------------------------------------------------------------
def test_partial_refund_replay(app):
    from app.models.payment import Refund
    if getattr(Refund, 'request_id', None) is None:
        pytest.skip('Refund.request_id 欄位尚未加入')
    item = make_item(app, 'pbr 退款餐點', price=300, stock=100)
    pos = login_staff(app, 'pbr_refund_owner', 'owner')
    r = pos.post('/api/pos/checkout', json={'items': [line(item)], 'payment_method': 'Cash', 'order_type': '內用',
                                            'cash_received': 300})
    oid = r.get_json()['order_id']
    body = {'amount': 50, 'reason': '少出餐', 'request_id': 'pbr-refund-1'}
    first = pos.post(f'/api/pos/orders/{oid}/refund', json=body)
    assert first.status_code == 200, first.get_data(as_text=True)
    again = pos.post(f'/api/pos/orders/{oid}/refund', json=body)
    assert again.status_code == 200 and again.get_json()['replayed'] is True
    with app.app_context():
        assert Refund.query.filter_by(order_id=oid).count() == 1


# ---------------------------------------------------------------------------
# R2-029：禮物卡售出以 request_id 去重 (GiftCard.request_id 欄位存在時)
# ---------------------------------------------------------------------------
def test_gift_card_sell_replay(app, store_settings):
    from app.models.member import GiftCard
    if getattr(GiftCard, 'request_id', None) is None:
        pytest.skip('GiftCard.request_id 欄位尚未加入')
    store_settings('gift_card', enabled=True)
    pos = login_staff(app, 'pbr_gift_owner', 'owner')
    body = {'amount': 500, 'payment_method': 'Cash', 'request_id': 'pbr-gift-0001'}
    first = pos.post('/api/pos/gift_cards/sell', json=body)
    assert first.status_code == 200, first.get_data(as_text=True)
    again = pos.post('/api/pos/gift_cards/sell', json=body)
    assert again.status_code == 200 and again.get_json()['replayed'] is True
    assert again.get_json()['card']['code'] == first.get_json()['card']['code']
    other = login_staff(app, 'pbr_gift_other', 'owner')
    assert other.post('/api/pos/gift_cards/sell', json=body).status_code == 409
