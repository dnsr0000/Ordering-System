"""修正小組 reports-excel-analytics-2 的回歸測試：營運報表、後台總覽分析、備料預測"""
import io
import json
import uuid
from datetime import date, datetime, timedelta

import openpyxl

from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.payment import PaymentRecord
from app.models.sync import SyncNode
from tests.support import count_queries, get_or_create_tenant, tenant_request

DAY = date(2019, 5, 6)   # 報表測試用的固定日期 (星期一)，避開其他測試的資料


def _order(tid, total, at=None, status='Completed', payment_status='', node_uuid=None, n=1, items=(), **kw):
    o = Order(tenant_id=tid, total_price=total, status=status, payment_status=payment_status,
              payment_method='現金', order_type='內用', pickup_number=n, node_uuid=node_uuid,
              created_at=at or datetime.combine(DAY, datetime.min.time()) + timedelta(hours=12), **kw)
    o.items = [OrderItem(item_name=name, price=price, quantity=qty, **extra) for name, price, qty, extra in items]
    return o


# ---------------------------------------------------------------------------
# 營運報表
# ---------------------------------------------------------------------------
def test_report_query_count_independent_of_orders(app):
    """B-372：報表逐張讀明細不可變成每張訂單一次查詢"""
    from app.services import report_service
    tid = get_or_create_tenant(app, 'rea2-nplus1')
    with tenant_request(app, tid):
        db.session.add_all([_order(tid, 100, n=i, items=[('湯', 100, 1, {})]) for i in range(2)])
        db.session.commit()
    with tenant_request(app, tid):
        with count_queries(app) as few:
            report_service.build_report(tid, DAY, DAY)
    with tenant_request(app, tid):
        db.session.add_all([_order(tid, 100, n=i, items=[('湯', 100, 1, {})]) for i in range(2, 12)])
        db.session.commit()
    with tenant_request(app, tid):
        with count_queries(app) as many:
            rep = report_service.build_report(tid, DAY, DAY)
    assert rep['order_count'] == 12
    assert many['n'] == few['n'], many['sql']


def test_report_payment_methods_follow_node_filter(app, monkeypatch):
    """B-373：選分店時付款方式只算該分店訂單的收款；B-382：Excel 分店工作表顯示分店名稱"""
    from app.services import report_service
    from app.sync import runtime as sync_runtime
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    tid = get_or_create_tenant(app, 'rea2-node-pay')
    with tenant_request(app, tid):
        a = SyncNode(tenant_id=tid, name='甲分店', token_hash=f'rea2-a-{uuid.uuid4().hex}')
        b = SyncNode(tenant_id=tid, name='乙分店', token_hash=f'rea2-b-{uuid.uuid4().hex}')
        db.session.add_all([a, b])
        db.session.commit()
        oa, ob = _order(tid, 300, node_uuid=a.uuid, n=1), _order(tid, 500, node_uuid=b.uuid, n=2)
        db.session.add_all([oa, ob])
        db.session.flush()
        at = oa.created_at
        db.session.add_all([PaymentRecord(tenant_id=tid, order_id=oa.id, method='Cash', amount=300, created_at=at),
                            PaymentRecord(tenant_id=tid, order_id=ob.id, method='Cash', amount=500, created_at=at),
                            PaymentRecord(tenant_id=tid, order_id=None, kind='topup', method='Cash', amount=1000,
                                          created_at=at)])
        db.session.commit()
        a_uuid, b_uuid = a.uuid, b.uuid
        only_a = report_service.build_report(tid, DAY, DAY, a_uuid)
        everything = report_service.build_report(tid, DAY, DAY)
    assert sum(v['net'] for v in only_a['by_method'].values()) == 300
    assert sum(v['net'] for v in everything['by_method'].values()) == 1800
    assert everything['node_names'] == {a_uuid: '甲分店', b_uuid: '乙分店'}
    wb = openpyxl.load_workbook(io.BytesIO(report_service.report_to_excel(everything, '測試').getvalue()))
    names = {row[0].value for row in wb['分店'].iter_rows(min_row=2)}
    assert names == {'甲分店', '乙分店'}
    # R2-145：線上端不計算人事成本 (打卡紀錄只在門市主機)
    assert everything['labor_available'] is False and only_a['labor_available'] is False


