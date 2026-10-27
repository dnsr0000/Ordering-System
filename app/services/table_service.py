"""
門市桌位與手機掃碼點餐。

流程：顧客用手機 (連店內 Wi-Fi) 掃桌上的 QR code → /t/<token> 綁定這一桌 (存在 session) →
以訪客或會員身分點餐 → 訂單標記桌號、payment_status='unpaid'，直接送廚房 → 餐後到櫃檯，
POS「桌位結帳」一次結清該桌所有未結帳訂單 (settled)。

桌位綁定的安全性：QR code 代碼隨機且可重新產生 (舊的立即失效)；綁定 TABLE_SESSION_HOURS 小時後需重新掃碼；
停用或刪除桌位、或代碼已重新產生時，已綁定的手機也會失效。
"""
import base64
import io
import re
import socket
from datetime import datetime, timedelta

from flask import g, request, session, url_for
from sqlalchemy import select, update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus, normalize_payment_method
from app.models.order import Order
from app.models.table import DiningTable, new_table_token
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.utils import safe_int

TABLE_SESSION_KEY = 'dining_table'
TABLE_SESSION_HOURS = 4
MAX_TABLES_PER_REQUEST = 200
_NAME_RE = re.compile(r'^[\w\-]{1,30}$')
_RANGE_RE = re.compile(r'^(?P<prefix>[^\d\s]*)(?P<start>\d{1,4})\s*[-~]\s*(?P=prefix)?(?P<end>\d{1,4})$')


# ---------------------------------------------------------------------------
# 後台管理
# ---------------------------------------------------------------------------
def parse_table_names(text):
    """「A1-A10, B1, 吧台1」→ ['A1', ..., 'A10', 'B1', '吧台1']；範圍的前綴可省略 (A1-10)"""
    names = []
    for part in re.split(r'[,，、\s]+', str(text or '').strip()):
        if not part:
            continue
        m = _RANGE_RE.match(part)
        if m:
            start, end = int(m['start']), int(m['end'])
            if end < start or end - start >= MAX_TABLES_PER_REQUEST:
                raise AppError(f'桌號範圍「{part}」不正確 (一次最多 {MAX_TABLES_PER_REQUEST} 桌)')
            width = len(m['start']) if m['start'].startswith('0') else 0
            names += [f"{m['prefix']}{str(n).zfill(width)}" for n in range(start, end + 1)]
        else:
            names.append(part)
    for name in names:
        if not _NAME_RE.match(name):
            raise AppError(f'桌號「{name[:30]}」只能使用中英文、數字、- 或 _，最多 30 字')
    if len(names) > MAX_TABLES_PER_REQUEST:
        raise AppError(f'一次最多新增 {MAX_TABLES_PER_REQUEST} 桌')
    return list(dict.fromkeys(names))


def create_tables(tenant_id, text):
    """新增桌位 (已存在的桌號略過)；回傳 (新增的桌號, 略過的桌號)"""
    names = parse_table_names(text)
    if not names:
        raise AppError('請輸入桌號，例如：A1-A10 或 1, 2, 3')
    existing = {t.name for t in DiningTable.query.filter(DiningTable.name.in_(names)).all()}
    order_base = db.session.query(db.func.coalesce(db.func.max(DiningTable.sort_order), 0)).scalar() or 0
    created = [n for n in names if n not in existing]
    for i, name in enumerate(created, 1):
        db.session.add(DiningTable(tenant_id=tenant_id, name=name, sort_order=order_base + i))
    return created, [n for n in names if n in existing]


def rename_table(table, name):
    name = str(name or '').strip()
    if not _NAME_RE.match(name):
        raise AppError('桌號只能使用中英文、數字、- 或 _，最多 30 字')
    if DiningTable.query.filter(DiningTable.name == name, DiningTable.id != table.id).first():
        raise AppError(f'桌號「{name}」已存在', 409)
    table.name = name


def rotate_token(table):
    """重新產生 QR code (舊的 QR code 與已綁定這桌的手機立即失效)"""
    table.token = new_table_token()


