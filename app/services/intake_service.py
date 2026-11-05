"""
接單控制 (系統設定 → 接單控制；POS 右上角「暫停接單」一鍵切換)：
- 暫停接單：點餐機、顧客手機 (桌邊掃碼 / 線上點餐) 與團購送單一律拒絕 (409 intake_paused)；
  POS 櫃檯、外送平台訂單與系統內部建立的預約單 (團體預購排程送單) 不受影響。可設定幾分鐘後自動恢復。
- 每時段訂單上限：以 window_minutes 為一個時段，同一時段內 (盡快取餐 = 下單時間；預約取餐 = 取餐時間) 的
  非 POS / 非外送平台訂單達上限時拒絕 (409 intake_full)。檢查在交易外進行，同時送出的訂單可能略為超過上限。
- 分店的暫停狀態隨同步上傳 (app/sync/node.py push → SyncNode.paused_until)，線上端的線上點餐選到該分店時一併擋下；
  線上端自己的「暫停接單」則擋下所有線上點餐。
設定屬於這台主機 (LOCAL_ONLY_SECTIONS)，總部不下發。
"""
from typing import Any
from datetime import datetime, timedelta

from sqlalchemy import func

from app.errors import AppError
from app.models.enums import OrderSource, OrderStatus
from app.models.order import Order
from app.services import settings_service
from app.utils import safe_int

DEFAULT_MESSAGE = '目前訂單較多，暫停接單中，請稍後再試或洽櫃檯。'
COUNTED_SOURCES = (OrderSource.KIOSK, OrderSource.TABLE, OrderSource.ONLINE)


config = settings_service.reader('order_intake')


def _parse_resume(raw: Any) -> datetime | None:
    raw = str(raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:16], '%Y-%m-%dT%H:%M')
    except ValueError:
        return None


def status(tenant_id: int | None = None, now: datetime | None = None) -> dict:
    """目前狀態：paused (已考慮自動恢復時間)、resume_at (ISO 分鐘)、message、window_minutes、max_orders"""
    cfg = config(tenant_id)
    now = now or datetime.now()
    resume_at = _parse_resume(cfg.get('resume_at'))
    paused = bool(cfg.get('paused')) and (resume_at is None or resume_at > now)
    return {
        'paused': paused,
        'resume_at': resume_at.strftime('%Y-%m-%dT%H:%M') if paused and resume_at else '',
        'message': (cfg.get('message') or '').strip() or DEFAULT_MESSAGE,
        'window_minutes': max(5, safe_int(cfg.get('window_minutes'), 15)),
        'max_orders': max(0, safe_int(cfg.get('max_orders_per_window'), 0)),
    }


def is_paused(tenant_id: int | None = None, now: datetime | None = None) -> bool:
    return status(tenant_id, now)['paused']


def set_paused(paused: bool, minutes: int = 0, tenant_id: int | None = None, changed_by: str = 'system',
               now: datetime | None = None) -> dict:
    """POS 切換：paused=True 時 minutes > 0 代表幾分鐘後自動恢復 (0 = 手動恢復)。由呼叫端 commit。"""
    now = now or datetime.now()
    minutes = max(0, min(24 * 60, safe_int(minutes, 0)))
    resume_at = (now + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M') if paused and minutes else ''
    settings_service.put('order_intake', {'paused': bool(paused), 'resume_at': resume_at}, tenant_id, changed_by)
    return status(tenant_id, now)


def node_pause(tenant_id: int | None = None, now: datetime | None = None) -> tuple[datetime | None, str]:
    """給同步上傳用：(暫停到什麼時候 or None, 訊息)。手動恢復的暫停以「現在 + 1 天」回報，之後每次同步會再更新"""
    st = status(tenant_id, now)
    if not st['paused']:
        return None, ''
    now = now or datetime.now()
    until = datetime.strptime(st['resume_at'], '%Y-%m-%dT%H:%M') if st['resume_at'] else now + timedelta(days=1)
    return until, st['message'][:200]


def window_bounds(at: datetime, window_minutes: int) -> tuple[datetime, datetime]:
    minute = (at.hour * 60 + at.minute) // window_minutes * window_minutes
    start = datetime.combine(at.date(), datetime.min.time()) + timedelta(minutes=minute)
    return start, start + timedelta(minutes=window_minutes)


def window_count(tenant_id: int, at: datetime, window_minutes: int, node_uuid: str | None = None,
                 scheduled: bool = False) -> int:
    """同一時段內已接的訂單數 (不含已取消、POS、外送平台)"""
    start, end = window_bounds(at, window_minutes)
    q = (Order.query.filter(Order.tenant_id == tenant_id, Order.status != OrderStatus.CANCELLED,
                            Order.order_source.in_([s.value for s in COUNTED_SOURCES]))
         .execution_options(skip_tenant_filter=True))
    if scheduled:
        q = q.filter(Order.pickup_at >= start, Order.pickup_at < end)
    else:
        q = q.filter(Order.pickup_at.is_(None), Order.created_at >= start, Order.created_at < end)
    if node_uuid:
        q = q.filter(Order.node_uuid == node_uuid)
    return q.with_entities(func.count(Order.id)).scalar() or 0


def customer_notice(tenant_id: int | None = None, node: Any = None, now: datetime | None = None) -> dict:
    """點餐頁橫幅：{'paused': bool, 'message': str}；線上端另看顧客選的取餐分店"""
    st = status(tenant_id, now)
    if st['paused']:
        return {'paused': True, 'message': st['message']}
    now = now or datetime.now()
    if node is not None and node.paused_until is not None and node.paused_until > now:
        return {'paused': True, 'message': (node.paused_message or '').strip() or DEFAULT_MESSAGE}
    return {'paused': False, 'message': ''}


def check(c: Any) -> None:
    """process_checkout._validate 呼叫：不適用的通路直接回傳；暫停或時段額滿時拋出 AppError (409)"""
    if c.is_pos or c.is_delivery or isinstance(c.pickup_at, datetime):
        return
    now = c.now or datetime.now()
    notice = customer_notice(c.tenant_id, c.stock_node if c.is_hub else None, now)
    if notice['paused']:
        raise AppError(notice['message'], 409, intake_paused=True)
    st = status(c.tenant_id, now)
    if st['max_orders'] <= 0:
        return
    scheduled = getattr(c, 'scheduled_at', None)
    at = scheduled or now
    count = window_count(c.tenant_id, at, st['window_minutes'], node_uuid=c.node_uuid if c.is_hub else None,
                         scheduled=scheduled is not None)
    if count >= st['max_orders']:
        if scheduled is not None:
            raise AppError('這個取餐時段的訂單已滿，請改選其他時段', 409, intake_full=True, refresh_slots=True)
        raise AppError(f"目前訂單較多 (每 {st['window_minutes']} 分鐘最多 {st['max_orders']} 張)，請稍後再試或洽櫃檯",
                       409, intake_full=True)

