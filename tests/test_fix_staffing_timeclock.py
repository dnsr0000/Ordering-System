"""修正小組 staffing-timeclock 的回歸測試：排班建議 / 自動排班 / 打卡 / 員工異常偵測"""
import time
from datetime import date, datetime, timedelta

from app.extensions import db
from app.models.order import Order, OrderItem
from app.models.payment import Refund
from app.models.staff import StaffAvailability, TimeClock, WorkSchedule
from app.models.tenant import StaffAccount
from app.models.user import StaffAuditLog
from app.services import scheduling_service, settings_service, staff_anomaly_service, staffing_service
from tests.support import assert_flash, get_or_create_tenant, login_staff, tenant_request

MONDAY = date(2026, 11, 16)
TUESDAY = MONDAY + timedelta(days=1)


def _staff(tid, username, active=True, deleted=False):
    acc = StaffAccount.query.filter_by(username=username).first()
    if acc is None:
        acc = StaffAccount(tenant_id=tid, username=username, role='staff', hourly_wage=200)
        acc.set_password('FixPass!2026x')
        db.session.add(acc)
    acc.is_active = active
    acc.deleted_at = datetime.now() if deleted else None
    db.session.flush()
    return acc


# ---------------------------------------------------------------- B-413 跨午夜營業
def test_business_hours_excludes_overnight_closed_period(app, monkeypatch):
    late = [0, *range(11, 24)]
    assert staffing_service.business_hours(late) == [*range(11, 24), 0]
    assert staffing_service.business_hours([11, 12, 18, 21]) == list(range(11, 22))   # 一般的店不變
    assert staffing_service.business_hours([]) == []
    demand = {0: 2, 1: 0.6, **{h: 5 for h in range(11, 24)}}
    monkeypatch.setattr(staffing_service, 'hourly_demand', lambda *a, **k: (demand, 1000, 2))
    tid = get_or_create_tenant(app, 'fix-stf-late')
    with tenant_request(app, tid):
        p = staffing_service.plan(tid, MONDAY + timedelta(days=21))
    need_hours = {r['hour'] for r in p['rows'] if r['need'] > 0}
    assert not need_hours & set(range(2, 11)), '打烊時段 (2~10 點) 不應需要人力'
    for s in p['shifts']:
        assert not (int(s['start'][:2]) < 11 and int(s['end'][:2]) > 2), f"建議班別排進打烊時段：{s}"


def test_suggest_shifts_keeps_original_behaviour_for_contiguous_hours():
    assert staffing_service.suggest_shifts({12: 2, 13: 0, 14: 0, 15: 0, 16: 1, 17: 1, 18: 1}, 4) == \
        [(12, 16), (12, 16), (15, 19)]
    assert staffing_service.suggest_shifts({0: 1, **{h: 1 for h in range(20, 24)}}, 4) == [(0, 1), (20, 24)]


# ---------------------------------------------------------------- R2-156 / B-412 在班人力
def test_scheduled_by_hour_counts_overnight_and_skips_inactive(app):
    tid = get_or_create_tenant(app, 'fix-stf-have')
    with tenant_request(app, tid):
        a = _staff(tid, 'fix_stf_have_a')
        gone = _staff(tid, 'fix_stf_have_gone', active=False, deleted=True)
        db.session.add_all([
            WorkSchedule(tenant_id=tid, staff_account_id=a.id, work_date=MONDAY, start_time='20:00', end_time='02:00'),
            WorkSchedule(tenant_id=tid, staff_account_id=gone.id, work_date=TUESDAY, start_time='09:00', end_time='17:00'),
        ])
        db.session.commit()
        have, rows = staffing_service.scheduled_by_hour(tid, TUESDAY)
        assert dict(have) == {0: 1, 1: 1} and rows == []
        # 停用 / 刪除時清除今天起的班表與可排時段
        db.session.add(StaffAvailability(tenant_id=tid, staff_account_id=gone.id, weekday=2, start_time='09:00',
                                         end_time='17:00'))
        db.session.flush()
        assert staffing_service.clear_future_schedules(tid, gone.id, today=MONDAY, availability=True) == 1
        assert StaffAvailability.query.filter_by(staff_account_id=gone.id).count() == 0
        db.session.commit()


