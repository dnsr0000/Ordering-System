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
from datetime import datetime

import requests
import sqlalchemy as sa
from flask import current_app
from PIL import Image

from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.sync import SyncConflict
from app.models.tenant import Tenant
from app.services.event_bus import order_event_bus
from app.services.points_service import recompute_points
from app.services.upload_paths import is_valid_menu_image, upload_file_path
from app.sync import runtime, tracking
from app.sync.engine import Applier, collect_changes, count_pending, merge_local_duplicate
from app.sync.registry import SPECS

logger = logging.getLogger(__name__)

PUSH_SPECS = SPECS
PAGE = 500


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


class NodeSync:
    """一次同步作業 (需在 app context 內使用)"""

    def __init__(self, transport):
        self.transport = transport

    # ---- 基本資料 ----
    def tenant(self):
        slug = current_app.config.get('SYNC_TENANT_SLUG', 'default')
        tenant = Tenant.query.filter_by(slug=slug).first()
        if tenant is None:
            raise SyncError(f'找不到本機店家 {slug}')
        return tenant

    def ensure_hello(self, force=False):
        """第一次連線：取得本分店在線上端的 uuid，並讓本機店家使用線上店家的 uuid"""
        if runtime.get_state('node_uuid') and not force:
            return
        info = self.transport.call('POST', '/hello')
        tenant = self.tenant()
        hub_tenant = info['tenant']['uuid']
        known = runtime.get_state('hub_tenant_uuid')
        if known and known != hub_tenant:
            raise SyncError('此分店先前已連線到另一個線上店家，請勿更換權杖 (如需更換，請先備份並清空同步狀態)')
        if tenant.uuid != hub_tenant:
            db.session.execute(sa.update(Tenant.__table__).where(Tenant.__table__.c.id == tenant.id)
                               .values(uuid=hub_tenant))
        runtime.set_state('node_uuid', info['node']['uuid'])
        runtime.set_state('node_name', info['node']['name'])
        runtime.set_state('hub_tenant_uuid', hub_tenant)
        runtime.set_state('hub_tenant_name', info['tenant']['name'])
        db.session.commit()

    # ---- 上傳 ----
    def push(self):
        tenant_id = self.tenant().id
        cursor = int(runtime.get_state('push_cursor', '-1') or -1)
        upto = tracking.current_rev(db.session)
        stock, stock_hash = self._stock_snapshot(tenant_id)
        send_stock = stock_hash != runtime.get_state('stock_hash')
        position, sent = (0, 0), 0
        while True:
            changes, nxt = collect_changes(tenant_id, PUSH_SPECS, cursor, upto, position, PAGE)
            db.session.rollback()
            if changes or send_stock:
                body = self.transport.call('POST', '/push', json_body={
                    'changes': changes, 'stock': stock if send_stock else None})
                send_stock = False
                sent += sum(len(v) for v in changes.values())
                self._handle_push_response(tenant_id, body)
            if nxt is None:
                break
            position = nxt
        runtime.set_state('push_cursor', upto)
        runtime.set_state('stock_hash', stock_hash)
        runtime.set_state('last_push_at', datetime.now().isoformat(timespec='seconds'))
        db.session.commit()
        return sent

    def _stock_snapshot(self, tenant_id):
        rows = db.session.execute(sa.select(MenuItem.uuid, MenuItem.stock, MenuItem.is_sold_out)
                                  .where(MenuItem.tenant_id == tenant_id).order_by(MenuItem.id)).all()
        stock = [{'uuid': u, 'stock': int(s or 0), 'is_sold_out': bool(so)} for u, s, so in rows]
        digest = hashlib.sha256(json.dumps(stock, sort_keys=True).encode()).hexdigest()
        return stock, digest

    def _handle_push_response(self, tenant_id, body):
        for c in body.get('conflicts') or ():
            db.session.add(SyncConflict(table_name=c.get('table', '')[:50], row_uuid=c.get('uuid', '')[:36],
                                        reason=str(c.get('reason', ''))[:300]))
        touched = set()
        with tracking.suppressed(db.session):
            for a in body.get('aliases') or ():
                touched |= merge_local_duplicate(tenant_id, a['table'], a['uuid'], a['canonical'])
            recompute_points(touched)
        db.session.commit()
        for rel_path in body.get('missing_media') or ():
            self._upload_menu_image(rel_path)

    # ---- 下載 ----
    def pull(self, wait=0):
        tenant_id = self.tenant().id
        my_node = runtime.get_state('node_uuid')
        params = {'cursor': int(runtime.get_state('pull_cursor', '-1') or -1), 'limit': PAGE, 'wait': wait}
        received = 0
        while True:
            body = self.transport.call('GET', '/pull', params=params)
            changes = body.get('changes') or {}
            if changes:
                received += sum(len(v) for v in changes.values())
                self._apply(tenant_id, my_node, changes)
            nxt = body.get('next') or {}
            if not body.get('has_more'):
                runtime.set_state('pull_cursor', nxt.get('cursor', params['cursor']))
                runtime.set_state('last_pull_at', datetime.now().isoformat(timespec='seconds'))
                db.session.commit()
                return received
            params = {**nxt, 'limit': PAGE, 'wait': 0}

    def _apply(self, tenant_id, my_node, changes):
        from app.services.order_service import consume_stock_unchecked, next_pickup_number, order_stock_demands, \
            _change_stock, flag_refund_if_paid
        with tracking.suppressed(db.session):
            result = Applier(tenant_id, 'node').apply(changes)
        # 以下是本分店因此產生的變更 (取餐號、庫存)，正常標記並在下次上傳
        for order in result.new_orders:
            if order.node_uuid == my_node and order.status != 'Cancelled':
                consume_stock_unchecked(tenant_id, order_stock_demands(order))
                if not order.pickup_number:
                    order.pickup_number = next_pickup_number(tenant_id)
        new_ids = {o.id for o in result.new_orders}
        for order in result.cancelled_orders:
            if order.id not in new_ids and runtime.owns_stock(order):
                _change_stock(tenant_id, order_stock_demands(order), +1)
            flag_refund_if_paid(order.id)  # 已在櫃檯結帳的訂單被線上端取消：標記需退款
        recompute_points(result.touched_users)
        db.session.commit()
        if result.orders_changed:
            order_event_bus.notify(tenant_id)
        for rel_path in sorted(result.menu_images):
            self._download_menu_image(rel_path)

    # ---- 餐點圖片 ----
    def _local_image(self, rel_path):
        if not is_valid_menu_image(rel_path):
            return None
        return upload_file_path(current_app.config['UPLOAD_FOLDER_MENU'], rel_path)

    def _download_menu_image(self, rel_path):
        path = self._local_image(rel_path)
        if not path or os.path.isfile(path):
            return
        try:
            data = self.transport.call('GET', f'/media/menu/{rel_path}', raw=True)
        except SyncError as exc:
            logger.warning('下載餐點圖片 %s 失敗：%s', rel_path, exc)
            return
        try:  # 只寫入真正的圖片 (此資料夾會公開提供下載)
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
        except Exception:  # noqa: BLE001
            logger.warning('線上端回傳的餐點圖片 %s 不是有效的圖片，已略過', rel_path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + '.part', 'wb') as fh:
            fh.write(data)
        os.replace(path + '.part', path)

    def _upload_menu_image(self, rel_path):
        path = self._local_image(rel_path)
        if not path or not os.path.isfile(path):
            return
        with open(path, 'rb') as fh:
            self.transport.call('PUT', f'/media/menu/{rel_path}', data=fh.read())

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
        'pending_changes': count_pending(tenant.id, PUSH_SPECS, cursor) if tenant else 0,
        'conflicts': [{'table': c.table_name, 'uuid': c.row_uuid, 'reason': c.reason,
                       'created_at': c.created_at.strftime('%Y-%m-%d %H:%M')}
                      for c in SyncConflict.query.order_by(SyncConflict.id.desc()).limit(50)],
        'online_orders_waiting': Order.query.filter(Order.order_source == 'Online', Order.pickup_number == 0).count()
        if tenant else 0,
    }


