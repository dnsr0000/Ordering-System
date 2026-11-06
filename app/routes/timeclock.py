"""員工打卡 (/timeclock，在門市電腦或平板以帳號密碼打卡) 與後台出勤 / 班表 / 工時統計"""
from datetime import date, datetime, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user
from openpyxl import Workbook

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db, ip_key, limiter
from app.models.staff import TimeClock, WorkSchedule
from app.models.tenant import StaffAccount
from app.services import auth_throttle
from app.services.audit import record_staff_audit
from app.services.settings_service import TIME_RE
from app.utils import day_bounds, get_own, parse_date_range, role_required, safe_int, send_xlsx

timeclock_bp = Blueprint('timeclock', __name__)


def _open_record(staff_id):
    return TimeClock.query.filter_by(staff_account_id=staff_id, clock_out=None).order_by(TimeClock.id.desc()).first()


@timeclock_bp.route('/timeclock', methods=['GET', 'POST'], endpoint='punch')
@limiter.limit('20/minute', methods=['POST'], key_func=ip_key)
def punch():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        keys = auth_throttle.staff_keys(username, request.remote_addr or '-')
        if auth_throttle.staff_lock_remaining(keys):
            raise AppError('密碼錯誤次數過多，請稍後再試', 429)
        staff = StaffAccount.query.filter_by(username=username).first()
        if staff is None:
            auth_throttle.spend_hash_time(password)
        if staff is None or staff.tenant_id != g.tenant_id or not staff.can_login or not staff.check_password(password):
            auth_throttle.record_staff_failure(keys)
            db.session.commit()
            raise AppError('帳號或密碼錯誤', 401)
        if not staff.totp_enabled:   # 兩步驟驗證的帳號：打卡只驗密碼，不可藉此把驗證碼的錯誤次數歸零
            auth_throttle.clear_keys(keys['device'], keys['account'])
        open_rec = _open_record(staff.id)
        now = datetime.now()
        if request.form.get('action') == 'out':
            if open_rec is None:
                raise AppError(f'{staff.username} 目前沒有上班打卡紀錄')
            open_rec.clock_out = now
            msg = f'✅ {staff.username} 下班打卡 {now:%H:%M}，本次工時 {open_rec.hours:.2f} 小時'
        else:
            if open_rec is not None:
                raise AppError(f'{staff.username} 已於 {open_rec.clock_in:%m-%d %H:%M} 上班打卡，請先下班打卡')
            db.session.add(TimeClock(tenant_id=g.tenant_id, staff_account_id=staff.id, clock_in=now))
            msg = f'✅ {staff.username} 上班打卡 {now:%H:%M}'
        db.session.commit()
        flash(msg, 'success')
        return redirect(url_for('timeclock.punch'))
    today = date.today()
    start, _ = day_bounds(today)
    records = TimeClock.query.filter(TimeClock.clock_in >= start).order_by(TimeClock.clock_in.desc()).all()
    schedules = WorkSchedule.query.filter_by(work_date=today).order_by(WorkSchedule.start_time).all()
    # 打卡頁不需登入：帳號名稱只在已登入員工或已註冊的店內裝置上完整顯示，其他人看到遮罩 (帳號名稱可被拿來猜密碼 / 鎖帳號)
    from app.services import device_service
    from app.utils import mask_name
    reveal = current_user.is_authenticated or device_service.current() is not None
    return render_template('timeclock.html', records=records, schedules=schedules, tenant=g.tenant,
                           show_name=(lambda name: name) if reveal else mask_name)


def _range():
    """預設本週 (週一起 7 天)"""
    return parse_date_range(week=True)


def _summary(records):
    out = {}
    for r in records:
        row = out.setdefault(r.staff_account_id, {'name': r.staff.username if r.staff else r.staff_account_id,
                                                   'hours': 0.0, 'days': set(), 'open': 0})
        row['hours'] += r.hours
        row['days'].add(r.clock_in.date())
        if r.clock_out is None:
            row['open'] += 1
    return {k: dict(v, days=len(v['days']), hours=round(v['hours'], 2)) for k, v in out.items()}


