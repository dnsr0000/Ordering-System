"""
線上端 (hub) 的同步 API：/api/sync/v1/*，以分店權杖 (Authorization: Bearer ...) 驗證。

- POST /hello   分店第一次連線：回傳分店與店家資訊
- POST /push    分店上傳變更 (含庫存快照)；回傳拒收的資料、合併的重複資料、線上缺少的餐點圖片
- GET  /pull    分店下載變更 (只含該分店需要的資料)，可長輪詢 (wait 秒) 等待新的線上訂單
- GET/PUT /media/menu/<路徑>  餐點圖片
"""
import io
import os
import time
from datetime import datetime

from flask import Blueprint, abort, current_app, g, jsonify, request, send_from_directory
from PIL import Image

from app.errors import AppError
from app.extensions import csrf, db, limiter
from app.models.menu import MenuItem
from app.models.sync import NodeStock, SyncNode, hash_token
from app.services.event_bus import order_event_bus
from app.services.order_service import flag_refund_if_paid
from app.services.points_service import recompute_points
from app.services.upload_paths import is_valid_menu_image, upload_file_path
from app.sync import tracking
from app.sync.engine import Applier, collect_changes
from app.sync.registry import SPECS
from app.tenant_context import tenant_bypass

PROTOCOL = 1
UNPAID = ('pending', 'expired')
PULL_SPECS = tuple(s for s in SPECS if s.direction == 'both')
PUSH_SPECS = SPECS
MAX_PAGE = 1000
# 單次上傳的上限 (分店每批最多送 500 列；庫存快照每個品項一筆)
MAX_PUSH_ROWS = 5000
MAX_STOCK_ROWS = 5000

sync_api = Blueprint('sync_api', __name__, url_prefix='/api/sync/v1')
csrf.exempt(sync_api)
limiter.exempt(sync_api)


@sync_api.before_request
def authenticate_node():
    if current_app.config.get('SYNC_ROLE') != 'hub':
        abort(404)
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    if not token:
        raise AppError('缺少分店權杖', 401)
    with tenant_bypass():
        node = SyncNode.query.filter_by(token_hash=hash_token(token)).first()
    if node is None or not node.is_active or not node.tenant or not node.tenant.is_active:
        raise AppError('分店權杖無效或已停用', 401)
    g.sync_node = node
    g.tenant, g.tenant_id = node.tenant, node.tenant_id
    node.last_seen_at = datetime.now()
    node.last_ip = (request.remote_addr or '')[:50]
    db.session.commit()


def _node():
    return g.sync_node


@sync_api.route('/hello', methods=['POST'])
def hello():
    node, tenant = _node(), _node().tenant
    return jsonify({
        'success': True, 'protocol': PROTOCOL, 'server_time': datetime.now().isoformat(),
        'node': {'uuid': node.uuid, 'name': node.name},
        'tenant': {'uuid': tenant.uuid, 'name': tenant.name, 'slug': tenant.slug},
    })


def _order_filter(node_uuid):
    def row_filter(spec, table):
        # 待付款 / 逾時未付款的線上訂單不送分店；付款完成時訂單與明細會重新標記 (payment_ecpay.mark_paid)
        if spec.name == 'order':
            return (table.c.node_uuid == node_uuid) & table.c.payment_status.notin_(UNPAID)
        if spec.name == 'order_item':
            orders = db.metadata.tables['order']
            return table.c.order_id.in_(db.select(orders.c.id).where(
                orders.c.node_uuid == node_uuid, orders.c.payment_status.notin_(UNPAID)))
        if spec.name == 'sync_tombstone':
            return table.c.table_name.in_([s.name for s in PULL_SPECS])
        return None
    return row_filter


@sync_api.route('/pull', methods=['GET'])
def pull():
    """
    參數：cursor (上次讀到的序號)、upto/table/after (同一輪分頁時沿用上一頁回傳的值)、limit、wait (長輪詢秒數)。
    回傳：changes、has_more、next (下一頁參數)；has_more 為 false 時 next.cursor 即新的游標。
    """
    node = _node()
    cursor = request.args.get('cursor', -1, type=int)
    limit = max(1, min(MAX_PAGE, request.args.get('limit', 500, type=int)))
    upto = request.args.get('upto', type=int)
    start = (request.args.get('table', 0, type=int), request.args.get('after', 0, type=int))
    wait = max(0, min(30, request.args.get('wait', 0, type=int)))
    row_filter = _order_filter(node.uuid)

    deadline = time.monotonic() + wait
    while True:
        rev = tracking.current_rev(db.session)
        page_upto = upto if upto is not None else rev
        changes, nxt = collect_changes(node.tenant_id, PULL_SPECS, cursor, page_upto, start, limit, row_filter)
        if changes or nxt is not None or upto is not None or time.monotonic() >= deadline:
            break
        db.session.rollback()  # 結束讀取交易，下一輪才看得到新提交的資料
        time.sleep(1)
    db.session.rollback()
    if nxt is None:
        return jsonify({'success': True, 'changes': changes, 'has_more': False, 'next': {'cursor': page_upto}})
    return jsonify({'success': True, 'changes': changes, 'has_more': True,
                    'next': {'cursor': cursor, 'upto': page_upto, 'table': nxt[0], 'after': nxt[1]}})


