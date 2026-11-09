"""
修正小組 customer-backend-1 的回歸測試 (app/routes/customer.py、app/routes/member_auth.py)：
- B-115 / B-132 線上端 (開啟線上付款) 不列儲值金與點餐機付款方式
- B-119 會員失效時整組顧客工作階段一併結束
- B-121 /api/verify_promo 回傳 message_i18n
- B-122 人臉登入的工作階段動用儲值金 / 點數需 PIN
- B-123 即時更新附上會員價
- B-124 LINE 電子收據與 Email 共用每張訂單每天 5 次的上限
- B-133 未註冊的用戶端補送暫存單照常套用暫停接單；B-134 同一次進場的會員訂單可當成暫存單回傳
- R2-046 取餐分店暫時離線時輪詢不改選別家
- B-116 人臉登入不可綁定 LINE；B-135 未開註冊簡訊驗證不發註冊驗證碼
- R2-045 LINE 待綁定：回到點餐首頁作廢、註冊需帶一次性權杖；R2-047 門市 LINE 登入不自動登入、記為 line_kiosk
"""
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.user import User
from tests.support import (checkout, default_tenant_id, guest_client, line, login_staff, make_item, make_member,
                           member_client, tenant_request)


def _face_session(app, phone):
    c = app.test_client()
    c.get('/?shop=default')
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            uid = User.query.filter_by(phone=phone).first().id
    with c.session_transaction() as sess:
        sess.update(user_id=uid, user_name='人臉', is_guest=False, auth_factor='face')
    return c


# ---------------------------------------------------------------------------
# B-115 / B-132
# ---------------------------------------------------------------------------
def test_hub_online_payment_lists_no_wallet_or_cash(app, store_settings, monkeypatch):
    from app.routes.customer import _storefront_extras
    store_settings('member', wallet_enabled=True)
    uid = make_member(app, 'CB1 線上儲值', '0937100001', wallet_balance=500)
    with tenant_request(app, default_tenant_id(app)):
        member = db.session.get(User, uid)
        monkeypatch.setitem(app.config, 'ONLINE_PAYMENT', True)
        pro = _storefront_extras(member, None, is_hub=True)
        assert pro['methods'] == ['Credit Card'] and pro['onlinePayment'] is True and pro['walletEnabled'] is False
        # 線上端不收線上付款 (到店付)：沿用點餐機付款方式，可用儲值金
        monkeypatch.setitem(app.config, 'ONLINE_PAYMENT', False)
        pro = _storefront_extras(member, None, is_hub=True)
        assert '儲值金' in pro['methods'] and pro['onlinePayment'] is False and pro['walletEnabled'] is True
        # 門市點餐機不受影響
        pro = _storefront_extras(member, None, is_hub=False)
        assert '儲值金' in pro['methods'] and pro['onlinePayment'] is False


# ---------------------------------------------------------------------------
# B-119
# ---------------------------------------------------------------------------
def test_deleted_member_closes_whole_customer_session(app):
    c = app.test_client()
    c.get('/?shop=default')
    with c.session_transaction() as sess:
        sess.update(user_id=987654321, user_name='已刪除會員', is_guest=False, customer_log_id=987654321,
                    auth_factor='pin')
    r = c.get('/')
    assert r.status_code == 302
    with c.session_transaction() as sess:
        for key in ('user_id', 'user_name', 'is_guest', 'customer_log_id', 'auth_factor'):
            assert key not in sess, key


# ---------------------------------------------------------------------------
# B-121
# ---------------------------------------------------------------------------
def test_verify_promo_returns_message_i18n(app):
    c = guest_client(app)
    body = c.post('/api/verify_promo', json={'promo_code': 'CB1-NO-SUCH', 'subtotal': 100}).get_json()
    assert body['valid'] is False and 'message_i18n' in body and body['message']