def test_generate_skips_staff_still_on_previous_overnight_shift(app, monkeypatch):
    tid = get_or_create_tenant(app, 'fix-stf-overnight')

    def fake_plan(tenant_id, d, today=None):
        rows = [{'hour': h, 'need': 1, 'have': 0} for h in range(0, 4)] if d == TUESDAY else []
        return {'rows': rows, 'need_hours': len(rows), 'have_hours': 0, 'days_used': 1}
    monkeypatch.setattr(staffing_service, 'plan', fake_plan)
    with tenant_request(app, tid):
        a = _staff(tid, 'fix_stf_on_a')
        b = _staff(tid, 'fix_stf_on_b')
        for s in (a, b):
            db.session.add(StaffAvailability(tenant_id=tid, staff_account_id=s.id, weekday=2, start_time='00:00',
                                             end_time='23:59'))
        db.session.add(WorkSchedule(tenant_id=tid, staff_account_id=a.id, work_date=MONDAY, start_time='20:00',
                                    end_time='02:00'))
        db.session.commit()
        plan = scheduling_service.generate(tid, MONDAY, today=MONDAY - timedelta(days=1))
    tue = plan['days'][1]['shifts']
    assert [(r['start'], r['end'], r['username']) for r in tue] == [('00:00', '04:00', 'fix_stf_on_b')]


# ---------------------------------------------------------------- B-411 / R2-158
def test_availability_2359_and_shift_longer_than_daily_cap(app, monkeypatch):
    tid = get_or_create_tenant(app, 'fix-stf-cap')

    def fake_plan(tenant_id, d, today=None):
        rows = [{'hour': h, 'need': 1, 'have': 0} for h in range(14, 24)] if d == MONDAY else []
        return {'rows': rows, 'need_hours': len(rows), 'have_hours': 0, 'days_used': 1}
    monkeypatch.setattr(staffing_service, 'plan', fake_plan)
    with tenant_request(app, tid):
        settings_service.put('staffing', {'shift_hours': 10, 'max_hours_day': 8}, tid)
        a = _staff(tid, 'fix_stf_cap_a')
        b = _staff(tid, 'fix_stf_cap_b')
        for s in (a, b):
            db.session.add(StaffAvailability(tenant_id=tid, staff_account_id=s.id, weekday=1, start_time='12:00',
                                             end_time='23:59'))
        db.session.commit()
        avail = scheduling_service.availability_map(tid)
        assert scheduling_service.is_available(avail, a.id, 1, 20, 24)
        plan = scheduling_service.generate(tid, MONDAY, today=MONDAY - timedelta(days=1))
    assert plan['unfilled'] == 0 and plan['assigned'] == 2
    assert all(r['hours'] <= 8 for r in plan['days'][0]['shifts'])


# ---------------------------------------------------------------- B-418 / R2-157 / R2-161
def test_staffing_apply_dedup_count_cap_and_date_range(app):
    tid = get_or_create_tenant(app, 'fix-stf-apply')
    target = date.today() + timedelta(days=3)
    with tenant_request(app, tid):
        sid = _staff(tid, 'fix_stf_apply_a').id
        db.session.commit()
        assert staffing_service.history_days(date(1, 1, 3), 4) == []
    owner = login_staff(app, 'fix_stf_apply_owner', 'owner', tenant_id=tid)
    form = {'date': target.isoformat(), 'count': '2', 'staff_0': str(sid), 'start_0': '10:00', 'end_0': '14:00',
            'staff_1': str(sid), 'start_1': '10:00', 'end_1': '14:00'}
    owner.post('/admin/staffing', data=form)
    owner.post('/admin/staffing', data=form)
    with app.app_context():
        assert WorkSchedule.query.filter_by(staff_account_id=sid, work_date=target).count() == 1
    t0 = time.monotonic()
    owner.post('/admin/staffing', data={'date': target.isoformat(), 'count': '999999999999'})
    owner.post('/admin/staffing/auto', data={'count': '999999999999'})
    assert time.monotonic() - t0 < 10
    for url in ('/admin/staffing?date=0001-01-03', '/admin/staffing/auto?week=0001-01-01'):
        assert owner.get(url).status_code != 500
    owner.get('/admin/staffing?date=0001-01-03')
    assert_flash(owner, '日期超出範圍')


