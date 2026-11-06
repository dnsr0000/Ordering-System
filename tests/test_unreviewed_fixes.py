"""第二輪全專案檢查 (先前沒審過的檔案) 找到的問題：會員資產驗證、寄杯品項刪除、Redis 重新連線、定位 / 麥克風權限、外送改派、
促銷時段、語音點餐、廚房通知、排班、推播、LINE 群發、簡訊驗證碼、多國語系訊息"""
import threading
import time

from app.extensions import db
from tests.support import line, login_staff, make_item, make_member


# ---------------------------------------------------------------------------
# 會員資產驗證：紅利兌換品項 / 會員持有的優惠券也要驗證
# ---------------------------------------------------------------------------
def test_pos_reward_item_and_held_coupon_require_member_verification(app, store_settings):
    from app.services import member_verify_service as mv
    store_settings('member', pos_verify=True)
    reward = make_item(app, '驗證兌換品', price=80, is_reward=True, reward_points=50)
    uid = make_member(app, '兌換會員', '0911333555', points=500, pin='7351')
    staff = login_staff(app, 'verify_reward_cashier', 'staff')
    r = staff.post('/api/pos/checkout', json={'items': [line(reward, quantity=5, is_reward=True)], 'payment_method': 'Cash',
                                              'user_id': uid, 'order_type': '內用'})
    assert r.status_code == 403 and r.get_json()['need_member_pin'], '只用電話查到會員就兌換紅利品項要驗證'
    from app.models.user import User
    with app.app_context():
        assert db.session.get(User, uid).points == 500, '未驗證不可扣點'
        # 會員持有的優惠券代碼算會員資產；公開代碼 / 沒有會員不算
        from app.models.coupon import Coupon, UserCoupon
        from tests.support import default_tenant_id
        tid = default_tenant_id(app)
        c = Coupon(tenant_id=tid, code='HELDONLY88', title='會員券', discount_type='fixed', discount_value=50)
        db.session.add(c)
        db.session.flush()
        db.session.add(UserCoupon(tenant_id=tid, user_id=uid, coupon_id=c.id, code='HELDONLY88'))
        db.session.commit()
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            assert mv.uses_member_assets({'user_id': uid, 'promo_code': ' heldonly88 ', 'items': []})
            assert not mv.uses_member_assets({'user_id': uid, 'promo_code': 'PUBLIC10', 'items': []})
            assert not mv.uses_member_assets({'user_id': uid, 'items': [line(reward)]})


# ---------------------------------------------------------------------------
# 寄杯綁定的品項不可直接刪除
# ---------------------------------------------------------------------------
def test_menu_item_with_outstanding_passes_cannot_be_deleted(app, admin_client):
    from app.models.member import MemberPass, PassProduct
    from app.models.menu import MenuItem
    from tests.support import default_tenant_id
    item = make_item(app, '寄杯美式', price=50)
    uid = make_member(app, '寄杯會員', '0911333666')
    with app.app_context():
        tid = default_tenant_id(app)
        mp = MemberPass(tenant_id=tid, user_id=uid, menu_item_id=item, name='美式 10 杯', total_uses=10, remaining=4)
        db.session.add(mp)
        db.session.commit()
        mp_id = mp.id
    r = admin_client.post(f'/admin/delete/{item}', follow_redirects=True)
    assert '寄杯還沒兌換完' in r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(MenuItem, item) is not None
        mp = db.session.get(MemberPass, mp_id)
        mp.remaining = 0
        db.session.add(PassProduct(tenant_id=tid, name='美式 10 杯', menu_item_id=item, uses=10, price=450))
        db.session.commit()
    r = admin_client.post(f'/admin/delete/{item}', follow_redirects=True)
    assert '販售中的寄杯商品' in r.get_data(as_text=True)
    with app.app_context():
        PassProduct.query.filter_by(menu_item_id=item).update({'is_active': False})
        db.session.commit()
    admin_client.post(f'/admin/delete/{item}')
    with app.app_context():
        assert db.session.get(MenuItem, item) is None, '寄杯都用完、商品停售後可以刪除'


# ---------------------------------------------------------------------------
# Redis 斷線後重新訂閱
# ---------------------------------------------------------------------------
class _FakePubSub:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.closed = False

    def subscribe(self, channel):
        pass

    def listen(self):
        if self.behaviour == 'fail':
            raise ConnectionError('Redis 斷線')
        yield {'data': b'{"tenant_id": 7, "event": "x"}'}
        threading.Event().wait(5)

    def close(self):
        self.closed = True