# ---------------------------------------------------------------------------
# B-122
# ---------------------------------------------------------------------------
def test_face_session_needs_pin_for_wallet_and_points(app, store_settings):
    store_settings('member', wallet_enabled=True)
    item = make_item(app, 'CB1 人臉儲值餐', price=60)
    make_member(app, 'CB1 人臉會員', '0937100002', pin='7391', points=500, wallet_balance=300, email='face@example.com')
    c = _face_session(app, '0937100002')
    r = checkout(c, [line(item)], payment_method='儲值金')
    assert r.status_code == 403 and r.get_json()['need_pin'] is True
    r = checkout(c, [line(item)], use_points=10)
    assert r.status_code == 403 and r.get_json()['need_pin'] is True
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            assert User.query.filter_by(phone='0937100002').first().wallet_balance == 300
    # 人臉登入不預填會員 Email
    assert 'face@example.com' not in c.get('/').get_data(as_text=True)
    # 一般現金結帳不需 PIN
    assert checkout(c, [line(item)]).status_code == 200
    # 輸入正確 PIN 後可用儲值金 (工作階段升級)
    r = checkout(c, [line(item)], payment_method='儲值金', pin='7391')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert checkout(c, [line(item)], payment_method='儲值金').status_code == 200


# ---------------------------------------------------------------------------
# B-123
# ---------------------------------------------------------------------------
def test_availability_includes_member_prices(app):
    item = make_item(app, 'CB1 會員價餐', price=120, member_price=100)
    body = guest_client(app).get('/api/menu/availability').get_json()
    assert body[str(item)]['member_prices'] == {'dine_in': 100, 'takeout': 100, 'online': 100, 'delivery': 100}
    assert body[str(item)]['prices']['dine_in'] == 120


# ---------------------------------------------------------------------------
# B-124
# ---------------------------------------------------------------------------
def test_line_receipt_shares_daily_limit_with_email(app, store_settings, monkeypatch):
    from app.services import line_service
    store_settings('line', messaging_enabled=True, messaging_access_token='tok-cb1')
    store_settings('receipt', email_enabled=True, line_enabled=True, auto_line=False)
    monkeypatch.setattr(line_service, 'push_text', lambda *a, **k: True)
    item = make_item(app, 'CB1 收據餐', price=40)
    make_member(app, 'CB1 LINE 收據', '0937100003', pin='7391', line_user_id='U-cb1-receipt')
    m = member_client(app, '0937100003', '7391', ip='10.37.0.3')
    oid = checkout(m, [line(item)]).get_json()['order_id']
    for _ in range(5):
        assert m.post(f'/api/my_orders/{oid}/receipt/line', json={}).status_code == 200
    assert m.post(f'/api/my_orders/{oid}/receipt/line', json={}).status_code == 429
    assert m.post(f'/api/my_orders/{oid}/receipt/email', json={'email': 'x@example.com'}).status_code == 429


# ---------------------------------------------------------------------------
# B-133 / B-134
# ---------------------------------------------------------------------------
def _offline_body(item, cid=None):
    return {'shop': 'default', 'code': 'CB12', 'queued_at': datetime.now().isoformat(timespec='seconds'),
            'order': {'client_order_id': cid or str(uuid.uuid4()), 'items': [line(item)], 'order_type': '內用'}}


def _kiosk_device_cookie(app):
    from flask import g
    from app.services import device_service
    tid = default_tenant_id(app)
    with app.app_context():
        g.tenant_id = tid
        d = device_service.create(tid, 'CB1 點餐機', 'kiosk')
        db.session.commit()
        d, raw = device_service.pair(tid, d.pair_code)
        db.session.commit()
        return raw


def test_offline_replay_intake_pause_only_bypassed_by_registered_kiosk(app, store_settings):
    item = make_item(app, 'CB1 暫存餐', price=50)
    pos = login_staff(app, 'cb1_intake', 'staff')
    store_settings('order_intake', paused=False, resume_at='')   # 記下原本的設定 (測試結束後還原)
    try:
        assert pos.post('/api/pos/intake', json={'paused': True, 'minutes': 15}).status_code == 200
        r = guest_client(app).post('/api/kiosk/offline_order', json=_offline_body(item))
        assert r.status_code == 409 and r.get_json().get('intake_paused') is True
        kiosk = app.test_client()
        kiosk.set_cookie('kiosk_device', _kiosk_device_cookie(app))
        kiosk.get('/?shop=default')
        r = kiosk.post('/api/kiosk/offline_order', json=_offline_body(item))
        assert r.status_code == 200, r.get_data(as_text=True)
    finally:
        store_settings('order_intake', paused=False, resume_at='')