def test_report_payments_without_order_use_own_node_and_skip_forfeit(app):
    """B-386：沒有訂單的收款依收款紀錄自己的分店歸屬；R2-030：訂金沒收不計入付款方式統計"""
    from app.services import report_service
    tid = get_or_create_tenant(app, 'rea2-node-pay-own')
    a_uuid, b_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    at = datetime.combine(DAY, datetime.min.time()) + timedelta(hours=12)
    with tenant_request(app, tid):
        oa = _order(tid, 300, node_uuid=a_uuid, n=1)
        db.session.add(oa)
        db.session.flush()
        db.session.add_all([
            PaymentRecord(tenant_id=tid, order_id=oa.id, method='Cash', amount=300, created_at=at),
            PaymentRecord(tenant_id=tid, order_id=None, kind='settle', method='Cash', amount=400, node_uuid=a_uuid,
                          created_at=at),   # 桌位結帳：沒有單一訂單
            PaymentRecord(tenant_id=tid, order_id=None, kind='topup', method='Cash', amount=1000, node_uuid=b_uuid,
                          created_at=at),
            PaymentRecord(tenant_id=tid, order_id=None, kind='deposit', method='Cash', amount=200, node_uuid=a_uuid,
                          created_at=at),
            PaymentRecord(tenant_id=tid, order_id=None, kind='deposit_forfeit', method='Cash', amount=200,
                          node_uuid=a_uuid, created_at=at),
        ])
        db.session.commit()
        only_a = report_service.build_report(tid, DAY, DAY, a_uuid)
        only_b = report_service.build_report(tid, DAY, DAY, b_uuid)
        everything = report_service.build_report(tid, DAY, DAY)
    assert sum(v['net'] for v in only_a['by_method'].values()) == 300 + 400 + 200
    assert sum(v['net'] for v in only_b['by_method'].values()) == 1000
    assert sum(v['net'] for v in everything['by_method'].values()) == 300 + 400 + 1000 + 200


def test_report_promotions_and_avg_ticket(app):
    """B-381：滿額贈計次但不分折扣；R2-144：客單價排除 0 元訂單"""
    from app.models.promotion import Promotion
    from app.services import report_service
    tid = get_or_create_tenant(app, 'rea2-promo')
    with tenant_request(app, tid):
        db.session.add(Promotion(tenant_id=tid, name='滿500送飲料', kind='threshold_gift'))
        db.session.add_all([
            _order(tid, 450, n=1, promotion_discount=50, promotion_names='第二件半價、滿500送飲料',
                   items=[('漢堡', 250, 2, {}), ('飲料', 0, 1, {'is_gift': True, 'customization': '[滿500送飲料] 贈品'})]),
            _order(tid, 550, n=2, promotion_discount=0, promotion_names='滿500送飲料', items=[('漢堡', 550, 1, {})]),
            _order(tid, 0, n=3),   # 寄杯扣次 / 純點數兌換
        ])
        db.session.commit()
        rep = report_service.build_report(tid, DAY, DAY)
    assert rep['promotions']['第二件半價'] == {'count': 1, 'amount': 50}
    assert rep['promotions']['滿500送飲料'] == {'count': 2, 'amount': 0}
    assert rep['order_count'] == 3 and rep['avg_ticket'] == 500


def test_labor_cost_ignores_forgotten_clock_out(app):
    """R2-140：忘了打下班卡的紀錄不可一路算到區間結束；超長的已下班紀錄列出提醒"""
    from app.models.staff import TimeClock
    from app.models.tenant import StaffAccount
    from app.services import report_service
    tid = get_or_create_tenant(app, 'rea2-labor')
    with tenant_request(app, tid):
        acc = StaffAccount(tenant_id=tid, username=f'rea2_labor_{uuid.uuid4().hex[:8]}', role='staff', hourly_wage=200,
                           password_hash='!')
        db.session.add(acc)
        db.session.flush()
        db.session.add_all([
            TimeClock(tenant_id=tid, staff_account_id=acc.id, clock_in=datetime(2019, 10, 1, 9),
                      clock_out=datetime(2019, 10, 1, 17)),
            TimeClock(tenant_id=tid, staff_account_id=acc.id, clock_in=datetime(2019, 10, 2, 9)),   # 忘了打下班卡
            TimeClock(tenant_id=tid, staff_account_id=acc.id, clock_in=datetime(2019, 10, 5, 9),
                      clock_out=datetime(2019, 10, 7, 10)),                                         # 隔兩天才補打
        ])
        db.session.commit()
        r = report_service.labor_cost(tid, datetime(2019, 10, 1), datetime(2019, 11, 1), 100000)
        open_clock_in = datetime.now() - timedelta(hours=2)
        open_now = TimeClock(tenant_id=tid, staff_account_id=acc.id, clock_in=open_clock_in)
        db.session.add(open_now)
        db.session.commit()
        t0 = datetime.combine(date.today(), datetime.min.time())
        today = report_service.labor_cost(tid, t0, t0 + timedelta(days=1), 1000)
    assert r['labor_hours'] == 8 + 49
    review = {(x['clock_in'], x['counted']) for x in r['labor_review']}
    assert review == {(datetime(2019, 10, 2, 9), False), (datetime(2019, 10, 5, 9), True)}
    if open_clock_in >= t0:   # 今天還在班的人照常算到現在
        assert 1.9 < today['labor_hours'] < 2.2 and today['labor_review'] == []


