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
from app.models.enums import OrderSource, OrderStatus
from app.models.menu import MenuItem
from app.models.order import Order
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
PULL_SPECS = tuple(s for s in SPECS if s.direction in ('both', 'down'))
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
    from app.services import branch_service
    branch_service.refresh_sites(node.tenant_id)
    db.session.commit()
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
        if spec.name == 'branch_menu_override':   # 分店價格 / 上下架：只送給該分店
            return table.c.node_uuid == node_uuid
        if spec.name == 'stock_transfer':         # 叫貨 / 調撥單：只送給出貨方與收貨方
            return (table.c.from_site == node_uuid) | (table.c.to_site == node_uuid)
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
    from app.services import branch_service
    if branch_service.refresh_sites(node.tenant_id, only_if_missing=True):   # 舊版升級：補上分店名單
        db.session.commit()

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
    fixed = _undo_duplicate_referrals(node, result.touched_users) | _true_up_points_expiry(node, result.touched_users)
    if fixed:
        recompute_points(fixed)
    from app.services import pass_service
    for mp in pass_service.recompute(result.touched_passes):   # 寄杯在兩家分店同步前同時用完
        from app.services import monitor_service
        monitor_service.alert(f'會員 {mp.user.name if mp.user else mp.user_id} 的寄杯「{mp.name}」在不同分店同時兌換後剩 '
                              f'{mp.remaining} 次 (同步前同時使用)；請確認是否補收差額', key=f'pass-negative-{mp.uuid}',
                              tenant_id=node.tenant_id, level='warning', source='sync')
    _reconcile_coupon_use(node, [r.get('uuid') for r in changes.get('order') or () if isinstance(r, dict)])
    _alert_negative_balances(node, set(result.touched_users) | fixed,
                             [r.get('gift_card_id') for r in changes.get('gift_card_ledger') or () if isinstance(r, dict)])
    for transfer, message in result.transfer_alerts:
        from app.services import monitor_service
        monitor_service.alert(message, key=f'transfer-{transfer.uuid}', tenant_id=node.tenant_id, level='warning',
                              source='sync')
    for order in result.cancelled_orders:  # 分店取消了已線上付款的訂單：線上端標記需退款
        flag_refund_if_paid(order.id)
    _save_stock_snapshot(node, stock)
    db.session.commit()
    if result.orders_changed:
        order_event_bus.notify(node.tenant_id)
    missing = sorted(p for p in result.menu_images if not _menu_image_exists(p))
    return jsonify({'success': True, 'applied': result.applied, 'conflicts': result.conflicts,
                    'aliases': result.aliases, 'missing_media': missing})


def _reconcile_coupon_use(node, order_uuids):
    """
    會員券在分店與線上端 (或兩家分店) 同步前被同時使用：兩邊都無法即時互鎖，訂單也都已成立。
    - 券的「已使用」以未取消的訂單為準：另一邊較新的「取消退券」不可把仍被訂單使用的券改回未使用 (否則可再用一次)。
    - 同一張券有兩張以上未取消的訂單：通知店家處理 (補收差額或取消其中一張)。
    只檢查這次上傳的訂單用到的券 (券本身的列可能在前一頁，訂單的取消也可能在後一頁，所以以訂單為準)。
    """
    from app.models.coupon import UserCoupon
    uuids = [u for u in order_uuids if u]
    if not uuids:
        return
    uc_ids = {i for (i,) in db.session.execute(db.select(Order.applied_user_coupon_id).where(
        Order.tenant_id == node.tenant_id, Order.uuid.in_(uuids), Order.applied_user_coupon_id.isnot(None)))}
    for uc_id in sorted(uc_ids):
        uc = db.session.get(UserCoupon, uc_id)
        if uc is None or uc.tenant_id != node.tenant_id:
            continue
        active = Order.query.filter(Order.tenant_id == node.tenant_id, Order.applied_user_coupon_id == uc.id,
                                    Order.status != OrderStatus.CANCELLED) \
            .execution_options(skip_tenant_filter=True).order_by(Order.created_at, Order.id).all()
        if active and not uc.is_used:
            uc.is_used, uc.used_at = True, active[0].created_at
        if len(active) > 1:
            from app.models.user import User
            from app.services import monitor_service
            places = {n.uuid: n.name for n in SyncNode.query.filter_by(tenant_id=node.tenant_id)
                      .execution_options(skip_tenant_filter=True)}
            member = db.session.get(User, uc.user_id)
            detail = '、'.join(f"#{o.id} ({'線上點餐' if o.order_source == OrderSource.ONLINE else places.get(o.node_uuid, '分店')})"
                              for o in active)
            monitor_service.alert(f"優惠券「{uc.code}」被重複使用：會員 {member.name if member else uc.user_id} 的同一張券"
                                  f"在同步前同時用於訂單 {detail}；請確認是否補收差額或取消其中一張",
                                  key=f'coupon-dup-{uc.uuid}', tenant_id=node.tenant_id, level='warning', source='sync')


