"""
同步資料的編碼、讀取變更與套用 (線上端與分店端共用)。

網路上傳送的每一列是 {欄位: 值}：
- 不含本機整數 id、tenant_id、sync_rev 與本機專屬欄位 (registry.local_only)
- 外鍵 (及 soft_refs) 換成被參照列的 uuid，接收端再換回自己的 id
- 日期時間為 ISO 字串、人臉特徵為 base64

套用規則 (merge)：
- lww：updated_at 較新者覆蓋 (同時間保留本機)
- insert_only：建立後不再變更 (點數異動、刪除紀錄)；order_item：訂單明細，只有作廢份數只增不減
- order：狀態只往前走 (製作中 < 已出餐 < 已取餐 < 已取消)，取餐號以分店編的為準
- voucher：兌換憑證狀態只往前走 (可用 < 已退點 < 已使用)
- 刪除紀錄 (sync_tombstone) 優先：已刪除的資料不會被舊的更新復活
- 自然鍵相同 (同手機的會員、同代碼的優惠券、每店一筆的紅利設定…)：
  線上端記錄別名並合併到既有那一筆；分店端把本機那一筆改用線上的 uuid

安全限制 (分店權杖外流或分店電腦被入侵時，影響範圍只限該店家的一般資料)：
- 負責人 (owner) 帳號只能在線上端建立、修改、刪除；分店上傳的負責人帳號與角色提升一律拒收
- 餐點圖片、會員照片路徑必須符合固定格式 (不可含 ../ 等路徑)，兩端都檢查
- 線上端不接受與其他店家資料相同的 uuid，別名也只在同一店家內生效
"""
import base64
from dataclasses import dataclass, field
from datetime import date, datetime

import sqlalchemy as sa
from flask import current_app
from sqlalchemy import LargeBinary

from app.extensions import db
from app.models.branch import HQ_SITE, TRANSFER_RANK
from app.models.enums import DELIVERY_STATUS_RANK, OrderStatus, VoucherStatus
from app.models.sync import SyncAlias, SyncTombstone
from app.models.user import FeatureVector, decode_feature_bytes, encode_feature_array
from app.services.upload_paths import is_valid_member_photo, is_valid_menu_image, remove_upload
from app.sync.registry import BY_NAME, SPECS, references, synced_columns

ORDER_RANK = {OrderStatus.PENDING: 0, OrderStatus.COMPLETED: 1, OrderStatus.PICKED_UP: 2, OrderStatus.CANCELLED: 3}
VOUCHER_RANK = {VoucherStatus.OPEN: 0, VoucherStatus.REFUNDED: 1, VoucherStatus.USED: 2}
_CHUNK = 500
# 負責人帳號在線上端才能變更的欄位
# 分店不可變更負責人帳號的這些欄位 (兩步驟驗證的金鑰 / 備用碼也包含在內：被入侵的分店不能關掉或換掉負責人的驗證)
OWNER_PROTECTED_COLUMNS = ('username', 'password_hash', 'role', 'is_active', 'deleted_at', 'totp_secret', 'totp_enabled',
                           'totp_backup_codes')


# ---------------------------------------------------------------------------
# 編碼
# ---------------------------------------------------------------------------
def _encode_value(col, value):
    if value is None:
        return None
    if isinstance(col.type, FeatureVector):
        try:
            return base64.b64encode(encode_feature_array(value)).decode('ascii')
        except ValueError:
            return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode('ascii')
    return value


def _decode_value(col, value):
    if value is None:
        return None
    col_type = col.type
    if isinstance(col_type, FeatureVector):
        return decode_feature_bytes(base64.b64decode(value))
    if isinstance(col_type, sa.DateTime):
        return datetime.fromisoformat(value)
    if isinstance(col_type, sa.Date):
        return date.fromisoformat(value)
    if isinstance(col_type, sa.Boolean):
        return bool(value)
    if isinstance(col_type, sa.Integer):
        return int(value)
    if isinstance(col_type, LargeBinary):
        return base64.b64decode(value)
    return value