# ---------------------------------------------------------------- R2-155 線上端
def test_timeclock_pages_404_on_hub(app, monkeypatch):
    from app.sync import runtime as sync_runtime
    owner = login_staff(app, 'fix_stf_hub_owner', 'owner', tenant_id=get_or_create_tenant(app, 'fix-stf-hub'))
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    for url in ('/timeclock', '/admin/timeclock', '/admin/staffing', '/admin/staffing/auto', '/admin/availability'):
        assert owner.get(url).status_code == 404, url


# ---------------------------------------------------------------- B-414 / B-415 / B-416 打卡
def test_punch_hashes_for_other_shop_and_blocks_duplicate_clock_in(app, monkeypatch):
    import app.routes.timeclock as timeclock_routes
    tid = get_or_create_tenant(app, 'fix-stf-punch')
    other = get_or_create_tenant(app, 'fix-stf-punch-other')
    with app.app_context():
        _staff(tid, 'fix_stf_punch_a')
        _staff(other, 'fix_stf_punch_other')
        db.session.commit()
    c = app.test_client()
    c.get('/?shop=fix-stf-punch')
    calls = []
    orig = StaffAccount.check_password
    monkeypatch.setattr(StaffAccount, 'check_password', lambda self, pw: calls.append(self.username) or orig(self, pw))
    r = c.post('/timeclock', data={'username': 'fix_stf_punch_other', 'password': 'FixPass!2026x', 'action': 'in'})
    assert calls == ['fix_stf_punch_other'] and r.status_code == 401
    c.post('/timeclock', data={'username': 'fix_stf_punch_a', 'password': 'FixPass!2026x', 'action': 'in'})
    assert_flash(c, '上班打卡')
    # 模擬併發：另一個請求查詢時還看不到這筆未下班紀錄
    monkeypatch.setattr(timeclock_routes, '_open_record', lambda staff_id: None)
    c.post('/timeclock', data={'username': 'fix_stf_punch_a', 'password': 'FixPass!2026x', 'action': 'in'})
    assert_flash(c, '請勿重複送出')
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='fix_stf_punch_a').one()
        assert TimeClock.query.filter_by(staff_account_id=acc.id, clock_out=None).count() == 1


def test_edit_record_rejects_stale_form(app):
    tid = get_or_create_tenant(app, 'fix-stf-edit')
    day = date.today().isoformat()
    with app.app_context():
        acc = _staff(tid, 'fix_stf_edit_a')
        rec = TimeClock(tenant_id=tid, staff_account_id=acc.id, clock_in=datetime.fromisoformat(f'{day}T09:00'),
                        clock_out=datetime.fromisoformat(f'{day}T18:00'))
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
    owner = login_staff(app, 'fix_stf_edit_owner', 'owner', tenant_id=tid)
    # 載入頁面時還沒下班 (orig_clock_out 空白)，之後員工才下班打卡
    owner.post('/admin/timeclock', data={'action': 'edit_record', 'record_id': rid, 'clock_in': f'{day}T08:30',
                                         'clock_out': '', 'orig_clock_in': f'{day}T09:00', 'orig_clock_out': '',
                                         'note': '補登'})
    assert_flash(owner, '請重新整理')
    with app.app_context():
        assert db.session.get(TimeClock, rid).clock_out is not None
    owner.post('/admin/timeclock', data={'action': 'edit_record', 'record_id': rid, 'clock_in': f'{day}T08:30',
                                         'clock_out': f'{day}T18:00', 'orig_clock_in': f'{day}T09:00',
                                         'orig_clock_out': f'{day}T18:00', 'note': '補登'})
    with app.app_context():
        r = db.session.get(TimeClock, rid)
        assert r.clock_in.hour == 8 and r.clock_out is not None and r.note == '補登'


