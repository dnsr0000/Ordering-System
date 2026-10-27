"""第三輪 #9、#11、#12：會員分群與自動行銷、訂位訂金 / 未到 / 提醒、員工異常偵測"""
from datetime import date, datetime, timedelta

import pytest

from app.extensions import db, limiter
from app.models.coupon import Coupon, UserCoupon
from app.models.marketing import CampaignSend, MarketingCampaign
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.payment import Refund
from app.models.reservation import Reservation
from app.models.shift import CashShift
from app.models.tenant import Tenant
from app.models.user import User
from app.services import line_service, monitor_service, payment_ecpay
from tests.support import assert_flash, login_staff, make_member, member_client, run_together
from tests.test_members_line_sms import line_api  # noqa: F401  (fixture)


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def alerts(monkeypatch):
    out = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: out.append(msg))
    return out


def _tenant(app, slug):
    with app.app_context():
        t = Tenant.query.filter_by(slug=slug).first()
        if t is None:
            t = Tenant(name=f'測試 {slug}', slug=slug)
            db.session.add(t)
            db.session.commit()
        return t.id


def _ctx(app, tenant_id):
    from contextlib import contextmanager

    @contextmanager
    def cm():
        from app.tenant_context import _use_tenant
        with app.test_request_context('/'):
            _use_tenant(db.session.get(Tenant, tenant_id), remember=False)
            yield
    return cm()


# ===========================================================================
# 9. 會員分群與自動行銷
# ===========================================================================
def test_rfm_scores_and_segments():
    from app.services.marketing_service import _score, segment_of
    values = [1, 3, 120, 200]
    assert [_score(v, values, higher_is_better=False) for v in values] == [5, 4, 2, 1]
    assert [_score(v, [1, 1, 2, 6]) for v in (6, 2, 1)] == [5, 4, 2]
    assert _score(5, [5, 5, 5, 5]) == 3            # 全部同值：中間
    assert segment_of(5, 5, 5, True) == 'champion' and segment_of(0, 0, 0, False) == 'new'
    assert segment_of(4, 2, 2, True) == 'promising' and segment_of(2, 4, 4, True) == 'at_risk'
    assert segment_of(1, 2, 2, True) == 'hibernating' and segment_of(3, 4, 3, True) == 'loyal'
    assert segment_of(3, 3, 3, True) == 'need_attention'


