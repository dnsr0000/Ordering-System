"""
排班建議 (第四輪)：以備料預測的方式 (過去幾週同一個星期幾) 估算指定日期每小時的訂單數，
換算需要的人力 = max(最少人數, ⌈訂單數 ÷ 每人每小時可處理張數⌉)，與該日班表 (work_schedule) 每小時在班人數比較，
並以「從最早缺人的時段開一個 N 小時的班」貪婪地補足，產生建議班別；店長選人後可直接加入班表。
同時估算建議人力的人事成本 (依員工時薪平均) 佔預估營業額的比例。
"""
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from app.extensions import db
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.staff import WorkSchedule
from app.models.tenant import StaffAccount
from app.services import settings_service

WEEKDAYS = '一二三四五六日'


def config(tenant_id=None):
    return settings_service.get('staffing', tenant_id)


def _history_days(target, weeks, today):
    return [target - timedelta(days=7 * k) for k in range(1, weeks + 1) if target - timedelta(days=7 * k) < today]


def hourly_demand(tenant_id, target, weeks=4, today=None):
    """{hour: 平均訂單數}、平均營業額、參考的天數 (只計有營業的那幾天)"""
    today = today or date.today()
    counts = defaultdict(int)
    revenue, used = 0, 0
    for d in _history_days(target, weeks, today):
        t0 = datetime.combine(d, datetime.min.time())
        rows = db.session.query(Order.created_at, Order.total_price).filter(
            Order.tenant_id == tenant_id, Order.created_at >= t0, Order.created_at < t0 + timedelta(days=1),
            Order.status != OrderStatus.CANCELLED).all()
        if not rows:
            continue
        used += 1
        for created, total in rows:
            counts[created.hour] += 1
            revenue += int(total or 0)
    n = max(1, used)
    return {h: c / n for h, c in counts.items()}, round(revenue / n), used


def scheduled_by_hour(tenant_id, target):
    """{hour: 在班人數}、當天班表列"""
    rows = WorkSchedule.query.filter(WorkSchedule.tenant_id == tenant_id, WorkSchedule.work_date == target) \
        .order_by(WorkSchedule.start_time).all()
    out = defaultdict(int)
    for ws in rows:
        for h in _hours_of(ws.start_time, ws.end_time):
            out[h] += 1
    return out, rows


def _hours_of(start, end):
    """'09:00'~'13:30' → [9, 10, 11, 12, 13] (跨午夜只算到 24 點)"""
    try:
        sh, sm = (int(x) for x in start.split(':'))
        eh, em = (int(x) for x in end.split(':'))
    except (ValueError, AttributeError):
        return []
    s, e = sh * 60 + sm, eh * 60 + em
    if e <= s:
        e = 24 * 60
    return [h for h in range(24) if h * 60 < e and (h + 1) * 60 > s]


def suggest_shifts(need, shift_hours):
    """need: {hour: 人數} → 建議班別 [(start_hour, end_hour)]：從最早缺人的時段開 shift_hours 小時的班，直到每小時都足額"""
    if not need:
        return []
    hours = sorted(h for h, n in need.items() if n > 0)
    if not hours:
        return []
    first, last = hours[0], hours[-1]
    cover = defaultdict(int)
    shifts = []
    for h in range(first, last + 1):
        while cover[h] < need.get(h, 0):
            start = h
            end = min(24, max(start + shift_hours, start + 1))
            if end > last + 1:   # 最後一段：班別往前挪，不排到打烊後
                start, end = max(first, last + 1 - shift_hours), last + 1
            for x in range(start, end):
                cover[x] += 1
            shifts.append((start, end))
    return shifts


def plan(tenant_id, target, today=None):
    cfg = config(tenant_id)
    per = max(1, int(cfg.get('orders_per_staff_hour') or 12))
    min_staff = max(0, int(cfg.get('min_staff') or 0))
    demand, revenue, used = hourly_demand(tenant_id, target, int(cfg.get('weeks') or 4), today)
    active = sorted(h for h, v in demand.items() if v >= 0.5)
    need = {}
    if active:
        for h in range(active[0], active[-1] + 1):   # 第一張到最後一張訂單之間都算營業時段
            need[h] = max(min_staff, math.ceil(demand.get(h, 0) / per))
    have, schedules = scheduled_by_hour(tenant_id, target)
    hours = sorted(set(need) | set(have))
    rows = [{'hour': h, 'orders': round(demand.get(h, 0), 1), 'need': need.get(h, 0), 'have': have.get(h, 0),
             'gap': have.get(h, 0) - need.get(h, 0)} for h in hours]
    missing = {h: max(0, need.get(h, 0) - have.get(h, 0)) for h in need}
    shifts = suggest_shifts(missing, max(1, int(cfg.get('shift_hours') or 4)))
    wages = [s.hourly_wage for s in StaffAccount.query.filter(
        StaffAccount.tenant_id == tenant_id, StaffAccount.is_active.is_(True), StaffAccount.deleted_at.is_(None)).all()
        if (s.hourly_wage or 0) > 0]
    avg_wage = round(sum(wages) / len(wages)) if wages else 0
    need_hours = sum(need.values())
    labor = need_hours * avg_wage
    return {
        'target': target, 'weekday': WEEKDAYS[target.weekday()], 'days_used': used, 'rows': rows,
        'shifts': [{'start': f'{s:02d}:00', 'end': f'{e:02d}:00' if e < 24 else '23:59', 'hours': e - s} for s, e in shifts],
        'need_hours': need_hours, 'have_hours': sum(have.values()), 'short_hours': sum(missing.values()),
        'over_hours': sum(max(0, have.get(h, 0) - need.get(h, 0)) for h in hours),
        'revenue': revenue, 'avg_wage': avg_wage, 'labor': labor,
        'labor_ratio': round(labor / revenue * 100, 1) if revenue else 0.0, 'schedules': schedules,
        'per': per, 'min_staff': min_staff,
    }


def apply_shifts(tenant_id, target, assignments, note='依排班建議'):
    """assignments: [(staff_id, 'HH:MM', 'HH:MM')] → 新增班表；回傳筆數。由呼叫端 commit"""
    staff = {s.id for s in StaffAccount.query.filter_by(tenant_id=tenant_id, is_active=True, deleted_at=None).all()}
    added = 0
    for staff_id, start, end in assignments:
        if staff_id not in staff:
            continue
        db.session.add(WorkSchedule(tenant_id=tenant_id, staff_account_id=staff_id, work_date=target,
                                    start_time=start, end_time=end, note=note))
        added += 1
    return added

