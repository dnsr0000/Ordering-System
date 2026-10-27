"""
團體預購 (第四輪)：

1. 主揪 (或店員代建) 填寫名稱、取餐時間、截止時間 → 取得「分享連結」(/group/<token>) 與「管理連結」(只顯示一次)。
2. 參加者打開分享連結，填名字並選餐點份數 (可選規格、備註)，送出後可用同一個瀏覽器 (或個人修改連結) 修改 / 刪除。
3. 截止時自動 (或主揪按「送單」) 把所有人的餐點合併成一張預約取餐訂單 (現場付款)，備註列出每個人點的內容，
   店員可列印分裝清單。送單以條件式更新搶 open → submitting，同時按兩次 / 排程同時執行只會送出一次；
   訂單以團購的 uuid 為 client_order_id (訂單 uuid)，重試也不會重複建單。
4. 送單失敗 (售完、庫存不足、時段已過…) → 狀態「送單失敗」並通知店家，主揪調整後可再送。
金額以送單當下的價格為準 (頁面顯示的是預估)。
"""
import json
import secrets
from collections import OrderedDict
from datetime import datetime, timedelta

from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderType
from app.models.group_order import GroupOrder, GroupOrderEntry, hash_key
from app.models.menu import MenuItem
from app.services import settings_service
from app.sync import runtime as sync_runtime
from app.utils import safe_int

PHONE_DIGITS = 10
ORDER_TYPES = (OrderType.TAKE_OUT, OrderType.DINE_IN)


def config(tenant_id=None):
    return settings_service.get('group_order', tenant_id)


def ensure_enabled(tenant_id):
    if sync_runtime.is_hub():
        raise AppError('團體預購請在門市使用', 404)
    if not config(tenant_id).get('enabled'):
        raise AppError('本店目前未開放團體預購', 404)


def _parse_dt(raw, label):
    try:
        return datetime.strptime(str(raw or '').strip()[:16], '%Y-%m-%dT%H:%M')
    except ValueError:
        raise AppError(f'請選擇{label}')


def create(tenant_id, form, staff='', now=None):
    """回傳 (group, manage_key)；由呼叫端 commit"""
    cfg = config(tenant_id)
    now = now or datetime.now()
    title = str(form.get('title') or '').strip()[:60]
    name = str(form.get('organizer_name') or '').strip()[:30]
    phone = ''.join(ch for ch in str(form.get('organizer_phone') or '') if ch.isdigit())
    if not title or not name:
        raise AppError('請輸入團購名稱與主揪姓名')
    if len(phone) != PHONE_DIGITS or not phone.startswith('09'):
        raise AppError('請輸入主揪的手機號碼 (09 開頭 10 碼)')
    pickup_at = _parse_dt(form.get('pickup_at'), '取餐時間')
    if pickup_at < now + timedelta(minutes=int(cfg.get('min_lead_minutes') or 0)):
        raise AppError(f"取餐時間至少要在 {cfg.get('min_lead_minutes')} 分鐘後")
    if pickup_at > now + timedelta(days=int(cfg.get('max_days') or 14)):
        raise AppError(f"最多只能預約 {cfg.get('max_days')} 天內")
    if form.get('deadline'):
        deadline = _parse_dt(form.get('deadline'), '截止時間')
    else:
        deadline = pickup_at - timedelta(minutes=int(cfg.get('deadline_minutes') or 60))
    if deadline <= now:
        raise AppError('截止時間必須晚於現在')
    if deadline > pickup_at - timedelta(minutes=15):
        raise AppError('截止時間至少要比取餐時間早 15 分鐘 (店家備餐)')
    order_type = form.get('order_type') if form.get('order_type') in ORDER_TYPES else OrderType.TAKE_OUT
    raw_key = secrets.token_urlsafe(18)
    group = GroupOrder(tenant_id=tenant_id, token=secrets.token_urlsafe(12), manage_key_hash=hash_key(raw_key),
                       title=title, organizer_name=name, organizer_phone=phone, order_type=order_type,
                       pickup_at=pickup_at, deadline=deadline, auto_submit=form.get('auto_submit', '1') not in ('', '0'),
                       note=str(form.get('note') or '').strip()[:100], created_by=staff)
    db.session.add(group)
    return group, raw_key