def delete_table(table):
    """
    刪除桌位 (有未結帳訂單時拒絕)。先鎖住這一桌再檢查，避免「檢查完、刪除前」剛好有顧客送單，
    訂單的桌位被清空而在 POS 桌位結帳看不到：
    - PostgreSQL：SELECT ... FOR UPDATE 與送單時外鍵檢查的鎖互斥 (送單會等刪除完成後因桌位不存在而失敗)
    - SQLite：先做一次寫入取得資料庫寫入鎖，檢查與刪除期間其他人無法寫入訂單
    """
    if db.engine.dialect.name == 'sqlite':
        db.session.execute(update(DiningTable).where(DiningTable.id == table.id)
                           .values(sort_order=DiningTable.sort_order).execution_options(synchronize_session=False))
    else:
        db.session.execute(select(DiningTable.id).where(DiningTable.id == table.id).with_for_update())
    if unpaid_orders_query().filter(Order.dining_table_id == table.id).first():
        db.session.rollback()
        raise AppError(f'{table.label}還有未結帳的訂單，請先到 POS 結帳後再刪除', 409)
    db.session.delete(table)


def normalize_base_url(value):
    value = str(value or '').strip().rstrip('/')
    if value and not re.match(r'^https?://[A-Za-z0-9.\-\[\]:]+(/[\w\-./]*)?$', value):
        raise AppError('門市網址格式不正確，例如：http://192.168.1.10:5000')
    return value


def suggest_base_url():
    """建議的門市網址：本機在區網的 IP + 目前的埠號 (手機連店內 Wi-Fi 時用這個網址開啟)"""
    ip = None
    try:  # 不會真的送出封包，只是讓作業系統選出對外的網路介面
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
    except OSError:
        pass
    host = request.host.split(':')
    port = host[1] if len(host) > 1 else ''
    if not ip or ip.startswith('127.'):
        return request.host_url.rstrip('/')
    return f"{request.scheme}://{ip}{':' + port if port else ''}"


def base_url(tenant):
    return (tenant.table_qr_base_url or '').rstrip('/') or suggest_base_url()


def table_url(table, base):
    return base.rstrip('/') + url_for('tables.scan', token=table.token)


def qr_data_uri(url):
    """QR code 的 SVG (data URI，可直接放在 <img src>)；容錯等級 M，印出來沾到一點髒污也掃得到"""
    buf = io.BytesIO()
    try:  # 只有列印桌卡需要；沒裝 segno 時系統其他功能照常啟動
        import segno
    except ImportError:
        raise AppError('產生 QR code 需要 segno 套件：請在專案資料夾執行 pip install -r requirements.txt 後重新啟動', 500)
    segno.make(url, error='m').save(buf, kind='svg', scale=8, border=2, dark='#000', light='#fff')
    return 'data:image/svg+xml;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


# ---------------------------------------------------------------------------
# 顧客：掃碼綁定桌位
# ---------------------------------------------------------------------------
def bind_table(table):
    session[TABLE_SESSION_KEY] = {'id': table.id, 'token': table.token, 'at': datetime.now().isoformat()}


def current_table():
    """
    這支手機目前綁定的桌位；過期、停用、刪除、重新產生 QR code 或不是目前店家時回傳 None。
    失效時不清除綁定，而是標記為失效 (table_binding_expired)：這支手機在重新掃碼前送單一律被拒，
    不會因為綁定被清掉而變成一般點餐機訂單 (那樣會漏掉桌號與結帳)。
    """
    data = session.get(TABLE_SESSION_KEY)
    if not isinstance(data, dict) or data.get('expired'):
        return None
    try:
        bound_at = datetime.fromisoformat(str(data.get('at')))
    except ValueError:
        bound_at = None
    table = db.session.get(DiningTable, safe_int(data.get('id'), 0))
    if (table is None or bound_at is None or datetime.now() - bound_at > timedelta(hours=TABLE_SESSION_HOURS)
            or not table.is_active or table.tenant_id != g.tenant_id or table.token != data.get('token')):
        session[TABLE_SESSION_KEY] = {'expired': True}
        return None
    return table


def table_binding_expired():
    """曾掃過桌位 QR code，但綁定已失效 (需重新掃碼才能點餐)"""
    data = session.get(TABLE_SESSION_KEY)
    return isinstance(data, dict) and bool(data.get('expired'))