def _id_uuid_map(table_name, ids):
    table = db.metadata.tables[table_name]
    ids, out = sorted(ids), {}
    for i in range(0, len(ids), _CHUNK):
        rows = db.session.execute(sa.select(table.c.id, table.c.uuid).where(table.c.id.in_(ids[i:i + _CHUNK])))
        out.update(dict(rows.all()))
    return out


def encode_rows(spec, rows):
    table = spec.model.__table__
    refs = references(spec)
    columns = synced_columns(spec)
    uuid_maps = {col: _id_uuid_map(target, {r[col] for r in rows if r[col] is not None}) for col, target in refs.items()}
    out = []
    for r in rows:
        item = {}
        for name in columns:
            value = r[name]
            item[name] = uuid_maps[name].get(value) if name in refs and value is not None else (
                None if name in refs else _encode_value(table.c[name], value))
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 讀取變更 (分頁：依 SPECS 順序逐表、表內依 id)
# ---------------------------------------------------------------------------
def collect_changes(tenant_id, specs, after_rev, upto_rev, start=(0, 0), limit=500, row_filter=None):
    """
    讀取 after_rev < sync_rev <= upto_rev 的變更，最多 limit 列。
    回傳 (changes, next_position)；next_position 為 None 代表這個區間已讀完。
    row_filter(spec, table) 可回傳額外的 WHERE 條件 (例如只送某分店的訂單)，回傳 False 代表整張表略過。
    """
    changes, remaining = {}, limit
    table_index, after_id = start
    for idx in range(table_index, len(specs)):
        spec = specs[idx]
        table = spec.model.__table__
        extra = row_filter(spec, table) if row_filter else None
        if extra is False:
            continue
        stmt = sa.select(table).where(table.c.tenant_id == tenant_id, table.c.sync_rev > after_rev,
                                      table.c.sync_rev <= upto_rev, table.c.id > (after_id if idx == table_index else 0))
        if extra is not None:
            stmt = stmt.where(extra)
        rows = db.session.execute(stmt.order_by(table.c.id).limit(remaining)).mappings().all()
        if rows:
            changes.setdefault(spec.name, []).extend(encode_rows(spec, rows))
            remaining -= len(rows)
            if remaining <= 0:
                return changes, (idx, rows[-1]['id'])
    return changes, None


def count_pending(tenant_id, specs, after_rev):
    total = 0
    for spec in specs:
        table = spec.model.__table__
        total += db.session.execute(sa.select(sa.func.count()).select_from(table).where(
            table.c.tenant_id == tenant_id, table.c.sync_rev > after_rev)).scalar() or 0
    return total


# ---------------------------------------------------------------------------
# 套用變更
# ---------------------------------------------------------------------------
@dataclass
class ApplyResult:
    conflicts: list = field(default_factory=list)       # [{table, uuid, reason}]
    aliases: list = field(default_factory=list)         # [{table, uuid, canonical}] (線上端合併的重複資料)
    touched_users: set = field(default_factory=set)     # 需要重算點數的會員 id
    touched_passes: set = field(default_factory=set)    # 需要重算剩餘次數的寄杯 id
    new_orders: list = field(default_factory=list)      # 新收到的訂單物件
    cancelled_orders: list = field(default_factory=list)  # 因同步而變成已取消的訂單物件
    orders_changed: bool = False
    menu_images: set = field(default_factory=set)       # 收到的餐點圖片路徑
    transfer_alerts: list = field(default_factory=list)  # [(叫貨 / 調撥單, 說明)]：已取消的叫貨單仍被出貨等
    applied: int = 0