@timeclock_bp.route('/admin/timeclock', methods=['GET', 'POST'], endpoint='admin')
@role_required(*MANAGER_ROLES)
def admin():
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'edit_record':
            rec = get_own(TimeClock, f.get('record_id'), '紀錄')
            try:
                rec.clock_in = datetime.fromisoformat(f.get('clock_in'))
                rec.clock_out = datetime.fromisoformat(f.get('clock_out')) if f.get('clock_out') else None
            except (TypeError, ValueError):
                raise AppError('時間格式不正確')
            if rec.clock_out and rec.clock_out < rec.clock_in:
                raise AppError('下班時間不可早於上班時間')
            rec.note = (f.get('note') or '').strip()[:200]
            rec.edited_by = current_user.username
            record_staff_audit('TIMECLOCK_EDIT', details=f'修改出勤紀錄 #{rec.id} ({rec.staff.username if rec.staff else ""})')
        elif action == 'add_schedule':
            staff = get_own(StaffAccount, f.get('staff_id'), '員工')
            try:
                day = date.fromisoformat(f.get('work_date') or '')
            except ValueError:
                raise AppError('日期格式不正確')
            st, en = (f.get('start_time') or '').strip(), (f.get('end_time') or '').strip()
            if not (TIME_RE.match(st) and TIME_RE.match(en)):
                raise AppError('班別時間格式應為 HH:MM')
            repeat = min(6, max(0, safe_int(f.get('repeat_days'), 0)))
            for i in range(repeat + 1):
                db.session.add(WorkSchedule(tenant_id=g.tenant_id, staff_account_id=staff.id, work_date=day + timedelta(days=i),
                                            start_time=st, end_time=en, note=(f.get('note') or '').strip()[:100]))
        elif action == 'delete_schedule':
            ws = get_own(WorkSchedule, f.get('schedule_id'), '班表')
            db.session.delete(ws)
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        flash('已更新', 'success')
        return redirect(url_for('timeclock.admin', start=request.args.get('start'), end=request.args.get('end')))
    start, end = _range()
    t0, t1 = day_bounds(start, end)
    records = TimeClock.query.filter(TimeClock.clock_in >= t0, TimeClock.clock_in < t1).order_by(TimeClock.clock_in).all()
    schedules = WorkSchedule.query.filter(WorkSchedule.work_date >= start, WorkSchedule.work_date <= end) \
        .order_by(WorkSchedule.work_date, WorkSchedule.start_time).all()
    staff = StaffAccount.query.filter_by(tenant_id=g.tenant_id, deleted_at=None, is_active=True).order_by(StaffAccount.username).all()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    grid = {}
    for s in schedules:
        grid.setdefault((s.staff_account_id, s.work_date), []).append(s)
    return render_template('admin_timeclock.html', records=records, schedules=schedules, staff=staff, start=start, end=end,
                           days=days, grid=grid, summary=_summary(records))


@timeclock_bp.route('/admin/timeclock/export', endpoint='export')
@role_required(*MANAGER_ROLES)
def export():
    start, end = _range()
    t0, t1 = day_bounds(start, end)
    records = TimeClock.query.filter(TimeClock.clock_in >= t0, TimeClock.clock_in < t1).order_by(TimeClock.clock_in).all()
    wb = Workbook()
    ws = wb.active
    ws.title = '出勤紀錄'
    ws.append(['員工', '上班', '下班', '工時', '備註', '修改者'])
    for r in records:
        ws.append([r.staff.username if r.staff else '', r.clock_in.strftime('%Y-%m-%d %H:%M'),
                   r.clock_out.strftime('%Y-%m-%d %H:%M') if r.clock_out else '', round(r.hours, 2), r.note, r.edited_by])
    s2 = wb.create_sheet('工時統計')
    s2.append(['員工', '出勤天數', '總工時', '未下班打卡'])
    for v in _summary(records).values():
        s2.append([v['name'], v['days'], v['hours'], v['open']])
    return send_xlsx(wb, f'出勤_{start}_{end}.xlsx')   # 備註為員工輸入的文字 (send_xlsx 會防公式注入)


