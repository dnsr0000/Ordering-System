"""自動排班：可排時段頁、生成一週班表 (可排時段 / 每日每週上限 / 連續天數 / 平均分攤)、套用與不重複"""
from datetime import date, datetime, timedelta

from app.extensions import db
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.staff import StaffAvailability, WorkSchedule
from app.models.tenant import StaffAccount
from app.services import scheduling_service
from tests.support import default_tenant_id, other_shop_owner_client

SHOP = 'sched-shop'   # 兩個測試各用獨立店家，不受其他測試在預設店家留下的訂單 / 班表影響
RULES_SHOP = 'sched-rules'

NEXT_MONDAY = date(2026, 11, 16)   # 固定的未來一週 (週一)


def _staff(app, username, max_hours_week=0, tid=None):
    tid = tid or default_tenant_id(app)
    with app.app_context():
        acc = StaffAccount.query.filter_by(username=username).first()
        if acc is None:
            acc = StaffAccount(tenant_id=tid, username=username, role='staff', hourly_wage=200)
            acc.set_password('SchedPass!2026')
            db.session.add(acc)
        acc.is_active, acc.deleted_at, acc.max_hours_week = True, None, max_hours_week
        db.session.commit()
        return acc.id


def _seed_orders(app, weekday_dates, hours, per_hour=15, tid=None):
    """在指定日期的幾個小時各塞 per_hour 張訂單 (讓 plan 估算出需要人力)"""
    tid = tid or default_tenant_id(app)
    with app.app_context():
        for d in weekday_dates:
            for h in hours:
                for k in range(per_hour):
                    db.session.add(Order(tenant_id=tid, total_price=100, status=OrderStatus.PICKED_UP, order_type='內用',
                                         payment_method='Cash', order_source='POS',
                                         created_at=datetime.combine(d, datetime.min.time()) + timedelta(hours=h, minutes=k)))
        db.session.commit()


def test_availability_page_and_generate_apply(app):
    from app.models.tenant import Tenant
    from app.services import settings_service
    manager_client = other_shop_owner_client(app, SHOP)
    with app.app_context():
        tid = Tenant.query.filter_by(slug=SHOP).first().id
        settings_service.put('staffing', {'orders_per_staff_hour': 12, 'min_staff': 0, 'shift_hours': 4, 'weeks': 4,
                                          'max_hours_day': 8, 'max_hours_week': 40, 'max_consecutive_days': 6}, tenant_id=tid)
        db.session.commit()
    a, b, c = _staff(app, 'sched_a', tid=tid), _staff(app, 'sched_b', max_hours_week=8, tid=tid), _staff(app, 'sched_c', tid=tid)
    # 過去四個週一 / 週二的 11~14 點各 15 張訂單 → 每小時需要 2 人 → 每天兩個 11:00–15:00 班別
    today = NEXT_MONDAY - timedelta(days=3)
    mondays = [NEXT_MONDAY - timedelta(days=7 * k) for k in range(1, 5)]
    tuesdays = [d + timedelta(days=1) for d in mondays]
    _seed_orders(app, mondays + tuesdays, [11, 12, 13, 14], tid=tid)

    # 可排時段頁：a 週一～週日 9~18 可排、b 只有週一、c 沒填
    r = manager_client.post('/admin/availability', data={'action': 'save', 'staff_id': a, 'max_hours_week': '',
                                                         'weekday': ['1', '2', '3', '4', '5', '6', '7'], 'start_time': ['09:00'] * 7,
                                                         'end_time': ['18:00'] * 7})
    assert r.status_code == 302
    manager_client.post('/admin/availability', data={'action': 'save', 'staff_id': b, 'max_hours_week': '8',
                                                     'weekday': ['1', ''], 'start_time': ['10:00', ''], 'end_time': ['16:00', '']})
    r = manager_client.post('/admin/availability', data={'action': 'save', 'staff_id': a, 'weekday': ['1'], 'start_time': ['9'],
                                                         'end_time': ['18:00']})
    assert r.status_code in (302, 400), '時間格式錯誤：表單錯誤以 flash 顯示'
    page = manager_client.get('/admin/availability').get_data(as_text=True)
    assert 'sched_a' in page and '7 段可排時段' in page and '尚未填寫' in page
    with app.app_context():
        assert StaffAvailability.query.filter_by(staff_account_id=a).count() == 7
        assert StaffAvailability.query.filter_by(staff_account_id=b).count() == 1
        assert db.session.get(StaffAccount, b).max_hours_week == 8

    with app.test_request_context('/'):
        from app.tenant_context import _use_tenant
        _use_tenant(Tenant.query.filter_by(slug=SHOP).first(), remember=False)
        plan = scheduling_service.generate(tid, NEXT_MONDAY, today=today)
    assert plan['week_start'] == NEXT_MONDAY and 'sched_c' in plan['skipped']
    monday = plan['days'][0]
    assert [s['start'] for s in monday['shifts']] == ['11:00', '11:00'] and monday['shifts'][0]['end'] == '15:00'
    assert {s['username'] for s in monday['shifts']} == {'sched_a', 'sched_b'}, '週一兩個班別分給兩個人 (同一人一天只排一班)'
    tuesday = plan['days'][1]
    assert [s['username'] for s in tuesday['shifts']] == ['sched_a', ''], 'b 週二不可排、c 沒填 → 第二個班別未排'
    assert plan['unfilled'] == 1 and plan['assigned'] == 3
    hours = {s['username']: s['hours'] for s in plan['summary']}
    assert hours['sched_a'] == 8 and hours['sched_b'] == 4
    assert next(s for s in plan['summary'] if s['username'] == 'sched_b')['cap'] == 8

    # 預覽頁與套用
    page = manager_client.get(f'/admin/staffing/auto?week={NEXT_MONDAY}').get_data(as_text=True)
    assert 'sched_a' in page and '未排' in page and 'name="count" value="3"' in page
    r = manager_client.post('/admin/staffing/auto', data={'week': str(NEXT_MONDAY), 'count': '3',
                                                          'date_0': str(NEXT_MONDAY), 'staff_0': a, 'start_0': '11:00', 'end_0': '15:00',
                                                          'date_1': str(NEXT_MONDAY), 'staff_1': b, 'start_1': '11:00', 'end_1': '15:00',
                                                          'date_2': str(NEXT_MONDAY + timedelta(days=1)), 'staff_2': a, 'start_2': '11:00', 'end_2': '15:00'})
    assert r.status_code == 302
    with app.app_context():
        rows = WorkSchedule.query.filter(WorkSchedule.tenant_id == tid, WorkSchedule.work_date >= NEXT_MONDAY,
                                         WorkSchedule.note == '自動排班').all()
        assert len(rows) == 3
    # 再套用一次不重複；既有班表讓需求變成已排足
    manager_client.post('/admin/staffing/auto', data={'week': str(NEXT_MONDAY), 'count': '1', 'date_0': str(NEXT_MONDAY), 'staff_0': a,
                                                      'start_0': '11:00', 'end_0': '15:00'})
    with app.app_context():
        assert WorkSchedule.query.filter(WorkSchedule.tenant_id == tid, WorkSchedule.work_date >= NEXT_MONDAY,
                                         WorkSchedule.note == '自動排班').count() == 3
    with app.test_request_context('/'):
        _use_tenant(Tenant.query.filter_by(slug=SHOP).first(), remember=False)
        plan2 = scheduling_service.generate(tid, NEXT_MONDAY, today=today)
    assert plan2['days'][0]['shifts'] == [] and plan2['days'][1]['shifts'][0]['username'] == '', '週一已排足；週二剩下的班別仍沒人'
    assert c


