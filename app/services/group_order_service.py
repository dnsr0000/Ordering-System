"""
團體預購：

1. 主揪 (或店員代建) 填寫名稱、取餐時間、截止時間 → 取得「分享連結」(/group/<token>) 與「管理連結」(只顯示一次)。
2. 參加者打開分享連結，填名字並選餐點份數 (可選規格、備註)，送出後可用同一個瀏覽器 (或個人修改連結) 修改 / 刪除。
3. 截止時自動 (或主揪按「送單」) 把所有人的餐點合併成一張預約取餐訂單 (現場付款)，備註列出每個人點的內容，
   店員可列印分裝清單。送單以條件式更新搶 open → submitting，同時按兩次 / 排程同時執行只會送出一次；
   訂單以團購的 uuid 為 client_order_id (去重識別)，重試也不會重複建單。
4. 送單失敗 (售完、庫存不足、時段已過…) → 通知店家；截止前失敗恢復收單 (參加者可調整)，截止後為「送單失敗」，主揪調整後可再送。
金額以送單當下的價格為準 (頁面顯示的是預估)。
"""
import json
import logging
import secrets
from collections import OrderedDict
from datetime import datetime, timedelta

from flask import current_app, g, has_request_context
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderType
from app.models.group_order import GroupOrder, GroupOrderEntry, hash_key
from app.models.menu import MenuItem, ModifierOption
from app.services import settings_service
from app.sync import runtime as sync_runtime
from app.utils import parse_local_dt, safe_int

logger = logging.getLogger(__name__)

PHONE_DIGITS = 10
ORDER_TYPES = (OrderType.TAKE_OUT, OrderType.DINE_IN)
# 非預期錯誤 (資料庫、系統設定…) 只給主揪看這段通用文字；細節寫進 log 與監控通知 (管理頁只靠管理連結即可開啟)
SYSTEM_ERROR = '系統暫時無法送單，已通知店家，請稍後再試'


config = settings_service.reader('group_order')
enabled = settings_service.flag('group_order')


def ensure_enabled(tenant_id):
    if sync_runtime.is_hub():
        raise AppError('團體預購請在門市使用', 404)
    if not config(tenant_id).get('enabled'):
        raise AppError('本店目前未開放團體預購', 404)


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
    pickup_at = parse_local_dt(form.get('pickup_at'), '取餐時間')
    if pickup_at < now + timedelta(minutes=int(cfg.get('min_lead_minutes') or 0)):
        raise AppError(f"取餐時間至少要在 {cfg.get('min_lead_minutes')} 分鐘後")
    if pickup_at > now + timedelta(days=int(cfg.get('max_days') or 14)):
        raise AppError(f"最多只能預約 {cfg.get('max_days')} 天內")
    if form.get('deadline'):
        deadline = parse_local_dt(form.get('deadline'), '截止時間')
    else:
        default_minutes = int(cfg.get('deadline_minutes') or 60)
        deadline = pickup_at - timedelta(minutes=default_minutes)
        if deadline <= now:
            # 截止留白、但依設定預設的截止時間已過 (取餐時間較近)：改在現在之後最近的 15 分鐘整點截止，
            # 仍須早於取餐 15 分鐘；來不及時說明原因，不顯示指向沒填欄位的「截止時間必須晚於現在」
            base = now.replace(second=0, microsecond=0)
            deadline = base + timedelta(minutes=15 - base.minute % 15)
            if deadline > pickup_at - timedelta(minutes=15):
                raise AppError(f'依店家設定，截止時間預設為取餐前 {default_minutes} 分鐘，目前已來不及；'
                               '請把取餐時間往後調，或手動填寫截止時間')
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
    """
    可團購的餐點 (有售價、未售完、供應中)。now：判斷供應時段 / 星期 / 販售期間的時間，
    團購應傳入取餐時間 (group.pickup_at)，上午揪中午供應的便當才點得到；None 為現在。
    """
    items = MenuItem.query.filter(MenuItem.tenant_id == tenant_id, MenuItem.price > 0) \
        .order_by(MenuItem.category, MenuItem.id).all()
    return [m for m in items if m.is_available and m.service_status(now)[0]]


def _channel(group):
    return 'takeout' if group.order_type == OrderType.TAKE_OUT else 'dine_in'


def parse_lines(tenant_id, group, form):
    """表單 qty_<id> / size_<id> / note_<id> → [{menu_item_id, quantity, size, customization}]"""
    cfg = config(tenant_id)
    allowed = {m.id: m for m in menu_items(tenant_id, group.pickup_at)}   # 供應時段以取餐時間判斷
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