def by_token(token):
    group = GroupOrder.query.filter_by(token=str(token or '')[:32]).first()
    if group is None:
        raise AppError('找不到這個團購 (連結可能有誤)', 404)
    return group


def check_manage_key(group, raw):
    return bool(raw) and secrets.compare_digest(hash_key(raw), group.manage_key_hash)


def menu_items(tenant_id, now=None):
    """可團購的餐點 (有售價、未售完、供應中)"""
    items = MenuItem.query.filter(MenuItem.tenant_id == tenant_id, MenuItem.price > 0) \
        .order_by(MenuItem.category, MenuItem.id).all()
    return [m for m in items if m.is_available and m.service_status(now)[0]]


def _channel(group):
    return 'takeout' if group.order_type == OrderType.TAKE_OUT else 'dine_in'


def parse_lines(tenant_id, group, form):
    """表單 qty_<id> / size_<id> / note_<id> → [{menu_item_id, quantity, size, customization}]"""
    cfg = config(tenant_id)
    allowed = {m.id: m for m in menu_items(tenant_id)}
    lines, total = [], 0
    for key in form.keys():
        if not key.startswith('qty_'):
            continue
        item_id = safe_int(key[4:], 0)
        qty = safe_int(form.get(key), 0)
        if qty <= 0:
            continue
        item = allowed.get(item_id)
        if item is None:
            raise AppError('選擇的餐點目前無法供應，請重新整理')
        if qty > 20:
            raise AppError(f'「{item.name}」一次最多 20 份')
        size = str(form.get(f'size_{item_id}') or '').strip()[:20]
        if size and size not in {n for n, _d in item.sizes}:
            raise AppError(f'「{item.name}」的規格不正確')
        lines.append({'menu_item_id': item_id, 'quantity': qty, 'size': size,
                      'customization': str(form.get(f'note_{item_id}') or '').strip()[:50]})
        total += qty
    if not lines:
        raise AppError('請至少選一份餐點')
    if total > int(cfg.get('max_qty_per_person') or 10):
        raise AppError(f"每人最多 {cfg.get('max_qty_per_person')} 份")
    return lines


def save_entry(group, name, lines, note='', entry=None, now=None):
    """新增或修改參加者的點餐；回傳 (entry, raw_key 或 None)。由呼叫端 commit"""
    now = now or datetime.now()
    if group.status != 'open' or now >= group.deadline:
        raise AppError('這個團購已截止收單', 409)
    lock_open(group)
    name = str(name or '').strip()[:30]
    if not name:
        raise AppError('請輸入您的名字')
    raw = None
    if entry is None:
        limit = int(config(group.tenant_id).get('max_people') or 50)
        if len(group.entries) >= limit:
            raise AppError(f'這個團購已滿 {limit} 人', 409)
        raw = secrets.token_urlsafe(12)
        entry = GroupOrderEntry(tenant_id=group.tenant_id, group=group, entry_key_hash=hash_key(raw))
        db.session.add(entry)
    entry.name, entry.items, entry.note = name, json.dumps(lines, ensure_ascii=False), str(note or '').strip()[:100]
    return entry, raw


def entry_by_key(group, raw):
    if not raw:
        return None
    h = hash_key(raw)
    return next((e for e in group.entries if secrets.compare_digest(e.entry_key_hash, h)), None)