# ---------------------------------------------------------------------------
# 定期寄送報表
# ---------------------------------------------------------------------------
def _report_settings(app, tid, **values):
    from app.services import settings_service
    with app.app_context():
        settings_service.put('report_email', dict({'enabled': True, 'send_time': '08:00',
                                                   'recipients': 'boss@example.com'}, **values), tenant_id=tid)
        db.session.commit()


def _fake_mail(monkeypatch):
    from app.services import mail_service
    sent = []

    def send_mail(tenant_id, recipients, subject, body, attachments=None):
        sent.append(subject)
        return 1
    monkeypatch.setattr(mail_service, 'send_mail', send_mail)
    return sent


def test_weekly_report_catches_up_when_monday_missed(app, monkeypatch):
    """B-383：週一沒開機，週二開機時補寄上週報表，之後不重複寄"""
    from app.services import report_service
    sent = _fake_mail(monkeypatch)
    tid = get_or_create_tenant(app, 'rea2-weekly')
    _report_settings(app, tid, frequency='weekly')
    monday = date(2026, 11, 2)
    with app.app_context():
        assert report_service.send_scheduled_report(tid, monday + timedelta(days=1)) == 1
        assert report_service.send_scheduled_report(tid, monday + timedelta(days=2)) == 0
        assert report_service.send_scheduled_report(tid, monday + timedelta(days=7)) == 1
    assert sent == ['營運報表 2026-10-26 ~ 2026-11-01', '營運報表 2026-11-02 ~ 2026-11-08']
    # 每天頻率：漏寄的日子合併補寄
    _report_settings(app, tid, frequency='daily')
    with app.app_context():
        assert report_service.send_scheduled_report(tid, date(2026, 11, 3)) == 1
        assert report_service.send_scheduled_report(tid, date(2026, 11, 6)) == 1
    assert sent[-2:] == ['營運報表 2026-11-02 ~ 2026-11-02', '營運報表 2026-11-03 ~ 2026-11-05']


def test_node_does_not_send_report_with_hq_settings(app, monkeypatch):
    """B-374：分店套用總部下發的定期報表設定時不由排程寄出 (手動立即寄送與本店自訂仍可寄)"""
    from app.models.settings import HqSetting
    from app.services import report_service, settings_service
    sent = _fake_mail(monkeypatch)
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
    tid = get_or_create_tenant(app, 'rea2-node-report')
    with app.app_context():
        db.session.add(HqSetting(tenant_id=tid, key='report_email', value=json.dumps(
            {'enabled': True, 'frequency': 'daily', 'recipients': 'boss@example.com'})))
        db.session.commit()
        assert settings_service.source('report_email', tid) == 'hq'
        assert report_service.send_scheduled_report(tid, date(2026, 11, 3)) == 0
        assert report_service.send_scheduled_report(tid, date(2026, 11, 3), force=True) == 1
    _report_settings(app, tid, frequency='daily')
    with app.app_context():
        assert report_service.send_scheduled_report(tid, date(2026, 11, 3)) == 1
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# 後台總覽
# ---------------------------------------------------------------------------
def _live(total, minutes_ago=5, status='Pending', **kw):
    now = datetime.now()
    return Order(total_price=total, status=status, pickup_number=1, created_at=now - timedelta(minutes=minutes_ago), **kw)