class _FakeRedis:
    def __init__(self):
        self.made = []

    def pubsub(self, **kw):
        ps = _FakePubSub('fail' if not self.made else 'ok')
        self.made.append(ps)
        return ps


def test_redis_listener_reconnects_after_connection_error():
    from app.services.event_bus import OrderEventBus
    bus = OrderEventBus()
    bus.retry_base = 0.01
    bus._redis = _FakeRedis()
    got = []
    bus._fanout = lambda tid, payload: got.append(tid)
    bus._ensure_listener()
    first = bus._listener
    bus._ensure_listener()
    assert bus._listener is first, '已有執行中的監聽執行緒時不再開第二條'
    deadline = time.time() + 3
    while not got and time.time() < deadline:
        time.sleep(0.02)
    assert got == [7], '第一次 listen() 斷線後要重新訂閱並繼續收通知'
    assert bus._redis.made[0].closed and len(bus._redis.made) == 2


# ---------------------------------------------------------------------------
# 定位 / 麥克風 / 相機只允許本站 (外送員 GPS、顧客「我的位置」、語音點餐)
# ---------------------------------------------------------------------------
def test_permissions_policy_allows_self_geolocation_and_microphone(app):
    policy = app.test_client().get('/healthz').headers.get('Permissions-Policy', '')
    assert 'geolocation=(self)' in policy and 'microphone=(self)' in policy and 'camera=(self)' in policy


# ---------------------------------------------------------------------------
# 外送改派：換新連結、舊連結失效、清掉前一位的位置
# ---------------------------------------------------------------------------
def test_reassigning_driver_rotates_tracking_token(app, staff_client, store_settings):
    from app.models.order import DeliveryPosition, Order
    from tests.test_home_delivery import ADDR, ZONES, _home
    store_settings('own_delivery', enabled=True, zones=ZONES, free_over=1000, drivers='阿明\n小華',
                   channels=['pos', 'online'], gps_tracking=True, gps_interval=10)
    item = make_item(app, '改派便當', price=220)
    oid = _home(staff_client, [line(item)], ADDR).get_json()['order_id']
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '阿明'})
    with app.app_context():
        old = db.session.get(Order, oid).driver_token
    public = app.test_client()
    assert public.post(f'/api/driver/{old}/position', json={'lat': 22.99, 'lng': 120.21}).status_code == 200
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '阿明'})
    with app.app_context():
        assert db.session.get(Order, oid).driver_token == old, '同一位外送員重新指派沿用連結'
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '小華'})
    with app.app_context():
        o = db.session.get(Order, oid)
        new, uuid = o.driver_token, o.uuid
        assert new and new != old
        assert DeliveryPosition.query.filter_by(order_uuid=uuid).count() == 0, '前一位外送員的位置要清掉'
    assert public.get(f'/driver/{old}').status_code == 404
    assert public.post(f'/api/driver/{old}/position', json={'lat': 22.99, 'lng': 120.21}).status_code == 404
    assert public.get(f'/driver/{new}').status_code == 200


# ---------------------------------------------------------------------------
# 促銷：跨午夜時段、滿額「應付 0%」
# ---------------------------------------------------------------------------
def test_promotion_window_across_midnight_and_zero_percent():
    from datetime import datetime
    from types import SimpleNamespace
    from app.services import promotion_service as ps
    promo = SimpleNamespace(is_active=True, start_date=None, end_date=None, days='1234567', start_time='22:00', end_time='02:00',
                            channels='', member_only=False)

    def at(hm):
        return datetime(2026, 11, 6, int(hm[:2]), int(hm[3:]))
    assert ps.is_active(promo, 'pos', False, at('23:30')) and ps.is_active(promo, 'pos', False, at('01:15'))
    assert not ps.is_active(promo, 'pos', False, at('12:00')) and not ps.is_active(promo, 'pos', False, at('02:30'))
    promo.start_time, promo.end_time = '11:00', '14:00'
    assert ps.is_active(promo, 'pos', False, at('11:00')) and not ps.is_active(promo, 'pos', False, at('14:01'))
    item = SimpleNamespace(uuid='u1', category='主餐')
    lines = [SimpleNamespace(menu_item=item, quantity=2, unit_price=100, is_add_on=False, is_reward=False)]
    free = SimpleNamespace(id=1, priority=0, is_active=True, start_date=None, end_date=None, days='1234567', start_time='',
                           end_time='', channels='', member_only=False, kind='threshold_discount', threshold=100, amount=0,
                           percent=0, target_type='all', target_value='', name='全免', stack_with_coupon=True)
    assert ps.evaluate(lines, 'pos', promotions=[free]).discount == 200, '應付 0% = 整單免費'


