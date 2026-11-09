"""
對外 API v1 (#14)：以 API 金鑰 (Authorization: Bearer kk_...) 驗證，依金鑰的權限範圍開放。
後台「API 與 Webhook」建立金鑰 (只顯示一次)。每把金鑰每分鐘最多 120 次請求。

GET  /api/v1/orders?since=ISO時間&after_id=&until=&status=&limit=   訂單 (orders:read)，依建立時間排序；
                                                           next 為下一頁參數 (同一時間的訂單以 after_id 接續)
GET  /api/v1/orders?cursor=0&limit=                        訂單異動 (新增與狀態 / 付款改變，依本機提交順序)；
                                                           回傳 next_cursor (整數，或同一筆提交跨頁時的「序號:id」)，
                                                           原樣帶入即可只拿到之後的異動
                                                           (since / until 帶時區時換算為本機時間；無時區視為門市本地時間)
GET  /api/v1/orders/<uuid>                                 單筆訂單 (orders:read)
GET  /api/v1/menu[?node=分店uuid]                          菜單與庫存 (menu:read；node 只用於線上端)
POST /api/v1/menu/<id>/availability  {"sold_out": bool, "stock": int}   更新售完 / 庫存 (menu:write)
GET  /api/v1/members/<手機>                                 會員姓名、點數、等級、儲值金 (members:read)
GET  /api/v1/reports/daily?date=YYYY-MM-DD                 當日訂單數、營業額、付款方式 (reports:read)
"""
import hashlib
import json
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Blueprint, g, jsonify, request

from app.errors import AppError
from app.extensions import csrf, db, limiter
from app.models.enums import OrderStatus
from app.models.integration import ApiKey, hash_secret
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.tenant import Tenant
from app.models.user import User
from app.services.webhook_service import order_payload
from app.sync import runtime as sync_runtime
from app.tenant_context import _use_tenant, tenant_bypass
from app.utils import MAX_DB_ID, get_json_dict, get_own, json_etag_response, safe_int

api_v1 = Blueprint('api_v1', __name__, url_prefix='/api/v1')
csrf.exempt(api_v1)
MAX_LIMIT = 500


def _bearer_secret():
    auth = request.headers.get('Authorization', '')
    return auth[7:].strip() if auth.lower().startswith('bearer ') else ''


def _key_bucket():
    # 以金鑰本身 (去掉 Bearer 的大小寫與空白差異) 計數：同一把金鑰換寫法也共用同一份額度
    return 'api:' + hashlib.sha256(_bearer_secret().encode()).hexdigest()[:24]


@api_v1.before_request
def authenticate():
    raw = _bearer_secret()
    if not raw:
        raise AppError('缺少 API 金鑰 (Authorization: Bearer ...)', 401)
    with tenant_bypass():
        key = ApiKey.query.filter_by(key_hash=hash_secret(raw)).execution_options(skip_tenant_filter=True).first()
        tenant = db.session.get(Tenant, key.tenant_id) if key is not None else None
    if key is None or not key.is_active or tenant is None or not tenant.is_active:
        raise AppError('API 金鑰無效或已停用', 401)
    _use_tenant(tenant, remember=False)
    g.api_key = key
    now = datetime.now()
    if key.last_used_at is None or (now - key.last_used_at).total_seconds() > 60:
        key.last_used_at, key.last_ip = now, (request.remote_addr or '')[:50]
        db.session.commit()


limiter.limit('120/minute', key_func=_key_bucket)(api_v1)