def _order_note(group, extra=''):
    """訂單備註：主揪資料、extra (結帳時會被移除的參加者備註，放在前面避免被截斷)、每個人點的內容"""
    parts = []
    for e in group.entries:
        desc = '、'.join(f"{ln_name}×{ln['quantity']}" for ln, ln_name in _named(e.lines))
        parts.append(f'{e.name}:{desc}')
    note = f'團購「{group.title}」{group.organizer_name} {group.organizer_phone}｜' + (f'{extra}｜' if extra else '') \
        + '；'.join(parts)
    return note if len(note) <= 200 else note[:197] + '…'


def _stripped_notes(tenant_id, lines):
    """
    參加者的品項備註中含加料名稱的段落 (例如「不要加荷包蛋」)：結帳時會被當成加料文字從明細移除 (order_pricing._clean_customization)，
    改寫進訂單備註，廚房單 / 看板才看得到。回傳「品名:段落」組成的文字 (沒有則為空字串)。
    """
    names = {m.name for m in ModifierOption.query.filter_by(tenant_id=tenant_id, is_active=True).all() if m.name}
    if not names:
        return ''
    out = []
    for ln, ln_name in _named(lines):
        lost = [p.strip() for p in str(ln.get('customization') or '').split('、')
                if p.strip() and any(n in p for n in names)]
        if lost:
            out.append(f"{ln_name}{('(' + ln['size'] + ')') if ln.get('size') else ''}:{'、'.join(lost)}")
    return ('備註 ' + '；'.join(out)) if out else ''


def _named(lines):
    menu = {m.id: m.name for m in MenuItem.query.filter(
        MenuItem.id.in_({ln.get('menu_item_id') for ln in lines} or [0])).all()}
    return [(ln, menu.get(ln.get('menu_item_id'), '?')) for ln in lines]