def record_error(exc):
    db.session.rollback()
    runtime.set_state('last_error', str(exc)[:500])
    runtime.set_state('last_error_at', datetime.now().isoformat(timespec='seconds'))
    db.session.commit()


def record_ok():
    if runtime.get_state('last_error'):
        runtime.set_state('last_error', '')
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

    def _step(self, fn):
        with self.app.app_context():
            try:
                sync = NodeSync(self.transport_factory(self.app))
                sync.ensure_hello()
                fn(sync)
                record_ok()
                return True
            except Exception as exc:  # noqa: BLE001  (斷網等錯誤只記錄，稍後重試)
                logger.warning('同步失敗：%s', exc)
                try:
                    record_error(exc)
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                return False

    def _push_loop(self):
        interval = self.app.config['SYNC_INTERVAL']
        while not self.stopping.is_set():
            self.local_change.clear()
            self._step(lambda s: s.push())
            self.local_change.wait(interval)
            time.sleep(0.5)  # 合併短時間內的多次提交

    def _pull_loop(self):
        interval = self.app.config['SYNC_INTERVAL']
        wait = self.app.config['SYNC_LONG_POLL']
        while not self.stopping.is_set():
            ok = self._step(lambda s: s.pull(wait=wait))
            if not ok or not wait:
                self.stopping.wait(interval)


def start_background_sync(app):
    worker = SyncWorker(app)
    worker.start()
    app.extensions['sync_worker'] = worker
    logger.info('分店同步已啟動 (線上端 %s)', app.config['SYNC_HUB_URL'])
    return worker