def test_labor_rules_weekly_cap_and_consecutive_days(app):
    # 獨立店家：預設店家有其他測試以「現在」建立的訂單，執行日落在歷史日 (約 2026-12-25 ~ 2027-01-21) 時會多出班別
    from app.models.tenant import Tenant
    from app.services import settings_service
    other_shop_owner_client(app, RULES_SHOP)
    with app.app_context():
        tid = Tenant.query.filter_by(slug=RULES_SHOP).first().id
        settings_service.put('staffing', {'orders_per_staff_hour': 12, 'min_staff': 0, 'shift_hours': 4, 'weeks': 4,
                                          'max_hours_day': 8, 'max_hours_week': 12, 'max_consecutive_days': 2}, tenant_id=tid)
        db.session.commit()
    a = _staff(app, 'rules_a', tid=tid)
    with app.app_context():
        StaffAvailability.query.filter_by(staff_account_id=a).delete(synchronize_session=False)
        for w in range(1, 8):
            db.session.add(StaffAvailability(tenant_id=tid, staff_account_id=a, weekday=w, start_time='09:00', end_time='20:00'))
        # 本店其他員工本測試不參與 (只動本店，不影響其他店家)
        StaffAvailability.query.filter(StaffAvailability.tenant_id == tid,
                                       StaffAvailability.staff_account_id != a).delete(synchronize_session=False)
        db.session.commit()
    base = NEXT_MONDAY + timedelta(days=70)   # 離第一個測試的歷史資料夠遠，週別不重疊
    today = base - timedelta(days=3)
    history = [base - timedelta(days=7 * k) + timedelta(days=off) for k in range(1, 5) for off in range(7)]
    _seed_orders(app, history, [12, 13, 14, 15], per_hour=12, tid=tid)   # 每天 12~16 點各需要 1 人 → 每天一個 4 小時班
    with app.test_request_context('/'):
        from app.tenant_context import _use_tenant
        _use_tenant(Tenant.query.filter_by(slug=RULES_SHOP).first(), remember=False)
        plan = scheduling_service.generate(tid, base, today=today)
    worked = [d['shifts'][0]['username'] == 'rules_a' for d in plan['days'] if d['shifts']]
    assert len(worked) == 7
    assert worked == [True, True, False, True, False, False, False], '連續 2 天後必須休 1 天；每週上限 12 小時 = 3 班'
    assert plan['unfilled'] == 4