def submit(group_id, by='', now=None, scheduled=False):
    """
    合併送單。回傳 (group, error)；成功 error 為 ''。由這裡 commit。
    以條件式更新搶到 open/failed → submitting 才繼續 (同時送單只有一個會成功)。
    scheduled：排程自動送單 (截止時已承諾送出)，不做暫停接單 / 每時段上限檢查；主揪 / 店員手動送單照常檢查。
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
        return _submit_claimed(group_id, group, by, now, scheduled)
    except Exception as exc:  # noqa: BLE001  (非預期錯誤也不可讓團購卡在「送單中」)
        db.session.rollback()
        logger.warning('團體預購 %s 送單時發生錯誤', group_id, exc_info=True)   # 通知由下方 alert 送出
        # 訂單可能已建立 (錯誤發生在建單之後)：維持「送單失敗」不開放修改，主揪再送時會直接連結已建立的訂單
        db.session.execute(update(GroupOrder).where(GroupOrder.id == group_id, GroupOrder.status == 'submitting')
                           .values(status='failed', error=SYSTEM_ERROR))
        db.session.commit()
        monitor_service.alert(f'團體預購送單時發生錯誤：{exc}', key=f'group_order_error:{group_id}',
                              tenant_id=group.tenant_id, source='group_order')
        return db.session.get(GroupOrder, group_id), '送單時發生錯誤，請稍後再試'


def _submit_claimed(group_id, group, by, now, scheduled=False):
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
    from app.services.order_service import find_by_client_order_id
    existing = find_by_client_order_id(group.tenant_id, group.uuid)
    if existing is not None:   # 之前已建單 (送單後狀態未及更新)：直接連結
        group.status, group.order_id, group.submitted_at, group.error = 'submitted', existing.id, now, ''
        db.session.commit()
        return group, ''
    if not error:
        try:
            order, _replayed = process_checkout(
                tenant_id=group.tenant_id, items=list(merged.values()), payment_method='Cash',
                order_type=group.order_type, need_cutlery=group.order_type == OrderType.TAKE_OUT,
                note=_order_note(group, _stripped_notes(group.tenant_id, list(merged.values()))),
                table_input=f'團購 {group.organizer_name}'[:50],
                client_order_id=group.uuid, external_ref=f'group:{group.token}',
                cashier_username=by[:50], pickup_at=group.pickup_at, now=now, skip_intake=scheduled)
            group = db.session.get(GroupOrder, group_id)
            group.status, group.order_id, group.submitted_at, group.error = 'submitted', order.id, now, ''
            db.session.commit()
            return group, ''
        except AppError as exc:
            db.session.rollback()
            error = exc.message
    group = db.session.get(GroupOrder, group_id)
    # 截止前送單失敗 (主揪提早送單時某品項售完、還沒有人點餐…)：恢復收單，參加者可調整自己的點餐，
    # 截止時仍會依設定自動送單；截止後才標示「送單失敗」(訂單未建立，主揪調整後可再送)
    group.status, group.error = ('failed' if now >= group.deadline else 'open'), error[:200]
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
    """取消團購 (由呼叫端 commit)。以條件式更新只取消仍為 open / failed 的團購：與送單同時發生時只有一個會成功"""
    if group.status in ('submitted', 'submitting', 'cancelled'):
        raise AppError('已送單或已取消的團購不能取消', 409)
    if group.status == 'failed':
        from app.services.order_service import find_by_client_order_id
        if find_by_client_order_id(group.tenant_id, group.uuid) is not None:   # 送單中斷前訂單其實已建立
            raise AppError('這個團購的訂單已經建立，請按「送單」連結訂單；不需要時請店家到訂單管理取消', 409)
    ok = db.session.execute(
        update(GroupOrder).where(GroupOrder.id == group.id, GroupOrder.status.in_(('open', 'failed')))
        .values(status='cancelled').execution_options(synchronize_session=False)).rowcount
    if not ok:
        db.session.rollback()
        raise AppError('已送單或已取消的團購不能取消', 409)
    db.session.refresh(group, ['status'])


def run_due(tenant_id, now=None):
    """
    排程：截止且設定自動送單的團購送單；未自動送單、取餐時間已過仍未送的取消。回傳 (送單數, 取消數)。
    背景排程只有 app context：送單 (結帳) 會讀取店家設定、依店家過濾查詢，因此在該店家的請求情境中執行。
    """
    if has_request_context() and getattr(g, 'tenant_id', None) == tenant_id:
        return _run_due(tenant_id, now)
    from app.models.tenant import Tenant
    from app.services import deferred
    from app.tenant_context import _use_tenant
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        return 0, 0
    prev = (g.get('tenant'), g.get('tenant_id'))   # g 屬於 app context：結束後還原，不影響排程後續的工作
    try:
        with current_app.test_request_context('/'):
            _use_tenant(tenant, remember=False)
            result = _run_due(tenant_id, now)
            deferred.run_ready()   # 送單後的廚房單列印等交易後工作
            return result
    finally:
        g.tenant, g.tenant_id = prev


def _run_due(tenant_id, now=None):
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
    # 功能已關閉：不再自動送單 (改走下方取餐時間過後取消)，通知店家到後台處理截止的自動送單團購
    is_enabled = bool(config(tenant_id).get('enabled'))
    pending = sum(1 for grp in due if grp.auto_submit)
    if not is_enabled and pending:
        from app.services import monitor_service
        monitor_service.alert(f'團體預購已關閉，有 {pending} 個截止的團購未自動送單，請到後台「團體預購」處理 (送單或取消)',
                              key=f'group_order_disabled:{tenant_id}', tenant_id=tenant_id, source='group_order')
    for grp in due:
        if grp.auto_submit and is_enabled:
            try:
                _group, err = submit(grp.id, by='system', now=now, scheduled=True)
                if not err:   # 送單失敗 (售完、沒有人點餐…) 已由 submit 通知，不算已送
                    sent += 1
            except Exception as exc:  # noqa: BLE001  (單一團購失敗不影響其他排程)
                db.session.rollback()
                logger.warning('團體預購 %s 自動送單失敗', grp.id, exc_info=True)
                db.session.execute(update(GroupOrder).where(GroupOrder.id == grp.id, GroupOrder.status == 'submitting')
                                   .values(status='failed', error=SYSTEM_ERROR))
                db.session.commit()
                from app.services import monitor_service
                monitor_service.alert(f'團體預購自動送單失敗：{exc}', key=f'group_order_job:{grp.id}', tenant_id=tenant_id)
        elif grp.pickup_at <= now:
            # 條件式：同時有人按送單 / 取消時不覆蓋對方的結果
            if db.session.execute(update(GroupOrder).where(GroupOrder.id == grp.id, GroupOrder.status == 'open')
                                  .values(status='cancelled').execution_options(synchronize_session=False)).rowcount:
                cancelled += 1
            db.session.commit()
    return sent, cancelled
