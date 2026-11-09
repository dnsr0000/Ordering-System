"""
排班建議：以備料預測的方式 (過去幾週同一個星期幾) 估算指定日期每小時的訂單數，
換算需要的人力 = max(最少人數, ⌈訂單數 ÷ 每人每小時可處理張數⌉)，與該日班表 (work_schedule) 每小時在班人數比較，
並以「從最早缺人的時段開一個 N 小時的班」貪婪地補足，產生建議班別；店長選人後可直接加入班表。
同時估算建議人力的人事成本 (依員工時薪平均) 佔預估營業額的比例。
"""
import math
from collections import defaultdict
from datetime import date, timedelta

from app.extensions import db
from app.models.enums import OrderStatus
from app.models.menu import WEEKDAY_LABELS
from app.models.order import Order
from app.models.staff import WorkSchedule
from app.models.tenant import StaffAccount
from app.services import settings_service
from app.utils import day_bounds, hm_minutes

config = settings_service.reader('staffing')


def history_days(target, weeks, today=None):
    """
    過去 weeks 週與 target 同一個星期幾的日期 (只取今天以前的)：從今天以前最近一個同星期幾的日期往回取，
    target 在很久以後時也取得到足夠的週數 (與 forecast_service.reference_days 相同)
    """
    today = today or date.today()
    gap = (target - today).days
    first = gap // 7 + 1 if gap >= 0 else 1   # 最小的 k 使 target - 7k < today
    span = (target - date.min).days   # 很早的日期 (例如 0001-01-03) 往前減會溢位：只取得到的那幾週
    return [target - timedelta(days=7 * (first + i)) for i in range(weeks) if 7 * (first + i) <= span]


def orders_by_hour(day, tenant_id=None):
    """某一天未取消的訂單 → ({hour: 張數}, 營業額)；tenant_id=None 時依目前請求的店家自動過濾"""
    t0, t1 = day_bounds(day)
    q = db.session.query(Order.created_at, Order.total_price).filter(
        Order.created_at >= t0, Order.created_at < t1, Order.status != OrderStatus.CANCELLED)
    if tenant_id:
        q = q.filter(Order.tenant_id == tenant_id)
    counts = defaultdict(int)
    revenue = 0
    for created, total in q.all():
        counts[created.hour] += 1
        revenue += int(total or 0)
    return counts, revenue


def hourly_demand(tenant_id, target, weeks=4, today=None):
    """{hour: 平均訂單數}、平均營業額、參考的天數 (只計有營業的那幾天)"""
    counts = defaultdict(int)
    revenue, used = 0, 0
    for d in history_days(target, weeks, today):
        day_counts, day_revenue = orders_by_hour(d, tenant_id)
        if not day_counts:
            continue
        used += 1
        revenue += day_revenue
        for h, c in day_counts.items():
            counts[h] += c
    n = max(1, used)
    return {h: c / n for h, c in counts.items()}, round(revenue / n), used


def active_schedules(tenant_id):
    """在職員工 (啟用且未刪除) 的班表查詢：停用 / 刪除的員工留下的班不算在班人力"""
    return WorkSchedule.query.join(StaffAccount, StaffAccount.id == WorkSchedule.staff_account_id).filter(
        WorkSchedule.tenant_id == tenant_id, StaffAccount.is_active.is_(True), StaffAccount.deleted_at.is_(None))


def clear_future_schedules(tenant_id, staff_id, today=None, availability=False):
    """員工停用 / 刪除時：刪除今天起的班表 (availability=True 時一併刪除可排時段)；回傳刪除的班表筆數，由呼叫端 commit"""
    from app.models.staff import StaffAvailability
    today = today or date.today()
    n = WorkSchedule.query.filter(WorkSchedule.tenant_id == tenant_id, WorkSchedule.staff_account_id == staff_id,
                                  WorkSchedule.work_date >= today).delete(synchronize_session=False)
    if availability:
        StaffAvailability.query.filter_by(tenant_id=tenant_id, staff_account_id=staff_id).delete(synchronize_session=False)
    return n


def scheduled_by_hour(tenant_id, target):
    """{hour: 在班人數}、當天班表列 (只計在職員工；前一天的跨夜班 (例如 20:00~02:00) 午夜後的時段也算在這天)"""
    base = active_schedules(tenant_id)
    rows = base.filter(WorkSchedule.work_date == target).order_by(WorkSchedule.start_time).all()
    out = defaultdict(int)
    for ws in rows:
        for h in _hours_of(ws.start_time, ws.end_time):
            out[h] += 1
    if target > date.min:
        for ws in base.filter(WorkSchedule.work_date == target - timedelta(days=1)).all():
            for h in _overnight_hours(ws.start_time, ws.end_time):
                out[h] += 1
    return out, rows