def scope(name):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if name not in g.api_key.scope_list:
                raise AppError(f'此 API 金鑰沒有「{name}」權限', 403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def _dt(value, label):
    if not value:
        return None
    try:
        value = datetime.fromisoformat(value)
    except ValueError:
        raise AppError(f'{label} 格式錯誤 (請用 ISO 8601，例如 2026-10-24T12:00:00)')
    if value.tzinfo is not None:   # 帶時區 (Z / +08:00)：換成本機時間 (訂單時間以門市本地時間、不帶時區保存)
        value = value.astimezone().replace(tzinfo=None)
    return value


def _bounded(value, label):
    """游標 / id 參數：超出 64 位元整數時回 400 (SQLite 綁定參數會拋 OverflowError → 500)"""
    num = safe_int(value, 0)
    if not -MAX_DB_ID <= num <= MAX_DB_ID:
        raise AppError(f'{label} 超出範圍')
    return num


def _parse_cursor(value):
    """cursor：上次回傳的 next_cursor。整數 = 序號 (feed_rev)；「序號:id」= 同一筆提交的訂單跨頁時接續的位置"""
    rev, sep, after_id = str(value).strip().partition(':')
    return _bounded(rev, 'cursor'), (_bounded(after_id, 'cursor') if sep else None)


_STATUS_BY_LOWER = {s.value.lower(): s.value for s in OrderStatus}


@api_v1.route('/orders')
@scope('orders:read')
def orders():
    limit = max(1, min(MAX_LIMIT, safe_int(request.args.get('limit'), 100)))
    q = Order.query.filter(Order.tenant_id == g.tenant_id)
    status = request.args.get('status')
    if status:
        q = q.filter(Order.status == _STATUS_BY_LOWER.get(status.strip().lower(), status))
    if request.args.get('cursor') is not None:   # 異動追蹤：依提交順序 (feed_rev)，含之後才同步進來的分店訂單
        rev, after_id = _parse_cursor(request.args.get('cursor'))
        cond = Order.feed_rev > rev
        if after_id is not None:
            cond = cond | ((Order.feed_rev == rev) & (Order.id > after_id))
        rows = q.filter(cond).order_by(Order.feed_rev, Order.id).limit(limit + 1).all()
        extra = rows[limit] if len(rows) > limit else None
        rows = rows[:limit]
        has_more = extra is not None
        if not rows:
            next_cursor = rev if after_id is None else f'{rev}:{after_id}'
        elif has_more and extra.feed_rev == rows[-1].feed_rev:
            # 同一筆交易 (同一個 feed_rev) 改到的訂單比一頁多：游標帶上 id，下一頁從同序號的下一張接續，不可整批跳過
            next_cursor = f'{rows[-1].feed_rev}:{rows[-1].id}'
        else:
            next_cursor = rows[-1].feed_rev
        return jsonify({'success': True, 'orders': [order_payload(o) for o in rows], 'next_cursor': next_cursor,
                        'has_more': has_more})
    since, until = _dt(request.args.get('since'), 'since'), _dt(request.args.get('until'), 'until')
    if since and request.args.get('after_id'):
        after_id = _bounded(request.args.get('after_id'), 'after_id')
        q = q.filter((Order.created_at > since) | ((Order.created_at == since) & (Order.id > after_id)))
    elif since:
        q = q.filter(Order.created_at > since)
    if until:
        q = q.filter(Order.created_at <= until)
    rows = q.order_by(Order.created_at, Order.id).limit(limit).all()
    nxt = {'since': rows[-1].created_at.isoformat(), 'after_id': rows[-1].id} if len(rows) == limit else None
    return jsonify({'success': True, 'orders': [order_payload(o) for o in rows], 'next': nxt,
                    'next_since': nxt['since'] if nxt else None})


@api_v1.route('/orders/<uuid>')
@scope('orders:read')
def order_detail(uuid):
    o = Order.query.filter_by(tenant_id=g.tenant_id, uuid=str(uuid)[:36]).first()
    if o is None:
        raise AppError('查無此訂單', 404)
    return jsonify({'success': True, 'order': order_payload(o)})


def _hub_stock(items):
    """
    線上端：回傳 (餐點 → (庫存, 分店是否售完)) 的函式。帶 ?node=<分店 uuid> 時為該分店回報的庫存
    (沒有回報過的品項視為售完，與線上點餐相同)；未指定分店時庫存為 null，任一開放線上點餐的分店有貨就不算售完。
    """
    from app.models.sync import NodeStock, SyncNode
    nodes = SyncNode.query.filter_by(tenant_id=g.tenant_id, is_active=True)
    node_uuid = (request.args.get('node') or '').strip()
    if node_uuid:
        node = nodes.filter_by(uuid=node_uuid[:36]).first()
        if node is None:
            raise AppError('查無此分店', 404)
        snap = {s.menu_item_id: (s.stock or 0, bool(s.is_sold_out)) for s in NodeStock.query.filter_by(node_id=node.id)}

        def of_node(m):
            stock, sold_out = snap.get(m.id, (0, True))
            return stock, sold_out or stock <= 0
        return of_node
    ids = [n.id for n in nodes.filter_by(accepts_online_orders=True)]
    in_stock = {mid for (mid,) in db.session.query(NodeStock.menu_item_id).filter(
        NodeStock.node_id.in_(ids), NodeStock.stock > 0, NodeStock.is_sold_out.is_not(True))} if ids else set()
    return lambda m: (None, m.id not in in_stock)


@api_v1.route('/menu')
@scope('menu:read')
def menu():
    from app.services import branch_service
    branch_service.activate_for_request(g.tenant_id)   # 分店主機：本分店的價格 / 下架
    items = MenuItem.query.filter_by(tenant_id=g.tenant_id).order_by(MenuItem.category, MenuItem.id).all()
    stock_of = _hub_stock(items) if sync_runtime.is_hub() else None
    now = datetime.now()
    out = []
    for m in items:
        in_service, hint = m.service_status(now)
        if stock_of is None:
            stock, sold_out, available = m.stock, bool(m.is_sold_out), m.is_available
        else:   # 線上端：線上點餐依分店回報的庫存 (本機 MenuItem 的庫存欄位不會被使用)
            stock, node_sold_out = stock_of(m)
            sold_out = bool(m.is_sold_out) or node_sold_out
            available = not sold_out and not m.is_branch_hidden
        out.append({
            'id': m.id, 'uuid': m.uuid, 'name': m.name, 'category': m.category, 'price': m.unit_price,
            'list_price': m.list_price,
            'prices': {ch: m.price_for(ch) for ch in ('dine_in', 'takeout', 'online', 'delivery')},
            'stock': stock, 'is_sold_out': sold_out, 'is_available': bool(available and in_service),
            'service_hint': hint, 'sale_from': m.sale_from.isoformat() if m.sale_from else None,
            'sale_until': m.sale_until.isoformat() if m.sale_until else None,
            'avail_days': m.avail_days or '1234567', 'avail_start': m.avail_start or '', 'avail_end': m.avail_end or '',
            'branch_hidden': m.is_branch_hidden, 'prep_minutes': m.prep_minutes or 0,
            'external_code': m.external_code or ''})
    body = json.dumps({'success': True, 'items': out}, ensure_ascii=False, separators=(',', ':'))
    # ETag：串接端定期拉菜單時，沒變化就回 304 (省流量；請帶 If-None-Match)
    return json_etag_response(body)


_FLAG_TEXT = {'true': True, '1': True, 'false': False, '0': False}


def _flag(value, label):
    """布林參數：JSON 布林、0 / 1、"true" / "false" / "1" / "0"；其他值回 400 (bool("false") 為 True，不可直接轉)"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _FLAG_TEXT:
        return _FLAG_TEXT[value.strip().lower()]
    raise AppError(f'{label} 需為 true 或 false')


@api_v1.route('/menu/<int:item_id>/availability', methods=['POST'])
@scope('menu:write')
def menu_availability(item_id):
    from app.services.audit import record_staff_audit
    item = get_own(MenuItem, item_id, '餐點')
    data = get_json_dict()
    changes = []
    if 'stock' in data and sync_runtime.is_hub():
        # 線上點餐依各分店回報的庫存扣減，線上端本機的庫存欄位不會被使用：改了也沒有作用，不可回成功
        raise AppError('線上端的庫存以各分店回報為準，請到分店主機調整 (線上端只能以 sold_out 暫停販售)')
    if 'sold_out' in data:
        item.is_sold_out = _flag(data['sold_out'], 'sold_out')
        changes.append('售完' if item.is_sold_out else '恢復供應')
    if 'stock' in data:
        stock = safe_int(data['stock'], -1)
        if not 0 <= stock <= 100000:
            raise AppError('stock 需為 0 ~ 100000')
        previous = item.stock or 0
        item.stock = stock
        item.total_stock = max(item.total_stock or 0, stock)
        if 'sold_out' not in data:
            # 與結帳扣庫存 / 回補一致：扣到 0 自動售完；原本庫存歸零 (自動售完) 補貨後解除，店員手動標示的售完 (庫存 > 0) 不變
            if stock <= 0:
                item.is_sold_out = True
            elif previous <= 0:
                item.is_sold_out = False
        changes.append(f'庫存 {stock}')
    if not changes:
        raise AppError('請提供 sold_out 或 stock')
    record_staff_audit('API_MENU_UPDATE', tenant_id=g.tenant_id, as_system=True,
                       details=f'API 金鑰「{g.api_key.name}」：{item.name} ' + '、'.join(changes))
    db.session.commit()
    from app.services.event_bus import order_event_bus
    order_event_bus.notify(g.tenant_id)
    return jsonify({'success': True, 'item': {'id': item.id, 'stock': item.stock, 'is_sold_out': bool(item.is_sold_out)}})


@api_v1.route('/members/<phone>')
@scope('members:read')
def member(phone):
    u = User.query.filter_by(tenant_id=g.tenant_id, phone=str(phone)[:20]).first()
    if u is None:
        raise AppError('查無此會員', 404)
    return jsonify({'success': True, 'member': {'uuid': u.uuid, 'name': u.name, 'phone': u.phone, 'points': u.points,
                                                'tier': u.tier.name if u.tier else '', 'wallet_balance': u.wallet_balance,
                                                'marketing_opt_out': bool(u.marketing_opt_out)}})


@api_v1.route('/reports/daily')
@scope('reports:read')
def daily():
    from app.services import report_service
    try:
        day = date.fromisoformat(request.args.get('date') or '') if request.args.get('date') else date.today()
    except ValueError:
        raise AppError('date 格式錯誤 (YYYY-MM-DD)')
    if day > date.today() + timedelta(days=1):
        raise AppError('date 不可是未來')
    r = report_service.build_report(g.tenant_id, day, day)
    return jsonify({'success': True, 'date': day.isoformat(), 'orders': r['order_count'], 'net': r['net'],
                    'cancelled': r['cancelled_count'], 'discounts': r['discounts'], 'avg_ticket': r['avg_ticket'],
                    'by_method': r['by_method'],
                    'pending_orders': Order.query.filter(Order.tenant_id == g.tenant_id,
                                                         Order.status == OrderStatus.PENDING)
                    .count()})
