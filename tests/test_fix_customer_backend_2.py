"""
修正小組 customer-backend-2 的回歸測試：
顧客 AI 客服 / 追蹤頁 / 推播金鑰 / PWA manifest 的店家隔離、語音點餐解析、顧客工作階段 (識別長度、id 重用、
跨主機兌換憑證)、接單控制 (自動恢復、團購送單、時段計數)、人臉辨識的非 ASCII 路徑。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.extensions import db
from tests.support import (checkout, default_tenant_id, get_or_create_tenant, line, make_item, make_member,
                           tenant_request)

SHOP_B = 'cb2-shopb'


def _shop_b_item(app, name, **kw):
    from app.models.menu import MenuItem
    tid = get_or_create_tenant(app, SHOP_B)
    with app.app_context():
        item = MenuItem(tenant_id=tid, name=name, category=kw.pop('category', 'B店甜點'), price=kw.pop('price', 50),
                        stock=100, total_stock=100, modifiers='none', **kw)
        db.session.add(item)
        db.session.commit()
        return tid, item.id


def _shop_b_order(app):
    """B 店 (非預設店家) 點餐機下的一張訂單，回傳 (店家 id, 訂單 uuid, 品名)"""
    name = 'CB2乙店追蹤雞'
    tid, item_id = _shop_b_item(app, name)
    c = app.test_client()
    c.get(f'/?shop={SHOP_B}')
    c.post('/guest_login')
    r = checkout(c, [line(item_id, 2)])
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return tid, r.get_json()['order_uuid'], name


# ---------------------------------------------------------------- B-117 AI 客服不可混入其他店家的菜單
def test_chat_answer_only_uses_current_shop_menu(app, store_settings):
    store_settings('chatbot', enabled=True, llm_enabled=False, handoff_enabled=False)
    make_item(app, 'CB2本店花生糖', price=30, allergens='peanut')
    _shop_b_item(app, 'CB2乙店祕密蛋糕', allergens='peanut')
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/api/chat', json={'message': '哪些含花生？'})
    assert r.status_code == 200
    answer = r.get_json()['answer']
    assert 'CB2乙店祕密蛋糕' not in answer
    r = c.post('/api/chat', json={'message': '菜單有哪些分類？'})
    assert 'B店甜點' not in r.get_json()['answer']


# ---------------------------------------------------------------- B-125 / B-126 掃收據 QR 開別店訂單的追蹤頁
def test_track_page_of_other_shop_order_lists_items_and_uses_order_shop_vapid(app):
    from app.services import push_service, settings_service
    tid, uuid, name = _shop_b_order(app)
    with app.app_context():
        settings_service.put('push', {'enabled': True, 'contact_email': 'cb2@example.com', 'vapid_private_key': '',
                                      'vapid_public_key': ''}, tenant_id=tid)
        db.session.commit()
    phone = app.test_client()   # 沒有 B 店 session 的手機
    html = phone.get(f'/track/{uuid}').get_data(as_text=True)
    assert name in html
    assert phone.get(f'/api/track/{uuid}').get_json()['push_enabled'] is True
    vapid = phone.get(f'/api/push/vapid?order={uuid}').get_json()
    assert vapid['enabled'] is True and vapid['public_key']
    with app.app_context():
        assert vapid['public_key'] == push_service.config(tid)['vapid_public_key']
    assert phone.get('/api/push/vapid?order=short').status_code == 404


# ---------------------------------------------------------------- B-127 PWA manifest 依店家區分
def test_manifest_start_url_and_id_follow_shop(app):
    get_or_create_tenant(app, SHOP_B)
    c = app.test_client()
    m = c.get(f'/manifest.webmanifest?shop={SHOP_B}').get_json()
    assert m['start_url'] == f'/shop/{SHOP_B}' and m['id'] == m['start_url'] and SHOP_B in m['name']
    d = app.test_client().get('/manifest.webmanifest').get_json()
    assert d['start_url'] == '/' and d['id'] == '/'


# ---------------------------------------------------------------- 語音點餐
def _voice_item(item_id=1, name='薯條', name_en='', sizes=(), modifiers='none', available=True):
    return SimpleNamespace(id=item_id, name=name, name_en=name_en, sizes=list(sizes), modifiers=modifiers,
                           is_available=available, service_status=lambda: (True, ''))


def test_voice_rule_parse_handles_multichar_lowercase():
    from app.services import voice_order_service as v
    lines, _left = v.rule_parse('İ fries', [_voice_item(name='薯條', name_en='fries')], [])
    assert [ln['name'] for ln in lines] == ['薯條']


def test_voice_spicy_words_are_not_sizes():
    from app.services import voice_order_service as v
    pot = _voice_item(name='麻辣鍋', sizes=[('小份', 0), ('大份', 30)], modifiers='spicy')
    lines, _ = v.rule_parse('我要一份麻辣鍋 大辣', [pot], [])
    assert lines[0]['size'] == '小份' and lines[0]['spicy'] == '大辣'
    lines, _ = v.rule_parse('麻辣鍋 大 小辣', [pot], [])
    assert lines[0]['size'] == '大份' and lines[0]['spicy'] == '微辣'
    noodle = _voice_item(name='牛肉麵', sizes=[('大碗', 0), ('小碗', -20)], modifiers='spicy')
    lines, _ = v.rule_parse('牛肉麵 小辣', [noodle], [])
    assert lines[0]['size'] == '大碗'


def test_voice_llm_bad_addons_do_not_crash(monkeypatch):
    from app.services import ai_service
    from app.services import voice_order_service as v
    rows = [{'name': '薯條', 'qty': 1, 'addons': 5}, {'name': '薯條', 'qty': 2, 'addons': True},
            {'name': '薯條', 'qty': 1, 'addons': ['起司', 3]}]
    monkeypatch.setattr(ai_service, 'generate_json', lambda *a, **k: (rows, 'gemini'))
    lines, unknown, source = v.llm_parse('薯 條', [_voice_item()], [])
    assert [ln['quantity'] for ln in lines] == [1, 2, 1] and unknown == [] and source == 'gemini'


def test_voice_availability_override():
    from app.services import voice_order_service as v
    item = _voice_item(item_id=7)
    assert v.parse('薯條', [item], [], use_llm=False)['lines'][0]['available'] is True
    assert v.parse('薯條', [item], [], use_llm=False, availability={7: False})['lines'][0]['available'] is False
    sold = _voice_item(item_id=8, available=False)
    assert v.parse('薯條', [sold], [], use_llm=False, availability={8: True})['lines'][0]['available'] is True


# ---------------------------------------------------------------- B-118 / R2-048 顧客工作階段
def test_long_member_name_identifier_truncated(app):
    from flask import session

    from app.models.user import CustomerLog, User
    from app.services import customer_session
    tid = default_tenant_id(app)
    uid = make_member(app, '長' * 100, '0939118118', pin='1357')
    with tenant_request(app, tid):
        customer_session.login_member(db.session.get(User, uid))
        log = db.session.get(CustomerLog, session['customer_log_id'])
        assert len(log.identifier) <= 100 and log.identifier.startswith('長')


def test_reused_member_id_is_not_logged_in(app):
    from flask import session

    from app.models.user import User
    from app.services import customer_session
    tid = default_tenant_id(app)
    a = make_member(app, 'CB2會員甲', '0939048001', pin='1357')
    b = make_member(app, 'CB2會員乙', '0939048002', pin='1357')
    with tenant_request(app, tid):
        customer_session.login_member(db.session.get(User, a))
        assert customer_session.current_member().id == a
        session['user_id'] = b   # 模擬 SQLite 把被刪除會員的 id 指派給新會員
        assert customer_session.current_member() is None
        # 升級前沒有記 uuid 的工作階段：照常取回並補記
        session.pop('user_uuid')
        assert customer_session.current_member().id == b
        assert session['user_uuid'] == db.session.get(User, b).uuid


# ---------------------------------------------------------------- B-128 同步架構下只自動退還本機這次兌換的憑證
def _open_voucher(tid, uid, cost):
    from app.models.enums import VoucherStatus
    from app.models.user import RewardRedemption
    v = RewardRedemption(tenant_id=tid, user_id=uid, points_cost=cost, status=VoucherStatus.OPEN)
    db.session.add(v)
    db.session.commit()
    return v


@pytest.mark.parametrize('role, expected', [('node', 30), ('standalone', 130)])
def test_auto_refund_only_session_vouchers_when_synced(app, monkeypatch, role, expected):
    from app.models.enums import VoucherStatus
    from app.models.user import RewardRedemption, User
    from app.services import customer_session
    monkeypatch.setitem(app.config, 'SYNC_ROLE', role)
    tid = default_tenant_id(app)
    uid = make_member(app, 'CB2憑證會員', f'09391280{0 if role == "node" else 1}{len(role) % 10}', pin='1357')
    with tenant_request(app, tid):
        customer_session.login_member(db.session.get(User, uid))
        remote = _open_voucher(tid, uid, 100)   # 別台主機兌換、同步過來的憑證
        local = _open_voucher(tid, uid, 30)
        customer_session.remember_voucher(local)
        before = db.session.get(User, uid).points
        customer_session.close_customer_session()
        db.session.expire_all()
        assert db.session.get(User, uid).points - before == expected
        assert db.session.get(RewardRedemption, local.id).status == VoucherStatus.REFUNDED
        remote_status = db.session.get(RewardRedemption, remote.id).status
        assert remote_status == (VoucherStatus.OPEN if role == 'node' else VoucherStatus.REFUNDED)


# ---------------------------------------------------------------- 接單控制
def test_intake_form_values_after_auto_resume():
    from app.services import intake_service
    now = datetime(2026, 11, 9, 12, 0)
    stale = {'paused': True, 'resume_at': '2026-11-09T11:30', 'message': 'x'}
    assert intake_service.form_view_values(stale, now)['paused'] is False
    assert intake_service.form_view_values(stale, now)['resume_at'] == ''
    cleaned = intake_service.clean_form_values(stale, now)
    assert cleaned['paused'] is True and cleaned['resume_at'] == ''
    future = {'paused': True, 'resume_at': '2026-11-09T12:30'}
    assert intake_service.form_view_values(future, now) == future
    assert intake_service.clean_form_values(future, now) == future


def test_intake_check_blocks_manual_group_submit_when_paused(app):
    from app.errors import AppError
    from app.services import intake_service
    tid = default_tenant_id(app)
    now = datetime.now()
    c = SimpleNamespace(is_pos=False, is_delivery=False, pickup_at=now + timedelta(hours=3), now=now, tenant_id=tid,
                        stock_node=None, is_hub=False, scheduled_at=now + timedelta(hours=3), node_uuid=None)
    with tenant_request(app, tid):
        intake_service.set_paused(True, 30, tenant_id=tid)
        db.session.commit()
        try:
            with pytest.raises(AppError) as exc:
                intake_service.check(c)
            assert exc.value.extra.get('intake_paused') is True
        finally:
            intake_service.set_paused(False, tenant_id=tid)
            db.session.commit()


def test_window_count_merges_asap_and_scheduled(app):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    from app.services import intake_service
    item = make_item(app, 'CB2時段餐', price=10)
    tid = default_tenant_id(app)
    later = datetime.now() + timedelta(days=2)
    with tenant_request(app, tid):
        have_now = intake_service.window_count(tid, datetime.now(), 60)
        assert intake_service.window_count(tid, datetime.now(), 60, scheduled=True) == have_now
        have_later = intake_service.window_count(tid, later, 60)
    g = app.test_client()
    g.get('/?shop=default')
    g.post('/guest_login')
    r = checkout(g, [line(item)])
    assert r.status_code == 200
    with tenant_request(app, tid):
        assert intake_service.window_count(tid, datetime.now(), 60, scheduled=True) == have_now + 1
        order = db.session.get(Order, r.get_json()['order_id'])
        order.pickup_at = later   # 預約取餐：改以取餐時間所在時段計算
        db.session.commit()
        assert intake_service.window_count(tid, later, 60) == have_later + 1
        assert intake_service.window_count(tid, datetime.now(), 60) == have_now
        order.status = OrderStatus.CANCELLED
        db.session.commit()


# ---------------------------------------------------------------- B-129 非 ASCII 路徑
def test_read_image_from_non_ascii_path(tmp_path):
    from app.services import cv_service
    if not cv_service.FACE_AVAILABLE:
        pytest.skip('未安裝 opencv')
    folder = tmp_path / '會員照片'
    folder.mkdir()
    path = folder / 'm.jpg'
    from tests.support import jpeg_bytes
    path.write_bytes(jpeg_bytes(64, 48).read())
    img = cv_service._read_image(str(path))
    assert img is not None and img.shape[:2] == (48, 64)


# ---------------------------------------------------------------- 跨檔請求 (第二輪)
def _member_qr_session(app, phone, factor):
    """模擬以 factor (face / line_kiosk) 登入的點餐機瀏覽器"""
    from app.models.user import User
    from app.tenant_context import tenant_bypass
    c = app.test_client()
    c.get('/?shop=default')
    with app.app_context():
        with tenant_bypass():
            uid = User.query.filter_by(phone=phone).first().id
    with c.session_transaction() as sess:
        sess.update(user_id=uid, user_name='x', is_guest=False, auth_factor=factor)
    return c


def test_kiosk_line_login_requires_pin_for_sensitive_actions(app):
    """R2-047：門市 (共用點餐機) 的 LINE 登入不視同 PIN，敏感操作要再輸入 PIN，訊息說明是門市 LINE 登入"""
    phone = '0939047047'
    make_member(app, 'CB2門市LINE', phone, pin='2580', line_user_id='U-cb2-kiosk')
    c = _member_qr_session(app, phone, 'line_kiosk')
    r = c.get('/api/member_qr')
    assert r.status_code == 403 and r.get_json()['need_pin'] is True
    assert '門市以 LINE 登入' in r.get_json()['message']
    assert c.post('/api/member_qr', json={'action': 'confirm', 'pin': '2580'}).status_code == 200
    assert c.get('/api/member_qr').status_code == 200
    # 人臉登入維持原本的訊息
    r = _member_qr_session(app, phone, 'face').get('/api/member_qr')
    assert r.status_code == 403 and '人臉辨識登入' in r.get_json()['message']
    # 線上端的 LINE 登入 (line) 不需要再輸入 PIN
    assert _member_qr_session(app, phone, 'line').get('/api/member_qr').status_code == 200


def test_require_confirmed_reserves_pin_attempt_before_verifying(app, monkeypatch):
    """B-358：先原子預扣裝置與帳號額度再驗證 PIN；已鎖定時即使 PIN 正確也拒絕，PIN 錯誤只另計 IP 層級"""
    from datetime import timedelta as td

    from app.services import auth_throttle
    phone = '0939358358'
    make_member(app, 'CB2預扣會員', phone, pin='3690')
    calls = []
    real_failure = auth_throttle.record_pin_failure
    monkeypatch.setattr(auth_throttle, 'record_pin_failure',
                        lambda keys, ip_only=False: (calls.append(ip_only), real_failure(keys, ip_only=ip_only)))
    c = _member_qr_session(app, phone, 'face')
    assert c.post('/api/member_qr', json={'action': 'confirm', 'pin': '0000'}).status_code == 403
    assert calls == [True]
    monkeypatch.setattr(auth_throttle, 'reserve_pin_attempt', lambda keys: td(minutes=5))
    r = c.post('/api/member_qr', json={'action': 'confirm', 'pin': '3690'})
    assert r.status_code == 429 and r.get_json()['need_pin'] is True


def test_require_confirmed_locks_after_device_limit(app):
    from app.services import auth_throttle
    phone = '0939358359'
    make_member(app, 'CB2鎖定會員', phone, pin='3691')
    c = _member_qr_session(app, phone, 'face')
    codes = [c.post('/api/member_qr', json={'action': 'confirm', 'pin': '0000'}).status_code
             for _ in range(auth_throttle.DEVICE_MAX_FAILS + 1)]
    assert codes == [403] * auth_throttle.DEVICE_MAX_FAILS + [429]
    assert c.post('/api/member_qr', json={'action': 'confirm', 'pin': '3691'}).status_code == 429


def _default_delivery_order(app, name):
    """預設店家的一張外送單 (配送中、已有外送員憑證)，回傳訂單 id 與 uuid"""
    import uuid as uuid_mod

    from app.models.order import Order
    item = make_item(app, name, price=120)
    c = app.test_client()
    c.get('/?shop=default')
    c.post('/guest_login')
    r = checkout(c, [line(item)])
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    oid, ouid = r.get_json()['order_id'], r.get_json()['order_uuid']
    with app.app_context():
        o = db.session.get(Order, oid)
        o.delivery_status, o.driver_name, o.driver_token = 'out', '阿明', str(uuid_mod.uuid4())
        db.session.commit()
    return oid, ouid


def test_track_gps_enabled_follows_shop_setting(app, store_settings):
    """B-280：關閉 GPS 回報的店家，追蹤頁 API 帶 gps_enabled=False (前端不顯示等待位置區塊)"""
    _oid, uuid = _default_delivery_order(app, 'CB2外送便當')
    public = app.test_client()
    store_settings('own_delivery', enabled=True, gps_tracking=False)
    assert public.get(f'/api/track/{uuid}').get_json()['gps_enabled'] is False
    store_settings('own_delivery', enabled=True, gps_tracking=True)
    assert public.get(f'/api/track/{uuid}').get_json()['gps_enabled'] is True
    # 非外送單一律 False
    g2 = app.test_client()
    g2.get('/?shop=default')
    g2.post('/guest_login')
    plain = checkout(g2, [line(make_item(app, 'CB2內用飯', price=60))]).get_json()['order_uuid']
    assert public.get(f'/api/track/{plain}').get_json()['gps_enabled'] is False


def test_hub_track_shows_driver_position_without_local_setting(app, monkeypatch, store_settings):
    """B-421：線上端不看本機自有外送設定，訂單有外送員憑證就顯示位置"""
    from app.models.order import Order
    from app.routes import public_extras
    from app.services import delivery_tracking_service as tracking
    store_settings('own_delivery', enabled=False, gps_tracking=False)
    oid, _uuid = _default_delivery_order(app, 'CB2線上外送便當')
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        o = db.session.get(Order, oid)
        tracking.record_position(o, 22.99, 120.21, 10)
        db.session.commit()
        assert public_extras._driver_position(o) is None   # 門市主機：依本機設定
        monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
        pos = public_extras._driver_position(o)
        assert pos and pos['lat'] == 22.99
        assert public_extras._gps_enabled(o) is True


def test_track_uses_order_shop_languages_and_feedback_translated(app, store_settings):
    """B-287 / B-486：追蹤頁依訂單店家的語言設定；評分成功訊息附翻譯"""
    from app.models.enums import OrderStatus
    from app.models.order import Order
    from app.tenant_context import tenant_bypass
    store_settings('menu', languages=[])   # 預設店家只有中文；B 店維持預設 (英 / 日)
    _tid, uuid, _name = _shop_b_order(app)
    phone = app.test_client()
    html = phone.get(f'/track/{uuid}', headers={'Accept-Language': 'en'}).get_data(as_text=True)
    assert 'Order status' in html
    with app.app_context():
        with tenant_bypass():
            o = Order.query.filter_by(uuid=uuid).first()
            o.status = OrderStatus.COMPLETED
            db.session.commit()
    r = phone.post(f'/api/track/{uuid}/feedback', json={'rating': 3}, headers={'Accept-Language': 'en'})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body['message'] == '謝謝您的評分！' and body['message_i18n'] == 'Thank you for your rating!'
