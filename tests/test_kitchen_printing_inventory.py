"""廚房工作站 (分站看板、各站完成、加急排序)、網路印表機出單 (模擬 TCP 9100)、原物料配方扣料 / 回補 / 盤點 / 低庫存警示"""
import pytest

from app.extensions import db
from app.models.inventory import Ingredient, StockMovement
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.printing import KitchenStation, Printer, PrintJob
from app.services import escpos, monitor_service
from tests.support import assert_flash, checkout, line, make_item

POS = '/api/pos/checkout'


@pytest.fixture
def clean_stations_printers(app):
    """工作站 / 印表機只在本測試存在 (其他測試檔共用資料庫，不可留下會影響出單的設定)"""
    def wipe():
        with app.app_context():
            PrintJob.query.delete()
            Printer.query.delete()
            MenuItem.query.filter(MenuItem.station_id.isnot(None)).update({'station_id': None})
            KitchenStation.query.delete()
            db.session.commit()
    wipe()
    yield
    wipe()


@pytest.fixture
def printer_wire(monkeypatch):
    """取代 TCP 送出：sent 收到 (host, port, bytes)；down 內的主機視為離線"""
    sent, down = [], set()

    def fake_send(host, port, data, timeout=5):
        if host in down:
            raise ConnectionRefusedError(f'{host} refused')
        sent.append((host, int(port), data))
    monkeypatch.setattr(escpos, 'send_raw', fake_send)
    return sent, down


def _events(app, limit):
    with app.app_context():
        return monitor_service.recent_events(limit)


def _stations(app):
    with app.app_context():
        return {s.name: s.id for s in KitchenStation.query.all()}


# ---------------------------------------------------------------------------
# 廚房工作站
# ---------------------------------------------------------------------------
def test_station_kds_and_completion(app, admin_client, staff_client, kitchen_client, clean_stations_printers):
    admin_client.post('/admin/printers', data={'action': 'add_station', 'name': '飲料站', 'categories': '飲料, 甜點'})
    admin_client.post('/admin/printers', data={'action': 'add_station', 'name': '熱食站', 'categories': '主餐'})
    st = _stations(app)
    tea = make_item(app, '工作站紅茶', price=30, stock=50, category='飲料')
    noodle = make_item(app, '工作站拉麵', price=150, stock=50, category='主餐')
    salad = make_item(app, '工作站沙拉', price=80, stock=50, category='輕食')   # 不屬於任何工作站
    soup = make_item(app, '工作站熱湯', price=40, stock=50, category='飲料')
    admin_client.post('/admin/printers', data={'action': 'item_station', 'item_id': soup, 'station_id': st['熱食站']})

    oid = checkout(staff_client, [line(tea, 2), line(noodle), line(salad), line(soup)], url=POS,
                   cash_received=500).get_json()['order_id']
    drink_view = kitchen_client.get(f"/api/kitchen_orders?station={st['飲料站']}").get_json()['orders']
    mine = next(o for o in drink_view if o['id'] == oid)
    assert [(i['name'], i['quantity']) for i in mine['items']] == [('工作站紅茶', 2)]
    hot_view = kitchen_client.get(f"/api/kitchen_orders?station={st['熱食站']}").get_json()['orders']
    assert sorted(i['name'] for i in next(o for o in hot_view if o['id'] == oid)['items']) == ['工作站拉麵', '工作站熱湯']
    full = next(o for o in kitchen_client.get('/api/kitchen_orders').get_json()['orders'] if o['id'] == oid)
    assert {i['station'] for i in full['items']} == {'飲料站', '熱食站', ''}
    assert kitchen_client.get('/api/kitchen_orders?station=999999').status_code == 404

    url = f'/api/kitchen/orders/{oid}/station_done'
    r = kitchen_client.post(url, json={'station_id': st['飲料站']})
    assert r.get_json() == {'success': True, 'items_done': 1, 'order_completed': False}
    # 飲料站已完成 → 飲料站看板不再顯示這張單；全部看板顯示已完成的品項
    assert oid not in [o['id'] for o in kitchen_client.get(f"/api/kitchen_orders?station={st['飲料站']}").get_json()['orders']]
    r = kitchen_client.post(url, json={'station_id': st['熱食站']})
    assert r.get_json()['order_completed'] is True
    with app.app_context():
        order = db.session.get(Order, oid)
        assert order.status == 'Completed'
        assert all(i.prepared_at is not None for i in order.items if i.menu_item_id != salad)
    assert kitchen_client.post(url, json={'station_id': st['熱食站']}).status_code == 409
    # 看板頁面顯示工作站分頁
    page = kitchen_client.get('/kitchen').get_data(as_text=True)
    assert '飲料站' in page and '熱食站' in page