def test_dashboard_analytics_matches_report_and_kitchen(app):
    """B-368 / R2-138 / B-369：營收只算已收款並扣掉部分退款；待製作 / 逾期與廚房看板一致"""
    from app.services.analytics_service import build_order_analytics
    now = datetime.now()
    if now.hour == 0 and now.minute < 30 or now.hour == 23 and now.minute > 30:
        return   # 換日前後「幾分鐘前」可能落在別天
    today0 = datetime.combine(date.today(), datetime.min.time())
    orders = [
        _live(300, status='Completed', refunded_amount=100),           # 現金單作廢一項退 $100
        _live(5000, payment_status='pending'),                          # 線上待付款：不算營收、不進廚房
        _live(200, payment_status='unpaid'),                            # 桌邊未結帳：不算營收，但在廚房製作中
        _live(150, pickup_at=today0 + timedelta(days=2)),               # 今天建立、預約後天取餐：還不進廚房
        Order(total_price=80, status='Pending', pickup_number=2, created_at=today0 - timedelta(days=1),
              pickup_at=now + timedelta(minutes=30)),                   # 昨天預約今天取餐：在廚房、不是逾期
        Order(total_price=90, status='Pending', pickup_number=3, created_at=today0 - timedelta(days=1),
              pickup_at=today0 + timedelta(days=3)),                    # 昨天預約之後取餐：不是逾期
        Order(total_price=70, status='Pending', pickup_number=4, created_at=today0 - timedelta(days=2)),   # 真的遺留
    ]
    with app.app_context():
        a = build_order_analytics(orders)
    assert a['today_revenue'] == 200 + 150 and a['today_orders'] == 2
    assert a['avg_order_value'] == 175.0
    assert a['pending_count'] == 2 and a['stale_pending_count'] == 1


def test_hub_eta_uses_busiest_store(app, monkeypatch):
    """R2-141：線上端的預估等候以最忙的一家分店計算，不把各分店排隊張數加總"""
    from app.services.analytics_service import DEFAULT_MINUTES_PER_ORDER, build_order_analytics
    from app.sync import runtime as sync_runtime
    now = datetime.now()
    if now.hour == 0 and now.minute < 30:
        return
    orders = [_live(100, minutes_ago=1, node_uuid=f'node-{i % 3}') for i in range(15)]
    with app.app_context():
        monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
        hub = build_order_analytics(orders)
        monkeypatch.setattr(sync_runtime, 'is_hub', lambda: False)
        single = build_order_analytics(orders)
    assert hub['pending_count'] == 15 and hub['eta_minutes'] == int(5 * DEFAULT_MINUTES_PER_ORDER)
    assert single['eta_minutes'] == int(15 * DEFAULT_MINUTES_PER_ORDER)


# ---------------------------------------------------------------------------
# 備料預測
# ---------------------------------------------------------------------------
def test_forecast_far_future_and_combo_sides_without_nplus1(app):
    """B-379：預測日期在很久以後仍取得到歷史資料；R2-142：套餐配餐不逐列查菜單"""
    from app.services import forecast_service
    tid = get_or_create_tenant(app, 'rea2-forecast')
    today = date(2030, 6, 3)
    target = today + timedelta(days=90)
    days = forecast_service.reference_days(target, 4, today)
    assert len(days) == 4 and all(d < today and d.weekday() == target.weekday() for d in days)
    assert days[0] > today - timedelta(days=7)
    assert forecast_service.reference_days(today + timedelta(days=1), 2, today) == \
        [today + timedelta(days=1 - 7), today + timedelta(days=1 - 14)]
    with tenant_request(app, tid):
        main = MenuItem(tenant_id=tid, name='預測套餐主餐', price=150, category='套餐')
        side = MenuItem(tenant_id=tid, name='預測配餐薯條', price=0, category='配餐')
        db.session.add_all([main, side])
        db.session.flush()
        sides, main_id = json.dumps([side.uuid]), main.id

        def combo_orders(count, start_n):
            return [_order(tid, 150, at=datetime.combine(days[0], datetime.min.time()) + timedelta(hours=12),
                           n=start_n + i, items=[('預測套餐主餐', 150, 1, {'menu_item_id': main_id, 'combo_sides': sides})])
                    for i in range(count)]
        db.session.add_all(combo_orders(2, 1))
        db.session.commit()
    with tenant_request(app, tid):
        with count_queries(app) as few:
            forecast_service.forecast(target, 4, today)
    with tenant_request(app, tid):
        db.session.add_all(combo_orders(6, 10))
        db.session.commit()
    with tenant_request(app, tid):
        with count_queries(app) as many:
            f = forecast_service.forecast(target, 4, today)
    rows = {r['name']: r for r in f['items']}
    assert rows['預測配餐薯條']['history'][0] == 8 and rows['預測套餐主餐']['history'][0] == 8
    assert many['n'] == few['n'], many['sql']
