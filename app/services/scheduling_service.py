"""
自動排班 (一鍵生成一週班表)。

輸入：員工的可排時段 (staff_availability：每週幾、起訖時間，可多段)、員工每週上限 (staff_account.max_hours_week)、
     排班參數 (每日 / 每週工時上限、最多連續工作天數、班別長度) 與既有班表。
需求：每天用 staffing_service.plan() 估算每小時需要人力 (過去同一個星期幾的訂單數)，不足的時段以 suggest_shifts 切成班別。
指派：貪婪啟發式 —— 每個班別從「當天可排、該時段在可排時段內、當天尚未排班、不超過每日 / 每週上限、
     不會形成超過上限的連續工作天」的員工中，挑本週已排工時最少 (再比班數) 的人，平均分攤。
勞基法規則明確編碼 (預設值；可在排班參數調整)：
- 每日正常工時 8 小時 (勞基法 §30)；
- 每週正常工時 40 小時 (§30)；
- 每七天至少一天例假：連續工作天數不超過 6 天 (§36，一例一休的保守版)。
排不出人的班別列為「未排」，店長可手動補。生成只是預覽，按「套用」才寫入班表。
"""
from collections import defaultdict
from datetime import date, timedelta

from app.extensions import db
from app.models.staff import StaffAvailability, WorkSchedule
from app.models.tenant import StaffAccount
from app.models.menu import WEEKDAY_LABELS
from app.services import settings_service, staffing_service
from app.utils import hm_minutes

DEFAULTS = {'max_hours_day': 8, 'max_hours_week': 40, 'max_consecutive_days': 6}


def config(tenant_id=None):
    cfg = settings_service.get('staffing', tenant_id)
    return {k: max(1, int(cfg.get(k) or v)) for k, v in DEFAULTS.items()} | {'shift_hours': max(1, int(cfg.get('shift_hours') or 4))}


# ---------------------------------------------------------------- 可排時段
_minutes = hm_minutes   # 'HH:MM' → 自 0 點起的分鐘數 (24:00 = 1440)；格式錯誤回 None


def availability_map(tenant_id):
    """{staff_id: {weekday(1~7): [(start_min, end_min)]}}"""
    out = defaultdict(lambda: defaultdict(list))
    for row in StaffAvailability.query.filter_by(tenant_id=tenant_id).all():
        s, e = _minutes(row.start_time), _minutes(row.end_time)
        if s is None or e is None:
            continue
        if e <= s:
            e = 24 * 60   # 23:59 / 跨午夜視為到當天結束
        out[row.staff_account_id][row.weekday].append((s, e))
    return out


def is_available(avail, staff_id, weekday, start_hour, end_hour):
    s, e = start_hour * 60, end_hour * 60
    return any(a <= s and b >= e for a, b in avail.get(staff_id, {}).get(weekday, []))


def set_availability(tenant_id, staff, rows):
    """rows: [(weekday, 'HH:MM', 'HH:MM')] → 覆寫該員工的可排時段；回傳筆數 (由呼叫端 commit)"""
    StaffAvailability.query.filter_by(tenant_id=tenant_id, staff_account_id=staff.id).delete(synchronize_session=False)
    n = 0
    for weekday, start, end in rows:
        if 1 <= int(weekday) <= 7 and _minutes(start) is not None and _minutes(end) is not None:
            db.session.add(StaffAvailability(tenant_id=tenant_id, staff_account_id=staff.id, weekday=int(weekday),
                                             start_time=start, end_time=end))
            n += 1
    return n


# ---------------------------------------------------------------- 生成
def week_start_of(target):
    return target - timedelta(days=target.weekday())


def _existing(tenant_id, start, end):
    rows = WorkSchedule.query.filter(WorkSchedule.tenant_id == tenant_id, WorkSchedule.work_date >= start,
                                     WorkSchedule.work_date <= end).all()
    hours = defaultdict(float)     # (staff_id, date) -> 小時
    for ws in rows:
        s, e = _minutes(ws.start_time), _minutes(ws.end_time)
        if s is None or e is None:
            continue
        if e <= s:
            e = 24 * 60
        hours[(ws.staff_account_id, ws.work_date)] += (e - s) / 60
    return hours


