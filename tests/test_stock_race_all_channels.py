"""
各通路同時搶同一份庫存：自助點餐機、POS 收銀、桌邊掃碼、Uber Eats / foodpanda Webhook、POS 手動登錄外送單、
POS 外送到府，以及線上端的線上點餐與分店現場銷售 (兩個資料庫) 之間的競爭。
驗證：不超賣、庫存不為負、失敗的一方得到明確的「庫存不足」(平台訂單拒單)、取餐號不重複、同一張平台單重送不重複建立。
"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.extensions import db, limiter
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from tests.support import checkout, guest_client, line, login_staff, make_item, run_together
from tests.test_delivery_and_reservations import _uber_post, delivery_on  # noqa: F401  (pytest fixture)

FP_HEADERS = {'Authorization': 'Bearer fp-token'}


@pytest.fixture
def no_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True


def _stock(app, item_id):
    with app.app_context():
        m = db.session.get(MenuItem, item_id)
        return m.stock, bool(m.is_sold_out)


def _sold(app, item_id):
    """這個品項在未取消的訂單中賣出的份數，與訂單清單"""
    with app.app_context():
        rows = db.session.query(Order.id, Order.order_source, Order.pickup_number, OrderItem.quantity) \
            .join(OrderItem, OrderItem.order_id == Order.id) \
            .filter(OrderItem.menu_item_id == item_id, Order.status != 'Cancelled').all()
    return sum(r.quantity for r in rows), rows


def _uber_event(order_id):
    return {'event_type': 'orders.notification', 'meta': {'user_id': 'store-uuid-1', 'resource_id': order_id},
            'resource_href': f'https://api.uber.com/v2/eats/order/{order_id}'}


def _table_phone(app, admin_client, name):
    from app.models.table import DiningTable
    admin_client.post('/admin/tables', data={'action': 'create', 'names': name})
    with app.app_context():
        token = DiningTable.query.filter_by(name=name).one().token
    c = app.test_client()
    c.get(f'/t/{token}')
    c.post('/guest_login')
    return c


# ===========================================================================
# 門市：所有通路同時搶 6 份
# ===========================================================================
def test_every_store_channel_competes_for_the_same_stock(app, admin_client, delivery_on, store_settings,  # noqa: F811
                                                        no_rate_limit):
    calls, carts = delivery_on
    store_settings('own_delivery', enabled=True, zones='近:30:0', channels=['pos'])
    item = make_item(app, '搶購限量便當', price=120, stock=6, external_code='RACE-BOX')
    for n in range(2):
        carts[f'race-ue-{n}'] = {'display_id': f'RUE{n}', 'cart': {'items': [
            {'external_data': 'RACE-BOX', 'title': '便當', 'quantity': 1}]}}
    login_staff(app, 'race_pos', 'staff')   # 先建立帳號 (避免多個執行緒同時建立同名帳號)
    kiosks = [guest_client(app) for _ in range(3)]
    phones = [_table_phone(app, admin_client, f'RC{n}') for n in range(2)]

    def pos():
        return login_staff(app, 'race_pos')

    def kiosk(c):
        return lambda: ('kiosk', checkout(c, [line(item)]))

    def table(c):
        return lambda: ('table', checkout(c, [line(item)]))

    def pos_sale(n):
        return lambda: ('pos', checkout(pos(), [line(item)], url='/api/pos/checkout', cash_received=500))

    def uber(n):
        return lambda: ('uber', _uber_post(app.test_client(), _uber_event(f'race-ue-{n}')))

    def foodpanda(n):
        payload = {'code': f'race-fp-{n}', 'products': [{'remoteCode': 'RACE-BOX', 'name': '便當', 'quantity': 1}]}
        return lambda: ('foodpanda', app.test_client().post('/webhooks/foodpanda/v1ab', json=payload, headers=FP_HEADERS))

    def manual_delivery():
        return ('manual', pos().post('/api/pos/delivery_order', json={
            'platform': '其他平台', 'external_ref': 'RACE-MANUAL', 'items': [line(item)]}))

    def home_delivery():
        return ('home', pos().post('/api/pos/checkout', json={
            'items': [line(item)], 'payment_method': 'Cash', 'cash_received': 500, 'order_type': '外送到府',
            'delivery': {'address': '台南市東區大學路 1 號', 'phone': '0912345678', 'zone': '近'}}))

    jobs = ([kiosk(c) for c in kiosks] + [table(c) for c in phones] + [pos_sale(n) for n in range(3)]
            + [uber(n) for n in range(2)] + [foodpanda(n) for n in range(2)] + [manual_delivery, home_delivery])
    results = run_together(*jobs)

    # 每個失敗的 HTTP 回應都是明確的「庫存不足」，沒有 500 或其他錯誤
    for kind, r in results:
        if kind == 'uber':
            assert r.status_code == 200
            continue
        assert r.status_code in (200, 400, 409, 422), (kind, r.status_code, r.get_data(as_text=True)[:200])
        if r.status_code != 200:
            body = r.get_json() or {}
            text = body.get('message') or body.get('reason') or ''
            assert body.get('insufficient_stock') or '庫存' in text or '售完' in text, (kind, body)

    sold, rows = _sold(app, item)
    assert sold == 6, f'只能賣出 6 份，實際 {sold}：{rows}'
    assert _stock(app, item) == (0, True), '庫存歸零並標示售完 (不會變成負數)'
    pickups = [r.pickup_number for r in rows]
    assert len(set(pickups)) == len(pickups), '取餐號不重複'

    # Uber Eats：建立成功的接單、失敗的拒單 (每張單恰好一個結果)
    accepted = {c[1].split('/')[-2] for c in calls if c[1].endswith('/accept_pos_order')}
    denied = {c[1].split('/')[-2] for c in calls if c[1].endswith('/deny_pos_order')}
    uber_orders = {r.id for r in rows if r.order_source == 'UberEats'}
    assert accepted | denied == {'race-ue-0', 'race-ue-1'} and not accepted & denied
    assert len(accepted) == len(uber_orders)


def test_stock_is_exact_after_concurrent_sales_and_cancellations_across_channels(app, admin_client, delivery_on,  # noqa: F811
                                                                                no_rate_limit):
    """一邊各通路同時賣、一邊同時取消 (含平台訂單)：最後庫存 = 期初 - 賣出 + 取消"""
    item = make_item(app, '併發回補便當', price=100, stock=30, external_code='RACE-UNDO')
    login_staff(app, 'race_pos2', 'staff')
    login_staff(app, 'race_mgr', 'manager')
    pre = [checkout(login_staff(app, 'race_pos2'), [line(item)], url='/api/pos/checkout', cash_received=100)
           .get_json()['order_id'] for _ in range(3)]
    fp = app.test_client().post('/webhooks/foodpanda/v1ab', headers=FP_HEADERS, json={
        'code': 'undo-fp', 'products': [{'remoteCode': 'RACE-UNDO', 'name': '便當', 'quantity': 2}]})
    pre.append(int(fp.get_json()['remoteResponse']['remoteOrderId']))
    assert _stock(app, item)[0] == 25

    def cancel(oid):
        return lambda: login_staff(app, 'race_mgr').post(f'/api/orders/{oid}/status', json={'status': 'Cancelled'})

    def sell(n):
        if n % 2:
            return lambda: checkout(guest_client(app), [line(item, 2)])
        return lambda: checkout(login_staff(app, 'race_pos2'), [line(item, 2)], url='/api/pos/checkout', cash_received=500)

    results = run_together(*([cancel(oid) for oid in pre] + [sell(n) for n in range(8)]))
    assert all(r.status_code == 200 for r in results), [r.get_data(as_text=True)[:120] for r in results if r.status_code != 200]
    assert _stock(app, item)[0] == 30 - 5 + 5 - 16 == 14


# ===========================================================================
# 同一張平台訂單同時重送 / 同時被手動登錄：只建立一張、只扣一次庫存
# ===========================================================================
def test_duplicate_platform_deliveries_create_one_order(app, delivery_on, no_rate_limit):  # noqa: F811
    calls, carts = delivery_on
    item = make_item(app, '重送測試便當', price=100, stock=50, external_code='DUP-BOX')
    carts['dup-ue'] = {'display_id': 'DUPUE', 'cart': {'items': [{'external_data': 'DUP-BOX', 'title': '便當', 'quantity': 2}]}}
    fp_payload = {'code': 'dup-fp', 'products': [{'remoteCode': 'DUP-BOX', 'name': '便當', 'quantity': 3}]}
    login_staff(app, 'dup_pos', 'staff')

    def uber():
        return _uber_post(app.test_client(), _uber_event('dup-ue'))

    def fp():
        return app.test_client().post('/webhooks/foodpanda/v1ab', json=fp_payload, headers=FP_HEADERS)

    def manual():
        return login_staff(app, 'dup_pos').post('/api/pos/delivery_order', json={
            'platform': 'foodpanda', 'external_ref': 'dup-fp', 'items': [line(item, 3)]})

    results = run_together(*([uber] * 4 + [fp] * 4 + [manual]))
    assert all(r.status_code in (200, 409) for r in results), [r.status_code for r in results]
    with app.app_context():
        assert Order.query.filter_by(external_ref='DUPUE').count() == 1, 'Uber 同一張單重送只建立一次'
        assert Order.query.filter_by(external_ref='dup-fp').count() == 1, 'foodpanda 重送與手動登錄只建立一次'
    assert _stock(app, item)[0] == 50 - 2 - 3
    fp_ids = {r.get_json()['remoteResponse']['remoteOrderId'] for r in results[4:8] if r.status_code == 200}
    assert len(fp_ids) == 1, '重送都回應同一個訂單編號'


# ===========================================================================
# 線上端 ↔ 分店：兩個資料庫之間的庫存競爭
# ===========================================================================
@pytest.fixture(scope='module')
def env():
    from tests.test_sync_hub_node import Env, _add_item, _register_member
    e = Env()
    e.box = _add_item(e.b, '跨店限量便當', price=150, stock=5)
    for n in range(8):
        _register_member(e.b, f'搶購會員{n}', f'09665{n:05d}')
    e.sync_all()
    yield e
    e.close()


def _b_stock(env):
    from tests.test_sync_hub_node import _item
    return _item(env.b, env.box)['stock']


def _set_b_stock(env, qty):
    from tests.test_sync_hub_node import tenant_ctx
    env.sync(env.b, rounds=2)   # 先收完之前的線上訂單
    with tenant_ctx(env.b):
        m = MenuItem.query.filter_by(uuid=env.box).one()
        m.stock, m.total_stock, m.is_sold_out = qty, max(m.total_stock or 0, qty), False
        db.session.commit()
    env.sync(env.b)   # 回報庫存 (線上端快照 = qty)


def _online(env, member_no, qty, pay=True):
    from tests.test_sync_hub_node import _checkout, _item, _member
    return _checkout(env.hub, [line(_item(env.hub, env.box)['id'], qty)], node_uuid=env.node_b_uuid,
                     user_id=_member(env.hub, f'09665{member_no:05d}')['id'], pay=pay)


def _branch_sale(env, qty):
    from tests.test_sync_hub_node import _checkout, _item
    return _checkout(env.b, [line(_item(env.b, env.box)['id'], qty)], order_source='POS', cashier_username='b_pos',
                     cash_received=10000)


def _push_only(env):
    """分店只上傳 (例如剛好在下載前回報庫存)：線上訂單還沒送到分店"""
    from app.sync.node import NodeSync
    with env.b.app_context():
        NodeSync(env.transports[env.b]).push()


def test_concurrent_online_orders_do_not_oversell_branch_snapshot(env):
    from app.errors import AppError
    _set_b_stock(env, 3)

    def attempt(n):
        try:
            return _online(env, n, 1)
        except AppError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(attempt, range(6)))
    ok = [r for r in results if isinstance(r, dict)]
    assert len(ok) == 3 and all('庫存' in r.message or '售完' in r.message for r in results if not isinstance(r, dict))
    env.sync(env.b)
    assert _b_stock(env) == 0


def test_branch_stock_report_does_not_resurrect_stock_held_by_online_orders(env):
    """
    線上訂單已在線上端預扣、但分店還沒下載前，分店因現場銷售回報新的庫存：
    線上端不可直接用回報值覆蓋快照 (否則同一份庫存會被線上再賣一次)，要扣掉尚未送達分店的線上訂單。
    """
    from app.errors import AppError
    _set_b_stock(env, 5)
    _online(env, 0, 3)                  # 線上先訂 3 份 (線上快照 5 → 2)
    _online(env, 1, 1, pay=False)       # 另一位待付款 1 份 (也要保留)
    _branch_sale(env, 1)                # 分店現場賣 1 份 (本機 5 → 4)
    _push_only(env)                     # 分店回報 4 (還沒收到線上訂單)
    with pytest.raises(AppError, match='庫存|售完'):
        _online(env, 2, 2)              # 實際只剩 5 - 3 - 1 - 1 = 0
    env.sync(env.b)
    assert _b_stock(env) == 1, '分店收到已付款的 3 份線上訂單後剩 1 份 (待付款的 1 份尚未送達)'


def test_branch_receiving_online_order_without_stock_alerts_staff(env, monkeypatch):
    """線上端快照落後 (分店先現場賣完還沒回報)：線上訂單照常成立，分店庫存扣到 0 並通知店員處理"""
    from app.services import monitor_service
    alerts = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: alerts.append(msg))
    _set_b_stock(env, 2)                # 上一個測試留下 1 份待付款的線上訂單：線上端快照 = 2 - 1 = 1
    _branch_sale(env, 2)                # 分店現場賣完，尚未回報
    order = _online(env, 3, 1)          # 線上端以舊快照接單
    env.sync(env.b)
    from tests.test_sync_hub_node import _order
    at_branch = _order(env.b, order['uuid'])
    assert at_branch is not None and at_branch['status'] == 'Pending'
    assert _b_stock(env) == 0, '庫存不會變成負數'
    assert any('庫存不足' in a and '跨店限量便當' in a for a in alerts), alerts
