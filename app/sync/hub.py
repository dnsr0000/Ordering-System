"""
線上端 (hub) 的同步 API：/api/sync/v1/*，以分店權杖 (Authorization: Bearer ...) 驗證。

- POST /hello   分店第一次連線：回傳分店與店家資訊、線上端接受上傳的資料表與欄位 (schema)
- POST /push    分店上傳變更 (含庫存快照；版本落差後可帶 backfill 補上新欄位)；
                回傳拒收的資料、合併的重複資料、線上缺少的餐點圖片、schema_hash
- GET  /pull    分店下載變更 (只含該分店需要的資料)，可長輪詢 (wait 秒) 等待新的線上訂單
- GET/PUT /media/menu/<路徑>  餐點圖片
"""
import hashlib
import io
import json
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
from app.sync.engine import _decode_value
from app.sync.registry import BY_NAME, SPECS, references, synced_columns
from app.tenant_context import tenant_bypass

PROTOCOL = 1
UNPAID = ('pending', 'expired')
# 帶有 order_id、可能與待付款的線上訂單在同一個結帳交易建立的資料表 (見 payment_ecpay._touch_order_rows)
ORDER_LINKED = frozenset({'reward_redemption', 'point_ledger', 'wallet_ledger', 'stamp_ledger', 'gift_card_ledger',
                          'pass_ledger'})
# 不會再出餐的付款狀態 (逾時未付 / 已取消需退款 / 已退款)：不佔分店庫存
NOT_SERVED = ('expired', 'refund_required', 'refunded')
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
        # 線上端接受上傳的資料表與欄位：分店先升級、線上端仍是舊版時，新欄位在線上端被略過 (分店游標照樣前進)；
        # 分店比對到線上端升級後多了欄位 / 資料表，會從頭重新上傳一次並帶 backfill (見 push)
        'schema': push_schema(), 'schema_hash': push_schema_hash(),
    })


def push_schema():
    """線上端接受上傳的資料表與欄位 {資料表: [欄位]}"""
    return {s.name: synced_columns(s) for s in SPECS if s.direction != 'down'}