# ---------------------------------------------------------------- B-417 / R2-159 / R2-160 員工異常偵測
def test_anomaly_override_status_split_cancel_and_void_items_by_node(app):
    tid = get_or_create_tenant(app, 'fix-stf-anomaly')
    now = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=12)
    with tenant_request(app, tid):
        def order(cashier, status='PickedUp', payment_status=None, override=None, node=None):
            o = Order(tenant_id=tid, total_price=100, cashier_username=cashier, status=status, payment_method='Cash',
                      payment_status=payment_status, order_type='內用', created_at=now, pickup_number=1, node_uuid=node)
            db.session.add(o)
            db.session.flush()
            db.session.add(OrderItem(tenant_id=tid, order_id=o.id, menu_item_id=None, item_name='x', price=50,
                                     quantity=1, original_price=override))
            return o
        order('cx', override=100)                               # 有效改價
        order('cx', status='Cancelled', override=100)           # 已作廢：不計
        order('cx', payment_status='pending', override=100)     # 未收款：不計
        split = order('cy')
        split.status = 'Cancelled'
        for amount in (30, 30, 40):   # 混合付款整單作廢：一次作廢三筆退款
            db.session.add(Refund(tenant_id=tid, order_id=split.id, kind='cancel', amount=amount, staff_username='cy'))
        o_a = order('cz', node='node-a')
        o_b = order('cz', node='node-b')
        db.session.add_all([
            StaffAuditLog(tenant_id=tid, staff_username='cz', action_type='VOID_ITEM', target_order_id=o_b.id,
                          created_at=now),
            StaffAuditLog(tenant_id=tid, staff_username='cz', action_type='VOID_ITEM', target_order_id=o_b.id,
                          created_at=now),
            StaffAuditLog(tenant_id=tid, staff_username='cz', action_type='VOID_ITEM', target_order_id=o_a.id,
                          created_at=now),
        ])
        db.session.commit()
        by = {r['name']: r for r in staff_anomaly_service.analyze(tid, date.today(), date.today())['staff']}
        assert by['cx']['override_count'] == 1 and by['cx']['override_amount'] == 50
        assert by['cy']['cancel_count'] == 1 and by['cy']['cancel_amount'] == 100
        assert by['cz']['void_items'] == 3
        by_a = {r['name']: r for r in staff_anomaly_service.analyze(tid, date.today(), date.today(), 'node-a')['staff']}
        assert by_a['cz']['void_items'] == 1


# ---------------------------------------------------------------- B-379 很久以後的日期
def test_history_days_far_future_target():
    today = date(2026, 11, 9)
    far = today + timedelta(weeks=30)
    days = staffing_service.history_days(far, 4, today)
    assert len(days) == 4 and all(d < today and d.weekday() == far.weekday() for d in days)
    assert days[0] >= today - timedelta(days=7)   # 從今天以前最近的同星期幾開始
    # 近期的日期結果與原本相同
    assert staffing_service.history_days(today + timedelta(days=1), 2, today) == \
        [today - timedelta(days=6), today - timedelta(days=13)]
    assert staffing_service.history_days(today + timedelta(days=7), 1, today) == [today - timedelta(days=7)]


# ---------------------------------------------------------------- R2-140 超過 16 小時才下班打卡
def test_punch_out_after_16_hours_is_flagged(app):
    tid = get_or_create_tenant(app, 'fix-stf-long')
    with app.app_context():
        acc = _staff(tid, 'fix_stf_long_a')
        db.session.add(TimeClock(tenant_id=tid, staff_account_id=acc.id, clock_in=datetime.now() - timedelta(hours=20)))
        db.session.commit()
        aid = acc.id
    c = app.test_client()
    c.get('/?shop=fix-stf-long')
    c.post('/timeclock', data={'username': 'fix_stf_long_a', 'password': 'FixPass!2026x', 'action': 'out'})
    assert_flash(c, '待店長修正')
    with app.app_context():
        rec = TimeClock.query.filter_by(staff_account_id=aid).one()
        assert rec.clock_out is not None and rec.note.startswith('[待店長修正]')


# ---------------------------------------------------------------- B-433 帶時區的修正時間
def test_edit_record_with_timezone_offset_is_not_500(app):
    tid = get_or_create_tenant(app, 'fix-stf-edit-tz')
    day = date.today().isoformat()
    with app.app_context():
        acc = _staff(tid, 'fix_stf_edit_tz_a')
        rec = TimeClock(tenant_id=tid, staff_account_id=acc.id, clock_in=datetime.fromisoformat(f'{day}T09:00'))
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
    owner = login_staff(app, 'fix_stf_edit_tz_owner', 'owner', tenant_id=tid)
    r = owner.post('/admin/timeclock', data={'action': 'edit_record', 'record_id': rid,
                                             'clock_in': f'{day}T09:00+08:00', 'clock_out': f'{day}T18:00'})
    assert r.status_code != 500
    with app.app_context():
        rec = db.session.get(TimeClock, rid)
        assert rec.clock_in.tzinfo is None and rec.clock_out is not None