# ---------------------------------------------------------------------------
# POS：桌位結帳
# ---------------------------------------------------------------------------
def unpaid_orders_query():
    return Order.query.filter(Order.payment_status == 'unpaid', Order.status != OrderStatus.CANCELLED,
                              Order.dining_table_id.isnot(None))


def unpaid_tables():
    """尚未結帳的桌位與訂單 (不限今天)：[{table_id, name, total, orders: [...]}]，依桌位排序"""
    groups = {}
    for o in unpaid_orders_query().order_by(Order.id).all():
        groups.setdefault(o.dining_table_id, []).append(o)
    if not groups:
        return []
    tables = {t.id: t for t in DiningTable.query.filter(DiningTable.id.in_(list(groups))).all()}
    out = []
    for table_id, orders in groups.items():
        table = tables.get(table_id)
        out.append({
            'table_id': table_id,
            'name': table.name if table else f'#{table_id}',
            'sort': (table.sort_order if table else 0, table.name if table else ''),
            'total': sum(o.total_price or 0 for o in orders),
            'orders': [{'id': o.id, 'pickup_number': o.pickup_number, 'status': o.status, 'total_price': o.total_price,
                        'created_at': o.created_at.strftime('%H:%M') if o.created_at else '',
                        'items': [{'name': i.item_name, 'quantity': i.quantity, 'price': i.price} for i in o.items]}
                       for o in orders],
        })
    out.sort(key=lambda t: t.pop('sort'))
    return out


def _parse_payments(payments, total, legacy_method, legacy_received, legacy_info=None):
    """
    分開付款：[{method, amount, received, auth_code, card_last4, one_time_key}]；未提供時視為單一付款 (整筆)。
    回傳整理後的清單 (金額合計需等於應付總額)。
    """
    from app.services import payment_service
    if not payments:
        info = legacy_info if isinstance(legacy_info, dict) else {}
        payments = [{'auth_code': info.get('auth_code'), 'card_last4': info.get('card_last4'),
                     'one_time_key': info.get('one_time_key'),
                     'method': legacy_method, 'amount': total, 'received': legacy_received}]
    if not isinstance(payments, list) or len(payments) > 10:
        raise AppError('分開付款的格式不正確 (最多 10 筆)')
    allowed = payment_service.allowed_methods('pos')
    out = []
    for p in payments:
        if not isinstance(p, dict):
            raise AppError('分開付款的格式不正確')
        method = normalize_payment_method(p.get('method'))
        if method is None:
            raise AppError(f"不支援的付款方式：{p.get('method')}")
        if method not in allowed or method in ('平台代收', '儲值金'):
            raise AppError(f'桌位結帳不可使用「{method}」')
        amount = safe_int(p.get('amount'), 0)
        if amount <= 0:
            raise AppError('每一筆付款金額都必須大於 0')
        received = safe_int(p.get('received'), 0)
        if method == 'Cash':
            if received and received < amount:
                raise AppError(f'實收現金 (${received}) 小於應付金額 (${amount})！')
            received = received or amount
        else:
            received = amount
        auth, last4 = ('', '')
        if method == 'Credit Card':
            auth, last4 = payment_service.validate_card_details(p, True)
        out.append({'method': method, 'amount': amount, 'received': received, 'change': received - amount,
                    'auth_code': auth, 'card_last4': last4, 'one_time_key': p.get('one_time_key')})
    if sum(p['amount'] for p in out) != total:
        raise AppError(f"分開付款合計 (${sum(p['amount'] for p in out)}) 與應付總額 (${total}) 不符")
    return out


