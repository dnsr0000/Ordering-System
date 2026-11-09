"""跨檔請求 templates/admin_timeclock.html 的回歸測試：B-201 / B-412 / B-416"""
from datetime import date, datetime

from app.extensions import db
from app.models.staff import TimeClock, WorkSchedule
from app.models.tenant import StaffAccount
from tests.support import get_or_create_tenant, login_staff


def test_admin_timeclock_page_renders_shop_link_inactive_schedules_and_orig_values(app):
    tid = get_or_create_tenant(app, 'fix-atc-page')
    today = date.today()
    with app.app_context():
        gone = StaffAccount(tenant_id=tid, username='fix_atc_gone', role='staff', is_active=False)
        gone.set_password('FixPass!2026x')
        db.session.add(gone)
        db.session.flush()
        ws = WorkSchedule(tenant_id=tid, staff_account_id=gone.id, work_date=today, start_time='09:00', end_time='17:00')
        rec = TimeClock(tenant_id=tid, staff_account_id=gone.id, clock_in=datetime(today.year, today.month, today.day, 9, 5))
        db.session.add_all([ws, rec])
        db.session.commit()
        wid = ws.id
    owner = login_staff(app, 'fix_atc_owner', 'owner', tenant_id=tid)
    html = owner.get(f'/admin/timeclock?start={today.isoformat()}&end={today.isoformat()}').get_data(as_text=True)
    # B-201：打卡頁連結與提示文字帶店家代號
    assert '/timeclock?shop=fix-atc-page' in html
    # B-412：停用員工的班表另外列出，可刪除
    assert '已停用 / 刪除員工的班表' in html and f'name="schedule_id" value="{wid}"' in html
    # B-416：修正表單帶載入當下的原值
    assert f'name="orig_clock_in" value="{today.isoformat()}T09:05"' in html
    assert 'name="orig_clock_out" value=""' in html
    owner.post(f'/admin/timeclock?start={today.isoformat()}', data={'action': 'delete_schedule', 'schedule_id': wid})
    with app.app_context():
        assert db.session.get(WorkSchedule, wid) is None