def test_rush_orders_sort_first(app, staff_client, kitchen_client):
    iid = make_item(app, '加急測試餐', price=50, stock=50)
    first = checkout(staff_client, [line(iid)], url=POS, cash_received=50).get_json()['order_id']
    second = checkout(staff_client, [line(iid)], url=POS, cash_received=50, is_rush=True).get_json()['order_id']
    third = checkout(staff_client, [line(iid)], url=POS, cash_received=50).get_json()['order_id']
    assert staff_client.post(f'/api/pos/orders/{third}/rush', json={'is_rush': True}).get_json()['is_rush'] is True
    ids = [o['id'] for o in kitchen_client.get('/api/kitchen_orders').get_json()['orders']]
    assert ids.index(second) < ids.index(third) < ids.index(first)
    assert kitchen_client.post(f'/api/kitchen/orders/{third}/rush', json={'is_rush': False}).get_json()['is_rush'] is False
    for oid in (first, second, third):
        staff_client.post(f'/api/orders/{oid}/status', json={'status': 'Completed'})
    assert staff_client.post(f'/api/pos/orders/{first}/rush', json={}).status_code == 409   # 已出餐不可加急


# ---------------------------------------------------------------------------
# 出單
# ---------------------------------------------------------------------------
def test_escpos_ticket_layout():
    assert escpos.text_width('牛肉麵ab') == 8
    assert escpos.wrap('一二三四五六七八九十', 8) == ['一二三四', '五六七八', '九十']
    t = escpos.Ticket(58)
    t.row('珍珠奶茶 (大)', '$65').qr('https://x.test/track/1').cut()
    raw = t.bytes()
    assert raw.startswith(b'\x1b@\x1c&') and '珍珠奶茶 (大)'.encode('big5') in raw and raw.endswith(b'\x1dVB\x00')
    line_bytes = next(ln for ln in raw.split(b'\n') if b'$65' in ln)
    assert escpos.text_width(line_bytes.split(b'!\x00')[-1].decode('big5')) == 32


