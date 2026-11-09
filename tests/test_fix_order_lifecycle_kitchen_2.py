"""
修正小組 order-lifecycle-kitchen-2 的回歸測試：
出單 (工作站限定店家、未分站品項、佇列隊頭阻塞、主機名稱格式錯誤、廚房單預約時間)、
線上預約取餐時段 (跨午夜、時段上限、多語系)、收據 (取消 / 退款 / 單品作廢 / 折扣進位)。
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.printing import KitchenStation, Printer, PrintJob
from app.services import escpos
from tests.support import default_tenant_id, get_or_create_tenant


@pytest.fixture
def clean_printing(app):
    """工作站 / 印表機 / 列印工作只在本測試存在 (其他測試檔共用資料庫)"""
    def wipe():
        with app.app_context():
            PrintJob.query.execution_options(skip_tenant_filter=True).delete()
            Printer.query.execution_options(skip_tenant_filter=True).delete()
            MenuItem.query.execution_options(skip_tenant_filter=True) \
                .filter(MenuItem.station_id.isnot(None)).update({'station_id': None})
            KitchenStation.query.execution_options(skip_tenant_filter=True).delete()
            db.session.commit()
    wipe()
    yield
    wipe()


@pytest.fixture
def wire(monkeypatch):
    sent, down = [], set()

    def fake_send(host, port, data, timeout=5):
        if host in down:
            raise ConnectionRefusedError(f'{host} refused')
        sent.append((host, int(port), data))
    monkeypatch.setattr(escpos, 'send_raw', fake_send)
    return sent, down


def _item(tid, name, category, price=100):
    mi = MenuItem(tenant_id=tid, name=name, category=category, price=price, stock=50, total_stock=50, modifiers='none')
    db.session.add(mi)
    db.session.flush()
    return mi


def _order(tid, lines, **fields):
    """lines: [(MenuItem 或 None, 名稱, 單價, 份數)]"""
    o = Order(tenant_id=tid, total_price=fields.pop('total_price', sum(p * q for _m, _n, p, q in lines)), **fields)
    db.session.add(o)
    db.session.flush()
    for mi, name, price, qty in lines:
        db.session.add(OrderItem(tenant_id=tid, order_id=o.id, menu_item_id=mi.id if mi else None,
                                 item_name=name, price=price, quantity=qty))
    db.session.flush()
    return o


def _texts(sent):
    return [data.decode('big5', 'replace') for _h, _p, data in sent]


# ---------------------------------------------------------------------------
# B-059：背景出單 (沒有請求情境) 時工作站限定本店
# ---------------------------------------------------------------------------
def test_background_print_uses_own_tenant_stations(app, clean_printing, wire):
    from app.services import print_service
    sent, _ = wire
    tid_a = default_tenant_id(app)
    tid_b = get_or_create_tenant(app, 'fix-olk2-shop-b')
    with app.app_context():   # 只有 app_context (與 deferred 背景執行緒相同)，自動店家過濾不生效
        st_a = KitchenStation(tenant_id=tid_a, name='A 飲料站', categories='飲料', sort_order=0)
        db.session.add(st_a)
        db.session.flush()
        st_b = KitchenStation(tenant_id=tid_b, name='B 飲料站', categories='飲料', sort_order=0)
        db.session.add(st_b)
        db.session.flush()
        assert st_a.id < st_b.id
        db.session.add(Printer(tenant_id=tid_b, name='B 飲料印表機', host='10.9.9.2', role='kitchen', station_id=st_b.id))
        tea = _item(tid_b, 'B店紅茶', '飲料', 30)
        o = _order(tid_b, [(tea, 'B店紅茶', 30, 1)])
        db.session.commit()
        assert print_service.print_order(o.id, tid_b, receipt=False, kitchen=True) == 1
    texts = _texts(sent)
    assert [h for h, _p, _d in sent] == ['10.9.9.2']
    assert 'B店紅茶' in texts[0] and 'B 飲料站' in texts[0]


# ---------------------------------------------------------------------------
# R2-018：所有廚房印表機都綁工作站時，未分站品項 / 停用工作站的品項仍要印出
# ---------------------------------------------------------------------------
def test_unassigned_items_printed_when_all_printers_bound(app, clean_printing, wire):
    from app.services import print_service
    sent, _ = wire
    tid = default_tenant_id(app)
    with app.app_context():
        hot = KitchenStation(tenant_id=tid, name='熱食站', categories='主餐')
        drink = KitchenStation(tenant_id=tid, name='飲料站', categories='飲料')
        db.session.add_all([hot, drink])
        db.session.flush()
        db.session.add_all([Printer(tenant_id=tid, name='熱食機', host='10.9.8.1', role='kitchen', station_id=hot.id),
                            Printer(tenant_id=tid, name='飲料機', host='10.9.8.2', role='kitchen', station_id=drink.id)])
        rice = _item(tid, '未分站燴飯', '主餐')
        cake = _item(tid, '未分站鬆餅', '甜點')
        coffee = _item(tid, '未分站咖啡', '飲料')
        o = _order(tid, [(rice, '未分站燴飯', 100, 1), (cake, '未分站鬆餅', 80, 1), (coffee, '未分站咖啡', 60, 1)])
        db.session.commit()
        oid, drink_id = o.id, drink.id
        print_service.print_order(oid, tid, receipt=False, kitchen=True)
    texts = _texts(sent)
    assert [h for h, _p, _d in sent] == ['10.9.8.1', '10.9.8.2', '10.9.8.1']
    assert '未分站燴飯' in texts[0] and '未分站鬆餅' not in texts[0]
    assert '未分站咖啡' in texts[1]
    assert '未分站鬆餅' in texts[2] and '未分站' in texts[2] and '未分站燴飯' not in texts[2]

    # 停用飲料站：綁定飲料站的印表機改印未分站品項 (咖啡、鬆餅)，不再一張都不印
    sent.clear()
    with app.app_context():
        db.session.get(KitchenStation, drink_id).is_active = False
        db.session.commit()
        print_service.print_order(oid, tid, receipt=False, kitchen=True)
    texts = _texts(sent)
    assert [h for h, _p, _d in sent] == ['10.9.8.1', '10.9.8.2']
    assert '未分站咖啡' in texts[1] and '未分站鬆餅' in texts[1] and '未分站燴飯' not in texts[1]


# ---------------------------------------------------------------------------
# B-068：離線印表機累積超過 limit 筆時，其他印表機的工作仍要送出
# ---------------------------------------------------------------------------
def test_offline_printer_backlog_does_not_block_others(app, clean_printing, wire):
    from app.services import print_service
    sent, down = wire
    tid = default_tenant_id(app)
    down.add('10.9.7.1')
    with app.app_context():
        dead = Printer(tenant_id=tid, name='缺紙收據機', host='10.9.7.1', role='receipt')
        alive = Printer(tenant_id=tid, name='廚房機', host='10.9.7.2', role='kitchen')
        db.session.add_all([dead, alive])
        db.session.flush()
        for i in range(60):
            db.session.add(PrintJob(tenant_id=tid, printer_id=dead.id, title=f'收據 {i}', payload=b'x'))
        db.session.flush()
        db.session.add(PrintJob(tenant_id=tid, printer_id=alive.id, title='廚房單', payload=b'k'))
        db.session.commit()
        alive_id = alive.id
        assert print_service.process_queue(tid, limit=50) == 1
        job = PrintJob.query.filter_by(printer_id=alive_id).one()
        assert job.status == 'done'
        # 離線印表機本輪只試一筆
        assert PrintJob.query.filter(PrintJob.attempts > 0, PrintJob.printer_id != alive_id).count() == 1
    assert [h for h, _p, _d in sent] == ['10.9.7.2']


# ---------------------------------------------------------------------------
# B-069：主機名稱格式錯誤 (socket 拋 UnicodeError) 當成無法連線處理
# ---------------------------------------------------------------------------
def test_malformed_host_is_treated_as_connection_error(app, clean_printing, monkeypatch):
    from app.services import print_service
    calls = []

    def bad_send(host, port, data, timeout=5):
        calls.append(host)
        if host == '192.168..50':
            raise UnicodeEncodeError('idna', host, 0, 1, 'label empty or too long')
    monkeypatch.setattr(escpos, 'send_raw', bad_send)
    tid = default_tenant_id(app)
    with app.app_context():
        bad = Printer(tenant_id=tid, name='打錯位址', host='192.168..50', role='receipt')
        good = Printer(tenant_id=tid, name='正常廚房機', host='10.9.6.2', role='kitchen')
        db.session.add_all([bad, good])
        db.session.flush()
        db.session.add(PrintJob(tenant_id=tid, printer_id=bad.id, title='收據', payload=b'r'))
        db.session.flush()
        db.session.add(PrintJob(tenant_id=tid, printer_id=good.id, title='廚房單', payload=b'k'))
        db.session.commit()
        bad_id = bad.id
        assert print_service.process_queue(tid) == 1, '後面的廚房單照樣送出'
        job = PrintJob.query.filter_by(printer_id=bad_id).one()
        assert job.status == 'queued' and '格式不正確' in job.last_error
        with pytest.raises(OSError):
            print_service.test_print(db.session.get(Printer, bad_id))
    assert calls[:2] == ['192.168..50', '10.9.6.2']


# ---------------------------------------------------------------------------
# B-080：廚房單印出預約取餐時間
# ---------------------------------------------------------------------------
def test_kitchen_ticket_shows_scheduled_pickup(app):
    from app.services import print_service
    tid = default_tenant_id(app)
    with app.app_context():
        o = _order(tid, [(None, '預約便當', 100, 30)], pickup_at=datetime.now() + timedelta(days=1, hours=1))
        printer = Printer(tenant_id=tid, name='x', host='10.0.0.1', role='kitchen')
        text = print_service.build_kitchen_ticket(o, printer, list(o.items)).decode('big5', 'replace')
        assert f'預約 {o.pickup_at_label} 取餐' in text
        o.pickup_at = None
        assert '取餐' not in print_service.build_kitchen_ticket(o, printer, list(o.items)).decode('big5', 'replace')
        db.session.rollback()


# ---------------------------------------------------------------------------
# B-067 / R2-022 / R2-023：預約取餐時段
# ---------------------------------------------------------------------------
def test_pickup_slots_overnight_capacity_and_language(app, store_settings):
    from app.services import pickup_service
    tid = default_tenant_id(app)
    # 跨午夜：17:00 ~ 01:00
    store_settings('online_order', scheduled_pickup=True, open_time='17:00', close_time='01:00', slot_minutes=30,
                   lead_minutes=0, days_ahead=0)
    with app.app_context():
        got = [t.strftime('%d %H:%M') for t, _ in pickup_service.slots(tid, now=datetime(2026, 10, 23, 16, 0))]
        assert got[0] == '23 17:00' and got[-1] == '24 01:00' and '24 00:30' in got
        # 凌晨 00:10：昨天開始的營業時段還沒結束
        got = [t.strftime('%d %H:%M') for t, _ in pickup_service.slots(tid, now=datetime(2026, 10, 24, 0, 10))]
        assert got[:2] == ['24 00:30', '24 01:00']
        assert pickup_service.parse('2026-10-24T00:30', tid, now=datetime(2026, 10, 23, 20, 0)) == datetime(2026, 10, 24, 0, 30)
    # 營業到午夜 (最晚 00:00)
    store_settings('online_order', scheduled_pickup=True, open_time='17:00', close_time='00:00', slot_minutes=30,
                   lead_minutes=0, days_ahead=0)
    with app.app_context():
        got = [t.strftime('%d %H:%M') for t, _ in pickup_service.slots(tid, now=datetime(2026, 10, 23, 16, 0))]
        assert got[0] == '23 17:00' and got[-1] == '24 00:00'
    # 可預約 7 天、5 分鐘一格：最後一天也在清單上
    store_settings('online_order', scheduled_pickup=True, open_time='10:00', close_time='21:00', slot_minutes=5,
                   lead_minutes=0, days_ahead=7)
    with app.app_context():
        got = pickup_service.slots(tid, now=datetime(2026, 10, 23, 9, 0))
        assert got[-1][0] == datetime(2026, 10, 30, 21, 0) and len(got) == 8 * 133
        now = datetime(2026, 11, 9, 9, 0)   # 週一
        assert pickup_service.label(datetime(2026, 11, 9, 12, 30), now, 'en') == 'Today 12:30'
        assert pickup_service.label(datetime(2026, 11, 10, 12, 30), now, 'en') == 'Tomorrow 12:30'
        assert pickup_service.label(datetime(2026, 11, 11, 12, 30), now, 'en') == 'Wed 11/11 12:30'
        assert pickup_service.label(datetime(2026, 11, 10, 12, 30), now, 'ja') == '明日 12:30'
        assert pickup_service.label(datetime(2026, 11, 11, 12, 30), now, 'ja') == '11/11(水) 12:30'
        assert pickup_service.label(datetime(2026, 11, 11, 12, 30), now) == '11/11(三) 12:30'
        assert pickup_service.slots(tid, now=now, lang='en')[0][1].startswith('Today ')


# ---------------------------------------------------------------------------
# B-070 / B-081 / R2-024：收據
# ---------------------------------------------------------------------------
def _receipt(order):
    from app.services import receipt_service
    return receipt_service.text_receipt(order)


def test_receipt_shows_cancel_refund_void_and_consistent_discounts(app):
    tid = default_tenant_id(app)
    with app.app_context():
        # 已取消：大字標示，不印本次贈點
        o = _order(tid, [(None, '收據取消餐', 300, 1)], status='Cancelled', points_earned=3, refunded_amount=300)
        text = _receipt(o)
        assert '已取消 / 已作廢' in text and '本次贈點' not in text
        assert '已退款 -$300' in text and '實收淨額 $0' in text

        # 已收款訂單單品作廢：應付不變、列已退款與實收淨額；品項列原份數與作廢份數
        o = _order(tid, [(None, '收據A餐', 100, 1), (None, '收據B餐', 100, 0)], refunded_amount=100, total_price=200)
        o.items[1].voided_qty = 1
        text = _receipt(o)
        assert '收據B餐 x1  $100' in text and '已作廢 x1' in text and '小計 $200' in text
        assert '應付總額 $200' in text and '已退款 -$100' in text and '實收淨額 $100' in text

        # 桌邊未結帳訂單作廢 (A100 + B100、優惠 -20、作廢 B 退 90 → 應付 90)：小計 - 優惠 - 作廢 = 應付
        o = _order(tid, [(None, '桌邊A餐', 100, 1), (None, '桌邊B餐', 100, 0)], total_price=90, discount_amount=20,
                   payment_status='unpaid')
        o.items[1].voided_qty = 1
        text = _receipt(o)
        assert '小計 $200' in text and '優惠券 / 點數 -$20' in text and '作廢品項 -$90' in text and '應付總額 $90' in text
        assert 'x0' not in text

        # 會員 5% 折扣 6.5 存成 7、折扣總額 6：列出的折扣加總要等於 小計 - 應付
        o = _order(tid, [(None, '會員折扣餐', 130, 1)], total_price=124, discount_amount=6, member_discount=7)
        text = _receipt(o)
        assert '會員折扣 -$6' in text and '優惠券' not in text and '應付總額 $124' in text

        # 一般訂單不受影響
        o = _order(tid, [(None, '一般收據餐', 100, 2)], points_earned=2)
        text = _receipt(o)
        assert '一般收據餐 x2  $200' in text and '小計 $200' in text and '已取消' not in text and '已退款' not in text
        db.session.rollback()


# ---------------------------------------------------------------------------
# R2-015：整單廚房單與「未分站」補印都要印套餐配餐
# ---------------------------------------------------------------------------
def test_kitchen_tickets_include_combo_sides(app, clean_printing, wire):
    import json

    from app.services import print_service
    sent, _ = wire
    tid = default_tenant_id(app)
    with app.app_context():
        db.session.add(Printer(tenant_id=tid, name='整單機', host='10.9.5.1', role='kitchen'))
        main = _item(tid, '配餐測試主餐', '主餐')
        side = _item(tid, '配餐測試薯條', '炸物')
        o = _order(tid, [(main, '配餐測試套餐', 150, 1)])
        o.items[0].combo_sides = json.dumps([side.uuid])
        db.session.commit()
        oid = o.id
        print_service.print_order(oid, tid, receipt=False, kitchen=True)
    texts = _texts(sent)
    assert len(texts) == 1 and '配餐測試套餐' in texts[0] and '配餐測試薯條' in texts[0]

    # 所有廚房印表機都綁工作站、配餐沒有工作站負責：未分站補印要印到配餐
    sent.clear()
    with app.app_context():
        Printer.query.execution_options(skip_tenant_filter=True).delete()
        hot = KitchenStation(tenant_id=tid, name='主餐站', categories='主餐')
        db.session.add(hot)
        db.session.flush()
        db.session.add(Printer(tenant_id=tid, name='主餐機', host='10.9.5.2', role='kitchen', station_id=hot.id))
        db.session.commit()
        print_service.print_order(oid, tid, receipt=False, kitchen=True)
    texts = _texts(sent)
    assert len(texts) == 2
    assert '配餐測試套餐' in texts[0] and '配餐測試薯條' not in texts[0]
    assert '配餐測試薯條' in texts[1] and '配餐測試套餐 x' not in texts[1]   # 配餐列註明所屬套餐，但不印主餐本身


# ---------------------------------------------------------------------------
# R2-194：線上訂單尚未由分店編號時，收據不可把訂單 id 印成取餐號碼
# ---------------------------------------------------------------------------
def test_receipt_pickup_number_pending_for_online_order(app):
    tid = default_tenant_id(app)
    with app.app_context():
        o = _order(tid, [(None, '線上待編號餐', 100, 1)], order_source='Online', pickup_number=0)
        text = _receipt(o)
        assert '取餐號碼：待分店確認' in text and f'取餐號碼 #{o.id:03d}' not in text
        o.pickup_number = 12
        assert '取餐號碼 #012' in _receipt(o)
        o2 = _order(tid, [(None, '門市餐', 100, 1)], order_source='Kiosk', pickup_number=7)
        assert '取餐號碼 #007' in _receipt(o2)
        db.session.rollback()
