"""
修正回歸測試 (order-lifecycle-kitchen-3)：ESC/POS 控制字元與編碼、線上端印表機連線固定 IP、預估等候時間的排隊量、
套餐配餐依工作站分流、作廢到 0 份不擋出餐、線上端沒有廚房 KDS。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.menu import ComboOption, MenuItem
from app.models.order import Order, OrderItem
from app.models.printing import KitchenStation, Printer, PrintJob
from app.services import escpos
from tests.support import (assert_flash, checkout, default_tenant_id, get_or_create_tenant, line, make_item,
                           tenant_request)

POS = '/api/pos/checkout'


@pytest.fixture
def clean_kitchen(app):
    """工作站 / 印表機只在本測試存在 (共用資料庫，不可留下會影響其他測試出單的設定)"""
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
def wire(monkeypatch):
    sent = []
    monkeypatch.setattr(escpos, 'send_raw', lambda host, port, data, timeout=5: sent.append((host, data)))
    return sent


def _stations(app):
    with app.app_context():
        return {s.name: s.id for s in KitchenStation.query.all()}


# ---------------------------------------------------------------------------
# B-058 / B-074：ESC/POS
# ---------------------------------------------------------------------------
def test_escpos_strips_control_chars_and_uses_cp950():
    t = escpos.Ticket(58)
    t.text('少冰\x1bp0\x19\x19\n第二行\x1dV')
    t.row('桌號\x1b@', '$65\x1bp')
    raw = t.bytes()
    body = raw[len(b'\x1b@\x1c&'):]
    # 只剩 Ticket 自己送的指令 (ESC a / ESC E / GS !)，顧客文字裡的 ESC p、GS V、ESC @ 都變成空白
    assert b'\x1bp' not in body and b'\x1dV' not in body and b'\x1b@' not in body
    assert '少冰 p0'.encode('cp950') in raw and '第二行'.encode('cp950') in raw
    # Big5 印表機用 cp950：恒、～ 不會變成 ?
    t = escpos.Ticket(80, 'big5')
    t.text('恒春 8～10 分')
    assert '恒春 8～10 分'.encode('cp950') in t.bytes() and b'?' not in t.bytes()
    # 中文模式下 × ○ 在 Big5 是雙位元組：對齊以 2 格計算，整行剛好是紙寬
    t = escpos.Ticket(58)
    t.row('雞排×2 ○', '$130')
    ln = next(x for x in t.bytes().split(b'\n') if b'$130' in x)
    assert len(ln.split(b'!\x00')[-1]) == 32
    assert escpos.text_width('×○', 'cp950') == 4 and escpos.text_width('×○') == 2


# ---------------------------------------------------------------------------
# B-057：線上端印表機每次連線都重新檢查並固定 IP；名稱不可帶換行
# ---------------------------------------------------------------------------
def test_send_raw_on_hub_pins_checked_ip(app, monkeypatch):
    from app.services import webhook_service
    from app.sync import runtime as sync_runtime
    connected = []

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendall(self, data):
            pass

    monkeypatch.setattr(escpos.socket, 'create_connection', lambda addr, timeout=5: connected.append(addr) or FakeSock())
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    monkeypatch.setattr(webhook_service, 'resolve_ips', lambda host: {'8.8.8.8'})
    with app.app_context():
        escpos.send_raw('p.example.com', 9100, b'x')
        assert connected[-1] == ('8.8.8.8', 9100)
        # 儲存後 DNS 改指向內網：連線前就擋下 (以 OSError 呈現，列印佇列照常記錄失敗)
        monkeypatch.setattr(webhook_service, 'resolve_ips', lambda host: {'172.18.0.5'})
        with pytest.raises(OSError):
            escpos.send_raw('p.example.com', 6379, b'FLUSHALL\n')
        assert len(connected) == 1
        # 門市主機照舊連主機名稱 (區網印表機)
        monkeypatch.setattr(sync_runtime, 'is_hub', lambda: False)
        escpos.send_raw('192.168.1.50', 9100, b'x')
        assert connected[-1] == ('192.168.1.50', 9100)


def test_printer_name_newlines_removed(app, admin_client, clean_kitchen):
    admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': 'x\nFLUSHALL\x1b', 'host': '192.168.1.60',
                                               'role': 'receipt', 'is_active': '1'})
    assert_flash(admin_client, '已新增印表機')
    with app.app_context():
        assert Printer.query.filter_by(host='192.168.1.60').one().name == 'x FLUSHALL'


# ---------------------------------------------------------------------------
# B-075 / R2-019：預估等候時間的排隊量
# ---------------------------------------------------------------------------
def test_eta_queue_excludes_own_order_and_counts_old_reservations(app, staff_client):
    from app.services import eta_service
    item = make_item(app, '修正ETA牛排', price=200, stock=50, prep_minutes=30)
    oid = checkout(staff_client, [line(item)], url=POS, cash_received=500).get_json()['order_id']
    tid = default_tenant_id(app)
    now = datetime.now()
    with tenant_request(app, tid):
        order = db.session.get(Order, oid)
        order.estimated_ready_at = None   # 模擬剛同步到分店、尚未估算的線上訂單
        db.session.commit()
        with_self = eta_service._queue_minutes(tid, now, 5)
        without = eta_service._queue_minutes(tid, now, 5, {oid})
        assert with_self - without == pytest.approx(30)
        # 訂單本身不算進排隊量
        expected = now + timedelta(minutes=eta_service.estimate(tid, [(db.session.get(MenuItem, item), 1)], now, {oid}))
        assert eta_service.ready_at_for_order(order, now) == expected

        # 前幾天建立、預約今天 (15 分鐘內) 取餐的訂單也在排隊中
        order.created_at = now - timedelta(days=2)
        order.pickup_at = now + timedelta(minutes=5)
        db.session.commit()
        assert eta_service._queue_minutes(tid, now, 5) - eta_service._queue_minutes(tid, now, 5, {oid}) == 30
        # 預約時段還很久 → 不算
        order.pickup_at = now + timedelta(hours=3)
        db.session.commit()
        assert eta_service._queue_minutes(tid, now, 5) - eta_service._queue_minutes(tid, now, 5, {oid}) == 0
        order.status = 'Completed'
        db.session.commit()


# ---------------------------------------------------------------------------
# R2-015：套餐配餐依配餐所屬工作站分流
# ---------------------------------------------------------------------------
def test_combo_sides_routed_to_their_station(app, admin_client, kitchen_client, staff_client, clean_kitchen, wire):
    admin_client.post('/admin/printers', data={'action': 'add_station', 'name': '修正熱食站', 'categories': '修正主餐'})
    admin_client.post('/admin/printers', data={'action': 'add_station', 'name': '修正飲料站', 'categories': '修正飲料'})
    st = _stations(app)
    admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '飲料出單', 'host': '192.168.1.71',
                                               'role': 'kitchen', 'station_id': st['修正飲料站'], 'is_active': '1'})
    burger = make_item(app, '修正套餐堡', price=120, stock=50, category='修正主餐')
    fries = make_item(app, '修正固定薯條', price=40, stock=50, category='修正主餐')
    make_item(app, '修正套餐紅茶', price=30, stock=50, category='修正飲料')
    make_item(app, '修正套餐奶茶', price=40, stock=50, category='修正飲料')
    r = admin_client.post('/admin/add_combo', data={'main_item_id': burger, 'name': '修正A餐', 'additional_price': 50,
                                                    'choices': '飲料: 修正套餐紅茶, 修正套餐奶茶', 'can_addon': '1',
                                                    'item1_id': fries})
    assert r.status_code == 302
    with app.app_context():
        combo = ComboOption.query.filter_by(name='修正A餐').one()
        combo_id = combo.id
        tea_choice = next(s.id for s in combo.sides if s.choice_group and s.menu_item.name == '修正套餐紅茶')

    oid = checkout(staff_client, [line(burger, combo_option_id=combo_id, combo_choices=[tea_choice])], url=POS,
                   cash_received=500).get_json()['order_id']
    # 飲料站印表機印得到套餐紅茶
    assert any('修正套餐紅茶'.encode('cp950') in data for host, data in wire if host == '192.168.1.71')
    # 飲料站 KDS 看得到這張單 (只有紅茶)；熱食站有主餐與固定配餐
    drink = next(o for o in kitchen_client.get(f"/api/kitchen_orders?station={st['修正飲料站']}").get_json()['orders']
                 if o['id'] == oid)
    assert [i['name'] for i in drink['items']] == ['修正套餐紅茶']
    hot = next(o for o in kitchen_client.get(f"/api/kitchen_orders?station={st['修正熱食站']}").get_json()['orders']
               if o['id'] == oid)
    assert sorted(i['name'] for i in hot['items']) == ['修正固定薯條', '修正套餐堡']
    # 配餐的虛擬明細不會被當成實際品項標記完成
    r = kitchen_client.post(f'/api/kitchen/orders/{oid}/station_done', json={'station_id': st['修正飲料站']})
    assert r.get_json()['items_done'] == 0
    with app.app_context():
        assert all(i.prepared_at is None for i in OrderItem.query.filter_by(order_id=oid))
    staff_client.post(f'/api/orders/{oid}/status', json={'status': 'Completed'})


# ---------------------------------------------------------------------------
# B-060：整個作廢 (剩 0 份) 的品項不算工作站待做
# ---------------------------------------------------------------------------
def test_fully_voided_item_does_not_block_station_completion(app, admin_client, kitchen_client, clean_kitchen):
    admin_client.post('/admin/printers', data={'action': 'add_station', 'name': '作廢飲料站', 'categories': '作廢飲料'})
    admin_client.post('/admin/printers', data={'action': 'add_station', 'name': '作廢熱食站', 'categories': '作廢主餐'})
    st = _stations(app)
    tea = make_item(app, '作廢修正紅茶', price=30, stock=50, category='作廢飲料')
    noodle = make_item(app, '作廢修正拉麵', price=150, stock=50, category='作廢主餐')
    oid = checkout(admin_client, [line(tea), line(noodle)], url=POS, cash_received=500).get_json()['order_id']
    with app.app_context():
        noodle_line = OrderItem.query.filter_by(order_id=oid, menu_item_id=noodle).one().id
    assert admin_client.post(f'/api/pos/orders/{oid}/void_item',
                             json={'item_id': noodle_line, 'quantity': 1, 'reason': '客人不要'}).status_code == 200
    # 熱食站看板不再顯示這張單
    hot = kitchen_client.get(f"/api/kitchen_orders?station={st['作廢熱食站']}").get_json()['orders']
    assert oid not in [o['id'] for o in hot]
    r = kitchen_client.post(f'/api/kitchen/orders/{oid}/station_done', json={'station_id': st['作廢飲料站']})
    assert r.get_json()['order_completed'] is True
    with app.app_context():
        assert db.session.get(Order, oid).status == 'Completed'


# ---------------------------------------------------------------------------
# R2-016：線上端沒有廚房 KDS
# ---------------------------------------------------------------------------
def test_active_stations_limited_to_tenant_without_request(app, clean_kitchen):
    """B-059：背景執行緒 (沒有請求情境) 呼叫時，有傳 tenant_id 只取本店工作站，同名分類不會對應到別店"""
    from app.services import kitchen_service
    tid = default_tenant_id(app)
    other = get_or_create_tenant(app, 'fix-kitchen3-other')
    with app.app_context():
        db.session.add(KitchenStation(tenant_id=other, name='別店飲料站', categories='修正B059飲料', sort_order=-1))
        db.session.add(KitchenStation(tenant_id=tid, name='本店飲料站', categories='修正B059飲料', sort_order=0))
        db.session.commit()
        mine = db.session.query(KitchenStation).filter_by(name='本店飲料站').one().id
        assert [s.name for s in kitchen_service.active_stations(tid)] == ['本店飲料站']
        resolve = kitchen_service.station_resolver(tenant_id=tid)
        assert resolve(SimpleNamespace(station_id=None, category='修正B059飲料')) == mine


def test_send_raw_malformed_host_raises_oserror(app, monkeypatch):
    """B-069：192.168..50 這類主機名稱 socket 拋 UnicodeError，send_raw 轉成 OSError"""
    from app.sync import runtime as sync_runtime
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: False)
    with app.app_context():
        with pytest.raises(OSError):
            escpos.send_raw('192.168..50', 9100, b'x', timeout=0.5)
        with pytest.raises(OSError):
            escpos.send_raw('a' * 64 + '.lan', 9100, b'x', timeout=0.5)


def test_printer_form_rejects_malformed_host(app, admin_client, clean_kitchen):
    """B-069：後台儲存印表機時就擋下格式錯誤的主機名稱"""
    for host in ('192.168..50', 'a' * 64 + '.lan', '-bad.lan', 'bad_host'):
        admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '格式錯誤', 'host': host,
                                                   'role': 'receipt', 'is_active': '1'})
        assert_flash(admin_client, 'IP 位址格式不正確')
    admin_client.post('/admin/printers', data={'action': 'add_printer', 'name': '主機名稱', 'host': 'printer-1.lan',
                                               'role': 'receipt', 'is_active': '1'})
    assert_flash(admin_client, '已新增印表機')
    with app.app_context():
        assert Printer.query.filter_by(name='格式錯誤').count() == 0
        assert Printer.query.filter_by(host='printer-1.lan').count() == 1


def test_kitchen_routes_not_available_on_hub(app, kitchen_client, monkeypatch):
    from app.sync import runtime as sync_runtime
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    assert kitchen_client.get('/kitchen').status_code == 404
    assert kitchen_client.get('/api/kitchen_orders').status_code == 404
    assert kitchen_client.post('/api/kitchen_complete_all', json={'order_ids': [1]}).status_code == 404
    assert kitchen_client.post('/api/kitchen/orders/1/station_done', json={'station_id': 1}).status_code == 404
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: False)
    assert kitchen_client.get('/api/kitchen_orders').status_code == 200