def test_auto_print_receipt_and_station_tickets(app, admin_client, staff_client, store_settings, clean_stations_printers,
                                                 printer_wire):
    sent, down = printer_wire
    admin_client.post('/admin/printers', data={'action': 'add_station', 'name': '吧台', 'categories': '飲料'})
    st = _stations(app)
    admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '櫃檯收據', 'host': '192.168.1.50',
                                               'role': 'receipt', 'paper_width': '80', 'is_active': '1'})
    admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '吧台出單', 'host': '192.168.1.51',
                                               'port': '9101', 'role': 'kitchen', 'station_id': st['吧台'], 'is_active': '1'})
    admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '壞掉的位址', 'host': 'bad host/x',
                                               'role': 'receipt'})
    assert_flash(admin_client, 'IP 位址格式不正確')
    store_settings('pos', auto_print_receipt=True, auto_print_kitchen=True)
    drink = make_item(app, '出單奶茶', price=60, stock=50, category='飲料')
    rice = make_item(app, '出單燴飯', price=120, stock=50, category='主餐')
    r = checkout(staff_client, [line(drink), line(rice)], url=POS, cash_received=200, note='去冰')
    oid = r.get_json()['order_id']
    hosts = [(h, p) for h, p, _ in sent]
    assert hosts == [('192.168.1.50', 9100), ('192.168.1.51', 9101)]
    receipt, ticket = sent[0][2].decode('big5', 'replace'), sent[1][2].decode('big5', 'replace')
    assert '出單奶茶' in receipt and '出單燴飯' in receipt and '$180' in receipt
    assert '出單奶茶' in ticket and '出單燴飯' not in ticket and '去冰' in ticket and '吧台' in ticket

    # 補印收據
    sent.clear()
    assert staff_client.post(f'/api/pos/orders/{oid}/reprint').get_json()['success'] is True
    assert len(sent) == 1 and '補印收據' in sent[0][2].decode('big5', 'replace')

    # 印表機離線：保留在佇列重試，三次後失敗並通知；後台可重印
    down.add('192.168.1.51')
    sent.clear()
    checkout(staff_client, [line(drink)], url=POS, cash_received=60)
    from app.services import print_service
    with app.app_context():
        job = PrintJob.query.filter_by(status='queued').one()
        print_service.process_queue()
        print_service.process_queue()
        db.session.refresh(job)
        assert job.status == 'failed' and job.attempts == 3 and 'refused' in job.last_error
        job_id = job.id
    assert any('吧台出單' in e['message'] for e in _events(app, 20))
    down.clear()
    admin_client.post('/admin/printers', data={'action': 'retry_job', 'job_id': job_id})
    with app.app_context():
        assert db.session.get(PrintJob, job_id).status == 'done'
    page = admin_client.get('/admin/printers').get_data(as_text=True)
    assert '櫃檯收據' in page and '吧台出單' in page

    # 測試列印
    with app.app_context():
        pid = Printer.query.filter_by(name='櫃檯收據').first().id
    admin_client.post('/admin/printers', data={'action': 'test_printer', 'printer_id': pid})
    assert_flash(admin_client, '已送出測試頁')
    down.add('192.168.1.50')
    admin_client.post('/admin/printers', data={'action': 'test_printer', 'printer_id': pid})
    assert_flash(admin_client, '無法連線')


def test_table_orders_print_kitchen_only(app, admin_client, store_settings, clean_stations_printers, printer_wire):
    sent, _ = printer_wire
    admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '總廚房', 'host': '10.0.0.9',
                                               'role': 'kitchen', 'is_active': '1'})
    admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '收據機', 'host': '10.0.0.8',
                                               'role': 'receipt', 'is_active': '1'})
    store_settings('pos', auto_print_receipt=True)
    admin_client.post('/admin/tables', data={'action': 'create', 'names': 'P1'})
    from app.models.table import DiningTable
    with app.app_context():
        token = DiningTable.query.filter_by(name='P1').first().token
    iid = make_item(app, '桌邊出單餐', price=70, stock=10)
    c = app.test_client()
    c.get(f'/t/{token}')
    c.post('/guest_login')
    assert checkout(c, [line(iid)]).status_code == 200
    assert [h for h, _p, _d in sent] == ['10.0.0.9'], '桌邊點餐下單只印廚房單 (收據於結帳時)'


# ---------------------------------------------------------------------------
# 原物料
# ---------------------------------------------------------------------------
def _ing(app, name):
    with app.app_context():
        i = Ingredient.query.filter_by(name=name).first()
        return {'id': i.id, 'stock': round(i.stock, 4), 'unit_cost': round(i.unit_cost, 4), 'alerted': i.low_alerted_at is not None}


