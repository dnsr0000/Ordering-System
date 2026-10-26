"""第三輪 #4~#6：桌位平面圖、桌邊線上結帳 (綠界)、寄杯 / 多次券"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db, limiter
from app.models.member import MemberPass, PassProduct
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.payment import PaymentRecord
from app.models.table import DiningTable, TablePayment
from tests.support import checkout, default_tenant_id, line, login_staff, make_item, make_member, member_client, \
    run_together

POS = '/api/pos/checkout'


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


def _make_table(app, admin_client, name):
    admin_client.post('/admin/tables', data={'action': 'create', 'names': name})
    with app.app_context():
        t = DiningTable.query.filter_by(name=name).one()
        return t.id, t.token


def _phone(app, token):
    c = app.test_client()
    c.get(f'/t/{token}')
    c.post('/guest_login')
    return c


# ===========================================================================
# 4. 桌位平面圖
# ===========================================================================
def test_floor_plan_layout_details_and_pos_status(app, admin_client, store_settings):
    from app.models.order import ServiceCall
    from app.models.reservation import Reservation
    ids = {n: _make_table(app, admin_client, n)[0] for n in ('FP1', 'FP2', 'FP3', 'FP4')}
    r = admin_client.post('/admin/tables', data={'action': 'rename', 'table_id': ids['FP1'], 'name': 'FP1', 'sort_order': 0,
                                                 'seats': 6, 'area': '戶外', 'shape': 'round'}, follow_redirects=True)
    assert r.status_code == 200
    bad = admin_client.post('/admin/tables', data={'action': 'rename', 'table_id': ids['FP2'], 'name': 'FP2', 'seats': 0},
                            follow_redirects=True)
    assert '座位數需介於' in bad.get_data(as_text=True)
    with app.app_context():
        t = db.session.get(DiningTable, ids['FP1'])
        assert (t.seats, t.area, t.shape) == (6, '戶外', 'round')

    r = admin_client.post('/admin/tables/layout', json={'tables': [
        {'id': ids['FP1'], 'x': 120, 'y': 2000}, {'id': ids['FP2'], 'x': 500, 'y': 300}, {'id': ids['FP3'], 'x': None, 'y': None}]})
    assert r.get_json()['updated'] == 3
    with app.app_context():
        assert (db.session.get(DiningTable, ids['FP1']).pos_x, db.session.get(DiningTable, ids['FP1']).pos_y) == (120, 1000)
        assert db.session.get(DiningTable, ids['FP3']).pos_x is None
    page = admin_client.get('/admin/tables').get_data(as_text=True)
    assert 'id="floorPlanEditor"' in page and 'value="戶外"' in page

    # POS 平面圖狀態：用餐中 / 呼叫中 / 即將訂位 / 空桌
    item = make_item(app, '平面圖餐', price=150, stock=20)
    tokens = {}
    with app.app_context():
        for n in ('FP1', 'FP2'):
            tokens[n] = db.session.get(DiningTable, ids[n]).token
    checkout(_phone(app, tokens['FP1']), [line(item, 2)])
    _phone(app, tokens['FP2']).post('/api/table/call', json={'kind': 'water'})
    with app.app_context():
        db.session.add(Reservation(tenant_id=default_tenant_id(app), name='平面圖訂位', phone='0912000111', party_size=2,
                                   reserved_at=datetime.now() + timedelta(minutes=30), table_name='FP3'))
        db.session.commit()
    floor = admin_client.get('/api/pos/floor').get_json()
    by = {t['name']: t for t in floor['tables'] if t['name'].startswith('FP')}
    assert by['FP1']['status'] == 'occupied' and by['FP1']['total'] == 300 and by['FP1']['orders'] == 1
    assert by['FP2']['status'] == 'calling' and by['FP2']['calls'] == ['加水 / 餐具']
    assert by['FP3']['status'] == 'reserved' and '平面圖訂位' in by['FP3']['reservation']
    assert by['FP4']['status'] == 'free'
    assert '戶外' in floor['areas']
    with app.app_context():
        for c in ServiceCall.query.filter_by(dining_table_id=ids['FP2']).all():
            c.handled_at = datetime.now()
        db.session.commit()


# ===========================================================================
# 6. 桌邊線上結帳
# ===========================================================================
def _ecpay_post(app, client, path, trade_no, amount, rtn='1', trade='2410240001', tamper=False):
    from app.services import payment_ecpay
    params = {'MerchantID': '3002607', 'MerchantTradeNo': trade_no, 'RtnCode': rtn, 'RtnMsg': '交易成功' if rtn == '1' else '失敗',
              'TradeNo': trade, 'TradeAmt': str(amount), 'PaymentDate': '2026/10/24 12:00:00', 'PaymentType': 'Credit_CreditCard',
              'SimulatePaid': '0', 'CustomField1': f'T:{trade_no}'}
    with app.app_context():
        _m, key, iv, _b = payment_ecpay._cfg()
        params['CheckMacValue'] = payment_ecpay.check_mac_value(params, key, iv)
    if tamper:
        params['TradeAmt'] = str(int(amount) + 1)
    return client.post(path, data=params)


def test_table_online_payment_settles_the_table(app, admin_client, store_settings):
    item = make_item(app, '線上結帳餐', price=120, stock=20)
    table_id, token = _make_table(app, admin_client, 'PAY1')
    phone = _phone(app, token)
    checkout(phone, [line(item)])
    checkout(phone, [line(item, 2)])
    assert phone.post('/api/table/pay').status_code == 404, '未啟用'
    store_settings('table_pay', enabled=True)
    bill = phone.get('/api/table/bill').get_json()
    assert (bill['total'], len(bill['orders']), bill['online_pay']) == (360, 2, True)
    page = phone.get('/').get_data(as_text=True)
    assert 'id="tableBillBtn"' in page

    r = phone.post('/api/table/pay')
    assert r.status_code == 200 and r.get_json()['amount'] == 360
    form = phone.get(r.get_json()['payment_url'])
    assert form.status_code == 200 and 'payment-stage.ecpay.com.tw' in form.headers['Content-Security-Policy']
    html = form.get_data(as_text=True)
    assert 'name="TotalAmount" value="360"' in html and '/payment/ecpay/table_notify' in html
    with app.app_context():
        tp = TablePayment.query.filter_by(dining_table_id=table_id).one()
        trade_no = tp.trade_no

    anon = app.test_client()
    assert _ecpay_post(app, anon, '/payment/ecpay/table_notify', trade_no, 360, tamper=True).get_data(as_text=True) \
        == '0|CheckMacValue Error'
    assert _ecpay_post(app, anon, '/payment/ecpay/table_notify', trade_no, 360).get_data(as_text=True) == '1|OK'
    assert _ecpay_post(app, anon, '/payment/ecpay/table_notify', trade_no, 360).get_data(as_text=True) == '1|OK'
    with app.app_context():
        tp = TablePayment.query.filter_by(trade_no=trade_no).one()
        assert tp.status == 'paid'
        orders = Order.query.filter_by(dining_table_id=table_id).all()
        assert {(o.payment_status, o.payment_method) for o in orders} == {('settled', 'Credit Card')}
        recs = PaymentRecord.query.filter_by(kind='settle', reference='2410240001').all()
        assert [(p.method, p.amount) for p in recs] == [('Credit Card', 360)], '重複通知只結帳一次'
    assert phone.get(f'/table/pay/status/{trade_no}').get_data(as_text=True).count('結帳完成') == 1
    assert phone.get('/api/table/bill').get_json()['total'] == 0


def test_table_online_payment_conflicts_are_flagged(app, admin_client, store_settings, monkeypatch):
    from app.services import monitor_service
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append(msg))
    store_settings('table_pay', enabled=True)
    item = make_item(app, '結帳衝突餐', price=100, stock=20)
    table_id, token = _make_table(app, admin_client, 'PAY2')
    phone = _phone(app, token)
    checkout(phone, [line(item)])
    phone.post('/api/table/pay')
    with app.app_context():
        trade_no = TablePayment.query.filter_by(dining_table_id=table_id).one().trade_no
    # 顧客在付款頁時，櫃檯已經結帳了
    assert admin_client.post(f'/api/pos/tables/{table_id}/settle', json={'payment_method': 'Cash',
                                                                        'cash_received': 100}).status_code == 200
    assert _ecpay_post(app, app.test_client(), '/payment/ecpay/table_notify', trade_no, 100,
                       trade='2410249901').get_data(as_text=True) == '1|OK'
    with app.app_context():
        tp = TablePayment.query.filter_by(trade_no=trade_no).one()
        assert tp.status == 'conflict' and tp.provider_trade_no == '2410249901'
        assert PaymentRecord.query.filter_by(kind='settle', reference='2410249901').count() == 0
    assert any('PAY2' in a and '退款' in a for a in alerts), alerts

    # 付款期間作廢品項 (金額變了) → 也是 conflict
    checkout(phone, [line(item, 2)])
    phone.post('/api/table/pay')
    with app.app_context():
        tp = TablePayment.query.filter_by(dining_table_id=table_id, status='pending').one()
        trade_no = tp.trade_no
        oi = db.session.get(Order, tp.order_id_list[0]).items[0]
    admin_client.post(f'/api/pos/orders/{oi.order_id}/void_item', json={'item_id': oi.id, 'quantity': 1, 'reason': '少一份'})
    r = _ecpay_post(app, app.test_client(), '/payment/ecpay/table_result', trade_no, 200, trade='2410240002')
    assert r.status_code == 303
    with app.app_context():
        assert TablePayment.query.filter_by(trade_no=trade_no).one().status == 'conflict'
        assert db.session.get(Order, oi.order_id).payment_status == 'unpaid'


# ===========================================================================
# 5. 寄杯 / 多次券
# ===========================================================================
def _product(app, admin_client, item, uses=3, price=240, name='寄杯美式'):
    r = admin_client.post('/admin/passes', data={'action': 'save', 'name': name, 'menu_item_id': item, 'uses': uses,
                                                 'price': price})
    assert r.status_code == 302
    with app.app_context():
        return PassProduct.query.filter_by(name=name).one().id


def _sell(client, uid, product, **kw):
    return client.post('/api/pos/passes/sell', json=dict(user_id=uid, product_id=product, payment_method='Cash',
                                                         cash_received=500, **kw))


def _remaining(app, pass_id):
    with app.app_context():
        return db.session.get(MemberPass, pass_id).remaining


def test_pass_sell_redeem_and_cancel(app, admin_client):
    coffee = make_item(app, '寄杯咖啡', price=90, stock=50)
    tea = make_item(app, '寄杯紅茶', price=40, stock=50)
    bad = admin_client.post('/admin/passes', data={'action': 'save', 'name': 'x', 'menu_item_id': coffee, 'uses': 1,
                                                   'price': 10}, follow_redirects=True)
    assert '次數需介於' in bad.get_data(as_text=True)
    product = _product(app, admin_client, coffee)
    uid = make_member(app, '寄杯會員', '0968200001', pin='2468')
    other = make_member(app, '別人', '0968200002')
    pos = login_staff(app, 'pass_pos', 'staff')

    assert _sell(pos, None, product).status_code == 400
    r = _sell(pos, uid, product)
    assert r.status_code == 200 and r.get_json()['cash_change'] == 260
    pass_id, sale_order = r.get_json()['pass']['id'], r.get_json()['order_id']
    assert _remaining(app, pass_id) == 3
    with app.app_context():
        o = db.session.get(Order, sale_order)
        assert (o.total_price, o.status, o.note) == (240, 'PickedUp', '寄杯購買')
        assert PaymentRecord.query.filter_by(order_id=sale_order).one().amount == 240
    member = pos.post('/api/pos/member_lookup', json={'member_id': uid}).get_json()['member']
    assert member['passes'][0]['remaining'] == 3
    assert '寄杯會員' in admin_client.get('/admin/passes').get_data(as_text=True)

    # POS 兌換：免費、扣庫存、扣次數；別人的寄杯 / 不同餐點不行
    r = checkout(pos, [line(coffee, pass_id=pass_id), line(tea)], url=POS, user_id=uid, cash_received=100)
    assert r.status_code == 200 and r.get_json()['total_price'] == 40
    redeem_order = r.get_json()['order_id']
    assert _remaining(app, pass_id) == 2
    assert checkout(pos, [line(coffee, pass_id=pass_id)], url=POS, user_id=other).status_code == 400
    assert checkout(pos, [line(tea, pass_id=pass_id)], url=POS, user_id=uid).status_code == 400
    # 點餐機 (會員登入) 兌換
    kiosk = member_client(app, '0968200001', '2468', ip='10.68.2.1')
    assert '我的寄杯' in kiosk.get('/').get_data(as_text=True)
    r = checkout(kiosk, [line(coffee, pass_id=pass_id)])
    assert r.status_code == 200 and r.get_json()['total_price'] == 0
    assert _remaining(app, pass_id) == 1
    assert checkout(kiosk, [line(coffee, 2, pass_id=pass_id)]).status_code == 409

    # 售出訂單已使用 → 不能整筆作廢；兌換訂單取消 → 退回次數
    assert admin_client.post(f'/api/orders/{sale_order}/status', json={'status': 'Cancelled'}).status_code == 409
    assert admin_client.post(f'/api/orders/{redeem_order}/status', json={'status': 'Cancelled'}).status_code == 200
    assert _remaining(app, pass_id) == 2
    # 新的一張沒用過 → 可整筆作廢 (寄杯一併作廢)
    r2 = _sell(pos, uid, product).get_json()
    assert admin_client.post(f"/api/orders/{r2['order_id']}/status", json={'status': 'Cancelled'}).status_code == 200
    with app.app_context():
        mp = db.session.get(MemberPass, r2['pass']['id'])
        assert (mp.status, mp.remaining) == ('void', 0)


def test_pass_redeemed_from_every_channel_at_once(app, admin_client, no_rate_limit):
    latte = make_item(app, '同時寄杯拿鐵', price=100, stock=50)
    product = _product(app, admin_client, latte, uses=2, price=180, name='同時寄杯')
    uid = make_member(app, '同時寄杯會員', '0968200003', pin='2468')
    pos = login_staff(app, 'pass_pos2', 'staff')
    pass_id = _sell(pos, uid, product).get_json()['pass']['id']
    kiosks = [member_client(app, '0968200003', '2468', ip=f'10.68.3.{n}') for n in range(3)]
    jobs = [lambda c=c: checkout(c, [line(latte, pass_id=pass_id)]) for c in kiosks]
    jobs += [lambda: checkout(login_staff(app, 'pass_pos2'), [line(latte, pass_id=pass_id)], url=POS, user_id=uid)] * 3
    results = run_together(*jobs)
    assert sorted(r.status_code for r in results) == [200, 200, 409, 409, 409, 409], [r.get_json() for r in results]
    assert all('剩餘' in r.get_json()['message'] for r in results if r.status_code == 409)
    assert _remaining(app, pass_id) == 0


def test_pass_sync_between_branches_and_online():
    from app.services import monitor_service
    from tests.test_sync_hub_node import Env, _add_item, _checkout, _item, _register_member, tenant_ctx
    env = Env()
    alerts = []
    real_alert = monitor_service.alert
    monitor_service.alert = lambda msg, **kw: alerts.append(msg)
    try:
        coffee = _add_item(env.a, '跨店寄杯咖啡', price=90, stock=50)
        _register_member(env.a, '跨店寄杯會員', '0968200009')
        env.sync_all()
        from app.services import pass_service
        with tenant_ctx(env.a) as tenant:
            item = MenuItem.query.filter_by(uuid=coffee).one()
            product = PassProduct(tenant_id=tenant.id, name='跨店寄杯', menu_item_id=item.id, uses=2, price=160)
            db.session.add(product)
            db.session.flush()
            from app.models.user import User
            user = User.query.filter_by(phone='0968200009').one()
            mp, _order = pass_service.sell(tenant.id, product.id, user, 'Cash', 'a_pos', cash_received=200)
            db.session.commit()
            pass_uuid = mp.uuid
        env.sync_all()

        def local_pass(app_):
            with tenant_ctx(app_):
                m = MemberPass.query.filter_by(uuid=pass_uuid).one()
                return m.id, m.remaining, m.user_id

        for app_ in (env.hub, env.b):
            assert local_pass(app_)[1] == 2
        # 線上點餐兌換一杯 (線上端)
        hp, _r, huid = local_pass(env.hub)
        _checkout(env.hub, [line(_item(env.hub, coffee)['id'], pass_id=hp)], node_uuid=env.node_a_uuid, user_id=huid)
        # 分店 B 同步前也兌換一杯 (它以為還有 2 杯)
        bp, _r, buid = local_pass(env.b)
        _checkout(env.b, [line(_item(env.b, coffee)['id'], pass_id=bp)], order_source='POS', cashier_username='b_pos',
                  user_id=buid)
        # 分店 A 同步前也兌換一杯 (同時用完)
        ap, _r, auid = local_pass(env.a)
        _checkout(env.a, [line(_item(env.a, coffee)['id'], pass_id=ap)], order_source='POS', cashier_username='a_pos',
                  user_id=auid)
        env.sync_all(rounds=2)
        for app_ in (env.hub, env.a, env.b):
            assert local_pass(app_)[1] == -1, app_.config['SYNC_ROLE']
        assert any('寄杯' in a and '跨店寄杯會員' in a for a in alerts), alerts
    finally:
        monitor_service.alert = real_alert
        env.close()