def test_offline_replay_returns_member_order_of_same_visit(app):
    item = make_item(app, 'CB1 會員暫存餐', price=70)
    make_member(app, 'CB1 暫存會員', '0937100004', pin='7391')
    m = member_client(app, '0937100004', '7391', ip='10.37.0.4')
    cid = str(uuid.uuid4())
    resp = checkout(m, [line(item)], client_order_id=cid)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    oid = resp.get_json()["order_id"]
    # 同一次進場 (回應遺失後轉成暫存單補送)：回傳原訂單
    body = m.post('/api/kiosk/offline_order', json=_offline_body(item, cid)).get_json()
    assert body['replayed'] is True and body['order_id'] == oid
    # 別人拿這個編號查不到
    assert guest_client(app).post('/api/kiosk/offline_order', json=_offline_body(item, cid)).status_code == 409


# ---------------------------------------------------------------------------
# R2-046
# ---------------------------------------------------------------------------
def test_pickup_node_not_switched_by_poll_when_chosen_branch_offline(app):
    from flask import get_flashed_messages, session
    from app.routes.customer import PICKUP_NODE_KEY, _pickup_node
    a, b = SimpleNamespace(uuid='node-a', name='A 店'), SimpleNamespace(uuid='node-b', name='B 店')
    with tenant_request(app, default_tenant_id(app)):
        session[PICKUP_NODE_KEY] = 'node-a'
        assert _pickup_node([a, b]) is a
        # A 暫時離線：輪詢不改 session、不改選 B
        assert _pickup_node([b], adjust=False) is None
        assert session[PICKUP_NODE_KEY] == 'node-a'
        # 重新載入點餐頁：只剩 B 時改選並提示
        assert _pickup_node([b]) is b and session[PICKUP_NODE_KEY] == 'node-b'
        assert any('B 店' in m for m in get_flashed_messages())
        # 沒選過、只有一家：自動選取 (不提示)
        session.pop(PICKUP_NODE_KEY)
        assert _pickup_node([a], adjust=False) is a and session[PICKUP_NODE_KEY] == 'node-a'


# ---------------------------------------------------------------------------
# B-116 / R2-047
# ---------------------------------------------------------------------------
@pytest.fixture
def line_login(monkeypatch, store_settings):
    from app.services import line_service
    store_settings('line', login_enabled=True, login_channel_id='1650000099', login_channel_secret='cb1-secret')
    state = {'sub': 'U' + 'e' * 32}

    def fake_post(url, **kwargs):
        if url == line_service.TOKEN_URL:
            return 200, {'id_token': 'jwt', 'access_token': 'x'}
        if url == line_service.VERIFY_URL:
            return 200, {'sub': state['sub'], 'name': 'LINE 攻擊者', 'nonce': kwargs['data']['nonce']}
        return 200, {}
    monkeypatch.setattr(line_service, '_post', fake_post)
    return state


def test_face_session_cannot_bind_line(app, line_login):
    make_member(app, 'CB1 人臉 LINE', '0937100005', pin='7391')
    c = _face_session(app, '0937100005')
    r = c.get('/auth/line')
    assert r.status_code == 302 and 'access.line.me' not in r.headers['Location']
    # 直接帶 state 呼叫 callback 也不綁定
    tid = default_tenant_id(app)
    with c.session_transaction() as sess:
        sess['line_login'] = {'state': 'st-cb1', 'nonce': 'n-cb1', 'tenant_id': tid}
    c.get('/auth/line/callback?code=ok&state=st-cb1')
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            assert User.query.filter_by(phone='0937100005').first().line_user_id == ''


def test_store_line_login_disables_auto_login_and_marks_kiosk_factor(app, line_login):
    line_login['sub'] = 'U' + 'f' * 32
    make_member(app, 'CB1 LINE 會員', '0937100006', pin='7391', line_user_id='U' + 'f' * 32)
    c = app.test_client()
    c.get('/?shop=default')
    r = c.get('/auth/line')
    loc = r.headers['Location']
    assert loc.startswith('https://access.line.me/') and 'disable_auto_login=true' in loc
    with c.session_transaction() as sess:
        state = sess['line_login']['state']
    c.get(f'/auth/line/callback?code=ok&state={state}')
    with c.session_transaction() as sess:
        assert sess['auth_factor'] == 'line_kiosk' and sess.get('user_id')


# ---------------------------------------------------------------------------
# B-135
# ---------------------------------------------------------------------------
def test_register_otp_refused_when_not_enabled(app, store_settings):
    store_settings('sms', provider='log', otp_on_register=False, allow_pin_reset=True)
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/api/otp/send', json={'phone': '0937100007', 'purpose': 'register'})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# R2-045
# ---------------------------------------------------------------------------
def _pending(tid, token='tok-cb1'):
    return {'user_id': 'U-cb1-pending', 'name': '別人的 LINE', 'tenant_id': tid, 'at': datetime.now().isoformat(),
            'token': token}