def summary(group):
    """{'people': [{entry, lines:[{name,size,qty,price,note}], subtotal}], 'items': [{name,size,qty,amount}], 'total', 'count'}"""
    ids = {ln.get('menu_item_id') for e in group.entries for ln in e.lines}
    menu = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_(ids or [0])).all()}
    channel = _channel(group)
    people, items = [], OrderedDict()
    for e in group.entries:
        rows, subtotal = [], 0
        for ln in e.lines:
            item = menu.get(ln.get('menu_item_id'))
            name = item.name if item else '(已刪除的餐點)'
            delta = dict(item.sizes).get(ln.get('size'), 0) if item and ln.get('size') else 0
            price = (item.price_for(channel) + delta) if item else 0
            qty = int(ln.get('quantity') or 0)
            rows.append({'name': name, 'size': ln.get('size') or '', 'qty': qty, 'price': price,
                         'note': ln.get('customization') or ''})
            subtotal += price * qty
            agg = items.setdefault((name, ln.get('size') or ''), {'name': name, 'size': ln.get('size') or '', 'qty': 0,
                                                                  'amount': 0})
            agg['qty'] += qty
            agg['amount'] += price * qty
        people.append({'entry': e, 'lines': rows, 'subtotal': subtotal})
    return {'people': people, 'items': list(items.values()), 'total': sum(p['subtotal'] for p in people),
            'count': sum(i['qty'] for i in items.values())}


def _order_note(group):
    parts = []
    for e in group.entries:
        desc = '、'.join(f"{ln_name}×{ln['quantity']}" for ln, ln_name in _named(e.lines))
        parts.append(f'{e.name}:{desc}')
    note = f'團購「{group.title}」{group.organizer_name} {group.organizer_phone}｜' + '；'.join(parts)
    return note if len(note) <= 200 else note[:197] + '…'


def _named(lines):
    menu = {m.id: m.name for m in MenuItem.query.filter(
        MenuItem.id.in_({ln.get('menu_item_id') for ln in lines} or [0])).all()}
    return [(ln, menu.get(ln.get('menu_item_id'), '?')) for ln in lines]


def submit(group_id, by='', now=None):
    """
    合併送單。回傳 (group, error)；成功 error 為 ''。由這裡 commit。
    以條件式更新搶到 open/failed → submitting 才繼續 (同時送單只有一個會成功)。
    """
    from app.services import monitor_service
    now = now or datetime.now()
    claimed = db.session.execute(
        update(GroupOrder).where(GroupOrder.id == group_id, GroupOrder.status.in_(('open', 'failed')))
        .values(status='submitting').execution_options(synchronize_session=False)).rowcount
    db.session.commit()
    group = db.session.get(GroupOrder, group_id)
    db.session.refresh(group)
    if not claimed:
        return group, '這個團購已送單或已取消'
    try:
        return _submit_claimed(group_id, group, by, now)
    except Exception as exc:  # noqa: BLE001  (非預期錯誤也不可讓團購卡在「送單中」)
        db.session.rollback()
        db.session.execute(update(GroupOrder).where(GroupOrder.id == group_id, GroupOrder.status == 'submitting')
                           .values(status='failed', error=f'送單時發生錯誤：{exc}'[:200]))
        db.session.commit()
        monitor_service.alert(f'團體預購送單時發生錯誤：{exc}', key=f'group_order_error:{group_id}',
                              tenant_id=group.tenant_id, source='group_order')
        return db.session.get(GroupOrder, group_id), '送單時發生錯誤，請稍後再試'


def _submit_claimed(group_id, group, by, now):
    from app.services import monitor_service
    from app.services.order_service import process_checkout
    merged = OrderedDict()
    for e in group.entries:
        for ln in e.lines:
            key = (ln.get('menu_item_id'), ln.get('size') or '', ln.get('customization') or '')
            if key in merged:
                merged[key]['quantity'] += int(ln.get('quantity') or 0)
            else:
                merged[key] = {'menu_item_id': ln.get('menu_item_id'), 'quantity': int(ln.get('quantity') or 0),
                               'size': ln.get('size') or '', 'customization': ln.get('customization') or ''}
    error = ''
    if not merged:
        error = '沒有人點餐'
    elif group.pickup_at <= now:
        error = '取餐時間已過'
    from app.models.order import Order
    existing = Order.query.filter_by(uuid=group.uuid).first()
    if existing is not None:   # 之前已建單 (送單後狀態未及更新)：直接連結
        group.status, group.order_id, group.submitted_at, group.error = 'submitted', existing.id, now, ''
        db.session.commit()
        return group, ''
    if not error:
        try:
            order, _replayed = process_checkout(
                tenant_id=group.tenant_id, items=list(merged.values()), payment_method='Cash',
                order_type=group.order_type, need_cutlery=group.order_type == OrderType.TAKE_OUT,
                note=_order_note(group), table_input=f'團購 {group.organizer_name}'[:50],
                client_order_id=group.uuid, external_ref=f'group:{group.token}',
                cashier_username=by[:50], pickup_at=group.pickup_at, now=now)
            group = db.session.get(GroupOrder, group_id)
            group.status, group.order_id, group.submitted_at, group.error = 'submitted', order.id, now, ''
            db.session.commit()
            return group, ''
        except AppError as exc:
            db.session.rollback()
            error = exc.message
    group = db.session.get(GroupOrder, group_id)
    group.status, group.error = 'failed', error[:200]
    db.session.commit()
    monitor_service.alert(f'團體預購「{group.title}」({group.organizer_name} {group.organizer_phone}) 送單失敗：{error}',
                          key=f'group_order_fail:{group.id}', tenant_id=group.tenant_id, level='warning',
                          source='group_order')
    return group, error