def _undo_duplicate_referrals(node, user_ids):
    """
    推薦好友獎勵「每位新會員一次」，但新會員的第一筆訂單可能在兩家分店 (或分店與線上) 同步前同時成立，兩邊各發一次。
    線上端合併後保留最早的一筆，其餘的以 referred_dup / referral_dup 沖銷 (新會員與推薦人各一筆，會同步到各分店)，並通知店家。
    回傳點數有變動的會員 ID。
    """
    from app.models.sync import PointLedger
    from app.models.user import User
    from app.services import monitor_service
    changed = set()
    for uid in sorted(i for i in user_ids if i):
        rows = PointLedger.query.filter(PointLedger.tenant_id == node.tenant_id, PointLedger.user_id == uid,
                                        PointLedger.reason == 'referred') \
            .execution_options(skip_tenant_filter=True).order_by(PointLedger.created_at, PointLedger.id).all()
        # 已取消的訂單在取消時已扣回該次獎勵，不算重複
        cancelled = {o for (o,) in db.session.execute(db.select(Order.id).where(
            Order.id.in_([r.order_id for r in rows if r.order_id]), Order.status == OrderStatus.CANCELLED))}
        rows = [r for r in rows if r.order_id not in cancelled]
        if len(rows) < 2:
            continue
        undone = {r.order_id for r in PointLedger.query.filter(PointLedger.user_id == uid,
                                                               PointLedger.reason == 'referred_dup')
                  .execution_options(skip_tenant_filter=True)}
        fixed_here = 0
        for dup in rows[1:]:
            if dup.order_id in undone:
                continue
            db.session.add(PointLedger(tenant_id=node.tenant_id, user_id=uid, delta=-(dup.delta or 0),
                                       reason='referred_dup', order_id=dup.order_id))
            changed.add(uid)
            fixed_here += 1
            for ref in PointLedger.query.filter(PointLedger.order_id == dup.order_id, PointLedger.order_id.isnot(None),
                                                PointLedger.reason == 'referral') \
                    .execution_options(skip_tenant_filter=True):
                db.session.add(PointLedger(tenant_id=node.tenant_id, user_id=ref.user_id, delta=-(ref.delta or 0),
                                           reason='referral_dup', order_id=dup.order_id))
                changed.add(ref.user_id)
        if fixed_here:
            member = db.session.get(User, uid)
            monitor_service.alert(f"會員 {member.name if member else uid} 的推薦好友獎勵在不同分店同步前各發了一次，"
                                  f"已自動沖銷重複的 {fixed_here} 次 (新會員與推薦人各扣回)",
                                  key=f'referral-dup-{member.uuid if member else uid}', tenant_id=node.tenant_id,
                                  level='warning', source='sync')
    return changed


def _true_up_points_expiry(node, user_ids):
    """線上端執行點數到期時，分店同步前已用掉同一批點數：依先進先出重算到期量並校正 (見 loyalty_service.true_up_expiry)"""
    from app.services import loyalty_service
    return {uid for uid in sorted(i for i in user_ids if i) if loyalty_service.true_up_expiry(node.tenant_id, uid)}