def test_line_pending_cleared_on_home_and_needs_token(app):
    tid = default_tenant_id(app)
    c = app.test_client()
    c.get('/?shop=default')
    with c.session_transaction() as sess:
        sess['line_pending'] = _pending(tid)
    c.get('/')
    with c.session_transaction() as sess:
        assert 'line_pending' not in sess
    from flask import session
    from app.routes.member_auth import bind_pending_line
    with tenant_request(app, tid):
        user = SimpleNamespace(id=-1, line_user_id='')
        session['line_pending'] = _pending(tid)
        assert bind_pending_line(user, token='wrong') is False and user.line_user_id == ''
        assert 'line_pending' not in session
        session['line_pending'] = _pending(tid)
        assert bind_pending_line(user, token='') is False
        session['line_pending'] = _pending(tid)
        assert bind_pending_line(user, token='tok-cb1') is True and user.line_user_id == 'U-cb1-pending'


# ---------------------------------------------------------------------------
# B-029 已取消的訂單不給「前往付款」
# ---------------------------------------------------------------------------
def test_my_orders_cancelled_pending_has_no_payment_url(app):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    item = make_item(app, 'CB1 取消付款餐', price=50)
    c = guest_client(app)
    r = checkout(c, [line(item)])
    assert r.status_code == 200, r.get_data(as_text=True)
    oid = r.get_json()['order_id']
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            order = db.session.get(Order, oid)
            order.payment_status, order.payment_note = 'pending', '待付款'
            db.session.commit()
    row = next(o for o in c.get('/my_orders').get_json() if o['order_id'] == oid)
    assert row['payment_url'] and row['payment_note'] == '待付款'
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            db.session.get(Order, oid).status = OrderStatus.CANCELLED
            db.session.commit()
    row = next(o for o in c.get('/my_orders').get_json() if o['order_id'] == oid)
    assert row['payment_url'] == '' and row['payment_note'] == ''


# ---------------------------------------------------------------------------
# B-229 點餐頁附上會員等級的點數倍率
# ---------------------------------------------------------------------------
def test_storefront_extras_tier_points_multiplier(app):
    from app.routes.customer import _storefront_extras

    def member(tier):
        return SimpleNamespace(tier=tier, wallet_balance=0, line_user_id='', email='', phone='')
    with tenant_request(app, default_tenant_id(app)):
        assert _storefront_extras(None, None, is_hub=False)['tierPointsMultiplier'] == 1
        tier = SimpleNamespace(name='金卡', discount_percent=5, points_multiplier=2)
        assert _storefront_extras(member(tier), None, is_hub=False)['tierPointsMultiplier'] == 2
        tier.points_multiplier = None
        assert _storefront_extras(member(tier), None, is_hub=False)['tierPointsMultiplier'] == 1
        tier.points_multiplier = -3
        assert _storefront_extras(member(tier), None, is_hub=False)['tierPointsMultiplier'] == 0


# ---------------------------------------------------------------------------
# B-136 語音點餐的可否點與菜單頁相同 (原料不足)
# ---------------------------------------------------------------------------
def test_voice_parse_marks_ingredient_shortage_unavailable(app, store_settings, monkeypatch):
    from app.services import inventory_service
    store_settings('menu', voice_ordering=True, voice_llm=False)
    item = make_item(app, 'CB1語音缺料麵', price=80)
    c = guest_client(app)
    body = c.post('/api/voice/parse', json={'text': '一碗CB1語音缺料麵'}).get_json()
    assert next(x for x in body['lines'] if x['menu_item_id'] == item)['available'] is True
    monkeypatch.setattr(inventory_service, 'shortage_items', lambda tid: [item])
    body = c.post('/api/voice/parse', json={'text': '一碗CB1語音缺料麵'}).get_json()
    assert next(x for x in body['lines'] if x['menu_item_id'] == item)['available'] is False


# ---------------------------------------------------------------------------
# B-486 寄出驗證碼回傳 message_i18n
# ---------------------------------------------------------------------------
def test_otp_send_returns_message_i18n(app, store_settings):
    store_settings('sms', provider='log', otp_on_register=True, allow_pin_reset=True)
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/api/otp/send', json={'phone': '0937100488', 'purpose': 'register'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['message'] == '驗證碼已寄出，10 分鐘內有效' and body['message_i18n']