def test_segments_winback_broadcast_and_admin(app, monkeypatch, alerts):
    from app.services import marketing_service, settings_service
    from app.services.order_service import process_checkout
    tid = _tenant(app, 'mkt-shop')
    pushes = []
    monkeypatch.setattr(line_service, '_post', lambda url, **kw: pushes.append((url, kw['json'])) or (200, {}))
    now = datetime.now()
    with _ctx(app, tid):
        settings_service.put('line', {'messaging_enabled': True, 'messaging_access_token': 'bot'}, tid)
        item = MenuItem(tenant_id=tid, name='行銷測試餐', price=100, stock=500, total_stock=500)
        db.session.add(item)
        users = {}
        for key, name, phone, line_id in (('A', '常客A', '0955100001', 'UA'), ('B', '沉睡B', '0955100002', 'UB'),
                                          ('C', '拒絕C', '0955100003', 'UC'), ('D', '新會員D', '0955100004', ''),
                                          ('E', '新客E', '0955100005', 'UE')):
            u = User(tenant_id=tid, name=name, phone=phone, photo_path='', line_user_id=line_id,
                     marketing_opt_out=(key == 'C'))
            db.session.add(u)
            users[key] = u
        db.session.commit()
        ids = {k: u.id for k, u in users.items()}
        coupon = Coupon(tenant_id=tid, code='MISSYOU', title='想念你折 50', discount_value=50, is_reward=False)
        expired = Coupon(tenant_id=tid, code='OLDONE', title='過期券', discount_value=10, is_reward=False,
                         valid_until=date.today() - timedelta(days=1))
        db.session.add_all([coupon, expired])
        db.session.commit()
        item_id = item.id

        def buy(key, days_ago, qty=1):
            process_checkout(tenant_id=tid, items=[{'menu_item_id': item_id, 'quantity': qty}], user_id=ids[key],
                             now=now - timedelta(days=days_ago, minutes=5))
        for d in range(1, 7):
            buy('A', d)
        buy('B', 200)
        buy('C', 120)
        buy('C', 121)
        buy('E', 3)
        rows = {r['name']: r for r in marketing_service.member_stats(tid)}
        assert {n: rows[n]['segment'] for n in rows} == {'常客A': 'champion', '沉睡B': 'hibernating', '拒絕C': 'at_risk',
                                                        '新會員D': 'new', '新客E': 'promising'}
        assert rows['沉睡B']['days'] == 200 and rows['常客A']['orders'] == 6 and rows['常客A']['spend'] == 600

        # 久未消費 90 天：B、C 符合，但 C 拒絕行銷
        wb = MarketingCampaign(tenant_id=tid, name='想念你', kind='winback', inactive_days=90, cooldown_days=60,
                               coupon_id=coupon.id, coupon_valid_days=10, message='{shop}好久不見')
        db.session.add(wb)
        db.session.commit()
        assert marketing_service.run_winbacks(tid, now) == 1
        uc = UserCoupon.query.filter_by(user_id=ids['B'], coupon_id=coupon.id).one()
        assert uc.source == 'campaign' and uc.expires_at.date() == (now + timedelta(days=10)).date()
        assert UserCoupon.query.filter_by(user_id=ids['C']).count() == 0
        multicast = [p for u, p in pushes if u == line_service.MULTICAST_URL]
        assert multicast[-1]['to'] == ['UB'] and '想念你折 50' in multicast[-1]['messages'][0]['text']
        assert marketing_service.run_winbacks(tid, now + timedelta(days=1)) == 0     # 冷卻期內不重發
        # B 回來消費 → 成效
        buy('B', -1)   # 收到後隔天回來消費
        st = marketing_service.stats(wb)
        assert (st['sent'], st['returned'], st['revenue'], st['coupons_issued']) == (1, 1, 100, 1)

        # 排程推播給冠軍 (訊息含 {name} → 逐一推播)
        bc = MarketingCampaign(tenant_id=tid, name='冠軍新品', kind='broadcast', segment='champion',
                               message='{name} 您好，新品搶先試吃！', send_at=now + timedelta(hours=1))
        db.session.add(bc)
        db.session.commit()
        assert marketing_service.run_due(tid, now) == 0
        assert marketing_service.run_due(tid, now + timedelta(hours=2)) == 1
        pushed = [p for u, p in pushes if u == line_service.PUSH_URL]
        assert pushed[-1]['to'] == 'UA' and pushed[-1]['messages'][0]['text'].startswith('常客A 您好')
        assert db.session.get(MarketingCampaign, bc.id).status == 'done'
        assert marketing_service.run_due(tid, now + timedelta(hours=3)) == 0

        # 券的活動已結束：自動暫停並通知
        bad = MarketingCampaign(tenant_id=tid, name='過期活動', kind='winback', inactive_days=30, cooldown_days=30,
                                coupon_id=expired.id, coupon_valid_days=5)
        db.session.add(bad)
        db.session.commit()
        marketing_service.run_winbacks(tid, now)
        assert db.session.get(MarketingCampaign, bad.id).status == 'paused'
        assert any('過期活動' in a for a in alerts)

    # 後台頁面 (另一家店的負責人)
    owner = login_staff(app, 'mkt_owner', 'owner', tenant_id=tid)
    page = owner.get('/admin/marketing').get_data(as_text=True)
    assert 'data-segment-count="champion">1<' in page and '想念你' in page and 'id="campaignForm"' in page
    seg = owner.get('/admin/marketing?segment=at_risk').get_data(as_text=True)
    assert '拒絕C' in seg and '拒絕行銷</span>' in seg and '常客A' not in seg
    assert owner.get('/admin/marketing?segment=champion&format=xlsx').data[:2] == b'PK'
    r = owner.post('/admin/marketing', data={'action': 'create', 'name': '週末推播', 'kind': 'broadcast', 'segment': 'all',
                                             'send_at': (now + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'),
                                             'message': '週末來吃'}, follow_redirects=True)
    assert '已建立「週末推播」' in r.get_data(as_text=True)
    r = owner.post('/admin/marketing', data={'action': 'create', 'name': '空活動', 'kind': 'winback'}, follow_redirects=True)
    assert '請選擇要發的優惠券或輸入推播訊息' in r.get_data(as_text=True)
    with app.app_context():
        assert CampaignSend.query.filter_by(tenant_id=tid).count() == 2


def test_member_marketing_opt_out(app, no_rate_limit):
    make_member(app, '行銷開關', '0955200001', pin='2468')
    c = member_client(app, '0955200001', '2468')
    assert 'id="marketingOptIn"' in c.get('/rewards').get_data(as_text=True)
    r = c.post('/api/member/marketing', json={'opt_in': False})
    assert r.get_json()['opt_in'] is False and '已關閉' in r.get_json()['message']
    with app.app_context():
        assert User.query.filter_by(phone='0955200001').one().marketing_opt_out is True
    page = c.get('/rewards').get_data(as_text=True)
    assert 'id="marketingOptIn"\n' in page or 'checked' not in page.split('id="marketingOptIn"')[1].split('>')[0]
    c.post('/api/member/marketing', json={'opt_in': True})
    with app.app_context():
        assert User.query.filter_by(phone='0955200001').one().marketing_opt_out is False


# ===========================================================================
# 11. 訂位訂金、未到、提醒
# ===========================================================================
@pytest.fixture
def deposits(app, store_settings, monkeypatch):
    store_settings('reservation', online_enabled=True, open_time='11:00', close_time='21:00', slot_minutes=60,
                   capacity_per_slot=30, max_party_size=8, deposit_per_person=100, deposit_min_party=4,
                   deposit_refund_hours=24, no_show_limit=1, deposit_on_seat='refund')
    calls = []
    state = {'code': '1'}

    def fake_post(url, data):
        calls.append(data)
        return {'RtnCode': state['code'], 'RtnMsg': 'OK' if state['code'] == '1' else '退款失敗'}
    monkeypatch.setattr(payment_ecpay, '_post', fake_post)
    return calls, state


def _book(c, phone, party, day_offset=3, hour=18, name='訂金客'):
    slot = f'{date.today() + timedelta(days=day_offset)}T{hour:02d}:00:00'
    return c.post('/reserve', data={'slot': slot, 'name': name, 'phone': phone, 'party_size': str(party)})


def _notify(c, app, trade_no, amount, code='1', tamper=False, custom='', provider=None):
    with app.app_context():
        merchant, key, iv, _ = payment_ecpay._cfg()
        params = {'MerchantID': merchant, 'MerchantTradeNo': trade_no, 'RtnCode': code, 'RtnMsg': 'OK',
                  'TradeNo': provider or 'EC' + trade_no[-8:], 'TradeAmt': str(amount), 'PaymentType': 'Credit_CreditCard',
                  'SimulatePaid': '0', 'CustomField1': custom}
        params['CheckMacValue'] = payment_ecpay.check_mac_value(params, key, iv)
    if tamper:
        params['TradeAmt'] = '1'
    return c.post('/payment/ecpay/reservation_notify', data=params).get_data(as_text=True)


def _res(app, token):
    with app.app_context():
        r = Reservation.query.filter_by(token=token).one()
        return dict(id=r.id, status=r.status, deposit=r.deposit_status, amount=r.deposit_amount, trade=r.deposit_trade_no,
                    note=r.deposit_note)


def test_deposit_pay_refund_on_cancel_and_seat(app, staff_client, deposits, no_rate_limit, alerts):
    calls, state = deposits
    c = app.test_client()
    c.get('/?shop=default')
    # 2 位不收訂金
    r = _book(c, '0966300001', 2)
    assert r.status_code == 302 and '/pay' not in r.headers['Location']
    # 4 位：先付訂金
    r = _book(c, '0966300002', 4)
    assert r.headers['Location'].endswith('/pay')
    token = r.headers['Location'].split('/')[-2]
    info = _res(app, token)
    assert (info['deposit'], info['amount']) == ('pending', 400)
    pay = c.get(f'/reserve/{token}/pay')
    html = pay.get_data(as_text=True)
    assert 'name="TotalAmount" value="400"' in html and 'reservation_notify' in html and f'value="R:{token}"' in html
    assert 'payment-stage.ecpay.com.tw' in pay.headers['Content-Security-Policy']
    first_trade = _res(app, token)['trade']
    assert first_trade != info['trade']                       # 每次開付款頁都換新的交易編號
    c.get(f'/reserve/{token}/pay')                            # 第一次付款失敗、重新開啟付款頁
    second_trade = _res(app, token)['trade']
    assert second_trade != first_trade
    assert '等待付款訂金' in c.get(f'/reserve/{token}').get_data(as_text=True)
    assert _notify(c, app, second_trade, 400, tamper=True) == '0|CheckMacValue Error'
    # 綠界帶回 CustomField1 (訂位代碼)：即使付款的是較早開啟的那一頁也找得到
    assert _notify(c, app, first_trade, 400, custom=f'R:{token}') == '1|OK'
    assert _notify(c, app, first_trade, 400, custom=f'R:{token}') == '1|OK'      # 重複通知
    assert _res(app, token)['deposit'] == 'paid' and _res(app, token)['trade'] == first_trade
    # 兩個付款頁都付了：第二筆通知店家退款
    assert _notify(c, app, second_trade, 400, custom=f'R:{token}', provider='ECDUP0001') == '1|OK'
    assert any('重複付款' in a for a in alerts)
    # 顧客 3 天前取消 → 自動退刷
    c.post(f'/reserve/{token}', data={'action': 'cancel'})
    assert_flash(c, '將退回原信用卡')
    assert _res(app, token)['deposit'] == 'refunded'
    assert calls[-1]['Action'] == 'R' and calls[-1]['TotalAmount'] == '400'

    # 入座 → 退還保證金；退刷失敗 → 標記並通知
    r = _book(c, '0966300003', 5, name='入座客')
    token2 = r.headers['Location'].split('/')[-2]
    _notify(c, app, _res(app, token2)['trade'], 500)
    state['code'] = '10200047'
    staff_client.post('/reservations', data={'action': 'reservation_status', 'reservation_id': _res(app, token2)['id'],
                                             'status': 'seated'})
    assert _res(app, token2)['deposit'] == 'refund_failed'
    assert any('訂金 $500 退款失敗' in a for a in alerts)
    board = staff_client.get(f'/reservations?day={date.today() + timedelta(days=3)}').get_data(as_text=True)
    assert '訂金退款失敗' in board and 'data-deposit="refund_failed"' in board
    state['code'] = '1'


def test_no_show_forfeit_late_cancel_expiry_and_block(app, staff_client, deposits, no_rate_limit, alerts):
    from app.services import deposit_service
    calls, _state = deposits
    c = app.test_client()
    c.get('/?shop=default')
    r = _book(c, '0966300010', 4, name='未到客')
    token = r.headers['Location'].split('/')[-2]
    _notify(c, app, _res(app, token)['trade'], 400)
    n_calls = len(calls)
    staff_client.post('/reservations', data={'action': 'reservation_status', 'reservation_id': _res(app, token)['id'],
                                             'status': 'no_show'})
    assert _res(app, token)['deposit'] == 'forfeited' and len(calls) == n_calls
    board = staff_client.get(f'/reservations?day={date.today() + timedelta(days=3)}').get_data(as_text=True)
    assert '未到 1 次' in board
    # 未到 1 次 → 暫停線上訂位
    _book(c, '0966300010', 2)
    assert_flash(c, '暫停線上訂位')

    # 訂位前 24 小時內取消：訂金不退
    with app.app_context():
        tid = Tenant.query.filter_by(slug='default').first().id
        late = Reservation(tenant_id=tid, name='臨時取消', phone='0966300011', party_size=4,
                           reserved_at=datetime.now() + timedelta(hours=2), source='online', deposit_amount=400,
                           deposit_status='paid', deposit_trade_no='KTESTLATE0001', deposit_provider_no='EC1')
        pend = Reservation(tenant_id=tid, name='沒付款', phone='0966300012', party_size=4,
                           reserved_at=datetime.now() + timedelta(days=2), source='online', deposit_amount=400,
                           deposit_status='pending', deposit_trade_no='KTESTPEND0001',
                           created_at=datetime.now() - timedelta(minutes=31))
        db.session.add_all([late, pend])
        db.session.commit()
        late_token, pend_token = late.token, pend.token
    c.post(f'/reserve/{late_token}', data={'action': 'cancel'})
    assert_flash(c, '訂金 $400 不退還')
    assert _res(app, late_token)['deposit'] == 'forfeited'
    # 30 分鐘未付款 → 自動取消；之後才付款成功 → 立即退還
    with app.app_context():
        assert deposit_service.expire_pending(tid) == 1
    assert (_res(app, pend_token)['status'], _res(app, pend_token)['deposit']) == ('cancelled', 'expired')
    assert _notify(c, app, 'KTESTPEND0001', 400) == '1|OK'
    assert _res(app, pend_token)['deposit'] == 'refunded'
    assert calls[-1]['MerchantTradeNo'] == 'KTESTPEND0001'


def test_customer_cancel_and_staff_seat_at_same_time_refund_once(app, staff_client, deposits, no_rate_limit):
    calls, _state = deposits
    c = app.test_client()
    c.get('/?shop=default')
    r = _book(c, '0966300020', 4, name='同時客', hour=19)
    token = r.headers['Location'].split('/')[-2]
    _notify(c, app, _res(app, token)['trade'], 400)
    before = len(calls)
    rid = _res(app, token)['id']
    staff2 = login_staff(app, 'res_staff2', role='manager')
    results = run_together(
        lambda: c.post(f'/reserve/{token}', data={'action': 'cancel'}).status_code,
        lambda: staff2.post('/reservations', data={'action': 'reservation_status', 'reservation_id': rid,
                                                   'status': 'seated'}).status_code)
    assert all(code in (200, 302) for code in results), results
    assert len(calls) - before == 1, calls[before:]
    assert _res(app, token)['deposit'] == 'refunded'
    assert _res(app, token)['status'] in ('cancelled', 'seated')


def test_reservation_reminder_by_line(app, line_api, store_settings):  # noqa: F811
    from app.services import deposit_service
    posts, _state = line_api
    store_settings('reservation', reminder_hours=3)
    make_member(app, '提醒會員', '0966300030', line_user_id='U' + 'r' * 32)
    with app.app_context():
        tid = Tenant.query.filter_by(slug='default').first().id
        db.session.add(Reservation(tenant_id=tid, name='提醒會員', phone='0966300030', party_size=2,
                                   reserved_at=datetime.now() + timedelta(hours=2)))
        db.session.add(Reservation(tenant_id=tid, name='太晚', phone='0966300030', party_size=2,
                                   reserved_at=datetime.now() + timedelta(hours=5)))
        db.session.commit()
        assert deposit_service.send_reminders(tid) == 1
        assert deposit_service.send_reminders(tid) == 0
    sent = [kw['json'] for url, kw in posts if url == line_service.PUSH_URL]
    assert sent[-1]['to'] == 'U' + 'r' * 32 and '2 位的訂位' in sent[-1]['messages'][0]['text']


# ===========================================================================
# 12. 員工異常偵測
# ===========================================================================
def test_staff_anomalies_flags_page_and_notify(app, alerts):
    from app.services import settings_service, staff_anomaly_service
    tid = _tenant(app, 'anomaly-shop')
    now = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=12)   # 固定今天中午 (避免跨午夜)
    with _ctx(app, tid):
        settings_service.put('anomaly', {'notify': True}, tid)
        item = MenuItem(tenant_id=tid, name='異常測試餐', price=100, stock=999, total_stock=999)
        db.session.add(item)
        db.session.flush()

        def order(cashier, total=100, manual=0, override=None, status='PickedUp', minutes=0):
            o = Order(tenant_id=tid, total_price=total, cashier_username=cashier, manual_discount=manual, status=status,
                      payment_method='Cash', order_type='內用', created_at=now - timedelta(minutes=minutes),
                      pickup_number=1)
            db.session.add(o)
            db.session.flush()
            db.session.add(OrderItem(tenant_id=tid, order_id=o.id, menu_item_id=item.id, item_name=item.name,
                                     price=total, quantity=1, original_price=override))
            return o
        for i in range(10):
            order('good', minutes=i)
            order('fine', minutes=i)
        bad_orders = [order('bad', minutes=i) for i in range(10)]
        for o in bad_orders[:4]:   # 作廢 4 筆
            o.status = 'Cancelled'
            db.session.add(Refund(tenant_id=tid, order_id=o.id, kind='cancel', amount=100, staff_username='bad'))
        db.session.add(Refund(tenant_id=tid, order_id=bad_orders[5].id, kind='partial', amount=30, staff_username='good'))
        for i in range(3):   # fine：改價 3 次、手動折扣
            order('fine', total=50, override=100, manual=20)
        db.session.add(CashShift(tenant_id=tid, status='closed', opened_by='bad', closed_by='bad', closed_at=now,
                                 expected_cash=5000, counted_cash=4500, difference=-500))
        db.session.commit()
        result = staff_anomaly_service.analyze(tid, date.today(), date.today())
        by = {r['name']: r for r in result['staff']}
        assert by['bad']['flags'] == ['void', 'shift'] and by['bad']['void_rate'] == 40.0
        assert by['bad']['cancel_count'] == 4 and by['bad']['shift_short'] == 500
        assert by['good']['flags'] == [] and by['good']['partial_amount'] == 30
        assert by['fine']['flags'] == ['discount'] and by['fine']['override_amount'] == 150
        assert result['staff'][0]['name'] == 'bad'
        assert staff_anomaly_service.notify_daily(tid, date.today() + timedelta(days=1)) == 2
    assert any('bad：作廢 / 退款偏高、交班短少' in a for a in alerts)
    owner = login_staff(app, 'anomaly_owner', 'owner', tenant_id=tid)
    page = owner.get(f'/admin/staff-anomalies?start={date.today()}&end={date.today()}').get_data(as_text=True)
    assert 'data-staff="bad"' in page and '作廢 / 退款偏高' in page and '折扣 / 改價偏高' in page
    assert login_staff(app, 'anomaly_staff', 'staff', tenant_id=tid).get('/admin/staff-anomalies').status_code == 403


def test_shift_close_with_large_difference_alerts(app, alerts, store_settings):
    tid = _tenant(app, 'anomaly-shop')
    owner = login_staff(app, 'anomaly_owner', 'owner', tenant_id=tid)
    assert owner.post('/api/pos/shift/open', json={'opening_cash': 1000}).status_code == 200
    r = owner.post('/api/pos/shift/close', json={'counted_cash': 700})
    assert r.status_code == 200, r.get_json()
    assert any('交班帳差過大' in a and '-300' in a for a in alerts), alerts


def test_marketing_opt_out_syncs_to_hub():
    from tests.test_sync_hub_node import Env, _member, _register_member, tenant_ctx
    env = Env()
    try:
        _register_member(env.a, '同步拒絕行銷', '0955300001')
        env.sync_all()
        with tenant_ctx(env.a):
            User.query.filter_by(phone='0955300001').one().marketing_opt_out = True
            db.session.commit()
        env.sync_all()
        for app_ in (env.hub, env.b):
            with app_.app_context():
                assert User.query.filter_by(phone='0955300001').one().marketing_opt_out is True
        assert _member(env.hub, '0955300001') is not None
    finally:
        env.close()