def settle_table(tenant_id, table_id, payment_method, cash_received, staff, order_ids=None, payments=None,
                 invoice_info=None, payment_info=None, online=None):
    """
    桌位結帳：結清該桌未結帳的掃碼點餐訂單 (order_ids 指定時只結這幾張 = 分開結帳)，
    payments 可分成多筆不同付款方式 (均分或自訂金額)。回傳 {orders, total, cash_received, change, ...}
    """
    from app.models.payment import PaymentKind, Settlement
    from app.services import einvoice_ecpay, linepay, payment_service, settings_service, wallet_service
    table = db.session.get(DiningTable, safe_int(table_id, 0))
    if table is None or table.tenant_id != tenant_id:
        raise AppError('查無此桌位', 404)
    query = unpaid_orders_query().filter(Order.dining_table_id == table.id)
    if order_ids:
        wanted = {safe_int(i, 0) for i in order_ids}
        query = query.filter(Order.id.in_(wanted))
    orders = query.order_by(Order.id).all()
    if not orders or (order_ids and len(orders) != len({safe_int(i, 0) for i in order_ids})):
        raise AppError(f'{table.label}沒有可結帳的訂單 (可能剛被結帳或取消)，請重新整理', 409)
    total = sum(o.total_price or 0 for o in orders)
    if online is not None:   # 桌邊線上結帳 (綠界已收款)：金額必須與付款時相同
        if total != online['amount']:
            raise AppError(f'訂單金額已異動 (付款 ${online["amount"]}，目前 ${total})', 409)
        parts = [{'method': online['method'], 'amount': total, 'received': total, 'change': 0,
                  'auth_code': str(online.get('reference') or '')[:60], 'card_last4': '', 'one_time_key': None}]
    else:
        parts = _parse_payments(payments, total, payment_method, cash_received, payment_info)
    if invoice_info:
        einvoice_ecpay.normalize_invoice_info(invoice_info, settings_service.get('einvoice', tenant_id))
    primary = max(parts, key=lambda p: p['amount'])['method']
    now = datetime.now()

    settlement = Settlement(tenant_id=tenant_id, table_name=table.name, total=total,
                            order_ids=','.join(str(o.id) for o in orders), staff_username=staff.username, created_at=now)
    db.session.add(settlement)
    db.session.flush()
    # LINE Pay (串接) 先扣款：之後的資料庫更新失敗時自動退款
    charged = []
    for p in parts:
        if p['method'] == 'Line Pay' and linepay.is_enabled(tenant_id):
            key = linepay.normalize_one_time_key(p.get('one_time_key'))
            ref = f'S{settlement.uuid}-{len(charged) + 1}'
            try:
                p['auth_code'] = linepay.pay(tenant_id, ref, p['amount'], key)
            except linepay.LinePayError as exc:
                db.session.rollback()
                _refund_charged(tenant_id, charged)
                raise AppError(exc.message, 402, payment_failed=True)
            charged.append((ref, p['amount']))
    settled = 0
    for o in orders:
        # 條件式更新：兩台 POS 同時結同一桌時，每張訂單只會被結一次
        settled += db.session.execute(
            update(Order).where(Order.id == o.id, Order.payment_status == 'unpaid', Order.status != OrderStatus.CANCELLED)
            .values(payment_status='settled', payment_method=primary, paid_at=now, cash_received=o.total_price,
                    cash_change=0, cashier_username=o.cashier_username or staff.username, settlement_id=settlement.id,
                    shift_id=payment_service.open_shift_id(tenant_id))
            .execution_options(synchronize_session=False)).rowcount
    if settled != len(orders):
        db.session.rollback()
        _refund_charged(tenant_id, charged)
        raise AppError('這一桌的訂單剛被其他人結帳或取消，請重新整理後再結帳！', 409)
    for p in parts:
        payment_service.record_payment(None, PaymentKind.SETTLE, p['method'], p['amount'], received=p['received'],
                                       change=p['change'], reference=p['auth_code'], card_last4=p['card_last4'],
                                       note=f'{table.label}結帳', staff=staff.username, settlement_id=settlement.id,
                                       tenant_id=tenant_id)
    for o in orders:
        db.session.refresh(o)
        if o.user_id:
            user = o.user
            user.total_spent = (user.total_spent or 0) + (o.total_price or 0)
            wallet_service.refresh_tier(user)
    if settings_service.get('einvoice', tenant_id).get('enabled') and total > 0:
        inv = einvoice_ecpay.create_invoice(orders[0], invoice_info or {}, settlement=settlement, amount=total)
        db.session.flush()
        if settings_service.get('einvoice', tenant_id).get('auto_issue', True):
            from app.services import deferred
            deferred.defer(einvoice_ecpay.issue_invoice, inv.id)
    received = sum(p['received'] for p in parts)
    change = sum(p['change'] for p in parts)
    detail = '；'.join(f"{p['method']} ${p['amount']}" for p in parts)
    record_staff_audit('TABLE_SETTLE', tenant_id=tenant_id,
                       details=f"{table.label}結帳：訂單 {', '.join('#' + str(o.id) for o in orders)} | 付款:{detail} "
                               f"應收:${total} 實收:${received} 找零:${change}")
    db.session.commit()
    order_event_bus.notify(tenant_id)
    return {'table': table.name, 'orders': [o.id for o in orders], 'total': total, 'cash_received': received,
            'change': change, 'payment_method': primary, 'settlement_id': settlement.id,
            'payments': [{'method': p['method'], 'amount': p['amount'], 'change': p['change']} for p in parts]}