def _hours_of(start, end):
    """'09:00'~'13:30' → [9, 10, 11, 12, 13] (跨午夜只算到 24 點；午夜後的部分見 _overnight_hours)"""
    s, e = hm_minutes(start), hm_minutes(end)
    if s is None or e is None:
        return []
    if e <= s:
        e = 24 * 60
    return [h for h in range(24) if h * 60 < e and (h + 1) * 60 > s]


def _overnight_hours(start, end):
    """跨夜班在隔天的時段：'20:00'~'02:00' → [0, 1]；沒有跨夜 (或剛好 24:00 下班) → []"""
    s, e = hm_minutes(start), hm_minutes(end)
    if s is None or e is None or e > s:
        return []
    return [h for h in range(24) if h * 60 < e]


def business_hours(active):
    """有訂單的小時 → 營業時段 [hour]：扣掉一天中最長的連續無訂單空檔 (視為打烊)。
    一般的店就是第一張到最後一張訂單之間；營業跨過午夜的店 (每天 0 點也有前一晚的訂單) 則是傍晚到 24 點再加上凌晨那幾小時，
    不會把凌晨到上午的打烊時段算進去。"""
    act = sorted(set(active))
    if not act:
        return []
    # 跨午夜的空檔 (最後一張之後到隔天第一張之前) 為預設；店內的空檔更長時才改用 (相同長度時沿用原本的算法)
    best_gap, open_hour = 23 - act[-1] + act[0], act[0]
    for a, b in zip(act, act[1:]):
        if b - a - 1 > best_gap:
            best_gap, open_hour = b - a - 1, b
    return [(open_hour + i) % 24 for i in range(24 - best_gap)]


def suggest_shifts(need, shift_hours):
    """need: {hour: 人數} (key 為營業時段) → 建議班別 [(start_hour, end_hour)]：
    從最早缺人的時段開 shift_hours 小時的班，直到每小時都足額。營業時段不連續 (跨午夜營業的凌晨那段) 時各段分別補，班別不排進打烊時段"""
    if not need:
        return []
    runs, cur = [], []
    for h in sorted(need):
        if cur and h != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(h)
    runs.append(cur)
    shifts = []
    for run in runs:
        hours = [h for h in run if need[h] > 0]
        if not hours:
            continue
        first, last = hours[0], hours[-1]
        cover = defaultdict(int)
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
    active = [h for h, v in demand.items() if v >= 0.5]
    need = {}
    for h in sorted(business_hours(active)):   # 營業時段 (扣掉最長的無訂單空檔) 都至少要最少人數
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
        'target': target, 'weekday': WEEKDAY_LABELS[target.weekday()], 'days_used': used, 'rows': rows,
        'shifts': [{'start': f'{s:02d}:00', 'end': f'{e:02d}:00' if e < 24 else '23:59', 'hours': e - s} for s, e in shifts],
        'need_hours': need_hours, 'have_hours': sum(have.values()), 'short_hours': sum(missing.values()),
        'over_hours': sum(max(0, have.get(h, 0) - need.get(h, 0)) for h in hours),
        'revenue': revenue, 'avg_wage': avg_wage, 'labor': labor,
        'labor_ratio': round(labor / revenue * 100, 1) if revenue else 0.0, 'schedules': schedules,
        'per': per, 'min_staff': min_staff,
    }


def apply_shifts(tenant_id, target, assignments, note='依排班建議'):
    """assignments: [(staff_id, 'HH:MM', 'HH:MM')] → 新增班表 (同一人同一天同時段已存在就略過，避免連點重複送出)；
    回傳筆數。由呼叫端 commit"""
    staff = {s.id for s in StaffAccount.query.filter_by(tenant_id=tenant_id, is_active=True, deleted_at=None).all()}
    added = 0
    for staff_id, start, end in assignments:
        if staff_id not in staff:
            continue
        if WorkSchedule.query.filter_by(tenant_id=tenant_id, staff_account_id=staff_id, work_date=target,
                                        start_time=start, end_time=end).first() is not None:
            continue
        db.session.add(WorkSchedule(tenant_id=tenant_id, staff_account_id=staff_id, work_date=target,
                                    start_time=start, end_time=end, note=note))
        added += 1
    return added