def lock_open(group, statuses=('open',)):
    """
    修改參加者的點餐前呼叫：以條件式更新「鎖住」團購列並確認仍可修改 (PostgreSQL 鎖到交易結束，SQLite 寫入本來就依序)。
    送單 (open → submitting) 與修改不會同時發生：送單先搶到 → 這裡失敗；這裡先搶到 → 送單等這筆提交後才讀取點餐內容。
    """
    ok = db.session.execute(update(GroupOrder).where(GroupOrder.id == group.id, GroupOrder.status.in_(statuses))
                            .values(status=GroupOrder.status).execution_options(synchronize_session=False)).rowcount
    if not ok:
        db.session.rollback()
        raise AppError('這個團購已截止或已送單，無法修改', 409)


def cancel(group, now=None):
    if group.status in ('submitted', 'submitting', 'cancelled'):
        raise AppError('已送單或已取消的團購不能取消', 409)
    group.status = 'cancelled'


def run_due(tenant_id, now=None):
    """排程：截止且設定自動送單的團購送單；未自動送單、取餐時間已過仍未送的取消。回傳 (送單數, 取消數)"""
    now = now or datetime.now()
    # 送單中途行程被中斷 (重啟、當機) 而停在「送單中」超過 5 分鐘：改為失敗並通知 (主揪可再送；已建立的訂單會直接連結)
    stuck = db.session.execute(
        update(GroupOrder).where(GroupOrder.tenant_id == tenant_id, GroupOrder.status == 'submitting',
                                 GroupOrder.updated_at < now - timedelta(minutes=5))
        .values(status='failed', error='送單中斷，請重新送出').execution_options(synchronize_session=False)).rowcount
    if stuck:
        db.session.commit()
        from app.services import monitor_service
        monitor_service.alert(f'有 {stuck} 個團體預購送單中斷，已標示為送單失敗，請確認後重新送出',
                              key=f'group_order_stuck:{tenant_id}', tenant_id=tenant_id, source='group_order')
    due = GroupOrder.query.filter(GroupOrder.tenant_id == tenant_id, GroupOrder.status == 'open',
                                  GroupOrder.deadline <= now).execution_options(skip_tenant_filter=True).all()
    sent = cancelled = 0
    for g in due:
        if g.auto_submit:
            try:
                submit(g.id, by='system', now=now)
                sent += 1
            except Exception as exc:  # noqa: BLE001  (單一團購失敗不影響其他排程)
                db.session.rollback()
                db.session.execute(update(GroupOrder).where(GroupOrder.id == g.id, GroupOrder.status == 'submitting')
                                   .values(status='failed', error=str(exc)[:200]))
                db.session.commit()
                from app.services import monitor_service
                monitor_service.alert(f'團體預購自動送單失敗：{exc}', key=f'group_order_job:{g.id}', tenant_id=tenant_id)
        elif g.pickup_at <= now:
            g.status = 'cancelled'
            cancelled += 1
            db.session.commit()
    return sent, cancelled