def _refund_charged(tenant_id, charged):
    from app.services import linepay, monitor_service
    for ref, amount in charged:
        try:
            linepay.refund(tenant_id, ref, amount)
        except linepay.LinePayError as exc:
            monitor_service.alert(f'桌位結帳失敗後 LINE Pay 自動退款失敗 ({ref} ${amount})：{exc.message}，請至 LINE Pay 商家後台退款',
                                  key=f'linepay-{ref}', tenant_id=tenant_id)


def move_orders(tenant_id, from_table_id, to_table_id, order_ids=None):
    """換桌 / 併桌：把未結帳的訂單移到另一桌 (order_ids 為空時整桌移過去)"""
    src = db.session.get(DiningTable, safe_int(from_table_id, 0))
    dst = db.session.get(DiningTable, safe_int(to_table_id, 0))
    if src is None or dst is None or src.tenant_id != tenant_id or dst.tenant_id != tenant_id:
        raise AppError('查無此桌位', 404)
    if src.id == dst.id:
        raise AppError('請選擇不同的桌位')
    query = unpaid_orders_query().filter(Order.dining_table_id == src.id)
    if order_ids:
        query = query.filter(Order.id.in_([safe_int(i, 0) for i in order_ids]))
    orders = query.all()
    if not orders:
        raise AppError(f'{src.label}沒有未結帳的訂單', 409)
    for o in orders:
        label = o.table_number or ''
        new_label = label.replace(src.label, dst.label, 1) if src.label in label else dst.label
        db.session.execute(update(Order).where(Order.id == o.id, Order.payment_status == 'unpaid')
                           .values(dining_table_id=dst.id, table_number=new_label[:50])
                           .execution_options(synchronize_session=False))
    record_staff_audit('TABLE_MOVE', tenant_id=tenant_id,
                       details=f"{src.label} → {dst.label}：訂單 {', '.join('#' + str(o.id) for o in orders)}")
    db.session.commit()
    order_event_bus.notify(tenant_id)
    return {'moved': [o.id for o in orders], 'from': src.name, 'to': dst.name}


def table_counts():
    """{桌位 id: 未結帳訂單數}"""
    rows = db.session.query(Order.dining_table_id, db.func.count(Order.id)).filter(
        Order.payment_status == 'unpaid', Order.status != OrderStatus.CANCELLED,
        Order.dining_table_id.isnot(None)).group_by(Order.dining_table_id).all()
    return dict(rows)


