"""
分店端 (node)：把本機變更上傳到線上端，並下載線上端的變更 (菜單、會員、其他分店的點數異動、線上訂單…)。

背景執行兩個執行緒 (伺服器啟動時自動開始，見 start_background_sync)：
- 上傳：本機有提交時立即觸發，另外每 SYNC_INTERVAL 秒檢查一次
- 下載：長輪詢線上端 (SYNC_LONG_POLL 秒)，有新的線上訂單時幾乎立刻收到
斷網時兩者只會記錄錯誤並稍後重試，門市營業不受影響；恢復連線後自動補齊。
"""
import hashlib
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests
import sqlalchemy as sa
from flask import current_app
from PIL import Image
from sqlalchemy.orm.attributes import set_committed_value

from app.extensions import db
from app.models.enums import OrderSource, OrderStatus
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.sync import SyncConflict
from app.models.tenant import Tenant
from app.models.user import User
from app.services.event_bus import order_event_bus
from app.services.points_service import recompute_points
from app.services.upload_paths import is_valid_member_photo, is_valid_menu_image, upload_file_path
from app.sync import runtime, tracking
from app.sync.engine import Applier, _decode_value, collect_changes, count_pending, merge_local_duplicate
from app.sync.registry import BY_NAME, SPECS, references, synced_columns

logger = logging.getLogger(__name__)

PUSH_SPECS = tuple(s for s in SPECS if s.direction != 'down')   # 總部設定只下發，不上傳
PAGE = 500


def _tag_local_member_photo_sources(tenant_id):
    """把有實體照片、但尚無來源標記的舊會員照片認領為本分店照片。"""
    node_uuid = runtime.local_node_uuid()
    if not node_uuid:
        return False
    member_dir = current_app.config['UPLOAD_FOLDER_MEMBER']
    changed = False
    users = User.query.filter(User.tenant_id == tenant_id, User.photo_path != '',
                              User.photo_source_node_uuid.is_(None)).yield_per(500)
    for user in users:
        if not is_valid_member_photo(user.photo_path):
            continue
        path = upload_file_path(member_dir, user.photo_path)
        if path and os.path.isfile(path):
            user.photo_source_node_uuid = node_uuid
            changed = True
    if changed:
        db.session.commit()
    return changed

_HELLO_OK = 'sync_hello_verified'   # app.extensions：本行程已向線上端確認過分店身分
SCHEMA_KEY = 'sync_schema'          # sync_state：上次記錄的本機同步結構 {資料表: [欄位]}
BACKFILL_KEY = 'sync_backfill'      # sync_state：版本更新後重新下載時要補值的新欄位 {資料表: [欄位]}
HUB_SCHEMA_KEY = 'hub_schema'       # sync_state：上次記錄的線上端接受上傳的結構 {資料表: [欄位]}
PUSH_BACKFILL_KEY = 'sync_push_backfill'   # sync_state：線上端升級後重新上傳時要補值的新欄位 {資料表: [欄位]}
PULL_RETRY_KEY = 'pull_retry'       # sync_state：下載後無法套用、待下次重試的列
PULL_RETRY_MAX_ROWS = 200
PULL_RETRY_MAX_TRIES = 20
MEDIA_DOWN_KEY = 'media_download'   # sync_state：待補下載的餐點圖片 {路徑: [失敗次數, 下次重試時間]}
MEDIA_UP_KEY = 'media_upload'       # sync_state：待補上傳的餐點圖片
MEDIA_MAX = 500
MEDIA_PER_ROUND = 20
MEDIA_MAX_TRIES = 30
MEDIA_BACKOFF, MEDIA_BACKOFF_MAX = 60, 3600


class SyncError(Exception):
    pass