def test_recipe_consume_restore_and_block(app, admin_client, staff_client, kitchen_client, store_settings):
    store_settings('inventory', enabled=True, block_when_short=False)
    admin_client.post('/admin/inventory', data={'action': 'add', 'name': '測試牛肉', 'unit': 'g', 'stock': '1000',
                                                'low_threshold': '300', 'unit_cost': '0.5'})
    admin_client.post('/admin/inventory', data={'action': 'add', 'name': '測試麵條', 'unit': '份', 'stock': '10',
                                                'low_threshold': '2', 'unit_cost': '8'})
    admin_client.post('/admin/inventory', data={'action': 'add', 'name': '測試牛肉', 'unit': 'g'})
    assert_flash(admin_client, '已存在')
    beef, noodle = _ing(app, '測試牛肉'), _ing(app, '測試麵條')
    iid = make_item(app, '配方牛肉麵', price=180, stock=100)
    admin_client.post('/admin/inventory', data={'action': 'recipe', 'menu_item_id': iid,
                                                'ingredient_id': [beef['id'], noodle['id'], ''],
                                                'quantity': ['150', '1', '']})
    page = admin_client.get(f'/admin/inventory?item={iid}').get_data(as_text=True)
    assert '配方牛肉麵' in page and '83' in page   # 每份成本 150*0.5 + 8 = 83

    oid = checkout(staff_client, [line(iid, 2)], url=POS, cash_received=400).get_json()['order_id']
    assert _ing(app, '測試牛肉')['stock'] == 700 and _ing(app, '測試麵條')['stock'] == 8
    assert staff_client.post(f'/api/orders/{oid}/status', json={'status': 'Cancelled',
                                                                'approver_username': 'admin_test',
                                                                'approver_password': 'AdminPass!2026'}).status_code == 200
    assert _ing(app, '測試牛肉')['stock'] == 1000 and _ing(app, '測試麵條')['stock'] == 10
    with app.app_context():
        assert [m.reason for m in StockMovement.query.filter_by(order_id=oid).order_by(StockMovement.id)] == \
            ['consume', 'consume', 'restore', 'restore']

    # 低於安全量 → 通知一次
    checkout(staff_client, [line(iid, 5)], url=POS, cash_received=900)
    assert _ing(app, '測試牛肉')['stock'] == 250 and _ing(app, '測試牛肉')['alerted'] is True
    assert any('測試牛肉' in e['message'] for e in _events(app, 10))

    # 禁止販售：原料不足時整筆失敗，餐點庫存不變
    store_settings('inventory', block_when_short=True)
    with app.app_context():
        stock_before = db.session.get(MenuItem, iid).stock
    r = checkout(staff_client, [line(iid, 2)], url=POS, cash_received=400)
    assert r.status_code == 400 and r.get_json()['insufficient_stock'] is True and '測試牛肉' in r.get_json()['message']
    with app.app_context():
        assert db.session.get(MenuItem, iid).stock == stock_before
        from app.services import inventory_service
        from tests.support import default_tenant_id
        assert iid not in inventory_service.shortage_items(default_tenant_id(app)), '還夠做一份 (250g ≥ 150g)'

    # 進貨 (加權平均成本)、報廢、盤點 — 廚房角色有原物料權限
    kitchen_client.post('/admin/inventory', data={'action': 'purchase', 'ingredient_id': beef['id'], 'quantity': '750',
                                                  'unit_cost': '0.9'})
    b = _ing(app, '測試牛肉')
    assert b['stock'] == 1000 and b['unit_cost'] == round((0.5 * 250 + 0.9 * 750) / 1000, 4) and b['alerted'] is False
    kitchen_client.post('/admin/inventory', data={'action': 'waste', 'ingredient_id': beef['id'], 'quantity': '100',
                                                  'note': '過期'})
    assert _ing(app, '測試牛肉')['stock'] == 900
    kitchen_client.post('/admin/inventory', data={'action': 'stocktake', f"count__{beef['id']}": '880', 'note': '月盤'})
    assert _ing(app, '測試牛肉')['stock'] == 880
    assert checkout(staff_client, [line(iid)], url=POS, cash_received=180).status_code == 200
    assert staff_client.get('/admin/inventory').status_code == 403


def test_inventory_disabled_does_not_consume(app, staff_client, store_settings):
    store_settings('inventory', enabled=False)
    with app.app_context():
        item = MenuItem.query.filter_by(name='配方牛肉麵').first()
        iid = item.id if item else make_item(app, '配方牛肉麵', price=180, stock=100)
    before = _ing(app, '測試牛肉')['stock'] if _ing_exists(app, '測試牛肉') else None
    assert checkout(staff_client, [line(iid)], url=POS, cash_received=180).status_code == 200
    if before is not None:
        assert _ing(app, '測試牛肉')['stock'] == before


def _ing_exists(app, name):
    with app.app_context():
        return Ingredient.query.filter_by(name=name).first() is not None