# ---------------------------------------------------------------------------
# 語音點餐：甜度 / 辣度裡的數字不是數量；兩個品名之間的字只算一邊
# ---------------------------------------------------------------------------
def test_voice_quantity_ignores_sugar_numbers_and_gap_belongs_to_one_item():
    from app.services import voice_order_service as voice
    from tests.test_voice_order import ADDONS, ITEMS

    def brief(text):
        return [(ln['name'], ln['quantity'], ln['sugar'], ln['ice'], ln['spicy']) for ln in voice.rule_parse(text, ITEMS, ADDONS)[0]]
    assert brief('兩杯紅茶三分糖') == [('紅茶', 2, '微糖', '', '')]
    assert brief('紅茶五分糖少冰') == [('紅茶', 1, '半糖', '少冰', '')]
    assert brief('兩份薯條一點辣') == [('薯條', 2, '', '', '微辣')]
    assert [b[:2] for b in brief('珍珠奶茶兩杯，薯條')] == [('珍珠奶茶', 2), ('薯條', 1)]
    assert brief('紅茶無糖，珍珠奶茶')[1][2] == '', '前一杯的甜度不可套到下一杯'
    assert brief('珍珠奶茶去冰 紅茶')[1][3] == ''


# ---------------------------------------------------------------------------
# 廚房一鍵出餐：重複送出不重複通知
# ---------------------------------------------------------------------------
def test_kitchen_complete_all_notifies_only_orders_it_completed(app, monkeypatch):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    from app.services import notify_service, order_lifecycle
    from tests.support import default_tenant_id
    with app.app_context():
        tid = default_tenant_id(app)
        o = Order(tenant_id=tid, total_price=100, status=OrderStatus.PENDING, order_type='內用', payment_method='Cash',
                  order_source='POS', payment_status='paid')
        db.session.add(o)
        db.session.commit()
        oid = o.id
    sent = []
    monkeypatch.setattr(notify_service, 'order_ready', lambda order: sent.append(order.id))
    from app.models.tenant import Tenant
    from app.tenant_context import _use_tenant
    for expected in (1, 0):
        with app.test_request_context('/'):
            _use_tenant(db.session.get(Tenant, tid), remember=False)
            assert order_lifecycle.complete_all_pending_orders(tid, [oid]) == expected
    assert sent == [oid]


# ---------------------------------------------------------------------------
# 自動排班：既有跨夜班的時數、往後的連續天數
# ---------------------------------------------------------------------------
def test_schedule_counts_overnight_shifts_and_consecutive_days_after(app):
    from datetime import date, timedelta
    from app.models.staff import WorkSchedule
    from app.models.tenant import Tenant
    from app.services import scheduling_service, settings_service
    from tests.support import other_shop_owner_client
    from tests.test_auto_schedule import NEXT_MONDAY, _seed_orders, _staff
    owner = other_shop_owner_client(app, 'sched-fwd-shop')
    with app.app_context():
        tid = Tenant.query.filter_by(slug='sched-fwd-shop').first().id
        settings_service.put('staffing', {'orders_per_staff_hour': 12, 'min_staff': 0, 'shift_hours': 4, 'weeks': 4,
                                          'max_hours_day': 8, 'max_hours_week': 40, 'max_consecutive_days': 6}, tenant_id=tid)
        db.session.commit()
    a = _staff(app, 'sched_fwd_a', tid=tid)
    owner.post('/admin/availability', data={'action': 'save', 'staff_id': a, 'max_hours_week': '',
                                            'weekday': ['1', '2', '3', '4', '5', '6', '7'], 'start_time': ['09:00'] * 7,
                                            'end_time': ['18:00'] * 7})
    mondays = [NEXT_MONDAY - timedelta(days=7 * k) for k in range(1, 5)]
    _seed_orders(app, mondays, [11, 12, 13, 14], per_hour=8, tid=tid)
    with app.app_context():
        # 週二～週日已手動排好 (連續 6 天)：週一再排就變成連上 7 天
        for k in range(1, 7):
            db.session.add(WorkSchedule(tenant_id=tid, staff_account_id=a, work_date=NEXT_MONDAY + timedelta(days=k),
                                        start_time='18:00', end_time='20:00'))
        db.session.add(WorkSchedule(tenant_id=tid, staff_account_id=a, work_date=date(2026, 12, 1), start_time='18:00',
                                    end_time='02:00'))
        db.session.commit()
        assert scheduling_service._existing(tid, date(2026, 12, 1), date(2026, 12, 1))[(a, date(2026, 12, 1))] == 8, \
            '18:00~02:00 跨夜班是 8 小時'
    with app.test_request_context('/'):
        from app.tenant_context import _use_tenant
        _use_tenant(Tenant.query.filter_by(slug='sched-fwd-shop').first(), remember=False)
        plan = scheduling_service.generate(tid, NEXT_MONDAY, today=NEXT_MONDAY - timedelta(days=3))
    monday = plan['days'][0]
    assert monday['shifts'] and all(s['username'] != 'sched_fwd_a' for s in monday['shifts']), '不可排出連上 7 天'