class Applier:
    """
    side='hub'：線上端套用分店上傳的資料 (source_node 為上傳的分店)
    side='node'：分店套用線上端下發的資料
    """

    def __init__(self, tenant_id, side, source_node_uuid=None):
        self.tenant_id = tenant_id
        self.side = side
        self.source_node_uuid = source_node_uuid
        self.result = ApplyResult()
        self._id_cache = {}
        self._alias_cache = {}

    # ---- uuid / id 對照 ----
    def resolve(self, table_name, uuid):
        """線上端：把已合併的別名 uuid 換成實際那一筆的 uuid"""
        if self.side != 'hub' or uuid is None:
            return uuid
        spec = BY_NAME.get(table_name)
        if spec is None or not (spec.natural_key or spec.singleton):
            return uuid
        key = (table_name, uuid)
        if key not in self._alias_cache:
            alias = db.session.get(SyncAlias, key)
            canonical = alias.canonical_uuid if alias else uuid
            if alias is not None and self._row_tenant(table_name, canonical) != self.tenant_id:
                canonical = uuid  # 別名屬於其他店家：不採用
            self._alias_cache[key] = canonical
        return self._alias_cache[key]

    @staticmethod
    def _row_tenant(table_name, uuid):
        table = db.metadata.tables[table_name]
        return db.session.execute(sa.select(table.c.tenant_id).where(table.c.uuid == uuid).limit(1)).scalar()

    def local_id(self, table_name, uuid):
        uuid = self.resolve(table_name, uuid)
        key = (table_name, uuid)
        if key not in self._id_cache:
            table = db.metadata.tables[table_name]
            self._id_cache[key] = db.session.execute(
                sa.select(table.c.id).where(table.c.uuid == uuid, table.c.tenant_id == self.tenant_id)).scalar()
        return self._id_cache[key]

    def _remember(self, table_name, uuid, row_id):
        self._id_cache[(table_name, uuid)] = row_id

    def conflict(self, spec, uuid, reason):
        self.result.conflicts.append({'table': spec.name, 'uuid': uuid, 'reason': reason})

    # ---- 主流程 ----
    def apply(self, changes):
        for spec in SPECS:
            if self.side == 'hub' and spec.direction == 'down':
                if changes.get(spec.name):
                    self.conflict(spec, '', '總部設定只能在線上端修改，分店上傳的內容已忽略')
                continue
            for data in changes.get(spec.name) or ():
                self._apply_row(spec, data)
            db.session.flush()
        return self.result

    def _decode(self, spec, data):
        table = spec.model.__table__
        refs = references(spec)
        values = {}
        for name in synced_columns(spec):
            if name not in data:
                continue
            raw = data[name]
            if name in refs:
                if raw is None:
                    values[name] = None
                    continue
                local = self.local_id(refs[name], raw)
                if local is None and not table.c[name].nullable:
                    return None, f'找不到關聯資料 {refs[name]} {raw}'
                values[name] = local
            else:
                values[name] = _decode_value(table.c[name], raw)
        return values, None

    def _tombstoned(self, spec, uuid):
        return db.session.execute(sa.select(SyncTombstone.id).where(
            SyncTombstone.tenant_id == self.tenant_id, SyncTombstone.table_name == spec.name,
            SyncTombstone.row_uuid == uuid).limit(1)).first() is not None

    def _apply_row(self, spec, data):
        uuid = self.resolve(spec.name, data.get('uuid'))
        if not uuid:
            return
        if spec.model is SyncTombstone:
            self._apply_tombstone(data)
            return
        if self._tombstoned(spec, uuid):
            return
        values, error = self._decode(spec, data)
        if error:
            self.conflict(spec, uuid, error)
            return
        values.pop('uuid', None)
        model = spec.model
        existing = db.session.execute(sa.select(model).where(model.uuid == uuid, model.tenant_id == self.tenant_id)
                                      .execution_options(skip_tenant_filter=True)).scalar_one_or_none()
        if existing is None and self.side == 'hub' and self._row_tenant(spec.name, uuid) is not None:
            self.conflict(spec, uuid, '資料識別碼已被其他店家使用')
            return
        if existing is None:
            existing = self._natural_match(spec, values)
            if existing is not None:
                if self.side == 'hub':
                    self._add_alias(spec, uuid, existing.uuid)
                    uuid = existing.uuid
                else:
                    existing.uuid = uuid  # 分店：本機那一筆改用線上的 uuid
        reason = self._rejected(spec, existing, values)
        if reason:
            self.conflict(spec, uuid, reason)
            return
        if existing is None:
            self._insert(spec, uuid, values)
            return
        self._merge(spec, existing, values)
        self.result.applied += 1

    def _rejected(self, spec, existing, values):
        """不可套用的資料 (回傳原因)：路徑格式不合法、分店變更負責人帳號"""
        if spec.name == 'menu_item' and not is_valid_menu_image(values.get('image_path')):
            return '餐點圖片路徑格式不正確'
        if spec.name == 'user' and not is_valid_member_photo(values.get('photo_path')):
            return '會員照片路徑格式不正確'
        if self.side == 'hub' and spec.name == 'expense' and existing is not None:
            if existing.node_uuid != self.source_node_uuid:   # 只能改自己登錄的費用 (線上端的費用 node_uuid 為空)
                return '不可修改其他分店或線上端的費用'
            values['node_uuid'] = self.source_node_uuid
        if self.side == 'hub' and spec.name == 'staff_account':
            if existing is not None and existing.role == 'owner':
                if any(name in values and values[name] != getattr(existing, name) for name in OWNER_PROTECTED_COLUMNS):
                    return '負責人帳號只能在線上端修改'
            elif values.get('role') == 'owner':
                return '負責人帳號只能在線上端建立'
        return None

    # ---- 自然鍵 / 別名 ----
    def _natural_match(self, spec, values):
        if not (spec.natural_key or spec.singleton):
            return None
        model = spec.model
        stmt = sa.select(model).where(model.tenant_id == self.tenant_id)
        for name in spec.natural_key:
            if values.get(name) is None:
                return None
            stmt = stmt.where(getattr(model, name) == values[name])
        return db.session.execute(stmt.execution_options(skip_tenant_filter=True).limit(1)).scalar()

    def _add_alias(self, spec, alias_uuid, canonical_uuid):
        if db.session.get(SyncAlias, (spec.name, alias_uuid)) is None:  # 已有 (可能屬於其他店家) 則不覆蓋
            db.session.add(SyncAlias(table_name=spec.name, alias_uuid=alias_uuid, canonical_uuid=canonical_uuid))
        self._alias_cache[(spec.name, alias_uuid)] = canonical_uuid
        self.result.aliases.append({'table': spec.name, 'uuid': alias_uuid, 'canonical': canonical_uuid})

    def _unique_clash(self, spec, values, own_id=None):
        """自然鍵 (唯一) 會不會撞到另一筆資料；員工帳號名稱是全系統唯一"""
        model = spec.model
        if spec.name == 'staff_account':
            stmt = sa.select(model.id, model.tenant_id).where(model.username == values.get('username'))
        elif spec.name in ('user', 'coupon'):
            key = spec.natural_key[0]
            stmt = sa.select(model.id, model.tenant_id).where(getattr(model, key) == values.get(key),
                                                              model.tenant_id == self.tenant_id)
        else:
            return None
        for row_id, tenant_id in db.session.execute(stmt.execution_options(skip_tenant_filter=True)):
            if row_id != own_id:
                return tenant_id
        return None

    # ---- 新增 ----
    def _insert(self, spec, uuid, values):
        model = spec.model
        clash = self._unique_clash(spec, values)
        if clash is not None:
            other = '其他店家' if clash != self.tenant_id else '本店另一筆資料'
            self.conflict(spec, uuid, f'{"、".join(spec.natural_key)} 已被{other}使用')
            return
        if spec.name == 'order' and self.side == 'hub':
            node_uuid = values.get('node_uuid') or self.source_node_uuid
            if node_uuid != self.source_node_uuid:
                self.conflict(spec, uuid, '不可建立其他分店的訂單')
                return
            values['node_uuid'] = node_uuid
        if spec.name == 'order_item' and self.side == 'hub' and not self._order_owned(values.get('order_id')):
            self.conflict(spec, uuid, '不可修改其他分店的訂單')
            return
        if spec.name == 'expense' and self.side == 'hub':
            values['node_uuid'] = self.source_node_uuid   # 費用一律記在上傳的分店
        if spec.name == 'stock_transfer' and self.side == 'hub':
            reason = self._transfer_insert_error(values)
            if reason:
                self.conflict(spec, uuid, reason)
                return
        obj = model(**values)
        obj.uuid, obj.tenant_id = uuid, self.tenant_id
        if spec.name == 'user':
            obj.points = 0  # 點數由異動帳重算
        db.session.add(obj)
        db.session.flush()
        self._remember(spec.name, uuid, obj.id)
        self.result.applied += 1
        self._after_change(spec, obj, created=True)

    def _transfer_insert_error(self, values):
        """線上端：分店只能建立自己是收貨方的叫貨單、自己是出貨方的調撥單，且對方必須是本店家的分店或總部"""
        from app.models.sync import SyncNode
        src, kind = self.source_node_uuid, values.get('kind')
        frm, to = values.get('from_site'), values.get('to_site')
        if frm == to:
            return '出貨方與收貨方不可相同'
        if kind == 'request' and (to != src or values.get('status') not in ('requested', 'cancelled')):
            return '分店只能建立自己收貨的叫貨單'
        if kind == 'transfer' and (frm != src or values.get('status') != 'shipped'):
            return '分店只能建立自己出貨的調撥單'
        if kind not in ('request', 'transfer'):
            return '單據種類不正確'
        other = frm if kind == 'request' else to
        if other != HQ_SITE and SyncNode.query.filter_by(tenant_id=self.tenant_id, uuid=other) \
                .execution_options(skip_tenant_filter=True).first() is None:
            return '找不到對方分店'
        return None

    def _merge_transfer(self, spec, obj, values):
        """叫貨 / 調撥單：狀態只往前走 (待出貨 → 取消 / 拒絕 → 已出貨 → 已收貨)；線上端檢查是否由該角色的分店操作"""
        incoming = values.get('status')
        rank_in, rank_now = TRANSFER_RANK.get(incoming, -1), TRANSFER_RANK.get(obj.status, -1)
        newer = values.get('updated_at') is not None and (obj.updated_at is None or values['updated_at'] > obj.updated_at)
        if incoming == 'cancelled' and obj.status in ('shipped', 'received') and \
                (self.side == 'node' or self.source_node_uuid == obj.to_site):
            # 收貨方取消時，出貨方已先出貨 (以已出貨為準)：通知收貨方照常收貨或安排退回
            self.result.transfer_alerts.append(
                (obj, f'叫貨單 {obj.number} 已由{obj.from_name}出貨，{obj.to_name}的取消未生效；請收貨入庫或安排退回'))
        if not (rank_in > rank_now or (rank_in == rank_now and incoming != obj.status and newer)):
            return
        if self.side == 'hub':
            actor = obj.from_site if incoming in ('shipped', 'rejected') else obj.to_site
            if actor != self.source_node_uuid:
                self.conflict(spec, obj.uuid, '不可代替其他分店變更叫貨 / 調撥單')
                return
        previous = obj.status
        for name in ('status', 'lines_json', 'shipped_by', 'shipped_at', 'received_by', 'received_at', 'reason'):
            if name in values:
                setattr(obj, name, values[name])
        if previous == 'cancelled' and incoming in ('shipped', 'received'):
            self.result.transfer_alerts.append(
                (obj, f'叫貨單 {obj.number} 已被{obj.to_name}取消，但{obj.from_name}在同步前已出貨；請收貨方收貨入庫或安排退回'))

    def _order_owned(self, order_id):
        if order_id is None:
            return False
        node_uuid = db.session.execute(sa.select(db.metadata.tables['order'].c.node_uuid)
                                       .where(db.metadata.tables['order'].c.id == order_id)).scalar()
        return node_uuid == self.source_node_uuid

    # ---- 合併既有資料 ----
    def _merge(self, spec, obj, values):
        if spec.merge == 'insert_only':
            return
        if spec.merge == 'order_item':   # 單品作廢：作廢份數只增不減 (剩下份數跟著更新)
            incoming = values.get('voided_qty') or 0
            if incoming > (obj.voided_qty or 0):
                if self.side == 'hub' and not self._order_owned(obj.order_id):
                    self.conflict(spec, obj.uuid, '不可修改其他分店的訂單')
                    return
                obj.voided_qty, obj.quantity = incoming, values.get('quantity', obj.quantity)
                obj.void_note = values.get('void_note') or obj.void_note
                self.result.orders_changed = True
            return
        if spec.merge == 'order':
            self._merge_order(spec, obj, values)
            return
        if spec.merge == 'transfer':
            self._merge_transfer(spec, obj, values)
            return
        if spec.merge == 'voucher':
            incoming = values.get('status')
            if VOUCHER_RANK.get(incoming, -1) > VOUCHER_RANK.get(obj.status, -1):
                obj.status = incoming
                obj.updated_at = max(filter(None, (obj.updated_at, values.get('updated_at'))), default=None)
            if obj.order_id is None and values.get('order_id'):
                obj.order_id = values['order_id']
            return
        incoming_at = values.get('updated_at')
        if incoming_at is None or (obj.updated_at is not None and incoming_at <= obj.updated_at):
            return
        if spec.natural_key and self._unique_clash(spec, values, own_id=obj.id) is not None:
            self.conflict(spec, obj.uuid, f'{"、".join(spec.natural_key)} 與另一筆資料重複，未套用此次修改')
            return
        old_photo = obj.photo_path if spec.name == 'user' else None
        for name, value in values.items():
            setattr(obj, name, value)
        if old_photo and old_photo != obj.photo_path:  # 照片已更換或撤回人臉同意：刪除本機舊照片
            remove_upload(current_app.config['UPLOAD_FOLDER_MEMBER'], old_photo)
        self._after_change(spec, obj, created=False)

    def _merge_order(self, spec, order, values):
        if self.side == 'hub' and order.node_uuid != self.source_node_uuid:
            self.conflict(spec, order.uuid, '不可修改其他分店的訂單')
            return
        changed = False
        incoming = values.get('status')
        if ORDER_RANK.get(incoming, -1) > ORDER_RANK.get(order.status, -1):
            order.status = incoming
            changed = True
            if incoming == OrderStatus.CANCELLED:
                self.result.cancelled_orders.append(order)
        # 桌邊未結帳的訂單作廢品項時直接降低應付金額 (只會變少)
        incoming_total = values.get('total_price')
        if incoming_total is not None and order.payment_status == 'unpaid' and incoming_total < (order.total_price or 0):
            order.total_price, changed = incoming_total, True
        if order.completed_at is None and values.get('completed_at'):
            order.completed_at, changed = values['completed_at'], True
        if not order.pickup_number and values.get('pickup_number'):
            order.pickup_number, changed = values['pickup_number'], True
        # 桌邊掃碼點餐的櫃檯結帳：unpaid → settled；已取消的訂單收到結帳 (兩邊同時操作) 或對方已標記退款 → refund_required
        incoming_pay = values.get('payment_status')
        if incoming_pay in ('settled', 'refund_required') and order.payment_status in ('unpaid', 'settled'):
            new_pay = 'refund_required' if incoming_pay == 'refund_required' or order.status == OrderStatus.CANCELLED \
                else 'settled'
            if new_pay != order.payment_status:
                order.payment_status, changed = new_pay, True
                order.payment_method = values.get('payment_method') or order.payment_method
                order.paid_at = values.get('paid_at') or order.paid_at
        # LINE Pay 扣款中 (authorizing) 的訂單剛好被同步出去：之後收到扣款成功 ('') 的版本
        if order.payment_status == 'authorizing' and incoming_pay == '':
            order.payment_status, changed = '', True
            order.payment_reference = values.get('payment_reference') or order.payment_reference
        # 已退款金額只增不減 (部分退款在建立訂單的那台主機執行)
        incoming_refunded = values.get('refunded_amount') or 0
        if incoming_refunded > (order.refunded_amount or 0):
            order.refunded_amount, changed = incoming_refunded, True
        if incoming_pay == 'refunded' and order.payment_status == 'refund_required':
            order.payment_status, changed = 'refunded', True
        # 自有外送的配送狀態只往前走 (分店派車 / 送達後同步回線上端，顧客在追蹤頁看到)；同一階段改派外送員以較新的為準
        incoming_ds = values.get('delivery_status') or ''
        rank_in = DELIVERY_STATUS_RANK.get(incoming_ds, 0)
        rank_now = DELIVERY_STATUS_RANK.get(order.delivery_status or '', 0)
        newer = values.get('updated_at') and (order.updated_at is None or values['updated_at'] > order.updated_at)
        if rank_in > rank_now or (rank_in == rank_now and rank_in and newer
                                  and (values.get('driver_name') or '') != (order.driver_name or '')):
            order.delivery_status = incoming_ds
            order.driver_name = values.get('driver_name') or order.driver_name
            order.dispatched_at = values.get('dispatched_at') or order.dispatched_at
            order.delivered_at = values.get('delivered_at') or order.delivered_at
            changed = True
        if changed:
            order.updated_at = max(filter(None, (order.updated_at, values.get('updated_at'))), default=None)
            self.result.orders_changed = True

    def _after_change(self, spec, obj, created):
        if spec.name == 'order':
            self.result.orders_changed = True
            if created:
                self.result.new_orders.append(obj)
        elif spec.name in ('point_ledger', 'wallet_ledger', 'stamp_ledger'):
            self.result.touched_users.add(obj.user_id)
        elif spec.name == 'pass_ledger':
            self.result.touched_passes.add(obj.member_pass_id)
        elif spec.name == 'user':
            self.result.touched_users.add(obj.id)
        elif spec.name == 'menu_item' and obj.image_path:
            self.result.menu_images.add(obj.image_path)

    # ---- 刪除紀錄 ----
    def _apply_tombstone(self, data):
        uuid, table_name, row_uuid = data.get('uuid'), data.get('table_name'), data.get('row_uuid')
        spec = BY_NAME.get(table_name)
        if spec is None or not row_uuid:
            return
        row_uuid = self.resolve(table_name, row_uuid)
        table = spec.model.__table__
        target = db.session.execute(sa.select(table).where(table.c.uuid == row_uuid, table.c.tenant_id == self.tenant_id)
                                    ).mappings().first()
        if self.side == 'hub' and spec.name == 'staff_account' and target is not None and target['role'] == 'owner':
            self.conflict(spec, row_uuid, '負責人帳號只能在線上端刪除')
            return
        exists = db.session.execute(sa.select(SyncTombstone.id).where(SyncTombstone.uuid == uuid)).first()
        if exists is None:
            db.session.add(SyncTombstone(uuid=uuid, tenant_id=self.tenant_id, table_name=table_name, row_uuid=row_uuid,
                                         deleted_at=_decode_value(SyncTombstone.__table__.c.deleted_at,
                                                                  data.get('deleted_at')) or datetime.now()))
        if spec.name == 'order':
            return  # 訂單不因同步刪除 (只會取消)
        if spec.name == 'user' and target is not None and target['photo_path']:
            remove_upload(current_app.config['UPLOAD_FOLDER_MEMBER'], target['photo_path'])
        db.session.execute(sa.delete(table).where(table.c.uuid == row_uuid, table.c.tenant_id == self.tenant_id))
        self._id_cache.pop((table_name, row_uuid), None)
        self.result.applied += 1