def push_schema_hash():
    """push_schema 的摘要：每次 push 回應都附上，分店發現改變 (線上端已升級) 時再呼叫 /hello 取得完整結構"""
    raw = json.dumps(push_schema(), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _order_filter(node_uuid):
    def row_filter(spec, table):
        # 待付款 / 逾時未付款的線上訂單不送分店；付款完成時訂單與明細會重新標記 (payment_ecpay.mark_paid)
        if spec.name == 'order':
            return (table.c.node_uuid == node_uuid) & table.c.payment_status.notin_(UNPAID)
        if spec.name == 'order_item':
            orders = db.metadata.tables['order']
            return table.c.order_id.in_(db.select(orders.c.id).where(
                orders.c.node_uuid == node_uuid, orders.c.payment_status.notin_(UNPAID)))
        if spec.name in ORDER_LINKED:
            # 建單時一併寫入的兌換憑證 / 異動帳：待付款期間先送到分店時找不到訂單，order_id 會被存成空值，
            # 分店之後取消這張訂單就找不到要退回的點數 / 寄杯次數。等付款完成 (mark_paid 會重新標記這些列) 再跟著訂單送出
            orders = db.metadata.tables['order']
            return ~db.exists().where(orders.c.id == table.c.order_id, orders.c.payment_status.in_(UNPAID))
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
    _lock_stock_rows(node, stock)
    refunded_before = _online_refunded(node, changes.get('order'))
    applier = Applier(node.tenant_id, 'hub', source_node_uuid=node.uuid)
    result = applier.apply(changes)
    _backfill(node, applier, changes, payload.get('backfill'))
    recompute_points(result.touched_users)
    fixed = _undo_duplicate_referrals(node, result.touched_users) | _true_up_points_expiry(node, result.touched_users)
    fixed |= _claw_back_reused_vouchers(node, result.reused_vouchers)
    if fixed:
        recompute_points(fixed)
    from app.services import pass_service
    for mp in pass_service.recompute(result.touched_passes):   # 寄杯在兩家分店同步前同時用完
        from app.services import monitor_service
        monitor_service.alert(pass_service.negative_alert_message(mp), key=f'pass-negative-{mp.uuid}',
                              tenant_id=node.tenant_id, level='warning', source='sync')
    order_uuids = [r.get('uuid') for r in changes.get('order') or () if isinstance(r, dict)]
    _reconcile_coupon_use(node, order_uuids)
    _reconcile_public_code_use(node, order_uuids)
    _alert_negative_balances(node, set(result.touched_users) | fixed,
                             [r.get('gift_card_id') for r in changes.get('gift_card_ledger') or () if isinstance(r, dict)])
    for transfer, message in result.transfer_alerts:
        from app.services import monitor_service
        monitor_service.alert(message, key=f'transfer-{transfer.uuid}', tenant_id=node.tenant_id, level='warning',
                              source='sync')
    for order in result.cancelled_orders:  # 分店取消了已線上付款的訂單：線上端標記需退款
        flag_refund_if_paid(order.id)
    _adjust_online_invoices(refunded_before, result.cancelled_orders)
    _save_stock_snapshot(node, stock)
    _save_intake(node, payload.get('intake'))
    _save_own_delivery(node, payload.get('own_delivery'))
    db.session.commit()
    if result.orders_changed:
        order_event_bus.notify(node.tenant_id)
    missing = sorted(p for p in result.menu_images if not _menu_image_exists(p))
    return jsonify({'success': True, 'applied': result.applied, 'conflicts': result.conflicts,
                    'aliases': result.aliases, 'missing_media': missing, 'schema_hash': push_schema_hash()})


# 版本落差補值 (push 的 backfill)：安全相關的資料表 / 由專用合併規則控制的欄位不可經由補值直接覆寫
BACKFILL_DENIED_TABLES = frozenset({'staff_account', 'sync_tombstone'})
BACKFILL_PROTECTED = frozenset({'id', 'uuid', 'tenant_id', 'sync_rev', 'created_at', 'updated_at', 'node_uuid',
                                'status', 'payment_status', 'refunded_amount', 'total_price', 'quantity', 'voided_qty'})


def _backfill_allowed(node, spec_name, obj):
    """補值只能改本分店有權修改的列：訂單 / 明細限本分店的訂單，叫貨 / 調撥單限本分店是出貨或收貨方"""
    if spec_name == 'order':
        return obj.node_uuid == node.uuid
    if spec_name == 'order_item':
        return db.session.execute(db.select(Order.node_uuid).where(Order.id == obj.order_id)).scalar() == node.uuid
    if spec_name == 'stock_transfer':
        return node.uuid in (obj.from_site, obj.to_site)
    return True


def _backfill(node, applier, changes, columns):
    """
    分店先升級、線上端較晚升級：線上端舊版時略過的新欄位 (分店游標已前進，不會再送)。
    分店比對到線上端結構多了欄位後從頭重新上傳一次，並以 backfill = {資料表: [欄位]} 指出要補值的欄位；
    一般套用時 updated_at 相同會略過，這裡把這些欄位的值補進線上端既有的列 (線上端在這之後又改過的列不覆蓋)。
    補上的值會標記 sync_rev，其他分店之後也會收到。
    """
    if not isinstance(columns, dict) or not columns:
        return
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm.attributes import flag_modified
    rejected = {(c.get('table'), c.get('uuid')) for c in applier.result.conflicts}
    for name, cols in columns.items():
        spec, rows = BY_NAME.get(name), changes.get(name)
        if spec is None or spec.direction == 'down' or name in BACKFILL_DENIED_TABLES \
                or not isinstance(cols, list) or not rows:
            continue
        allowed = set(synced_columns(spec)) - BACKFILL_PROTECTED
        cols = [c for c in cols if isinstance(c, str) and c in allowed]
        if not cols:
            continue
        model, table, refs = spec.model, spec.model.__table__, references(spec)
        for data in rows:
            if not isinstance(data, dict) or not data.get('uuid'):
                continue
            uuid = applier.resolve(name, data['uuid'])
            if (name, uuid) in rejected or (name, data['uuid']) in rejected:
                continue
            values = {}
            for col in cols:
                if col not in data:
                    continue
                raw = data[col]
                if col in refs and raw is not None:
                    local = applier.local_id(refs[col], raw)
                    if local is None:
                        continue
                    values[col] = local
                else:
                    values[col] = None if col in refs else _decode_value(table.c[col], raw)
            if not values:
                continue
            obj = db.session.execute(db.select(model).where(model.uuid == uuid, model.tenant_id == node.tenant_id)
                                     .execution_options(skip_tenant_filter=True)).scalar_one_or_none()
            if obj is None or not _backfill_allowed(node, name, obj):
                continue
            if 'updated_at' in table.c and obj.updated_at is not None and data.get('updated_at'):
                if _decode_value(table.c.updated_at, data['updated_at']) < obj.updated_at:
                    continue   # 線上端在這之後又改過這一列：以線上端為準
            if applier._rejected(spec, obj, values):
                continue
            values = {k: v for k, v in values.items() if getattr(obj, k) != v}
            if not values:
                continue
            try:
                with db.session.begin_nested():
                    for key, value in values.items():
                        setattr(obj, key, value)
                    if 'updated_at' in table.c:
                        flag_modified(obj, 'updated_at')   # 保留原本的 updated_at
                    db.session.flush()
            except IntegrityError:
                applier.conflict(spec, uuid, '補上新欄位的值時與其他資料重複，未套用')


def _online_refunded(node, rows):
    """
    這次上傳的訂單中，由線上端收款 (發票由線上端開立) 的線上點餐訂單 {order_id: 套用前的已退款金額}。
    到店付款的線上訂單 (unpaid → 分店結帳 settled) 由分店收款、開發票並自行作廢，不在此列。
    """
    uuids = [str(r.get('uuid')) for r in rows or () if isinstance(r, dict) and r.get('uuid')]
    if not uuids:
        return {}
    return {oid: refunded or 0 for oid, refunded in db.session.execute(
        db.select(Order.id, Order.refunded_amount).where(
            Order.tenant_id == node.tenant_id, Order.uuid.in_(uuids), Order.node_uuid == node.uuid,
            Order.order_source == OrderSource.ONLINE, Order.payment_status.notin_(('unpaid', 'settled'))))}


def _adjust_online_invoices(refunded_before, cancelled_orders):
    """
    線上點餐的發票由線上端開立 (發票只上傳不下發，分店沒有這張發票)：分店取消或部分退款後同步上來時，
    由線上端作廢 / 折讓 (與本機取消、部分退款相同：payment_service._void_invoices / _refund_order_invoices)。
    """
    if not refunded_before:
        return
    from app.services import payment_service
    cancelled = {o.id for o in cancelled_orders}
    for order_id in sorted(refunded_before):
        order = db.session.get(Order, order_id)
        if order is None:
            continue
        delta = max(0, (order.refunded_amount or 0) - refunded_before[order_id])
        if order_id in cancelled:
            payment_service._void_invoices(order, delta)
        elif delta > 0:
            payment_service._refund_order_invoices(order, delta, '分店退款')


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


def _reconcile_public_code_use(node, order_uuids):
    """
    公開優惠代碼的「每位會員限用 N 次」在各主機只能看到本機的訂單：線上端下發給分店的訂單只限該分店出餐的，
    同一位會員在其他分店 (或線上點餐取餐選其他分店) 的使用次數，分店永遠算不到。
    線上端看得到所有分店上傳的訂單：收到使用公開代碼的會員訂單時比對未取消的次數，超過上限就通知店家。
    """
    from app.models.coupon import Coupon
    uuids = [u for u in order_uuids if u]
    if not uuids:
        return
    pairs = sorted(set(db.session.execute(db.select(Order.user_id, Order.coupon_code).where(
        Order.tenant_id == node.tenant_id, Order.uuid.in_(uuids), Order.user_id.isnot(None),
        Order.coupon_code.isnot(None), Order.coupon_code != '', Order.applied_user_coupon_id.is_(None))).all()))
    if not pairs:
        return
    places = None
    for user_id, code in pairs:
        coupon = Coupon.query.filter_by(tenant_id=node.tenant_id, code=code).first()
        if coupon is None or coupon.requires_holding or not coupon.uses_per_member:
            continue
        used = Order.query.filter(Order.tenant_id == node.tenant_id, Order.user_id == user_id,
                                  Order.coupon_code == code, Order.status != OrderStatus.CANCELLED,
                                  Order.applied_user_coupon_id.is_(None)) \
            .execution_options(skip_tenant_filter=True).order_by(Order.created_at, Order.id).all()
        if len(used) <= coupon.uses_per_member:
            continue
        from app.models.user import User
        from app.services import monitor_service
        if places is None:
            places = {n.uuid: n.name for n in SyncNode.query.filter_by(tenant_id=node.tenant_id)
                      .execution_options(skip_tenant_filter=True)}
        member = db.session.get(User, user_id)
        detail = '、'.join(f"#{o.id} ({'線上點餐' if o.order_source == OrderSource.ONLINE else places.get(o.node_uuid, '分店')})"
                          for o in used)
        monitor_service.alert(f"優惠代碼「{code}」每位會員限用 {coupon.uses_per_member} 次，會員 "
                              f"{member.name if member else user_id} 已在 {detail} 使用 {len(used)} 次；請確認是否補收差額",
                              key=f'promo-overuse-{code}-{user_id}', tenant_id=node.tenant_id, level='warning',
                              source='sync')


def _claw_back_reused_vouchers(node, vouchers):
    """
    紅利兌換憑證在兩台主機同步前一邊退點、一邊核銷 (合併後以已使用為準)：會員拿到商品，退回的點數也還在。
    由線上端扣回退回的點數 (最多扣到 0) 並通知店家。以訂單上已有的 voucher_reuse 異動判斷是否處理過
    (上傳回應遺失而重送同一頁時不重複扣點)。回傳點數有變動的會員 ID。
    """
    from app.models.sync import PointLedger
    from app.models.user import User
    from app.services import monitor_service
    from app.services.points_service import change_points
    seen, groups = set(), {}
    for v in vouchers:
        if v is None or v.uuid in seen or not v.user_id or not (v.points_cost or 0) > 0:
            continue
        seen.add(v.uuid)
        groups.setdefault((v.user_id, v.order_id), []).append(v)
    changed = set()
    for (user_id, order_id), items in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        done = db.session.execute(db.select(PointLedger.id).where(
            PointLedger.tenant_id == node.tenant_id, PointLedger.user_id == user_id,
            PointLedger.reason == 'voucher_reuse',
            (PointLedger.order_id == order_id) if order_id else PointLedger.order_id.is_(None)).limit(1)).first()
        if done is not None:
            continue
        member = db.session.get(User, user_id)
        for v in sorted(items, key=lambda x: x.id or 0):
            taken = change_points(node.tenant_id, user_id, -(v.points_cost or 0), 'voucher_reuse', order_id=order_id,
                                  clamp=True) or 0
            changed.add(user_id)
            monitor_service.alert(f'會員 {member.name if member else user_id} 的紅利兌換憑證在同步前於兩處同時退點與核銷，'
                                  f'已追回退回的 {-taken} 點'
                                  + (f' (應追回 {v.points_cost} 點，會員點數不足)' if -taken < v.points_cost else ''),
                                  key=f'voucher-reuse-{v.uuid}', tenant_id=node.tenant_id, level='warning', source='sync')
    return changed


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
    到店付款 (ONLINE_PAYMENT=0) 的訂單付款狀態不是 pending / paid，同樣佔用份數：只排除不會再出餐的狀態。
    """
    from app.services.order_service import order_stock_demands
    orders = Order.query.filter(Order.tenant_id == node.tenant_id, Order.node_uuid == node.uuid,
                                Order.order_source == OrderSource.ONLINE, Order.status != OrderStatus.CANCELLED,
                                Order.payment_status.notin_(NOT_SERVED),
                                db.or_(Order.pickup_number.is_(None), Order.pickup_number == 0)) \
        .execution_options(skip_tenant_filter=True).all()
    held = {}
    for order in orders:
        for item_id, qty in order_stock_demands(order).items():
            held[item_id] = held.get(item_id, 0) + qty
    return held


def _save_intake(node, intake):
    """分店回報的暫停接單狀態 (intake_service.node_pause)：線上點餐選到該分店時擋下"""
    if not isinstance(intake, dict):
        return
    raw = str(intake.get('paused_until') or '').strip()
    until = None
    if raw:
        try:
            until = datetime.strptime(raw[:16], '%Y-%m-%dT%H:%M')
        except ValueError:
            until = None
    node.paused_until = until
    node.paused_message = str(intake.get('message') or '')[:200] if until else ''
    # 分店生效中的自有外送設定摘要 (node._intake_snapshot 的 'delivery')；舊版分店不帶 = 維持原值 (未回報則沿用線上端設定)
    _save_own_delivery(node, _intake_delivery(intake.get('delivery')))


def _intake_delivery(delivery):
    """
    把 intake['delivery'] ({'enabled', 'online', 'zones': [{'name', 'fee', 'min_spend'}], 'free_over'})
    轉成 _save_own_delivery 的格式 (channels 清單、zones 為「名稱:外送費:最低消費」多行文字)；不是 dict 時回傳 None
    """
    if not isinstance(delivery, dict):
        return None
    lines = []
    for z in delivery.get('zones') if isinstance(delivery.get('zones'), list) else ():
        if not isinstance(z, dict):
            continue
        name = str(z.get('name') or '').replace(':', ' ').replace('：', ' ').replace('\r', ' ').replace('\n', ' ')
        name = name.strip()[:30]
        if not name:
            continue
        lines.append(f"{name}:{_bounded_int(z.get('fee'), 0, 100000, 0)}:"
                     f"{_bounded_int(z.get('min_spend'), 0, 100000, 0)}")
        if len(lines) >= 20:
            break
    return {'enabled': bool(delivery.get('enabled')), 'channels': ['online'] if delivery.get('online') else [],
            'zones': '\n'.join(lines), 'free_over': delivery.get('free_over')}


def _bounded_int(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _save_own_delivery(node, info):
    """
    分店回報的本店生效「自有外送」設定 (分店可存成本店自訂：關閉外送或改配送區域 / 費率)：
    存到 SyncNode.own_delivery_json，線上點餐選到該分店時依此驗證外送到府與計算外送費。
    只在有變動時回報 (未帶 = 維持上次的回報)。
    """
    if not isinstance(info, dict):
        return
    channels = info.get('channels')
    summary = {
        'enabled': bool(info.get('enabled')),
        'channels': [c for c in ('online', 'pos') if isinstance(channels, list) and c in channels],
        'zones': str(info.get('zones') or '')[:2000],
        'free_over': _bounded_int(info.get('free_over'), 0, 100000, 0),
        'eta_minutes': _bounded_int(info.get('eta_minutes'), 10, 240, 45),
    }
    node.own_delivery_json = json.dumps(summary, ensure_ascii=False, sort_keys=True)


def _stock_item_ids(node, stock):
    """分店回報的庫存中，本店家餐點的 {uuid: menu_item_id}"""
    uuids = [str(s.get('uuid')) for s in stock if isinstance(s, dict) and s.get('uuid')]
    return dict(db.session.execute(db.select(MenuItem.uuid, MenuItem.id).where(
        MenuItem.tenant_id == node.tenant_id, MenuItem.uuid.in_(uuids))).all())


def _lock_stock_rows(node, stock):
    """
    回報庫存前先鎖住這家分店要覆蓋的庫存快照列 (依品項排序，與結帳扣庫存的順序相同)：
    之後才算「還沒送達分店的線上訂單」佔用份數並以絕對值覆蓋，同時結帳的線上訂單不會夾在中間被蓋掉預扣。
    在套用其他變更 (會鎖會員等列) 之前執行，鎖的順序與結帳相同 (庫存 → 會員)，避免交叉鎖死。
    (SQLite 只有單一寫入者，FOR UPDATE 會被忽略)
    """
    if not isinstance(stock, list) or not stock:
        return
    item_ids = sorted(set(_stock_item_ids(node, stock).values()))
    if item_ids:
        db.session.execute(db.select(NodeStock.menu_item_id).where(
            NodeStock.node_id == node.id, NodeStock.menu_item_id.in_(item_ids))
            .order_by(NodeStock.menu_item_id).with_for_update()).all()


def _save_stock_snapshot(node, stock):
    """
    分店回報的庫存：[{uuid, stock, is_sold_out}]，覆蓋線上端的庫存快照。
    回報值是分店「本機」的庫存，還沒送達分店的線上訂單並未扣在裡面，必須扣掉，
    否則線上已預扣的份數會被回報值蓋回來，同一份庫存被線上再賣一次 (超賣)。
    """
    if not isinstance(stock, list) or not stock:
        return
    ids = _stock_item_ids(node, stock)
    held = _in_flight_demands(node)   # 庫存快照列已在 push 開頭鎖住 (_lock_stock_rows)
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