@sync_api.route('/push', methods=['POST'])
def push():
    node = _node()
    payload = request.get_json(silent=True) or {}
    changes = payload.get('changes') or {}
    if not isinstance(changes, dict) or not all(isinstance(rows, list) for rows in changes.values()):
        raise AppError('changes 格式錯誤')
    if sum(len(rows) for rows in changes.values()) > MAX_PUSH_ROWS:
        raise AppError(f'單次上傳超過 {MAX_PUSH_ROWS} 筆，請分批上傳', 413)
    stock = payload.get('stock')
    if isinstance(stock, list) and len(stock) > MAX_STOCK_ROWS:
        raise AppError('庫存回報筆數過多', 413)
    applier = Applier(node.tenant_id, 'hub', source_node_uuid=node.uuid)
    result = applier.apply(changes)
    recompute_points(result.touched_users)
    for order in result.cancelled_orders:  # 分店取消了已線上付款的訂單：線上端標記需退款
        flag_refund_if_paid(order.id)
    _save_stock_snapshot(node, stock)
    db.session.commit()
    if result.orders_changed:
        order_event_bus.notify(node.tenant_id)
    missing = sorted(p for p in result.menu_images if not _menu_image_exists(p))
    return jsonify({'success': True, 'applied': result.applied, 'conflicts': result.conflicts,
                    'aliases': result.aliases, 'missing_media': missing})


def _save_stock_snapshot(node, stock):
    """分店回報的庫存：[{uuid, stock, is_sold_out}]，覆蓋線上端的庫存快照"""
    if not isinstance(stock, list) or not stock:
        return
    uuids = [str(s.get('uuid')) for s in stock if isinstance(s, dict) and s.get('uuid')]
    ids = dict(db.session.execute(db.select(MenuItem.uuid, MenuItem.id).where(
        MenuItem.tenant_id == node.tenant_id, MenuItem.uuid.in_(uuids))).all())
    now = datetime.now()
    for s in stock:
        item_id = ids.get(str(s.get('uuid'))) if isinstance(s, dict) else None
        if item_id is None:
            continue
        row = db.session.get(NodeStock, (node.id, item_id))
        if row is None:
            row = NodeStock(node_id=node.id, menu_item_id=item_id)
            db.session.add(row)
        row.stock = max(0, int(s.get('stock') or 0))
        row.is_sold_out = bool(s.get('is_sold_out'))
        row.reported_at = now


# ---------------------------------------------------------------------------
# 餐點圖片
# ---------------------------------------------------------------------------
def _menu_image_path(rel_path):
    if not rel_path or not is_valid_menu_image(rel_path):
        return None
    return upload_file_path(current_app.config['UPLOAD_FOLDER_MENU'], rel_path)


def _menu_image_exists(rel_path):
    path = _menu_image_path(rel_path)
    return bool(path and os.path.isfile(path))


def _own_menu_image(rel_path):
    """圖片必須是這個分店所屬店家的餐點正在使用的圖片 (不可讀寫其他店家的圖片)"""
    return db.session.execute(db.select(MenuItem.id).where(
        MenuItem.tenant_id == g.tenant_id, MenuItem.image_path == rel_path).limit(1)).first() is not None


@sync_api.route('/media/menu/<path:rel_path>', methods=['GET'])
def get_menu_image(rel_path):
    path = _menu_image_path(rel_path)
    if path is None or not _own_menu_image(rel_path) or not os.path.isfile(path):
        abort(404)
    return send_from_directory(current_app.config['UPLOAD_FOLDER_MENU'], rel_path)


@sync_api.route('/media/menu/<path:rel_path>', methods=['PUT'])
def put_menu_image(rel_path):
    path = _menu_image_path(rel_path)
    if path is None:
        raise AppError('不支援的圖片路徑或格式')
    if not _own_menu_image(rel_path):
        raise AppError('沒有使用這張圖片的餐點', 404)
    if os.path.exists(path):  # 圖片檔名隨機且不會改內容：已存在就不覆蓋 (避免覆寫其他店家同名路徑的圖片)
        return jsonify({'success': True, 'existed': True})
    data = request.get_data(cache=False)
    if not data or len(data) > current_app.config['MAX_SINGLE_FILE_SIZE']:
        raise AppError('圖片大小不符')
    try:  # 只接受真正的圖片檔 (此資料夾會公開提供下載)
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except Exception:  # noqa: BLE001
        raise AppError('不是有效的圖片檔')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.part'
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.replace(tmp, path)
    return jsonify({'success': True})