# ---------------------------------------------------------------------------
# 資料一致性檢查 (flask --app run tables check)
# ---------------------------------------------------------------------------
def find_conflicts(stale_hours=24):
    """
    找出桌邊點餐資料的衝突 / 異常 (所有店家)，回傳 [{kind, order_id, tenant_id, message}]：
    - orphan：未結帳的桌邊訂單沒有桌位 (POS 桌位結帳看不到，可能漏收款)
    - cancelled_paid：已取消卻仍是已結帳 / 已線上付款 (應標記需退款)
    - wrong_tenant：訂單的桌位屬於其他店家
    - stale_unpaid：超過 stale_hours 小時仍未結帳 (可能忘了結帳)
    """
    from app.tenant_context import tenant_bypass
    found = []
    with tenant_bypass():
        rows = db.session.execute(
            select(Order.id, Order.tenant_id, Order.table_number, Order.payment_status, Order.status, Order.created_at,
                   Order.dining_table_id, DiningTable.tenant_id)
            .outerjoin(DiningTable, DiningTable.id == Order.dining_table_id)
            .where((Order.order_source == 'Table') | Order.dining_table_id.isnot(None)
                   | Order.payment_status.in_(('settled', 'paid')))).all()
    deadline = datetime.now() - timedelta(hours=stale_hours)
    for oid, tid, label, pay, status, created, table_id, table_tenant in rows:
        cancelled = status == OrderStatus.CANCELLED
        if cancelled and pay in ('settled', 'paid'):
            found.append(dict(kind='cancelled_paid', order_id=oid, tenant_id=tid,
                              message=f'訂單 #{oid} ({label}) 已取消但付款狀態為 {pay}，應標記需退款'))
        if pay == 'unpaid' and not cancelled:
            if table_id is None:
                found.append(dict(kind='orphan', order_id=oid, tenant_id=tid,
                                  message=f'訂單 #{oid} ({label}) 未結帳但桌位已不存在，POS 桌位結帳看不到'))
            elif created and created < deadline:
                found.append(dict(kind='stale_unpaid', order_id=oid, tenant_id=tid,
                                  message=f'訂單 #{oid} ({label}) 超過 {stale_hours} 小時仍未結帳'))
        if table_id is not None and table_tenant is not None and table_tenant != tid:
            found.append(dict(kind='wrong_tenant', order_id=oid, tenant_id=tid,
                              message=f'訂單 #{oid} 的桌位屬於其他店家'))
    return found


def fix_conflicts(found):
    """自動修正「已取消卻已收款」→ 需退款；其他項目需人工確認 (到 POS 或後台處理)，回傳修正筆數"""
    from app.services.order_service import flag_refund_if_paid
    from app.tenant_context import tenant_bypass
    fixed = 0
    with tenant_bypass():
        for item in found:
            if item['kind'] == 'cancelled_paid':
                flag_refund_if_paid(item['order_id'])
                fixed += 1
        db.session.commit()
    return fixed


# ---------------------------------------------------------------------------
# 桌邊呼叫服務
# ---------------------------------------------------------------------------
CALL_KINDS = {'water': '加水 / 餐具', 'bill': '想結帳', 'help': '需要服務'}
CALL_EXPIRE_HOURS = 6   # 超過幾小時仍未處理的呼叫不再顯示 (忘了按「已處理」)


def call_service(table, kind, note=''):
    """顧客按「呼叫服務」：同一桌同一種呼叫未處理前不重複建立。回傳 (ServiceCall, 是否新建立)"""
    from app.models.order import ServiceCall
    if kind not in CALL_KINDS:
        raise AppError('不支援的呼叫類型')
    since = datetime.now() - timedelta(hours=CALL_EXPIRE_HOURS)
    existing = ServiceCall.query.filter(ServiceCall.dining_table_id == table.id, ServiceCall.kind == kind,
                                        ServiceCall.handled_at.is_(None), ServiceCall.created_at >= since).first()
    if existing is not None:
        return existing, False
    call = ServiceCall(tenant_id=table.tenant_id, dining_table_id=table.id, kind=kind,
                       note=str(note or '').strip()[:100], created_at=datetime.now())
    db.session.add(call)
    db.session.commit()
    order_event_bus.notify(table.tenant_id)
    return call, True