def merge_local_duplicate(tenant_id, table_name, alias_uuid, canonical_uuid):
    """
    分店端：線上端回報本機某一筆 (alias) 與既有資料 (canonical) 重複。
    本機也有 canonical 時，把所有參照改指 canonical 並刪除 alias；沒有時直接把 alias 改用 canonical 的 uuid。
    回傳受影響的會員 id (需要重算點數)。
    """
    table = db.metadata.tables[table_name]
    alias_id = db.session.execute(sa.select(table.c.id).where(table.c.uuid == alias_uuid,
                                                              table.c.tenant_id == tenant_id)).scalar()
    if alias_id is None:
        return set()
    canonical_id = db.session.execute(sa.select(table.c.id).where(table.c.uuid == canonical_uuid,
                                                                  table.c.tenant_id == tenant_id)).scalar()
    if canonical_id is None:
        db.session.execute(sa.update(table).where(table.c.id == alias_id)
                           .values(uuid=canonical_uuid, updated_at=table.c.updated_at))
        return {alias_id} if table_name == 'user' else set()
    for spec in SPECS:
        for col, target in references(spec).items():
            if target == table_name:
                ref_table = spec.model.__table__
                db.session.execute(sa.update(ref_table).where(ref_table.c[col] == alias_id)
                                   .values({col: canonical_id, 'updated_at': ref_table.c.updated_at}))
    db.session.execute(sa.delete(table).where(table.c.id == alias_id))
    return {canonical_id} if table_name == 'user' else set()