class HttpTransport:
    """以 HTTPS 呼叫線上端的同步 API"""

    def __init__(self, base_url, token, timeout=30):
        self.base_url = base_url.rstrip('/') + '/api/sync/v1'
        self.http = requests.Session()
        self.http.headers['Authorization'] = f'Bearer {token}'
        self.timeout = timeout

    def call(self, method, path, params=None, json_body=None, data=None, timeout=None, raw=False):
        try:
            resp = self.http.request(method, self.base_url + path, params=params, json=json_body, data=data,
                                     timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise SyncError(f'無法連線到線上端：{exc}') from exc
        if raw and resp.ok:
            return resp.content
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not resp.ok or body.get('success') is False:
            raise SyncError(body.get('message') or f'線上端回應 HTTP {resp.status_code}')
        return body


def make_transport(app):
    cfg = app.config
    if not cfg.get('SYNC_HUB_URL') or not cfg.get('SYNC_NODE_TOKEN'):
        raise SyncError('尚未設定 SYNC_HUB_URL / SYNC_NODE_TOKEN')
    return HttpTransport(cfg['SYNC_HUB_URL'], cfg['SYNC_NODE_TOKEN'], timeout=max(30, cfg['SYNC_LONG_POLL'] + 15))


def _json_state(key, default):
    """讀取 sync_state 中的 JSON 值 (格式不符時回傳 default)"""
    try:
        value = json.loads(runtime.get_state(key) or 'null')
    except ValueError:
        value = None
    return value if isinstance(value, type(default)) else default


def _set_json_state(key, value):
    if value or runtime.get_state(key):
        runtime.set_state(key, json.dumps(value, sort_keys=True, ensure_ascii=False) if value else '')


def _record_conflict(table_name, row_uuid, reason):
    """記錄同步衝突 (同一列同一原因只留一筆，再次發生時更新時間)：上傳卡住重送時不會重複寫入洗版"""
    table_name, row_uuid, reason = str(table_name or '')[:50], str(row_uuid or '')[:36], str(reason or '')[:300]
    existing = SyncConflict.query.filter_by(table_name=table_name, row_uuid=row_uuid, reason=reason).first()
    if existing is not None:
        existing.created_at = datetime.now()
    else:
        db.session.add(SyncConflict(table_name=table_name, row_uuid=row_uuid, reason=reason))


class NodeSync:
    """一次同步作業 (需在 app context 內使用)"""

    def __init__(self, transport):
        self.transport = transport
        self._hub_schema_changed = False   # push 回應的 schema_hash 與記錄的不同 (線上端已升級)

    # ---- 基本資料 ----
    def tenant(self):
        slug = current_app.config.get('SYNC_TENANT_SLUG', 'default')
        tenant = Tenant.query.filter_by(slug=slug).first()
        if tenant is None:
            raise SyncError(f'找不到本機店家 {slug}')
        return tenant

    def ensure_hello(self, force=False):
        """
        取得本分店在線上端的 uuid，並讓本機店家使用線上店家的 uuid。
        每個行程第一次同步時都向線上端確認一次身分 (之後沿用)：權杖被換成其他店家或同店家另一筆分店時停止同步，
        不可沿用舊的分店 uuid 與游標，把資料寫進別的店家 / 分店。
        """
        if runtime.get_state('node_uuid') and not force and current_app.extensions.get(_HELLO_OK):
            return
        info = self.transport.call('POST', '/hello')
        tenant = self.tenant()
        hub_tenant, hub_node = info['tenant']['uuid'], info['node']['uuid']
        known = runtime.get_state('hub_tenant_uuid')
        if known and known != hub_tenant:
            raise SyncError('此分店先前已連線到另一個線上店家，請勿更換權杖 (如需更換，請先備份並清空同步狀態)')
        known_node = runtime.get_state('node_uuid')
        if known_node and known_node != hub_node:
            raise SyncError('同步權杖屬於另一筆分店，與此主機先前連線的分店不同，已停止同步。要沿用原分店請在線上端分店管理'
                            '「重新產生權杖」；確定要改為另一筆分店，請先備份並清空同步狀態')
        if tenant.uuid != hub_tenant:
            db.session.execute(sa.update(Tenant.__table__).where(Tenant.__table__.c.id == tenant.id)
                               .values(uuid=hub_tenant))
        runtime.set_state('node_uuid', hub_node)
        runtime.set_state('node_name', info['node']['name'])
        runtime.set_state('hub_tenant_uuid', hub_tenant)
        runtime.set_state('hub_tenant_name', info['tenant']['name'])
        self._check_schema()
        self._check_hub_schema(info)
        db.session.commit()
        current_app.extensions[_HELLO_OK] = True

    def _check_schema(self):
        """
        本機同步結構 (下載的資料表與欄位) 與上次記錄的不同 (版本更新新增了同步欄位 / 資料表)：從頭重新下載一次，
        並把新欄位在線上端的值補進本機既有的列。否則版本落差期間線上端寫入的新欄位 / 新資料表的列，
        舊版下載時被略過、游標卻已前進，升級後永遠不會再收到。
        """
        current = {s.name: synced_columns(s) for s in SPECS if s.direction != 'up'}
        stored = _json_state(SCHEMA_KEY, {})
        if stored:
            added = {name: [c for c in cols if c not in (stored.get(name) or ())]
                     for name, cols in current.items() if name in stored}
            added = {name: cols for name, cols in added.items() if cols}
            if added or any(name not in stored for name in current):
                pending = _json_state(BACKFILL_KEY, {})
                for name, cols in added.items():
                    pending[name] = sorted(set(pending.get(name) or ()) | set(cols))
                _set_json_state(BACKFILL_KEY, pending)
                runtime.set_state('pull_cursor', -1)
                logger.info('同步資料結構已更新 (新增欄位 %s)，將從頭重新下載一次', added)
        if stored != current:
            runtime.set_state(SCHEMA_KEY, json.dumps(current, sort_keys=True))

    def _check_hub_schema(self, info):
        """
        反方向的版本落差 (分店先升級、線上端較晚升級)：線上端舊版時略過了分店上傳的新欄位 / 新資料表，
        分店游標卻已前進。比對線上端接受上傳的結構 (/hello 的 schema) 與上次記錄的不同時，從頭重新上傳一次，
        並帶 backfill 讓線上端把新欄位補進既有的列。第一次記錄時只存，不重新上傳。
        """
        hub = info.get('schema')
        if not isinstance(hub, dict):
            return   # 舊版線上端沒有回報結構
        stored = _json_state(HUB_SCHEMA_KEY, {})
        local = {s.name: synced_columns(s) for s in SPECS if s.direction != 'down'}
        if stored:
            added = {name: [c for c in local[name] if c in (hub.get(name) or ()) and c not in (stored.get(name) or ())]
                     for name in local if name in hub and name in stored}
            added = {name: cols for name, cols in added.items() if cols}
            if added or any(name in hub and name not in stored for name in local):
                pending = _json_state(PUSH_BACKFILL_KEY, {})
                for name, cols in added.items():
                    pending[name] = sorted(set(pending.get(name) or ()) | set(cols))
                _set_json_state(PUSH_BACKFILL_KEY, pending)
                runtime.set_state('push_cursor', -1)
                logger.info('線上端同步資料結構已更新 (新增欄位 %s)，將從頭重新上傳一次', added)
        if stored != hub:
            runtime.set_state(HUB_SCHEMA_KEY, json.dumps(hub, sort_keys=True))
        hub_hash = info.get('schema_hash')
        if hub_hash and hub_hash != runtime.get_state('hub_schema_hash'):
            runtime.set_state('hub_schema_hash', hub_hash)

    # ---- 上傳 ----
    def push(self):
        tenant_id = self.tenant().id
        _tag_local_member_photo_sources(tenant_id)
        self._sync_media(MEDIA_UP_KEY, self._try_upload)   # 先補傳先前上傳失敗的餐點圖片
        cursor = int(runtime.get_state('push_cursor', '-1') or -1)
        upto = tracking.current_rev(db.session)
        stock, stock_hash = self._stock_snapshot(tenant_id)
        send_stock = stock_hash != runtime.get_state('stock_hash')
        intake, intake_hash = self._intake_snapshot(tenant_id)
        send_intake = intake_hash != runtime.get_state('intake_hash')
        delivery, delivery_hash = self._own_delivery_snapshot(tenant_id)
        send_delivery = delivery_hash != runtime.get_state('own_delivery_hash')
        # 線上端較晚升級後的完整重新上傳：每一頁都帶要補值的新欄位 (見 _check_hub_schema)
        backfill = _json_state(PUSH_BACKFILL_KEY, {}) if cursor == -1 else {}
        self._hub_schema_changed = False
        position, sent = (0, 0), 0
        while True:
            changes, nxt = collect_changes(tenant_id, PUSH_SPECS, cursor, upto, position, PAGE)
            db.session.rollback()
            # 庫存隨最後一頁送出：線上端先套用本分店已收到的線上訂單 (取餐號)，再以回報值扣掉尚未送達的線上訂單
            last = nxt is None
            if changes or ((send_stock or send_intake or send_delivery) and last):
                payload = {'changes': changes, 'stock': stock if send_stock and last else None,
                           'intake': intake if send_intake and last else None,
                           'own_delivery': delivery if send_delivery and last else None}
                if backfill:
                    payload['backfill'] = backfill
                body = self.transport.call('POST', '/push', json_body=payload)
                sent += sum(len(v) for v in changes.values())
                self._handle_push_response(tenant_id, body)
            if nxt is None:
                break
            position = nxt
        runtime.set_state('push_cursor', upto)
        if backfill:
            runtime.set_state(PUSH_BACKFILL_KEY, '')
        runtime.set_state('stock_hash', stock_hash)
        runtime.set_state('intake_hash', intake_hash)
        runtime.set_state('own_delivery_hash', delivery_hash)
        runtime.set_state('last_push_at', datetime.now().isoformat(timespec='seconds'))
        db.session.commit()
        if self._hub_schema_changed:   # 線上端在本行程執行中升級：重新取得線上端結構比對 (可能觸發完整重新上傳)
            self.ensure_hello(force=True)
        return sent

    def _stock_snapshot(self, tenant_id):
        rows = db.session.execute(sa.select(MenuItem.uuid, MenuItem.stock, MenuItem.is_sold_out)
                                  .where(MenuItem.tenant_id == tenant_id).order_by(MenuItem.id)).all()
        stock = [{'uuid': u, 'stock': int(s or 0), 'is_sold_out': bool(so)} for u, s, so in rows]
        digest = hashlib.sha256(json.dumps(stock, sort_keys=True).encode()).hexdigest()
        return stock, digest

    def _intake_snapshot(self, tenant_id):
        """
        本分店的暫停接單狀態 (intake_service) 與生效中的自有外送設定摘要：有變動時隨同步上傳，
        線上點餐選到本分店時擋下 / 依本分店的外送設定驗證與計費 (分店可把總部下發的 own_delivery 存成本店自訂)。
        """
        from app.services import intake_service, own_delivery_service
        until, message = intake_service.node_pause(tenant_id)
        cfg = own_delivery_service.config(tenant_id)
        delivery = {'enabled': bool(cfg.get('enabled')), 'online': 'online' in (cfg.get('channels') or []),
                    'zones': own_delivery_service.zones(tenant_id), 'free_over': int(cfg.get('free_over') or 0)}
        intake = {'paused_until': until.strftime('%Y-%m-%dT%H:%M') if until else '', 'message': message,
                  'delivery': delivery}
        return intake, hashlib.sha256(json.dumps(intake, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _own_delivery_snapshot(tenant_id):
        """
        本分店生效的自有外送設定 (分店可存成本店自訂)：有變動時隨同步上傳 (push 的 own_delivery)，
        線上端存到 SyncNode.own_delivery_json，線上點餐選到本分店時依此驗證外送到府與計算外送費
        """
        from app.services import own_delivery_service
        cfg = own_delivery_service.config(tenant_id)
        info = {'enabled': bool(cfg.get('enabled')), 'channels': list(cfg.get('channels') or []),
                'zones': str(cfg.get('zones') or ''), 'free_over': int(cfg.get('free_over') or 0),
                'eta_minutes': int(cfg.get('eta_minutes') or 45)}
        return info, hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()

    def _handle_push_response(self, tenant_id, body):
        hub_hash = body.get('schema_hash')
        if hub_hash and hub_hash != runtime.get_state('hub_schema_hash'):
            self._hub_schema_changed = True
        for c in body.get('conflicts') or ():
            _record_conflict(c.get('table', ''), c.get('uuid', ''), c.get('reason', ''))
        # 線上端缺少的圖片先記入待補傳清單再上傳：上傳失敗 (或回應遺失) 時重送同一頁，線上端不會再回報 missing_media
        missing = sorted({p for p in body.get('missing_media') or () if isinstance(p, str)})
        self._queue_media(MEDIA_UP_KEY, missing)   # 與衝突、別名一起提交
        touched = set()
        with tracking.suppressed(db.session):
            for a in body.get('aliases') or ():
                touched |= merge_local_duplicate(tenant_id, a['table'], a['uuid'], a['canonical'])
            recompute_points(touched)
        db.session.commit()
        self._sync_media(MEDIA_UP_KEY, self._try_upload, only=missing)

    # ---- 下載 ----
    def pull(self, wait=0):
        tenant_id = self.tenant().id
        my_node = runtime.get_state('node_uuid')
        params = {'cursor': int(runtime.get_state('pull_cursor', '-1') or -1), 'limit': PAGE, 'wait': wait}
        backfill = _json_state(BACKFILL_KEY, {}) if params['cursor'] == -1 else {}
        received, new_ids, retry = 0, set(), {}
        while True:
            body = self.transport.call('GET', '/pull', params=params)
            changes = body.get('changes') or {}
            if changes:
                received += sum(len(v) for v in changes.values())
                result = self._apply(tenant_id, my_node, changes, new_ids, backfill)
                self._hold_for_retry(changes, result.conflicts, retry)
            nxt = body.get('next') or {}
            if not body.get('has_more'):
                if received:
                    self._retry_rows(tenant_id, my_node, retry, new_ids)
                # 整輪 (所有分頁) 套用完才處理新到的線上訂單：訂單與明細可能被切在不同頁
                self._receive_orders(tenant_id, my_node, new_ids)
                runtime.set_state('pull_cursor', nxt.get('cursor', params['cursor']))
                runtime.set_state('last_pull_at', datetime.now().isoformat(timespec='seconds'))
                if backfill:
                    runtime.set_state(BACKFILL_KEY, '')
                db.session.commit()
                self._sync_media(MEDIA_DOWN_KEY, self._download_menu_image)   # 補下載先前失敗的餐點圖片
                return received
            params = {**nxt, 'limit': PAGE, 'wait': 0}

    def _apply(self, tenant_id, my_node, changes, new_ids, backfill=None):
        from app.services import inventory_service, settings_service
        from app.services.order_service import _change_stock, flag_refund_if_paid
        from app.services.order_stock import restock_demands
        with tracking.suppressed(db.session):
            result = Applier(tenant_id, 'node').apply(changes)
            if backfill:
                self._backfill(tenant_id, changes, backfill)
        self._bump_feed_rev(result)
        new_ids.update(o.id for o in result.new_orders)
        # 以下是本分店因此產生的變更 (庫存)，正常標記並在下次上傳
        restock = bool(result.cancelled_orders) and settings_service.get('refund', tenant_id).get('restock_on_cancel', True)
        for order in result.cancelled_orders:
            if restock and order.id not in new_ids and runtime.owns_stock(order) and self._stock_taken(order):
                _change_stock(tenant_id, restock_demands(order), +1)   # 只回補送達時實際扣到的份數
                inventory_service.restore(tenant_id, order.id)
            flag_refund_if_paid(order.id)  # 已在櫃檯結帳的訂單被線上端取消：標記需退款
        recompute_points(result.touched_users)
        from app.services import pass_service
        pass_service.recompute(result.touched_passes)
        images = sorted(p for p in result.menu_images if self._local_image(p) and not os.path.isfile(self._local_image(p)))
        self._queue_media(MEDIA_DOWN_KEY, images)   # 先記入待下載清單：下載失敗 (線上端還沒收到圖片等) 時之後重試
        db.session.commit()
        for transfer, message in result.transfer_alerts:   # 叫貨單：本店取消時對方已出貨等
            from app.services import monitor_service
            monitor_service.alert(message, key=f'transfer-{transfer.uuid}', tenant_id=tenant_id, level='warning',
                                  source='sync')
        if result.orders_changed:
            order_event_bus.notify(tenant_id)
        self._sync_media(MEDIA_DOWN_KEY, self._download_menu_image, only=images)
        return result

    @staticmethod
    def _bump_feed_rev(result):
        """
        從線上端套用進來的訂單異動 (新單、取消、退款、付款 / 外送狀態) 在 suppressed 區塊內不會標記 sync_rev，
        API v1 的 cursor 異動追蹤因此收不到：另外更新本機專用的 Order.feed_rev。
        用 Core 的 Table update (不標記 sync_rev、不改 updated_at)，否則這些資料會被當成本機變更傳回線上端。
        """
        table = Order.__table__
        ids = set(getattr(result, 'changed_order_ids', None) or ()) | {o.id for o in result.new_orders}
        ids.discard(None)
        if not ids:
            return
        rev = tracking.next_rev(db.session)
        db.session.execute(sa.update(table).where(table.c.id.in_(ids))
                           .values(feed_rev=rev, updated_at=table.c.updated_at))

    @staticmethod
    def _stock_taken(order):
        """本機是否扣過這張訂單的庫存：線上訂單在分店收到 (編取餐號) 時才扣，尚未編號的不可回補"""
        return order.order_source != OrderSource.ONLINE or (order.pickup_number or 0) > 0

    def _backfill(self, tenant_id, changes, columns):
        """版本更新後的完整重新下載：把新增同步欄位的值補進本機既有的列 (updated_at 相同時一般套用會略過)"""
        applier = Applier(tenant_id, 'node')
        for name, cols in columns.items():
            spec, rows = BY_NAME.get(name), changes.get(name)
            if spec is None or not rows:
                continue
            table = spec.model.__table__
            refs = references(spec)
            cols = [c for c in cols if c in table.c and c not in ('id', 'uuid', 'tenant_id', 'sync_rev')]
            for data in rows:
                values = {}
                for col in cols:
                    if col not in data:
                        continue
                    raw = data[col]
                    if col in refs and raw is not None:
                        local = applier.local_id(refs[col], raw)
                        if local is None and not table.c[col].nullable:
                            continue
                        values[col] = local
                    else:
                        values[col] = None if col in refs else _decode_value(table.c[col], raw)
                if not values or not data.get('uuid'):
                    continue
                stmt = sa.update(table).where(table.c.uuid == data['uuid'], table.c.tenant_id == tenant_id)
                if 'updated_at' in table.c:
                    incoming_at = data.get('updated_at')
                    if incoming_at:   # 本機在這之後又改過的列不覆蓋
                        stmt = stmt.where(sa.or_(table.c.updated_at.is_(None), table.c.updated_at <= _decode_value(
                            table.c.updated_at, incoming_at)))
                    values['updated_at'] = table.c.updated_at
                try:
                    with db.session.begin_nested():
                        db.session.execute(stmt.values(**values))
                except sa.exc.IntegrityError:   # 補值會撞到唯一鍵：保留本機值 (不讓整輪下載失敗)
                    logger.warning('補上 %s %s 的新欄位時與其他資料衝突，已略過', name, data['uuid'])

    # ---- 無法套用的列：整輪下載完再重試 ----
    @staticmethod
    def _hold_for_retry(changes, conflicts, retry):
        """
        記下這一頁無法套用的列 (自然鍵撞到同批稍後才刪除的資料、關聯資料在後面的分頁…)。
        游標照常前進 (這些列之後不會重送)，所以整輪下載完要再套用一次，仍失敗才記錄衝突並留待下次重試。
        """
        for c in conflicts:
            name, uuid = c.get('table'), c.get('uuid')
            if not uuid or name == 'sync_tombstone':
                continue
            row = next((r for r in changes.get(name) or () if isinstance(r, dict) and r.get('uuid') == uuid), None)
            if row is not None:
                retry[(name, uuid)] = {'table': name, 'row': row, 'reason': str(c.get('reason', '')), 'tries': 0}

    def _retry_rows(self, tenant_id, my_node, retry, new_ids):
        pending = {}
        for item in _json_state(PULL_RETRY_KEY, []):
            if isinstance(item, dict) and isinstance(item.get('row'), dict) and item.get('table'):
                pending[(item['table'], item['row'].get('uuid'))] = item
        pending.update(retry)   # 同一列這一輪又收到時以這一輪的內容為準
        if not pending:
            return
        changes = {}
        for (name, _uuid), item in pending.items():
            changes.setdefault(name, []).append(item['row'])
        result = self._apply(tenant_id, my_node, changes, new_ids)
        failed = {(c.get('table'), c.get('uuid')): str(c.get('reason', '')) for c in result.conflicts}
        keep = []
        for key, item in pending.items():
            if key not in failed:
                continue
            item['reason'], item['tries'] = failed[key], int(item.get('tries') or 0) + 1
            _record_conflict(key[0], key[1], f'線上端資料無法套用：{failed[key]}')
            if item['tries'] < PULL_RETRY_MAX_TRIES:
                keep.append(item)
        _set_json_state(PULL_RETRY_KEY, keep[-PULL_RETRY_MAX_ROWS:])
        db.session.commit()

    # ---- 新到的線上訂單 ----
    def _receive_orders(self, tenant_id, my_node, new_ids):
        """
        本分店新收到的線上訂單：編取餐號、扣庫存 (含原料)、估計完成時間、庫存不足通知、自動出單。
        只處理「線上點餐、製作中、尚未編號」的訂單 (換新主機重新下載的歷史訂單、門市自己的訂單都不處理)；
        也會補處理前一輪下載中斷而沒處理到的訂單 (最近一天內建立或付款的)。
        """
        from app.services.order_service import consume_stock_unchecked, next_pickup_number, order_stock_demands
        if not my_node:
            return
        recent = datetime.now() - timedelta(days=1)
        stmt = sa.select(Order).where(
            Order.tenant_id == tenant_id, Order.node_uuid == my_node, Order.order_source == OrderSource.ONLINE,
            Order.status == OrderStatus.PENDING, sa.or_(Order.pickup_number.is_(None), Order.pickup_number == 0))
        orders = [o for o in db.session.execute(stmt.order_by(Order.id).execution_options(skip_tenant_filter=True))
                  .scalars() if o.id in new_ids or (o.created_at and o.created_at >= recent)
                  or (o.paid_at and o.paid_at >= recent)]
        if not orders:
            return
        received, shortages = [], []
        for idx, order in enumerate(orders):
            number = self._assign_pickup_number(tenant_id, order, next_pickup_number)
            if number is None:
                continue   # 另一個同步作業已處理
            demands = order_stock_demands(order)
            # 記錄沒扣到的份數：之後取消 (分店或線上端) 時只回補實際扣到的份數 (restock_demands)
            short = consume_stock_unchecked(tenant_id, demands, order_uuid=order.uuid)
            short_ingredient = self._consume_ingredients(tenant_id, order, demands)
            if order.estimated_ready_at is None:   # 線上訂單到分店才知道排隊狀況：此時估算預計完成時間
                from app.services import eta_service
                # 同一批排在後面、尚未估算的新單不算進這張的排隊量 (訂單本身由 ready_at_for_order 排除)
                order.estimated_ready_at = eta_service.ready_at_for_order(
                    order, exclude_ids={o.id for o in orders[idx + 1:]})
            if short or short_ingredient:
                shortages.append((order, short, short_ingredient))
            received.append(order)
        db.session.commit()
        for order, short, short_ingredient in shortages:
            # 線上端以較舊的庫存快照接單 (分店現場先賣掉、尚未回報)：訂單照常成立，通知店員聯絡顧客或改換餐點
            from app.services import monitor_service
            detail = '、'.join(f'{name} (需 {need}，剩 {had})' for name, need, had in short)
            if short_ingredient:
                detail = '、'.join(filter(None, (detail, f'原料未扣 ({short_ingredient})')))
            monitor_service.alert(f'線上訂單 #{order.id} (取餐 #{order.pickup_number:03d}) 送達分店時庫存不足：{detail}；'
                                  f'請聯絡顧客改換餐點或取消退款', key=f'online-short-{order.uuid}', tenant_id=tenant_id,
                                  level='warning', source='sync')
        if received:
            order_event_bus.notify(tenant_id)
        self._print_orders(tenant_id, [o.id for o in received])

    @staticmethod
    def _assign_pickup_number(tenant_id, order, next_pickup_number):
        """
        以條件式 UPDATE 編取餐號 (同時有手動同步與背景同步時，只有一邊會處理這張訂單)；已被處理時回傳 None。
        預約之後營業日取餐的訂單依取餐日的計數編號：取餐當天現場訂單沿用同一列計數往後編，不會撞號。
        """
        from app.services.order_stock import business_day
        day = business_day(order.pickup_at) if order.pickup_at is not None else None
        number = next_pickup_number(tenant_id, business_date=day if day and day > business_day() else None)
        claimed = db.session.execute(
            sa.update(Order).where(Order.id == order.id, sa.or_(Order.pickup_number.is_(None), Order.pickup_number == 0))
            .values(pickup_number=number).execution_options(synchronize_session=False)).rowcount
        if not claimed:
            return None
        set_committed_value(order, 'pickup_number', number)
        return number

    @staticmethod
    def _consume_ingredients(tenant_id, order, demands):
        """依配方扣原料 (訂單已在線上成立，原料不足也照扣，庫存可為負數)"""
        from app.services import inventory_service
        inventory_service.consume(tenant_id, order.id, demands, block=False)
        return ''

    @staticmethod
    def _print_orders(tenant_id, order_ids):
        """依分店的「自動列印廚房單 / 收據」設定出單 (到店付款的訂單付款時才印收據)"""
        from app.services import print_service
        for order_id in order_ids:
            try:
                order = db.session.get(Order, order_id)
                receipt = False if order is not None and order.payment_status == 'unpaid' else None
                print_service.print_order(order_id, tenant_id, receipt=receipt)
            except Exception:  # noqa: BLE001  (出單失敗不影響同步)
                db.session.rollback()
                logger.exception('線上訂單 #%s 自動出單失敗', order_id)

    # ---- 餐點圖片 ----
    def _local_image(self, rel_path):
        if not is_valid_menu_image(rel_path):
            return None
        return upload_file_path(current_app.config['UPLOAD_FOLDER_MENU'], rel_path)

    def _download_menu_image(self, rel_path):
        """下載餐點圖片；回傳 False 表示稍後重試 (線上端尚未收到圖片、暫時連不上…)"""
        path = self._local_image(rel_path)
        if not path or os.path.isfile(path):
            return True
        try:
            data = self.transport.call('GET', f'/media/menu/{rel_path}', raw=True)
        except SyncError as exc:
            logger.warning('下載餐點圖片 %s 失敗 (稍後重試)：%s', rel_path, exc)
            return False
        try:  # 只寫入真正的圖片 (此資料夾會公開提供下載)
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
        except Exception:  # noqa: BLE001
            logger.warning('線上端回傳的餐點圖片 %s 不是有效的圖片，已略過 (稍後重試)', rel_path)
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + '.part', 'wb') as fh:
            fh.write(data)
        os.replace(path + '.part', path)
        return True

    def _try_upload(self, rel_path):
        """上傳餐點圖片；回傳 False 表示稍後重試"""
        path = self._local_image(rel_path)
        if not path or not os.path.isfile(path):
            return True
        try:
            with open(path, 'rb') as fh:
                self.transport.call('PUT', f'/media/menu/{rel_path}', data=fh.read())
        except SyncError as exc:
            logger.warning('上傳餐點圖片 %s 失敗 (稍後重試)：%s', rel_path, exc)
            return False
        return True

    @staticmethod
    def _media_queue(key):
        return {p: v for p, v in _json_state(key, {}).items()
                if isinstance(p, str) and isinstance(v, list) and len(v) == 2}

    def _queue_media(self, key, paths):
        if not paths:
            return
        queue = self._media_queue(key)
        for rel_path in paths:
            queue.setdefault(rel_path, [0, 0])
        _set_json_state(key, dict(sorted(queue.items(), key=lambda kv: kv[1][1])[:MEDIA_MAX]))

    def _sync_media(self, key, transfer, only=None):
        """
        待補下載 / 待補上傳的餐點圖片：到期的逐一重試 (only 指定時立即處理這些)，成功或已不需要
        (本機已沒有餐點使用這張圖片) 時移除；失敗則延後重試，多次失敗後放棄。
        """
        queue = self._media_queue(key)
        if not queue:
            return
        now = time.time()
        due = sorted(p for p, (_, at) in queue.items() if (p in only if only is not None else at <= now))
        if not due:
            return
        for rel_path in due[:MEDIA_PER_ROUND]:
            in_use = db.session.execute(sa.select(MenuItem.id).where(MenuItem.image_path == rel_path).limit(1)
                                        .execution_options(skip_tenant_filter=True)).first() is not None
            if not in_use or transfer(rel_path):
                queue.pop(rel_path, None)
                continue
            tries = queue[rel_path][0] + 1
            if tries >= MEDIA_MAX_TRIES:
                logger.warning('餐點圖片 %s 多次同步失敗，已放棄', rel_path)
                queue.pop(rel_path, None)
            else:
                queue[rel_path] = [tries, now + min(MEDIA_BACKOFF * 2 ** (tries - 1), MEDIA_BACKOFF_MAX)]
        _set_json_state(key, queue)
        db.session.commit()

    # ---- 狀態 ----
    def run_once(self):
        self.ensure_hello()
        pushed = self.push()
        pulled = self.pull()
        return pushed, pulled


def sync_status():
    """同步狀態頁 / CLI 顯示用"""
    tenant = Tenant.query.filter_by(slug=current_app.config.get('SYNC_TENANT_SLUG', 'default')).first()
    cursor = int(runtime.get_state('push_cursor', '-1') or -1)
    return {
        'role': runtime.role(),
        'hub_url': current_app.config.get('SYNC_HUB_URL', ''),
        'node_uuid': runtime.get_state('node_uuid'),
        'node_name': runtime.get_state('node_name'),
        'hub_tenant_name': runtime.get_state('hub_tenant_name'),
        'last_push_at': runtime.get_state('last_push_at'),
        'last_pull_at': runtime.get_state('last_pull_at'),
        'last_error': runtime.get_state('last_error'),
        'last_error_at': runtime.get_state('last_error_at'),
        'last_push_error': runtime.get_state('last_push_error'),
        'last_pull_error': runtime.get_state('last_pull_error'),
        'pending_changes': count_pending(tenant.id, PUSH_SPECS, cursor) if tenant else 0,
        'conflicts': [{'table': c.table_name, 'uuid': c.row_uuid, 'reason': c.reason,
                       'created_at': c.created_at.strftime('%Y-%m-%d %H:%M')}
                      for c in SyncConflict.query.order_by(SyncConflict.id.desc()).limit(50)],
        # 已取消的線上訂單不會編號，不算在等待中
        'online_orders_waiting': Order.query.filter(Order.order_source == 'Online', Order.pickup_number == 0,
                                                    Order.status != OrderStatus.CANCELLED).count()
        if tenant else 0,
    }


_KINDS = ('push', 'pull')


def record_error(exc, kind=None):
    """記錄同步失敗；kind ('push' / 'pull') 指定時另外分開記錄，只在該方向成功時清除"""
    db.session.rollback()
    message, at = str(exc)[:500], datetime.now().isoformat(timespec='seconds')
    runtime.set_state('last_error', message)
    runtime.set_state('last_error_at', at)
    if kind in _KINDS:
        runtime.set_state(f'last_{kind}_error', message)
        runtime.set_state(f'last_{kind}_error_at', at)
    db.session.commit()


def record_ok(kind=None):
    """
    同步成功：清除該方向的錯誤 (kind 為 None 時清除全部)。另一個方向仍在失敗時繼續顯示它的錯誤，
    不會被下載長輪詢的成功立刻清掉 (上傳持續失敗時同步狀態頁看得到)。
    """
    kinds = _KINDS if kind is None else (kind,)
    changed = False
    for k in kinds:
        if runtime.get_state(f'last_{k}_error'):
            runtime.set_state(f'last_{k}_error', '')
            changed = True
    still = next((k for k in _KINDS if k not in kinds and runtime.get_state(f'last_{k}_error')), None)
    if still is not None:
        message = runtime.get_state(f'last_{still}_error')
        if runtime.get_state('last_error') != message:
            runtime.set_state('last_error', message)
            runtime.set_state('last_error_at', runtime.get_state(f'last_{still}_error_at'))
            changed = True
    elif runtime.get_state('last_error'):
        runtime.set_state('last_error', '')
        changed = True
    if changed:
        db.session.commit()


# ---------------------------------------------------------------------------
# 背景同步
# ---------------------------------------------------------------------------
class SyncWorker:
    def __init__(self, app, transport_factory=make_transport):
        self.app = app
        self.transport_factory = transport_factory
        self.local_change = threading.Event()
        self.stopping = threading.Event()
        self.threads = []

    def start(self):
        tracking.COMMIT_LISTENERS.append(self.local_change.set)
        for target, name in ((self._push_loop, 'sync-push'), (self._pull_loop, 'sync-pull')):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stopping.set()
        self.local_change.set()

    def _step(self, fn, kind=None):
        with self.app.app_context():
            try:
                sync = NodeSync(self.transport_factory(self.app))
                sync.ensure_hello()
                fn(sync)
                record_ok(kind)
                return True
            except Exception as exc:  # noqa: BLE001  (斷網等錯誤只記錄，稍後重試)
                logger.warning('同步失敗：%s', exc)
                try:
                    record_error(exc, kind)
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                return False

    def _push_loop(self):
        interval = self.app.config['SYNC_INTERVAL']
        while not self.stopping.is_set():
            self.local_change.clear()
            self._step(lambda s: s.push(), 'push')
            self.local_change.wait(interval)
            time.sleep(0.5)  # 合併短時間內的多次提交

    def _pull_loop(self):
        interval = self.app.config['SYNC_INTERVAL']
        wait = self.app.config['SYNC_LONG_POLL']
        while not self.stopping.is_set():
            ok = self._step(lambda s: s.pull(wait=wait), 'pull')
            if not ok or not wait:
                self.stopping.wait(interval)


def start_background_sync(app):
    worker = SyncWorker(app)
    worker.start()
    app.extensions['sync_worker'] = worker
    logger.info('分店同步已啟動 (線上端 %s)', app.config['SYNC_HUB_URL'])
    return worker
