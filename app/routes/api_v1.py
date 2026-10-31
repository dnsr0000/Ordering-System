"""
對外 API v1 (第三輪 #14)：以 API 金鑰 (Authorization: Bearer kk_...) 驗證，依金鑰的權限範圍開放。
後台「API 與 Webhook」建立金鑰 (只顯示一次)。每把金鑰每分鐘最多 120 次請求。

GET  /api/v1/orders?since=ISO時間&after_id=&until=&status=&limit=   訂單 (orders:read)，依建立時間排序；
                                                           next 為下一頁參數 (同一時間的訂單以 after_id 接續)
GET  /api/v1/orders?cursor=0&limit=                        訂單異動 (新增與狀態 / 付款改變，依本機提交順序)；
                                                           回傳 next_cursor，下次帶入即可只拿到之後的異動
GET  /api/v1/orders/<uuid>                                 單筆訂單 (orders:read)
GET  /api/v1/menu                                          菜單與庫存 (menu:read)
POST /api/v1/menu/<id>/availability  {"sold_out": bool, "stock": int}   更新售完 / 庫存 (menu:write)
GET  /api/v1/members/<手機>                                 會員姓名、點數、等級、儲值金 (members:read)
GET  /api/v1/reports/daily?date=YYYY-MM-DD                 當日訂單數、營業額、付款方式 (reports:read)
"""
import hashlib
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
from app.tenant_context import _use_tenant, tenant_bypass
from app.utils import get_json_dict, get_own, safe_int

api_v1 = Blueprint('api_v1', __name__, url_prefix='/api/v1')
csrf.exempt(api_v1)
MAX_LIMIT = 500


def _key_bucket():
    return 'api:' + hashlib.sha256(request.headers.get('Authorization', '').encode()).hexdigest()[:24]


@api_v1.before_request
def authenticate():
    auth = request.headers.get('Authorization', '')
    raw = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
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
        return datetime.fromisoformat(value)
    except ValueError:
        raise AppError(f'{label} 格式錯誤 (請用 ISO 8601，例如 2026-10-24T12:00:00)')


@api_v1.route('/orders')
@scope('orders:read')
def orders():
    limit = max(1, min(MAX_LIMIT, safe_int(request.args.get('limit'), 100)))
    q = Order.query.filter(Order.tenant_id == g.tenant_id)
    status = request.args.get('status')
    if status:
        q = q.filter(Order.status == status)
    if request.args.get('cursor') is not None:   # 異動追蹤：依提交順序 (sync_rev)，含之後才同步進來的分店訂單
        cursor = safe_int(request.args.get('cursor'), 0)
        rows = q.filter(Order.sync_rev > cursor).order_by(Order.sync_rev, Order.id).limit(limit).all()
        next_cursor = rows[-1].sync_rev if rows else cursor
        return jsonify({'success': True, 'orders': [order_payload(o) for o in rows], 'next_cursor': next_cursor,
                        'has_more': len(rows) == limit})
    since, until = _dt(request.args.get('since'), 'since'), _dt(request.args.get('until'), 'until')
    if since and request.args.get('after_id'):
        after_id = safe_int(request.args.get('after_id'), 0)
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


@api_v1.route('/menu')
@scope('menu:read')
def menu():
    from app.services import branch_service
    branch_service.activate_for_request(g.tenant_id)   # 分店主機：本分店的價格 / 下架
    items = MenuItem.query.filter_by(tenant_id=g.tenant_id).order_by(MenuItem.category, MenuItem.id).all()
    return jsonify({'success': True, 'items': [{
        'id': m.id, 'uuid': m.uuid, 'name': m.name, 'category': m.category, 'price': m.list_price,
        'prices': {ch: m.price_for(ch) for ch in ('dine_in', 'takeout', 'online', 'delivery')},
        'stock': m.stock, 'is_sold_out': bool(m.is_sold_out), 'is_available': m.is_available,
        'branch_hidden': m.is_branch_hidden,
        'external_code': m.external_code or ''} for m in items]})


@api_v1.route('/menu/<int:item_id>/availability', methods=['POST'])
@scope('menu:write')
def menu_availability(item_id):
    from app.services.audit import record_staff_audit
    item = get_own(MenuItem, item_id, '餐點')
    data = get_json_dict()
    changes = []
    if 'sold_out' in data:
        item.is_sold_out = bool(data['sold_out'])
        changes.append('售完' if item.is_sold_out else '恢復供應')
    if 'stock' in data:
        stock = safe_int(data['stock'], -1)
        if not 0 <= stock <= 100000:
            raise AppError('stock 需為 0 ~ 100000')
        item.stock = stock
        item.total_stock = max(item.total_stock or 0, stock)
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