# ---------------------------------------------------------------------------
# 推播：金鑰格式錯誤的訂閱
# ---------------------------------------------------------------------------
def test_push_rejects_malformed_keys_and_bad_subscription_does_not_break_others(app):
    import base64
    from app.errors import AppError
    from app.models.order import Order
    from app.services import push_service
    from tests.support import default_tenant_id

    def b64(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    with app.app_context():
        tid = default_tenant_id(app)
        o = Order(tenant_id=tid, total_price=50, status='pending', order_type='外帶', payment_method='Cash', order_source='KIOSK')
        db.session.add(o)
        db.session.commit()
        for keys in ({'p256dh': 'abc', 'auth': 'def'}, {'p256dh': b64(bytes([4]) + b'1' * 64), 'auth': b64(b'short')}):
            try:
                push_service.subscribe(o, {'endpoint': 'https://push.example.com/x', 'keys': keys})
                raise AssertionError('格式錯誤的金鑰要拒絕')
            except AppError:
                pass
        # 已存在 (舊版存進去) 的壞訂閱：送出時判定刪除、回傳失敗，不拋例外
        sub = push_service.PushSubscription(tenant_id=tid, endpoint='https://push.example.com/bad', order_uuid=o.uuid,
                                            p256dh='abc', auth='def')
        ok, gone, err = push_service._send_one(sub, {'title': 't'}, push_service.ensure_keys(tid)[0], 'a@example.com')
        assert (ok, gone) == (False, True) and '金鑰無效' in err


# ---------------------------------------------------------------------------
# LINE 群發：後面批次失敗時回報已送出人數，不讓店家整批重送
# ---------------------------------------------------------------------------
def test_line_multicast_partial_failure_returns_sent_count(app, monkeypatch, store_settings):
    from app.errors import AppError
    from app.services import line_service
    from tests.support import default_tenant_id
    store_settings('line', messaging_enabled=True, messaging_access_token='bot-token')
    calls = []

    def fake_post(url, **kw):
        calls.append(len(kw['json']['to']))
        return (200, {}) if len(calls) == 1 else (500, {'message': 'server error'})
    monkeypatch.setattr(line_service, '_post', fake_post)
    ids = [f'U{i:032d}' for i in range(700)]
    with app.app_context():
        tid = default_tenant_id(app)
        sent = line_service.multicast_text(tid, ids, '週末優惠')
        assert sent == 500 and '其餘 200 人未送出' in sent.error
        monkeypatch.setattr(line_service, '_post', lambda url, **kw: (500, {'message': 'down'}))
        try:
            line_service.multicast_text(tid, ids, '週末優惠')
            raise AssertionError('第一批就失敗要拋錯 (什麼都沒送出)')
        except AppError:
            pass


# ---------------------------------------------------------------------------
# 簡訊驗證碼：簡訊商失敗時驗證碼紀錄仍保留 (計入頻率限制)
# ---------------------------------------------------------------------------
def test_otp_record_kept_when_sms_provider_fails(app, monkeypatch, store_settings):
    from app.models.member import OtpCode
    from app.services import sms_service
    store_settings('sms', provider='log', otp_on_register=True)

    def boom(*a, **k):
        raise RuntimeError('簡訊商逾時')
    monkeypatch.setattr(sms_service, 'send_sms', boom)
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/api/otp/send', json={'phone': '0966100077', 'purpose': 'register'})
    assert r.status_code >= 400
    with app.app_context():
        from app.tenant_context import tenant_bypass
        with tenant_bypass():
            assert OtpCode.query.filter_by(phone='0966100077').count() == 1
    assert c.post('/api/otp/send', json={'phone': '0966100077', 'purpose': 'register'}).status_code == 429, '失敗也計入 60 秒限制'


# ---------------------------------------------------------------------------
# 背景工作：savepoint 回滾不丟掉外層交易的工作
# ---------------------------------------------------------------------------
def test_deferred_tasks_survive_savepoint_rollback(app):
    from app.services import deferred
    with app.test_request_context('/'):
        deferred.defer(lambda: None)
        nested = db.session.begin_nested()
        nested.rollback()
        assert db.session.info.get(deferred._PENDING), 'savepoint 回滾後外層的背景工作仍要在'
        db.session.rollback()
        assert not db.session.info.get(deferred._PENDING), '外層回滾才丟掉'


def test_pin_lockout_message_translated():
    from app.services.i18n_service import translate_message
    msg = translate_message('⚠️ PIN 碼錯誤次數過多，此帳號或裝置已暫時鎖定，請於 5 分鐘後再試，或改用人臉辨識登入！', 'en')
    assert msg.startswith('⚠️ Too many incorrect PINs') and '5 minutes' in msg


# ---------------------------------------------------------------------------
# 外送員 GPS 中斷時重送的舊位置：記錄實際取得時間
# ---------------------------------------------------------------------------
def test_driver_position_age_backdates_recorded_time(app, staff_client, store_settings):
    from datetime import datetime
    from app.models.order import DeliveryPosition, Order
    from tests.test_home_delivery import ADDR, ZONES, _home
    store_settings('own_delivery', enabled=True, zones=ZONES, free_over=1000, drivers='阿明', channels=['pos', 'online'],
                   gps_tracking=True, gps_interval=10)
    item = make_item(app, '定位時間便當', price=220)
    oid = _home(staff_client, [line(item)], ADDR).get_json()['order_id']
    staff_client.post('/admin/dispatch', data={'order_id': oid, 'action': 'assign', 'driver': '阿明'})
    with app.app_context():
        o = db.session.get(Order, oid)
        token, uuid = o.driver_token, o.uuid
    public = app.test_client()
    assert public.post(f'/api/driver/{token}/position', json={'lat': 22.99, 'lng': 120.21, 'age': 120}).status_code == 200
    assert public.post(f'/api/driver/{token}/position', json={'lat': 22.99, 'lng': 120.21, 'age': 'x'}).status_code == 200
    with app.app_context():
        rows = DeliveryPosition.query.filter_by(order_uuid=uuid).order_by(DeliveryPosition.id).all()
        stale, fresh = [(datetime.now() - r.recorded_at).total_seconds() for r in rows[:2]]
        assert 110 <= stale <= 140 and fresh < 10


# ---------------------------------------------------------------------------
# 前端：平面圖、待機輪播、Service Worker、會員條碼、付款頁
# ---------------------------------------------------------------------------
def test_frontend_fixes_present():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'static' / 'js'
    fp = (root / 'floor-plan.js').read_text(encoding='utf-8')
    assert 'el.offsetWidth / rect.width' in fp and '0.95' not in fp, '拖曳範圍扣掉桌子寬高'
    assert fp.index('place(ev.clientX, ev.clientY)') > fp.index('canvas.appendChild(el)'), '從待擺放區點一下也夾在平面圖內'
    attract = (root / 'kiosk-attract.js').read_text(encoding='utf-8')
    assert 'wokeAt' in attract and 'passive: false' in attract
    sw = (root / 'sw.js').read_text(encoding='utf-8')
    assert "searchParams.has('v')" in sw and 'ignoreSearch: true' in sw
    rewards = (root / 'rewards.js').read_text(encoding='utf-8')
    assert 'if (qrOpen)' in rewards and 'qrOpen = false' in rewards
    pay = (root / 'pay.js').read_text(encoding='utf-8')
    assert "addEventListener('submit', () => clearTimeout(timer))" in pay
    driver = (root / 'driver.js').read_text(encoding='utf-8')
    assert 'fixAt' in driver and 'age }' in driver
