"""
線上點餐預約取餐時間 (系統設定 → 線上點餐)：依營業時段、時段間隔、最少提前分鐘數與可預約天數產生可選時段，
結帳時驗證顧客選的時間 (空白 = 盡快)。
"""
from datetime import datetime, timedelta

from app.errors import AppError
from app.models.menu import WEEKDAY_LABELS
from app.services import settings_service
from app.utils import hm_minutes, parse_local_dt

# 只是防呆上限：合法設定的最大量 (可預約 7 天 + 跨午夜的前一天 = 9 段，5 分鐘一格每段最多 289 格) 也不會超過，
# 不會把店家設定的後面幾天截掉
MAX_SLOTS = 3000

# 時段文字依顧客介面語言 (英 / 日文顧客才分得出今天、明天)
DAY_WORDS = {'zh': ('今天', '明天'), 'en': ('Today', 'Tomorrow'), 'ja': ('今日', '明日')}
WEEKDAYS = {'zh': WEEKDAY_LABELS, 'en': ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'), 'ja': '月火水木金土日'}


config = settings_service.reader('online_order')
enabled = settings_service.flag('online_order', 'scheduled_pickup')


def _windows(cfg, now):
    open_min, close_min = hm_minutes(cfg.get('open_time'), '10:00'), hm_minutes(cfg.get('close_time'), '21:00')
    # 結束早於開始 = 跨午夜 (例如 17:00 ~ 01:00；最晚 00:00 = 營業到午夜)：結束時間算隔天
    overnight = close_min < open_min
    # 跨午夜時，今天凌晨的時段屬於「昨天」開始的營業時段
    for d in range(-1 if overnight else 0, int(cfg.get('days_ahead') or 0) + 1):
        day = datetime.combine((now + timedelta(days=d)).date(), datetime.min.time())
        start = day + timedelta(minutes=open_min)
        end = day + timedelta(days=1 if overnight else 0, minutes=close_min)
        yield start, end


def _language():
    """顧客頁的介面語言；沒有請求情境 (排程、測試) 時為中文"""
    from flask import has_request_context
    if not has_request_context():
        return 'zh'
    from app.services import i18n_service
    return i18n_service.effective_language()


def slots(tenant_id=None, now=None, lang=None):
    """可選的預約時段 [(datetime, 顯示文字)]；未開放預約時為空。顯示文字依 lang (預設為目前顧客頁語言)"""
    cfg = config(tenant_id)
    if not cfg.get('scheduled_pickup'):
        return []
    lang = lang or _language()
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
            out.append((t, label(t, now, lang)))
            t += timedelta(minutes=step)
    return out


def label(t, now=None, lang='zh'):
    now = now or datetime.now()
    lang = lang if lang in DAY_WORDS else 'zh'
    days = (t.date() - now.date()).days
    if days in (0, 1):
        prefix = DAY_WORDS[lang][days]
    elif lang == 'en':
        prefix = f'{WEEKDAYS[lang][t.weekday()]} {t:%m/%d}'
    else:
        prefix = f'{t:%m/%d}({WEEKDAYS[lang][t.weekday()]})'
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