@timeclock_bp.route('/admin/availability', methods=['GET', 'POST'], endpoint='availability')
@role_required(*MANAGER_ROLES)
def availability():
    """員工可排時段與每週工時上限 (自動排班的輸入)"""
    from app.services import scheduling_service
    if request.method == 'POST':
        f = request.form
        if f.get('action') != 'save':
            raise AppError('不支援的操作')
        staff = get_own(StaffAccount, f.get('staff_id'), '員工')
        rows = []
        for weekday, st, en in zip(f.getlist('weekday'), f.getlist('start_time'), f.getlist('end_time')):
            if not weekday:
                continue
            if not (TIME_RE.match(st or '') and TIME_RE.match(en or '')):
                raise AppError('可排時段的時間格式應為 HH:MM')
            rows.append((safe_int(weekday, 0), st, en))
        n = scheduling_service.set_availability(g.tenant_id, staff, rows)
        staff.max_hours_week = min(84, max(0, safe_int(f.get('max_hours_week'), 0)))
        record_staff_audit('AVAILABILITY_CHANGE', details=f'更新 {staff.username} 的可排時段 ({n} 段，每週上限 {staff.max_hours_week or "預設"})')
        db.session.commit()
        flash(f'已更新 {staff.username} 的可排時段', 'success')
        return redirect(url_for('timeclock.availability'))
    staff = StaffAccount.query.filter_by(tenant_id=g.tenant_id, deleted_at=None, is_active=True).order_by(StaffAccount.username).all()
    from app.models.staff import StaffAvailability
    avail = {}
    for row in StaffAvailability.query.order_by(StaffAvailability.weekday, StaffAvailability.start_time).all():
        avail.setdefault(row.staff_account_id, []).append(row)
    return render_template('admin_availability.html', staff=staff, avail=avail, rules=scheduling_service.config(g.tenant_id))


@timeclock_bp.route('/admin/staffing/auto', methods=['GET', 'POST'], endpoint='auto_schedule')
@role_required(*MANAGER_ROLES)
def auto_schedule():
    """一鍵生成一週班表 (預覽 → 套用)，說明見 app/services/scheduling_service.py"""
    from app.services import scheduling_service
    raw = request.values.get('week') or ''
    try:
        week = date.fromisoformat(raw) if raw else date.today() + timedelta(days=7 - date.today().weekday())
    except ValueError:
        raise AppError('日期格式不正確')
    if request.method == 'POST':
        f = request.form
        assignments = []
        for i in range(safe_int(f.get('count'), 0)):
            try:
                d = date.fromisoformat(f.get(f'date_{i}') or '')
            except ValueError:
                continue
            st, en = (f.get(f'start_{i}') or '').strip(), (f.get(f'end_{i}') or '').strip()
            sid = safe_int(f.get(f'staff_{i}'), 0)
            if sid and TIME_RE.match(st) and TIME_RE.match(en):
                assignments.append((d, sid, st, en))
        if not assignments:
            raise AppError('沒有可套用的班別')
        n = scheduling_service.apply(g.tenant_id, assignments)
        record_staff_audit('SCHEDULE_AUTO', details=f'自動排班套用 {week} 起一週：新增 {n} 筆班表')
        db.session.commit()
        flash(f'已加入 {n} 筆班表', 'success')
        return redirect(url_for('timeclock.admin', start=scheduling_service.week_start_of(week).isoformat(),
                                end=(scheduling_service.week_start_of(week) + timedelta(days=6)).isoformat()))
    return render_template('admin_auto_schedule.html', g_plan=scheduling_service.generate(g.tenant_id, week))


@timeclock_bp.route('/admin/staffing', methods=['GET', 'POST'], endpoint='staffing')
@role_required(*MANAGER_ROLES)
def staffing():
    """排班建議 (說明見 app/services/staffing_service.py)"""
    from app.services import staffing_service
    try:
        target = date.fromisoformat(request.values.get('date') or '') if request.values.get('date') \
            else date.today() + timedelta(days=1)
    except ValueError:
        raise AppError('日期格式不正確')
    if request.method == 'POST':
        f = request.form
        assignments = []
        for i in range(safe_int(f.get('count'), 0)):
            staff_id = safe_int(f.get(f'staff_{i}'), 0)
            st, en = (f.get(f'start_{i}') or '').strip(), (f.get(f'end_{i}') or '').strip()
            if not staff_id:
                continue
            if not (TIME_RE.match(st) and TIME_RE.match(en)):
                raise AppError('班別時間格式應為 HH:MM')
            assignments.append((staff_id, st, en))
        if not assignments:
            raise AppError('請為建議的班別選擇員工')
        n = staffing_service.apply_shifts(g.tenant_id, target, assignments)
        record_staff_audit('SCHEDULE_SUGGEST', details=f'依排班建議新增 {target} 班表 {n} 筆')
        db.session.commit()
        flash(f'已加入 {n} 筆班表', 'success')
        return redirect(url_for('timeclock.staffing', date=target.isoformat()))
    staff = StaffAccount.query.filter_by(tenant_id=g.tenant_id, deleted_at=None, is_active=True) \
        .order_by(StaffAccount.username).all()
    return render_template('admin_staffing.html', p=staffing_service.plan(g.tenant_id, target), staff=staff)