def _alert_negative_balances(node, user_ids, gift_card_uuids=()):
    """
    點數 / 儲值金 / 禮物卡在不同分店 (或線上) 同步前同時使用，合併異動帳後變成負數 (兩個資料庫無法即時互鎖)：
    通知店家處理 (負數期間無法再折抵、兌換或扣款)。
    """
    from app.models.member import GiftCard
    from app.models.user import User
    from app.services import giftcard_service, monitor_service

    def warn(message, key):
        monitor_service.alert(message, key=key, tenant_id=node.tenant_id, level='warning', source='sync')

    ids = sorted(i for i in user_ids if i)
    if ids:
        for user in User.query.filter(User.tenant_id == node.tenant_id, User.id.in_(ids),
                                      db.or_(User.points < 0, User.wallet_balance < 0)) \
                .execution_options(skip_tenant_filter=True):
            if (user.points or 0) < 0:
                warn(f'會員 {user.name} 的點數在不同分店 / 線上同時使用後為 {user.points} 點 (同步前同時折抵或兌換)；'
                     f'負數期間無法再折抵或兌換，請確認是否需要調整', f'points-negative-{user.uuid}')
            if (user.wallet_balance or 0) < 0:
                warn(f'會員 {user.name} 的儲值金在不同分店 / 線上同時使用後為 ${user.wallet_balance} (同步前同時扣款)；'
                     f'請向會員補收差額或請其加值', f'wallet-negative-{user.uuid}')
    if ids:   # 集點卡在兩家分店同時集滿 (各自兌換一次獎勵)：章數合計變負數，之後集的章先補回
        from app.models.member import StampLedger
        for uid, total in db.session.query(StampLedger.user_id, db.func.sum(StampLedger.delta)) \
                .filter(StampLedger.tenant_id == node.tenant_id, StampLedger.user_id.in_(ids)) \
                .group_by(StampLedger.user_id).all():
            if (total or 0) < 0:
                member = db.session.get(User, uid)
                warn(f'會員 {member.name if member else uid} 的集點卡在不同分店同步前同時集滿，多兌換了一次獎勵 '
                     f'(章數 {total})；之後集的章會先補回，如需處理請聯絡會員', f'stamps-negative-{member.uuid if member else uid}')
    uuids = sorted({str(u) for u in gift_card_uuids if u})
    if uuids:
        for card in GiftCard.query.filter(GiftCard.tenant_id == node.tenant_id, GiftCard.uuid.in_(uuids)) \
                .execution_options(skip_tenant_filter=True):
            left = giftcard_service.balance(card)
            if left < 0:
                warn(f'禮物卡 {card.masked_code} 在不同分店同時使用後餘額為 ${left} (同步前同時扣款)；請確認是否補收差額',
                     f'giftcard-negative-{card.uuid}')


def _in_flight_demands(node):
    """
    已在線上端成立 (含待付款)、但分店還沒收到的線上訂單佔用的份數 {menu_item_id: 份數}。
    分店收到訂單時才扣本機庫存並編取餐號 (取餐號同步回來前，線上端的取餐號為 0)。
    """
    from app.services.order_service import order_stock_demands
    orders = Order.query.filter(Order.tenant_id == node.tenant_id, Order.node_uuid == node.uuid,
                                Order.order_source == OrderSource.ONLINE, Order.status != OrderStatus.CANCELLED,
                                Order.payment_status.in_(('pending', 'paid')),
                                db.or_(Order.pickup_number.is_(None), Order.pickup_number == 0)) \
        .execution_options(skip_tenant_filter=True).all()
    held = {}
    for order in orders:
        for item_id, qty in order_stock_demands(order).items():
            held[item_id] = held.get(item_id, 0) + qty
    return held


def _save_stock_snapshot(node, stock):
    """
    分店回報的庫存：[{uuid, stock, is_sold_out}]，覆蓋線上端的庫存快照。
    回報值是分店「本機」的庫存，還沒送達分店的線上訂單並未扣在裡面，必須扣掉，
    否則線上已預扣的份數會被回報值蓋回來，同一份庫存被線上再賣一次 (超賣)。
    """
    if not isinstance(stock, list) or not stock:
        return
    uuids = [str(s.get('uuid')) for s in stock if isinstance(s, dict) and s.get('uuid')]
    ids = dict(db.session.execute(db.select(MenuItem.uuid, MenuItem.id).where(
        MenuItem.tenant_id == node.tenant_id, MenuItem.uuid.in_(uuids))).all())
    held = _in_flight_demands(node)
    now = datetime.now()
    for s in stock:
        item_id = ids.get(str(s.get('uuid'))) if isinstance(s, dict) else None
        if item_id is None:
            continue
        row = db.session.get(NodeStock, (node.id, item_id))
        if row is None:
            row = NodeStock(node_id=node.id, menu_item_id=item_id)
            db.session.add(row)
        row.stock = max(0, int(s.get('stock') or 0) - held.get(item_id, 0))
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