# ---------------------------------------------------------------------------
# 跨檔請求 B-065：重送 (replayed) 回傳已取消的訂單時不給付款連結
# ---------------------------------------------------------------------------
def test_replayed_cancelled_order_has_no_payment_url(app):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    item = make_item(app, 'CB1 重送取消餐', price=55)
    c = guest_client(app)
    cid = str(uuid.uuid4())
    r = checkout(c, [line(item)], client_order_id=cid)
    assert r.status_code == 200, r.get_data(as_text=True)
    oid = r.get_json()['order_id']
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            order = db.session.get(Order, oid)
            order.payment_status = 'pending'
            db.session.commit()
    body = checkout(c, [line(item)], client_order_id=cid).get_json()
    assert body['replayed'] is True and body.get('payment_url')
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            db.session.get(Order, oid).status = OrderStatus.CANCELLED
            db.session.commit()
    body = checkout(c, [line(item)], client_order_id=cid).get_json()
    assert body['replayed'] is True and 'payment_url' not in body


# ---------------------------------------------------------------------------
# 跨檔請求 R2-002：點餐頁依所選取餐分店顯示外送到府選項
# ---------------------------------------------------------------------------
def test_storefront_home_delivery_follows_pickup_node(app, store_settings):
    import json
    from app.routes.customer import _storefront_extras
    store_settings('own_delivery', enabled=True, channels=['online', 'pos'], zones='近:30:200\n遠:60:400',
                   free_over=1000, eta_minutes=40)
    with tenant_request(app, default_tenant_id(app)):
        pro = _storefront_extras(None, None, is_hub=True)
        assert [z['name'] for z in pro['homeDelivery']['zones']] == ['近', '遠']
        assert pro['homeDelivery']['freeOver'] == 1000 and pro['homeDelivery']['eta'] == 40
        closed = SimpleNamespace(own_delivery_json=json.dumps({'enabled': False, 'channels': ['online']}))
        assert _storefront_extras(None, None, is_hub=True, pickup_node=closed)['homeDelivery'] is None
        own = SimpleNamespace(own_delivery_json=json.dumps(
            {'enabled': True, 'channels': ['online'], 'zones': '近:50:300', 'free_over': 0}))
        hd = _storefront_extras(None, None, is_hub=True, pickup_node=own)['homeDelivery']
        assert hd['zones'] == [{'name': '近', 'fee': 50, 'min_spend': 300}] and hd['freeOver'] == 0
        # 門市點餐機不顯示外送到府
        assert _storefront_extras(None, None, is_hub=False)['homeDelivery'] is None


# ---------------------------------------------------------------------------
# 跨檔請求 R2-047：門市 LINE 登入的工作階段綁定 LINE 時提示正確的登入方式
# ---------------------------------------------------------------------------
def test_line_bind_needs_pin_message_matches_factor(app):
    from flask import session
    from app.routes.member_auth import LINE_BIND_NEEDS_PIN, LINE_BIND_NEEDS_PIN_KIOSK, _line_bind_needs_pin_message
    with tenant_request(app, default_tenant_id(app)):
        session['auth_factor'] = 'face'
        assert _line_bind_needs_pin_message() == LINE_BIND_NEEDS_PIN
        session['auth_factor'] = 'line_kiosk'
        assert _line_bind_needs_pin_message() == LINE_BIND_NEEDS_PIN_KIOSK and '門市以 LINE 登入' in LINE_BIND_NEEDS_PIN_KIOSK


# ---------------------------------------------------------------------------
# 跨檔請求 R2-045：註冊頁取消 LINE 待綁定後留在註冊頁
# ---------------------------------------------------------------------------
def test_line_pending_discard_returns_to_register(app):
    tid = default_tenant_id(app)
    c = app.test_client()
    c.get('/?shop=default')
    with c.session_transaction() as sess:
        sess['line_pending'] = _pending(tid)
    r = c.post('/auth/line/pending/discard', data={'next': 'register'})
    assert r.status_code == 302 and r.headers['Location'].endswith('/register')
    with c.session_transaction() as sess:
        assert 'line_pending' not in sess
        sess['line_pending'] = _pending(tid)
    r = c.post('/auth/line/pending/discard', data={'next': 'https://evil.example/'})
    assert r.status_code == 302 and 'evil' not in r.headers['Location'] and '/register' not in r.headers['Location']