def generate(tenant_id, week_start, today=None):
    """產生一週 (週一起) 的排班預覽；不寫入資料庫"""
    today = today or date.today()
    week_start = week_start_of(week_start)
    cfg = config(tenant_id)
    staff = StaffAccount.query.filter(StaffAccount.tenant_id == tenant_id, StaffAccount.is_active.is_(True),
                                      StaffAccount.deleted_at.is_(None)).order_by(StaffAccount.username).all()
    avail = availability_map(tenant_id)
    eligible = [s for s in staff if s.id in avail]
    skipped = [s.username for s in staff if s.id not in avail]
    # 既有班表：本週 (計週工時) 與前 6 天 (計連續工作天)
    worked = _existing(tenant_id, week_start - timedelta(days=cfg['max_consecutive_days']), week_start + timedelta(days=6))
    week_hours = defaultdict(float)
    for (sid, d), h in worked.items():
        if week_start <= d <= week_start + timedelta(days=6):
            week_hours[sid] += h
    shift_count = defaultdict(int)
    days, unfilled = [], 0

    def consecutive_before(sid, d):
        n = 0
        while worked.get((sid, d - timedelta(days=n + 1))):
            n += 1
        return n

    for offset in range(7):
        d = week_start + timedelta(days=offset)
        weekday = d.isoweekday()
        plan = staffing_service.plan(tenant_id, d, today)
        missing = {r['hour']: max(0, r['need'] - r['have']) for r in plan['rows']}
        shifts = staffing_service.suggest_shifts(missing, cfg['shift_hours'])
        day_rows = []
        for s_h, e_h in shifts:
            length = e_h - s_h
            limit_day = cfg['max_hours_day']
            candidates = []
            for st in eligible:
                cap_week = min(cfg['max_hours_week'], st.max_hours_week or cfg['max_hours_week'])
                if worked.get((st.id, d)):
                    continue   # 當天已有班 (既有或本次已排)
                if length > limit_day or week_hours[st.id] + length > cap_week:
                    continue
                if consecutive_before(st.id, d) >= cfg['max_consecutive_days']:
                    continue
                if not is_available(avail, st.id, weekday, s_h, e_h):
                    continue
                candidates.append(st)
            candidates.sort(key=lambda st: (week_hours[st.id], shift_count[st.id], st.username))
            row = {'date': d, 'start': f'{s_h:02d}:00', 'end': f'{e_h:02d}:00' if e_h < 24 else '23:59', 'hours': length,
                   'staff_id': None, 'username': ''}
            if candidates:
                pick = candidates[0]
                row.update(staff_id=pick.id, username=pick.username)
                worked[(pick.id, d)] = float(length)
                week_hours[pick.id] += length
                shift_count[pick.id] += 1
            else:
                unfilled += 1
            day_rows.append(row)
        days.append({'date': d, 'weekday': WEEKDAY_LABELS[d.weekday()], 'shifts': day_rows, 'need_hours': plan['need_hours'],
                     'have_hours': plan['have_hours'], 'days_used': plan['days_used']})
    summary = [{'staff_id': st.id, 'username': st.username, 'hours': round(week_hours[st.id], 1),
                'cap': min(cfg['max_hours_week'], st.max_hours_week or cfg['max_hours_week']), 'shifts': shift_count[st.id]}
               for st in eligible]
    return {'week_start': week_start, 'week_end': week_start + timedelta(days=6), 'days': days, 'unfilled': unfilled,
            'assigned': sum(1 for d in days for r in d['shifts'] if r['staff_id']), 'summary': summary,
            'skipped': skipped, 'rules': cfg}


def apply(tenant_id, assignments, note='自動排班'):
    """assignments: [(date, staff_id, 'HH:MM', 'HH:MM')] → 新增班表 (同一人同一天同時段已存在就略過)；回傳筆數"""
    staff_ids = {s.id for s in StaffAccount.query.filter_by(tenant_id=tenant_id, is_active=True, deleted_at=None).all()}
    added = 0
    for d, staff_id, start, end in assignments:
        if staff_id not in staff_ids:
            continue
        dup = WorkSchedule.query.filter_by(tenant_id=tenant_id, staff_account_id=staff_id, work_date=d,
                                          start_time=start, end_time=end).first()
        if dup is not None:
            continue
        db.session.add(WorkSchedule(tenant_id=tenant_id, staff_account_id=staff_id, work_date=d, start_time=start,
                                    end_time=end, note=note))
        added += 1
    return added