def open_calls():
    """POS 顯示的待處理呼叫 (最早的在前)"""
    from app.models.order import ServiceCall
    since = datetime.now() - timedelta(hours=CALL_EXPIRE_HOURS)
    calls = ServiceCall.query.filter(ServiceCall.handled_at.is_(None), ServiceCall.created_at >= since) \
        .order_by(ServiceCall.created_at).limit(50).all()
    return [{'id': c.id, 'table': c.dining_table.name if c.dining_table else '?', 'kind': c.kind,
             'label': CALL_KINDS.get(c.kind, c.kind), 'note': c.note,
             'at': c.created_at.strftime('%H:%M'),
             'minutes': int((datetime.now() - c.created_at).total_seconds() // 60)} for c in calls]


def handle_call(call_id, staff_username):
    from app.models.order import ServiceCall
    call = db.session.get(ServiceCall, safe_int(call_id, 0))
    if call is None or call.tenant_id != g.tenant_id:
        raise AppError('查無此呼叫', 404)
    if call.handled_at is None:
        call.handled_at, call.handled_by = datetime.now(), staff_username
        db.session.commit()
        order_event_bus.notify(call.tenant_id)
    return call


# ---------------------------------------------------------------------------
# 桌位平面圖
# ---------------------------------------------------------------------------
TABLE_SHAPES = {'square': '方桌', 'round': '圓桌', 'long': '長桌'}


def update_table_details(table, seats=None, area=None, shape=None):
    """座位數 (1~30)、區域、形狀"""
    if seats is not None:
        n = safe_int(seats, 0)
        if not 1 <= n <= 30:
            raise AppError('座位數需介於 1 ~ 30')
        table.seats = n
    if area is not None:
        table.area = str(area or '').strip()[:20]
    if shape is not None:
        table.shape = shape if shape in TABLE_SHAPES else 'square'


def save_layout(positions):
    """平面圖擺放：[{id, x, y}] (平面圖寬高的千分比 0~1000；x 或 y 為空 = 從平面圖移除)。回傳更新筆數"""
    if not isinstance(positions, list) or len(positions) > 500:
        raise AppError('平面圖資料格式不正確')
    tables = {t.id: t for t in DiningTable.query.all()}
    n = 0
    for p in positions:
        if not isinstance(p, dict):
            continue
        table = tables.get(safe_int(p.get('id'), 0))
        if table is None:
            continue
        x, y = p.get('x'), p.get('y')
        if x is None or y is None:
            table.pos_x = table.pos_y = None
        else:
            table.pos_x = max(0, min(1000, safe_int(x, 0)))
            table.pos_y = max(0, min(1000, safe_int(y, 0)))
        n += 1
    return n


def floor_plan(now=None):
    """
    POS 平面圖：每一桌的狀態 free 空桌 / occupied 用餐中 (有未結帳的掃碼點餐) / calling 呼叫服務中 / reserved 即將訂位到場，
    用餐中顯示開始時間、已用餐分鐘、未結帳金額與張數。
    """
    from app.models.order import ServiceCall
    from app.models.reservation import Reservation
    now = now or datetime.now()
    tables = DiningTable.query.filter_by(is_active=True).order_by(DiningTable.sort_order, DiningTable.id).all()
    unpaid = {}
    for o in unpaid_orders_query().order_by(Order.id).all():
        unpaid.setdefault(o.dining_table_id, []).append(o)
    calls = {}
    since = now - timedelta(hours=CALL_EXPIRE_HOURS)
    for c in ServiceCall.query.filter(ServiceCall.handled_at.is_(None), ServiceCall.created_at >= since).all():
        calls.setdefault(c.dining_table_id, []).append(c)
    reserved = {}
    for r in Reservation.query.filter(Reservation.status == 'booked', Reservation.table_name != '',
                                      Reservation.reserved_at >= now - timedelta(minutes=30),
                                      Reservation.reserved_at <= now + timedelta(minutes=60)).all():
        reserved.setdefault(r.table_name, r)
    out = []
    for t in tables:
        orders = unpaid.get(t.id, [])
        first = min((o.created_at for o in orders if o.created_at), default=None)
        r = reserved.get(t.name)
        status = 'calling' if calls.get(t.id) else ('occupied' if orders else ('reserved' if r else 'free'))
        out.append({
            'id': t.id, 'name': t.name, 'seats': t.seats or 0, 'area': t.area or '', 'shape': t.shape or 'square',
            'x': t.pos_x, 'y': t.pos_y, 'status': status,
            'total': sum(o.total_price or 0 for o in orders), 'orders': len(orders),
            'since': first.strftime('%H:%M') if first else '',
            'minutes': int((now - first).total_seconds() // 60) if first else 0,
            'calls': [CALL_KINDS.get(c.kind, c.kind) for c in calls.get(t.id, [])],
            'reservation': f"{r.reserved_at:%H:%M} {r.name} {r.party_size}位" if r else '',
        })
    areas = sorted({t['area'] for t in out if t['area']})
    return {'tables': out, 'areas': areas}
