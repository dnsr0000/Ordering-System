"""
預估等候時間 (系統設定 → 廚房看板)：
- 一張訂單的工作量 = 品項中最長的製作時間 (餐點未設定時用預設值) + 其餘每份 1 分鐘 (同時備餐，只多裝盤時間)。
- 等候時間 = 基本等候 + 本單工作量 + 排隊中訂單剩餘工作量 ÷ 廚房同時製作的訂單數；
  排隊中 = 今天「製作中」且不是預約未來時段的訂單，剩餘工作量以結帳時存的 estimated_ready_at 推算 (越接近完成越少)。
- 結帳時把預計完成時間存進 order.estimated_ready_at (預約單不存)；點餐頁 (購物車)、收據、我的訂單、出餐進度頁顯示。
純估算，不影響出餐順序；數字一律無條件進位到整分鐘並限制在 1~180 分鐘。
"""
from typing import Any
import math
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import or_

from app.models.enums import OrderStatus
from app.models.menu import MenuItem
from app.models.order import Order
from app.services import settings_service
from app.utils import safe_int

MAX_MINUTES = 180
HIDDEN_PAYMENT_STATUSES = ('pending', 'expired', 'authorizing')   # 尚未付款的線上訂單還不會進廚房


config = settings_service.reader('kds')
enabled = settings_service.flag('kds', 'eta_enabled', True)


def _prep_of(item, default_prep):
    minutes = safe_int(getattr(item, 'prep_minutes', 0), 0) if item is not None else 0
    return minutes if minutes > 0 else default_prep


def workload(pairs: Iterable[tuple[Any, int]], default_prep: int) -> int:
    """pairs = [(MenuItem, 份數)] → 這張訂單的工作量 (分鐘)"""
    longest, units = 0, 0
    for item, qty in pairs:
        qty = max(1, safe_int(qty, 1))
        longest = max(longest, _prep_of(item, default_prep))
        units += qty
    if units == 0:
        return 0
    return longest + max(0, units - 1)


def _queue_minutes(tenant_id: int, now: datetime, default_prep: int) -> float:
    """排隊中訂單的剩餘工作量合計 (有預計完成時間的以剩餘時間計，沒有的以工作量計)"""
    start = datetime.combine(now.date(), datetime.min.time())
    end = start + timedelta(days=1)
    orders = (Order.query.filter(Order.tenant_id == tenant_id, Order.status == OrderStatus.PENDING,
                                 Order.created_at >= start - timedelta(hours=6), Order.created_at < end,
                                 or_(Order.pickup_at.is_(None), Order.pickup_at <= now + timedelta(minutes=15)),
                                 Order.payment_status.notin_(HIDDEN_PAYMENT_STATUSES))
              .execution_options(skip_tenant_filter=True).all())
    if not orders:
        return 0.0
    menu = {m.id: m for m in MenuItem.query.filter(MenuItem.tenant_id == tenant_id)
            .execution_options(skip_tenant_filter=True).all()}
    total = 0.0
    for o in orders:
        if o.estimated_ready_at is not None:
            remaining = (o.estimated_ready_at - now).total_seconds() / 60
            total += max(0.0, min(float(MAX_MINUTES), remaining))
            continue
        pairs = [(menu.get(i.menu_item_id), (i.quantity or 0)) for i in o.items if (i.quantity or 0) > 0]
        total += workload(pairs, default_prep)
    return total


def estimate(tenant_id: int, pairs: Iterable[tuple[Any, int]], now: datetime | None = None) -> int:
    """
    pairs = [(MenuItem, 份數)]。回傳預估分鐘數 (int)；功能關閉時回傳 0。
    """
    cfg = config(tenant_id)
    if not cfg.get('eta_enabled', True):
        return 0
    now = now or datetime.now()
    default_prep = max(1, safe_int(cfg.get('eta_default_prep'), 5))
    parallel = max(1, safe_int(cfg.get('eta_parallel'), 3))
    base = max(0, safe_int(cfg.get('eta_base_minutes'), 3))
    own = workload(list(pairs), default_prep)
    queue = _queue_minutes(tenant_id, now, default_prep) / parallel
    minutes = int(math.ceil(base + own + queue))
    return max(1, min(MAX_MINUTES, minutes))


def pairs_from_items(items_data: list, tenant_id: int) -> list[tuple[Any, int]]:
    """前端購物車 (menu_item_id / quantity) → [(MenuItem, 份數)]；不存在的品項略過"""
    ids = {safe_int(raw.get('menu_item_id') or raw.get('id'), 0) for raw in items_data if isinstance(raw, dict)}
    ids.discard(0)
    menu = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_(ids)).all()} if ids else {}
    pairs = []
    for raw in items_data:
        if not isinstance(raw, dict):
            continue
        item = menu.get(safe_int(raw.get('menu_item_id') or raw.get('id'), 0))
        if item is not None and item.tenant_id == tenant_id:
            pairs.append((item, safe_int(raw.get('quantity'), 1)))
    return pairs


def ready_at_for_order(order: Any, now: datetime | None = None) -> datetime | None:
    """新收到的訂單 (線上訂單到分店、斷線補送)：依目前排隊狀況算出預計完成時間；預約單回傳 None"""
    if order.pickup_at is not None:
        return None
    now = now or datetime.now()
    menu_ids = {i.menu_item_id for i in order.items if i.menu_item_id}
    menu = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_(menu_ids))
            .execution_options(skip_tenant_filter=True).all()} if menu_ids else {}
    pairs = [(menu.get(i.menu_item_id), i.quantity or 1) for i in order.items]
    minutes = estimate(order.tenant_id, pairs, now)
    return now + timedelta(minutes=minutes) if minutes else None


def status_payload(tenant_id: int, pairs: list | None = None, now: datetime | None = None) -> dict:
    """給前端的預估 (分鐘數與預計時間文字)；功能關閉時 minutes = 0"""
    now = now or datetime.now()
    minutes = estimate(tenant_id, pairs or [], now)
    return {'minutes': minutes, 'ready_at': (now + timedelta(minutes=minutes)).strftime('%H:%M') if minutes else ''}

