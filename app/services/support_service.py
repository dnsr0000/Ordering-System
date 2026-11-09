"""
真人客服：顧客在 AI 客服對話窗按「轉接真人客服」→ 建立對話 (附上與 AI 的對話紀錄) → 店員在後台「客服對話」回覆。
- 顧客端以對話憑證 (token) 存取，只能看到自己的對話；店員端依店家隔離 (TenantScoped)。
- 新訊息以輪詢取得 (Kiosk.poll；顧客頁不用 SSE)：GET 帶 after=<最後一則 id> 只回較新的訊息。
- 門市本機資料，不同步到其他主機。
"""
from datetime import datetime
from typing import Any

from sqlalchemy import case, func

from app.errors import AppError
from app.extensions import db
from app.models.support import SupportChat, SupportMessage

MAX_TEXT = 500
MAX_MESSAGES = 300            # 單一對話的訊息上限 (防止灌爆)
MAX_TRANSCRIPT = 12           # 轉接時帶過去的 AI 對話則數

CLOSED_BY_CUSTOMER = '顧客'


def _clean(text: Any) -> str:
    return str(text or '').strip()[:MAX_TEXT]


def _add(chat: Any, sender: str, text: str, staff_name: str = '') -> Any:
    msg = SupportMessage(tenant_id=chat.tenant_id, chat_id=chat.id, sender=sender, text=text, staff_name=staff_name)
    db.session.add(msg)
    return msg


def start(tenant_id: int, transcript: Any, *, user: Any = None, order: Any = None, page: str = '',
          first_message: str = '') -> Any:
    """建立轉接對話 (由呼叫端 commit)；transcript 是對話窗裡與 AI 的對話 [{role: user|bot, text}]"""
    label = user.name if user is not None else ((f'訂單 #{order.id} (待分店確認)' if order.pickup_pending
                                                  else f'取餐 #{order.display_pickup_number:03d}')
                                                 if order is not None else '訪客')
    chat = SupportChat(tenant_id=tenant_id, customer_label=label[:60], page=page[:20],
                       user_id=getattr(user, 'id', None), order_id=getattr(order, 'id', None),
                       last_customer_at=datetime.now())
    db.session.add(chat)
    db.session.flush()
    for row in (transcript if isinstance(transcript, list) else [])[-MAX_TRANSCRIPT:]:
        if not isinstance(row, dict):
            continue
        text = _clean(row.get('text'))
        if text:
            _add(chat, 'customer' if row.get('role') == 'user' else 'bot', text)
    _add(chat, 'system', '顧客要求轉接真人客服')
    if _clean(first_message):
        _add(chat, 'customer', _clean(first_message))
    return chat


def by_token(token: Any) -> Any:
    token = str(token or '')[:40]
    chat = SupportChat.query.filter_by(token=token).first() if len(token) >= 12 else None
    if chat is None:
        raise AppError('找不到這個客服對話', 404)
    return chat


def _ensure_open(chat: Any) -> None:
    if chat.status == 'closed':
        raise AppError('這個客服對話已結束', 409)
    if len(chat.messages) >= MAX_MESSAGES:
        raise AppError('這個客服對話的訊息已達上限，請重新轉接', 409)


def customer_message(chat: Any, text: Any) -> Any:
    text = _clean(text)
    if not text:
        raise AppError('請輸入訊息')
    _ensure_open(chat)
    chat.last_customer_at = datetime.now()
    chat.status = 'waiting'   # 顧客說了新的話 → 等店員回覆
    return _add(chat, 'customer', text)


def staff_reply(chat: Any, staff_name: str, text: Any) -> Any:
    text = _clean(text)
    if not text:
        raise AppError('請輸入回覆內容')
    _ensure_open(chat)
    chat.status = 'open'
    chat.last_staff_at = datetime.now()
    msg = _add(chat, 'staff', text, staff_name=staff_name[:120])
    db.session.flush()
    chat.staff_seen_id = msg.id
    return msg


def close(chat: Any, by: str) -> None:
    if chat.status == 'closed':
        return
    chat.status = 'closed'
    chat.closed_at = datetime.now()
    chat.closed_by = by[:120]
    _add(chat, 'system', '顧客已結束對話' if by == CLOSED_BY_CUSTOMER else '客服人員已結束對話')


def mark_seen(chat: Any) -> None:
    last = chat.messages[-1].id if chat.messages else 0
    if last > (chat.staff_seen_id or 0):
        chat.staff_seen_id = last


def message_dict(m: Any, for_customer: bool = False) -> dict[str, Any]:
    out = {'id': m.id, 'sender': m.sender, 'text': m.text, 'time': m.created_at.strftime('%H:%M') if m.created_at else ''}
    if not for_customer:
        out['staff_name'] = m.staff_name
    return out


def messages_after(chat: Any, after: Any, for_customer: bool = False) -> list[dict[str, Any]]:
    try:
        after_id = int(after or 0)
    except (TypeError, ValueError):
        after_id = 0
    # 顧客端不顯示系統紀錄 (「顧客要求轉接」等)，對話窗自己顯示當地語言的提示
    return [message_dict(m, for_customer) for m in chat.messages
            if m.id > after_id and not (for_customer and m.sender == 'system')]


def unread_count(chat: Any) -> int:
    return sum(1 for m in chat.messages if m.sender == 'customer' and m.id > (chat.staff_seen_id or 0))


def chat_dict(chat: Any) -> dict[str, Any]:
    last = chat.messages[-1] if chat.messages else None
    return {
        'id': chat.id, 'status': chat.status, 'status_label': chat.status_label, 'customer': chat.customer_label,
        'page': chat.page, 'order_id': chat.order_id, 'unread': unread_count(chat),
        'created_at': chat.created_at.strftime('%m/%d %H:%M') if chat.created_at else '',
        'last_text': last.text[:60] if last else '', 'last_at': last.created_at.strftime('%H:%M') if last else '',
    }


def list_chats(include_closed: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    """後台列表：等待回覆的在最前面，其次處理中，最後 (選擇時) 已結束；同狀態依最新訊息排序"""
    q = SupportChat.query
    if not include_closed:
        q = q.filter(SupportChat.status != 'closed')
    # 先依狀態排序再取筆數：較早建立但顧客又傳訊息 (waiting) 的對話不可被較新的對話擠出清單 (徽章有計入)
    rank = case((SupportChat.status == 'waiting', 0), (SupportChat.status == 'open', 1), else_=2)
    chats = q.order_by(rank, SupportChat.created_at.desc()).limit(limit).all()
    order = {'waiting': 0, 'open': 1, 'closed': 2}
    chats.sort(key=lambda c: (order.get(c.status, 3), -(c.messages[-1].id if c.messages else 0)))
    return [chat_dict(c) for c in chats]


def forget_member(user_id: int) -> int:
    """會員刪除帳號時刪除他的客服對話 (含姓名與內容)；回傳刪除的對話數 (由呼叫端 commit)"""
    chats = SupportChat.query.filter_by(user_id=user_id).execution_options(skip_tenant_filter=True).all()   # 排程清除時沒有店家情境
    for chat in chats:
        db.session.delete(chat)
    return len(chats)


def waiting_count() -> int:
    return int(db.session.query(func.count(SupportChat.id)).filter(SupportChat.status == 'waiting').scalar() or 0)
