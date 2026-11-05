"""
線上點餐預約取餐時間 (系統設定 → 線上點餐)：依營業時段、時段間隔、最少提前分鐘數與可預約天數產生可選時段，
結帳時驗證顧客選的時間 (空白 = 盡快)。
"""
from datetime import datetime, timedelta

from app.errors import AppError
from app.models.menu import WEEKDAY_LABELS
from app.services import settings_service
from app.utils import parse_hm, parse_local_dt

MAX_SLOTS = 300


config = settings_service.reader('online_order')
enabled = settings_service.flag('online_order', 'scheduled_pickup')


def _windows(cfg, now):
    oh, om = parse_hm(cfg.get('open_time'), '10:00')
    ch, cm = parse_hm(cfg.get('close_time'), '21:00')
    for d in range(int(cfg.get('days_ahead') or 0) + 1):
        day = (now + timedelta(days=d)).date()
        start = datetime(day.year, day.month, day.day, oh, om)
        end = datetime(day.year, day.month, day.day, ch, cm)
        if end >= start:
            yield start, end


def slots(tenant_id=None, now=None):
    """可選的預約時段 [(datetime, 顯示文字)]；未開放預約時為空"""
    cfg = config(tenant_id)
    if not cfg.get('scheduled_pickup'):
        return []
    now = now or datetime.now()
    step = max(5, int(cfg.get('slot_minutes') or 15))
    earliest = now + timedelta(minutes=int(cfg.get('lead_minutes') or 0))
    out = []
    for start, end in _windows(cfg, now):
        t = start
        if earliest > t:
            steps = -(-int((earliest - start).total_seconds()) // (step * 60))   # 無條件進位到下一個時段
            t = start + timedelta(minutes=steps * step)
        while t <= end and len(out) < MAX_SLOTS:
            out.append((t, label(t, now)))
            t += timedelta(minutes=step)
    return out


def label(t, now=None):
    now = now or datetime.now()
    days = (t.date() - now.date()).days
    prefix = '今天' if days == 0 else ('明天' if days == 1 else f'{t:%m/%d}({WEEKDAY_LABELS[t.weekday()]})')
    return f'{prefix} {t:%H:%M}'


def parse(raw, tenant_id=None, now=None):
    """
    驗證顧客選的預約時間 (ISO 格式 YYYY-MM-DDTHH:MM)。空白 = 盡快 → None。
    必須是設定中的時段；顧客停留在結帳頁較久時，容許比最少提前時間晚一半 (仍不可早於現在)。
    """
    raw = str(raw or '').strip()
    if not raw:
        return None
    cfg = config(tenant_id)
    if not cfg.get('scheduled_pickup'):
        raise AppError('本店目前不開放預約取餐時間')
    t = parse_local_dt(raw, '預約取餐時間')
    now = now or datetime.now()
    step = max(5, int(cfg.get('slot_minutes') or 15))
    lead = int(cfg.get('lead_minutes') or 0)
    if t < now + timedelta(minutes=lead / 2) or t <= now:
        raise AppError('選擇的取餐時間已過或太接近，請重新選擇', 409, refresh_slots=True)
    for start, end in _windows(cfg, now):
        if start <= t <= end and int((t - start).total_seconds()) % (step * 60) == 0:
            return t
    raise AppError('請選擇營業時段內的取餐時間')
