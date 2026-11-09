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
- feedback：評價不依 updated_at 判斷，店家回覆 / 公開以較新的 replied_at 為準；gift_card：作廢後不會被蓋回可用
- voucher：兌換憑證狀態只往前走 (可用 < 已退點 < 已使用)；例外：訂單已取消時「已使用 → 已退點」
- staff_account：安全優先 (登出所有裝置的序號只增不減、用掉的備用碼不復活、分店一律套用線上端的停用 / 刪除)
- 刪除紀錄 (sync_tombstone) 優先：已刪除的資料不會被舊的更新復活；
  自然鍵 / 每店一筆的資料表的刪除紀錄在同一批的新增與修改之前先套用 (刪除後用同一個自然鍵重建、改名成被刪掉的名稱)
- 自然鍵相同 (同手機的會員、同代碼的優惠券、每店一筆的紅利設定…)：
  線上端記錄別名並合併到既有那一筆；分店端把本機那一筆改用線上的 uuid。
  第一次配對時不比較 updated_at：以線上端那一筆為準，另一筆只補空白欄位 (見 Applier._first_merge)
- 本機已封存 (搬到封存檔) 的訂單不會因對方的更新被插回主資料庫

安全限制 (分店權杖外流或分店電腦被入侵時，影響範圍只限該店家的一般資料)：
- 負責人 (owner) 帳號只能在線上端建立、修改、刪除；分店上傳的負責人帳號與角色提升一律拒收
- 餐點圖片、會員照片路徑必須符合固定格式 (不可含 ../ 等路徑)，兩端都檢查
- 線上端不接受與其他店家資料相同的 uuid，別名也只在同一店家內生效
"""
import base64
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache

import sqlalchemy as sa
from flask import current_app
from sqlalchemy import LargeBinary
from sqlalchemy.orm.attributes import flag_modified

from app.extensions import db
from app.models.branch import HQ_SITE, TRANSFER_RANK
from app.models.enums import DELIVERY_STATUS_RANK, OrderStatus, VoucherStatus
from app.models.sync import SyncAlias, SyncTombstone
from app.models.user import FeatureVector, User, decode_feature_bytes, encode_feature_array
from app.services.upload_paths import is_valid_member_photo, is_valid_menu_image, remove_upload
from app.sync import tracking
from app.sync.registry import BY_NAME, SPECS, references, synced_columns

ORDER_RANK = {OrderStatus.PENDING: 0, OrderStatus.COMPLETED: 1, OrderStatus.PICKED_UP: 2, OrderStatus.CANCELLED: 3}
VOUCHER_RANK = {VoucherStatus.OPEN: 0, VoucherStatus.REFUNDED: 1, VoucherStatus.USED: 2}
_CHUNK = 500
# 負責人帳號在線上端才能變更的欄位
# 分店不可變更負責人帳號的這些欄位 (兩步驟驗證的金鑰 / 備用碼也包含在內：被入侵的分店不能關掉或換掉負責人的驗證)
OWNER_PROTECTED_COLUMNS = ('username', 'password_hash', 'role', 'is_active', 'deleted_at', 'totp_secret', 'totp_enabled',
                           'totp_backup_codes')
# 員工帳號的安全欄位：對方的 session_epoch 較舊 (在「登出所有裝置」之前的版本) 時不採用
STAFF_SECURITY_COLUMNS = ('password_hash', 'role', 'is_active', 'deleted_at', 'session_epoch')
# 已退款 (全額) 可蓋過的本機付款狀態 ('' 為已付款)
_REFUNDABLE_PAY = ('', 'paid', 'settled', 'refund_required')
# 封存只處理 6 個月 (archive_service.run 的下限 150 天) 以前的訂單：較新的訂單不必查封存檔
_ARCHIVE_MIN_AGE = timedelta(days=140)
_DEFERRED = object()
# 會員刪除後訂單上的姓名 (見 privacy_service._deidentify_member_records)
_DELETED_MEMBER = '已刪除會員'


def _blank(value):
    """自然鍵第一次配對時「空白」的欄位 (0 / False 是有意義的設定值，不算空白)"""
    return value is None or (isinstance(value, str) and not value.strip())


def _bump(*stamps):
    """比雙方都新的時間 (讓合併後的那一筆在各主機的 LWW 中勝出)"""
    stamps = [s for s in stamps if s is not None]
    return max(stamps) + timedelta(microseconds=1) if stamps else datetime.now()


def _code_list(raw):
    try:
        codes = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return None
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        return None
    return codes


def _merge_backup_codes(local, incoming):
    """
    兩步驟驗證的一次性備用碼 (雜湊 JSON)：兩邊都還有的才保留 (任一台主機用掉的都失效)。
    沒有交集 (重新產生了一整組、停用、或全部用完) 或格式不對時回傳 None，交給一般規則。
    """
    if incoming is None:
        return None
    mine, theirs = _code_list(local), _code_list(incoming)
    if mine is None or theirs is None:
        return None
    common = set(mine) & set(theirs)
    if not common:
        return None
    merged = [c for c in mine if c in common]
    return local if merged == mine else json.dumps(merged)


def _codes_removed_only(local, incoming):
    """incoming 只是把 local 的備用碼用掉幾組 (沒有新增)"""
    mine, theirs = _code_list(local), _code_list(incoming)
    return mine is not None and theirs is not None and set(theirs) <= set(mine)


@lru_cache(maxsize=None)
def _unique_columns(table_name):
    """資料表上含 tenant_id 的唯一約束 / 唯一索引的欄位組合"""
    table = db.metadata.tables[table_name]
    groups = [c.columns for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    groups += [ix.columns for ix in table.indexes if ix.unique]
    return tuple(frozenset(col.name for col in cols) for cols in groups)


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
    # 超過欄位長度的字串截斷 (SQLite 不檢查長度，舊版分店可能已存了超長資料；PostgreSQL 會拒收並讓整批同步失敗)
    if isinstance(col_type, sa.String) and col_type.length and isinstance(value, str) and len(value) > col_type.length:
        return value[:col_type.length]
    return value


def _int_out_of_range(col, value):
    """整數欄位的值超出資料庫範圍 (PostgreSQL INTEGER ±2^31-1、BIGINT ±2^63-1)：整批交易會失敗，改為拒收該列"""
    if not isinstance(col.type, sa.Integer) or isinstance(col.type, sa.Boolean) or not isinstance(value, int):
        return False
    limit = 2 ** 63 - 1 if isinstance(col.type, sa.BigInteger) else 2 ** 31 - 1
    return not -limit - 1 <= value <= limit


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
    changed_order_ids: set = field(default_factory=set)  # 合併時實際改到欄位的既有訂單 id (新訂單見 new_orders)
    orders_changed: bool = False
    menu_images: set = field(default_factory=set)       # 收到的餐點圖片路徑
    transfer_alerts: list = field(default_factory=list)  # [(叫貨 / 調撥單, 說明)]：已取消的叫貨單仍被出貨等
    # 兌換憑證在兩台主機同步前一邊退點 (已退點)、一邊核銷 (已使用)：合併後以已使用為準，退回的點數需另外追回
    reused_vouchers: list = field(default_factory=list)
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
        self._deferred = None     # 第一輪撞到唯一鍵的列 (同一批稍後的改名 / 刪除可能讓它不再撞)
        self._archive = None      # 封存檔的連線 (False = 沒有封存檔)

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
        # 自然鍵 / 每店一筆的資料表的刪除紀錄先套用 (其餘的刪除紀錄照 SPECS 順序最後才套用)：
        # 同一批「刪除後用同一個自然鍵重建」、「刪除後把另一筆改成同名 / 同代碼」時，
        # 新的那一筆才不會配對到 (或撞到) 稍後才會被刪除的舊資料
        early = [d for d in changes.get(SyncTombstone.__tablename__) or ()
                 if isinstance(d, dict) and self._early_tombstone(d)]
        early_ids = {id(d) for d in early}
        try:
            for data in early:
                self._apply_row_isolated(BY_NAME[SyncTombstone.__tablename__], data, self._apply_tombstone)
            if early:
                db.session.flush()
            for spec in SPECS:
                if self.side == 'hub' and spec.direction == 'down':
                    if changes.get(spec.name):
                        self.conflict(spec, '', '總部設定只能在線上端修改，分店上傳的內容已忽略')
                    continue
                self._deferred = []
                for data in changes.get(spec.name) or ():
                    if id(data) not in early_ids:
                        self._apply_row_isolated(spec, data, self._apply_row)
                # 撞到唯一鍵的修改 (例如連續改名：金卡→鑽石、銀卡→金卡) 等同表其他列套用完再試一次，仍撞到才記衝突
                deferred, self._deferred = self._deferred, None
                for data in deferred:
                    self._apply_row_isolated(spec, data, self._apply_row)
                db.session.flush()
        finally:
            self._deferred = None
            self._close_archive()
        return self.result

    def _apply_row_isolated(self, spec, data, fn):
        """
        線上端：每一列各自用 savepoint 套用，寫入失敗 (欄位過長、違反約束…) 只放棄那一列並記成衝突，
        其餘照常套用 (否則整批回 500，分店的上傳游標永遠卡在這一列)。分店端行為不變。
        fn 為 self._apply_row (spec, data) 或 self._apply_tombstone (data)。
        """
        def call():
            if fn == self._apply_tombstone:
                fn(data)
            else:
                fn(spec, data)

        if self.side != 'hub':
            call()
            return
        from app.services import deferred as deferred_tasks
        result = self.result
        lists = (result.conflicts, result.aliases, result.new_orders, result.cancelled_orders,
                 result.reused_vouchers, result.transfer_alerts)
        lengths = [len(items) for items in lists]
        sets = (result.touched_users, result.touched_passes, result.menu_images, result.changed_order_ids)
        copies = [set(items) for items in sets]
        applied, orders_changed = result.applied, result.orders_changed
        retry = len(self._deferred) if self._deferred is not None else None
        pending = db.session.info.get(deferred_tasks._PENDING)
        pending_len = len(pending) if pending is not None else 0
        try:
            with db.session.begin_nested():
                call()
                db.session.flush()
        except (sa.exc.DataError, sa.exc.IntegrityError) as exc:
            # 已回滾的列不可留在結果裡 (hub.push 會對 id 為 None 的訂單呼叫 flag_refund_if_paid 等後處理)
            for items, length in zip(lists, lengths):
                del items[length:]
            for items, copy in zip(sets, copies):
                items.clear()
                items.update(copy)
            result.applied, result.orders_changed = applied, orders_changed
            if retry is not None and self._deferred is not None:
                del self._deferred[retry:]
            # savepoint 回滾不會丟掉交易後工作：這一列排入的推播等一併撤回
            pending = db.session.info.get(deferred_tasks._PENDING)
            if pending is not None:
                del pending[pending_len:]
            # 被回滾的新增列可能已經記進快取
            self._id_cache.clear()
            self._alias_cache.clear()
            self.conflict(spec, data.get('uuid') or '',
                          f'資料無法寫入線上端資料庫：{str(getattr(exc, "orig", exc))[:150]}')

    @staticmethod
    def _early_tombstone(data):
        spec = BY_NAME.get(data.get('table_name'))
        return spec is not None and bool(spec.natural_key or spec.singleton)

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
                if _int_out_of_range(table.c[name], values[name]):
                    return None, f'整數超出範圍 ({name})'
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
            if spec.name == 'order_item' and self._archived(spec, uuid):
                return   # 本機已封存的訂單明細 (訂單已搬到封存檔)
            self.conflict(spec, uuid, error)
            return
        values.pop('uuid', None)
        if spec.name == 'user' and values.get('referred_by_uuid'):   # 推薦人已合併到另一筆 (同手機)：改指實際那一筆
            values['referred_by_uuid'] = self.resolve('user', values['referred_by_uuid'])
        model = spec.model
        existing = db.session.execute(sa.select(model).where(model.uuid == uuid, model.tenant_id == self.tenant_id)
                                      .execution_options(skip_tenant_filter=True)).scalar_one_or_none()
        if existing is None and self.side == 'hub' and self._row_tenant(spec.name, uuid) is not None:
            self.conflict(spec, uuid, '資料識別碼已被其他店家使用')
            return
        matched = False
        if existing is None:
            existing = self._natural_match(spec, values)
            if existing is not None:
                matched = True
                if self.side == 'hub':
                    self._add_alias(spec, uuid, existing.uuid)
                    uuid = existing.uuid
                else:
                    if spec.name == 'user':
                        _repoint_referrals(self.tenant_id, existing.uuid, uuid)
                    existing.uuid = uuid  # 分店：本機那一筆改用線上的 uuid
        reason = self._rejected(spec, existing, values)
        if reason:
            self.conflict(spec, uuid, reason)
            return
        if existing is None:
            if spec.name == 'order' and self._archived(spec, uuid, values.get('created_at')):
                return   # 本機已封存的舊訂單：對方更新了它 (例如會員刪除帳號、退款) 也不插回主資料庫
            self._insert(spec, uuid, values)
            return
        if matched:
            self._first_merge(spec, existing, values)
        else:
            if self._merge(spec, existing, values) is _DEFERRED:
                self._deferred.append(data)
                return
        self.result.applied += 1

    def _rejected(self, spec, existing, values):
        """不可套用的資料 (回傳原因)：路徑格式不合法、分店變更負責人帳號"""
        if spec.name == 'menu_item' and not is_valid_menu_image(values.get('image_path')):
            return '餐點圖片路徑格式不正確'
        if self.side == 'hub' and spec.name == 'menu_item' and self._foreign_menu_image(existing, values.get('image_path')):
            return '餐點圖片路徑已被其他店家使用'
        if spec.name == 'user' and not is_valid_member_photo(values.get('photo_path')):
            return '會員照片路徑格式不正確'
        if self.side == 'hub' and spec.name == 'expense' and existing is not None:
            if existing.node_uuid != self.source_node_uuid:   # 只能改自己登錄的費用 (線上端的費用 node_uuid 為空)
                return '不可修改其他分店或線上端的費用'
            values['node_uuid'] = self.source_node_uuid
        if self.side == 'hub' and spec.name == 'staff_account':
            if existing is not None and existing.role == 'owner':
                if any(self._owner_column_changed(existing, values, name) for name in OWNER_PROTECTED_COLUMNS):
                    return '負責人帳號只能在線上端修改'
            elif values.get('role') == 'owner':
                return '負責人帳號只能在線上端建立'
        return None

    @staticmethod
    def _owner_column_changed(existing, values, name):
        if name not in values or values[name] == getattr(existing, name):
            return False
        # 負責人在分店用掉一組備用碼：只減不增的變更照收 (否則用過的備用碼在線上端與其他分店仍可再用)
        return not (name == 'totp_backup_codes' and _codes_removed_only(existing.totp_backup_codes, values[name]))

    def _foreign_menu_image(self, existing, path):
        """
        線上端：分店上傳的餐點圖片路徑是其他店家的餐點正在使用的圖片 (否則本店之後刪除 / 換圖時會刪掉別家店的圖片檔)。
        路徑的資料夾數字是建立圖片那台主機的店家 id (分店本機的店家 id 與線上端不同)，所以以「誰在用」判斷。
        """
        if not path or (existing is not None and existing.image_path == path):
            return False
        model = BY_NAME['menu_item'].model
        return db.session.execute(sa.select(model.id).where(model.image_path == path, model.tenant_id != self.tenant_id)
                                  .execution_options(skip_tenant_filter=True).limit(1)).first() is not None

    # ---- 自然鍵 / 別名 ----
    def _natural_match(self, spec, values):
        if not (spec.natural_key or spec.singleton):
            return None
        model = spec.model
        # 已刪除 (有刪除紀錄) 的資料不當作配對對象：否則新建的資料會被併進已刪除的那一筆，之後跟著被刪除
        deleted = sa.select(SyncTombstone.id).where(SyncTombstone.tenant_id == self.tenant_id,
                                                    SyncTombstone.table_name == spec.name,
                                                    SyncTombstone.row_uuid == model.uuid)
        stmt = sa.select(model).where(model.tenant_id == self.tenant_id, ~deleted.exists())
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
        if spec.name == 'user':
            _repoint_referrals(self.tenant_id, alias_uuid, canonical_uuid)

    def _unique_clash(self, spec, values, own_id=None, current=None):
        """
        自然鍵 (唯一) 會不會撞到另一筆資料：員工帳號名稱是全系統唯一，其餘依資料表上「店家 + 自然鍵」的唯一約束
        (會員手機、優惠券 / 禮物卡代碼、會員等級名稱、分類翻譯…)。current 為既有那一筆 (values 沒帶的欄位沿用)。
        """
        model = spec.model
        if spec.name == 'staff_account':
            stmt = sa.select(model.id, model.tenant_id).where(model.username == values.get('username'))
        elif spec.natural_key and frozenset(('tenant_id',) + spec.natural_key) in _unique_columns(spec.name):
            stmt = sa.select(model.id, model.tenant_id).where(model.tenant_id == self.tenant_id)
            for key in spec.natural_key:
                value = values[key] if key in values or current is None else getattr(current, key)
                if value is None:   # 唯一約束不比較 NULL
                    return None
                stmt = stmt.where(getattr(model, key) == value)
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
        if spec.name in ('expense', 'payment_record') and self.side == 'hub':
            values['node_uuid'] = self.source_node_uuid   # 費用 / 收款一律記在上傳的分店 (含首次連線前建立、node_uuid 為空的收款)
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

    def _tombstone_denied(self, spec, target):
        """
        線上端：分店上傳的刪除紀錄是否不可套用 (回傳衝突原因；True = 靜默略過)。刪除與修改適用相同規則，
        否則分店可用刪除紀錄繞過「總部設定只能在線上端修改」、訂單 / 調撥單的擁有權檢查。
        """
        if spec.direction == 'down':
            return '總部設定只能在線上端刪除，分店上傳的刪除已忽略'
        if spec.direction == 'up':
            return True   # 只上傳的資料 (日誌、收款、退款、費用…) 在線上端彙整保存，分店本機清除 (封存) 不影響線上端
        if spec.name == 'order':
            # 訂單不因同步刪除，但刪除紀錄會讓之後的所有更新被略過：只接受擁有該訂單的分店送來的
            # (線上端沒有這張訂單時不記錄，避免預先凍結之後才上傳的訂單)
            if target is None:
                return True
            if target['node_uuid'] != self.source_node_uuid:
                return '不可刪除其他分店或線上端的訂單'
            return None
        if target is None:
            return None
        if spec.name == 'stock_transfer':
            return '叫貨 / 調撥單不可刪除 (請改為取消或拒絕)'
        if spec.name == 'order_item' and not self._order_owned(target.get('order_id')):
            return '不可刪除其他分店的訂單明細'
        return None

    def _order_owned(self, order_id):
        if order_id is None:
            return False
        node_uuid = db.session.execute(sa.select(db.metadata.tables['order'].c.node_uuid)
                                       .where(db.metadata.tables['order'].c.id == order_id)).scalar()
        return node_uuid == self.source_node_uuid

    # ---- 合併既有資料 ----
    def _merge(self, spec, obj, values):
        if spec.merge == 'insert_only':
            # 線上訂單待付款期間就下發的帳本 (當時分店還沒有該訂單，order_id 存成空白)：付款後重新下發時補上
            if self.side == 'node' and values.get('order_id') and 'order_id' in spec.model.__table__.c \
                    and obj.order_id is None:
                obj.order_id = values['order_id']
                flag_modified(obj, 'updated_at')   # 保留原本的 updated_at
            return
        if spec.merge == 'feedback':
            # 評價不依 updated_at 判斷：店家回覆 / 公開以較新的 replied_at 為準 (每次儲存回覆都更新)；星數與意見建立後不變
            incoming = values.get('replied_at')
            if isinstance(incoming, datetime) and (obj.replied_at is None or incoming > obj.replied_at):
                obj.reply = values.get('reply') or ''
                obj.replied_at = incoming
                obj.is_public = bool(values.get('is_public'))
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
            self._merge_voucher(obj, values)
            return
        incoming_at = values.get('updated_at')
        newer = incoming_at is not None and (obj.updated_at is None or incoming_at > obj.updated_at)
        if spec.name == 'staff_account':
            return self._merge_staff(spec, obj, values, newer)
        if not newer:
            return
        if self._clashes(spec, obj, values):
            return _DEFERRED if self._deferred is not None else None
        if spec.name == 'gift_card' and obj.status == 'void' and values.get('status') != 'void':
            # 禮物卡作廢不可回退 (餘額已扣光)：較新的列 (同步前另一邊改了備註等) 不會把卡蓋回可用
            values = {**values, 'status': 'void', 'voided_at': obj.voided_at or values.get('voided_at')}
        old_photo = obj.photo_path if spec.name == 'user' else None
        for name, value in values.items():
            setattr(obj, name, value)
        if old_photo and old_photo != obj.photo_path:  # 照片已更換或撤回人臉同意：刪除本機舊照片
            remove_upload(current_app.config['UPLOAD_FOLDER_MEMBER'], old_photo)
        self._after_change(spec, obj, created=False)

    def _clashes(self, spec, obj, values):
        """修改會撞到另一筆的唯一鍵：第一輪先延後 (同一批稍後的改名可能讓它不再撞)，重試時仍撞到才記衝突"""
        if not spec.natural_key or self._unique_clash(spec, values, own_id=obj.id, current=obj) is None:
            return False
        if self._deferred is None:
            self.conflict(spec, obj.uuid, f'{"、".join(spec.natural_key)} 與另一筆資料重複，未套用此次修改')
        return True

    def _order_cancelled(self, order_id):
        if not order_id:
            return False
        table = db.metadata.tables['order']
        return db.session.execute(sa.select(table.c.status).where(table.c.id == order_id)).scalar() \
            == OrderStatus.CANCELLED

    def _merge_voucher(self, obj, values):
        """
        兌換憑證：狀態只往前走 (可用 < 已退點 < 已使用)。
        例外：訂單取消時該訂單用掉的憑證會從已使用改成已退點 (點數已退回)，訂單已取消就接受；反過來不讓舊的已使用蓋回去。
        """
        incoming, current = values.get('status'), obj.status
        order_id = obj.order_id or values.get('order_id')
        if incoming == VoucherStatus.REFUNDED and current == VoucherStatus.USED and self._order_cancelled(order_id):
            accept = True
        elif incoming == VoucherStatus.USED and current == VoucherStatus.REFUNDED and obj.order_id \
                and self._order_cancelled(obj.order_id):
            accept = False
        else:
            accept = VOUCHER_RANK.get(incoming, -1) > VOUCHER_RANK.get(current, -1)
            # 同步前一邊退點、一邊核銷 (訂單沒有取消)：以已使用為準，退回的點數交給線上端追回並通知店家。
            # 只在線上端記錄 (兩種先後順序都會經過線上端)，避免各分店重複追回
            if self.side == 'hub' and {incoming, current} == {VoucherStatus.USED, VoucherStatus.REFUNDED}:
                self.result.reused_vouchers.append(obj)
        if accept:
            obj.status = incoming
            obj.updated_at = max(filter(None, (obj.updated_at, values.get('updated_at'))), default=None)
        if obj.order_id is None and values.get('order_id'):
            obj.order_id = values['order_id']

    def _merge_staff(self, spec, obj, values, newer):
        """
        員工帳號 (安全優先)：整列 LWW 會讓「只是登入時更新了驗證欄位、updated_at 變新」的舊資料蓋掉對方的停用 / 改密碼。
        - session_epoch (登出所有裝置) 只增不減；對方的序號較舊代表那一列是安全事件之前的版本，不採用它的密碼 / 角色 / 停用狀態
        - 一次性備用碼：兩邊都還有的才保留 (任一台主機用掉的都失效，不因較新的整列而復活)
        - 分店端：線上端的停用 / 刪除一律套用；負責人帳號的保護欄位只能在線上端修改，一律以線上端為準
        以上不受 updated_at 新舊影響的變更另外套用 (保留本機 updated_at)。
        """
        forced = {}
        in_epoch, now_epoch = values.get('session_epoch'), obj.session_epoch or 0
        if in_epoch is not None and in_epoch > now_epoch:
            forced['session_epoch'] = in_epoch
        codes = _merge_backup_codes(obj.totp_backup_codes, values.get('totp_backup_codes'))
        if codes is not None:
            values['totp_backup_codes'] = codes
            if codes != obj.totp_backup_codes:
                forced['totp_backup_codes'] = codes
        if self.side == 'node':
            if obj.role == 'owner':
                for name in OWNER_PROTECTED_COLUMNS:
                    if name != 'username' and name not in forced and name in values \
                            and values[name] != getattr(obj, name):
                        forced[name] = values[name]
            if values.get('is_active') is False and obj.is_active:
                forced['is_active'] = False
            if values.get('deleted_at') and obj.deleted_at is None:
                forced['deleted_at'] = values['deleted_at']
        if newer:
            if self._clashes(spec, obj, values):
                if self._deferred is not None:
                    return _DEFERRED
                newer = False
        if newer:
            apply = dict(values)
            if in_epoch is not None and in_epoch < now_epoch:
                for name in STAFF_SECURITY_COLUMNS:
                    apply.pop(name, None)
            if 'totp_last_counter' in apply:   # 防重放的時間窗只增不減
                apply['totp_last_counter'] = max(apply['totp_last_counter'] or 0, obj.totp_last_counter or 0)
            apply.update(forced)
            for name, value in apply.items():
                setattr(obj, name, value)
        elif forced:
            for name, value in forced.items():
                setattr(obj, name, value)
            flag_modified(obj, 'updated_at')   # 保留原本的 updated_at
        return None

    def _first_merge(self, spec, obj, values):
        """
        自然鍵 / 每店一筆的資料第一次配對 (線上端記下別名、分店把本機那一筆改用線上的 uuid)：不比較 updated_at。
        以線上端那一筆為準，另一筆只補空白欄位；會員的拒絕行銷取「任一邊拒絕」、生日禮年份取較大者。
        (分店開機建立的預設紅利設定 / 加料、分店離線時重新註冊的空白會員資料，不會因為建立時間較新而蓋掉線上端的設定)
        合併後的時間戳記比雙方都新，讓各主機都改成合併後的內容。
        """
        hub = self.side == 'hub'
        contributed = set()   # 由非線上端那一筆補上的欄位
        merged = {}
        for name, value in values.items():
            if name == 'updated_at':
                continue
            local = getattr(obj, name)
            canonical, other = (local, value) if hub else (value, local)
            if spec.name == 'user' and name == 'marketing_opt_out':
                result = bool(canonical) or bool(other)
            elif spec.name == 'user' and name == 'birthday_gift_year':
                result = max(canonical or 0, other or 0)
            elif _blank(canonical) and not _blank(other):
                result = other
            else:
                result = canonical
            if result is not canonical and result != canonical:
                contributed.add(name)
            merged[name] = result
        old_photo = obj.photo_path if spec.name == 'user' else None
        for name, result in merged.items():
            if hub and name not in contributed:
                continue   # 線上端：保留既有值
            if not hub and name in contributed and result is getattr(obj, name):
                continue   # 分店：保留本機補上的值
            setattr(obj, name, result)
        if hub or contributed:
            obj.updated_at = _bump(obj.updated_at, values.get('updated_at'))
            db.session.flush()
            # 讓合併後的內容再傳一次 (線上端：下發給各分店；分店：補上的欄位上傳給線上端)
            tracking.mark_rows(db.session, spec.model, spec.model.id == obj.id)
        else:
            obj.updated_at = values.get('updated_at') or obj.updated_at
        if old_photo and old_photo != obj.photo_path:   # 本機舊照片已不再使用
            remove_upload(current_app.config['UPLOAD_FOLDER_MEMBER'], old_photo)
        self._after_change(spec, obj, created=False)

    # ---- 封存 ----
    def _archived(self, spec, uuid, created_at=None):
        """本機已封存 (搬到封存檔並從主資料庫刪除) 的訂單 / 明細"""
        if created_at is not None and created_at > datetime.now() - _ARCHIVE_MIN_AGE:
            return False
        if self._archive is None:
            from app.services import archive_service
            path = archive_service.archive_path(self.tenant_id)
            try:
                self._archive = sqlite3.connect(path) if os.path.isfile(path) else False
            except sqlite3.Error:
                self._archive = False
        if not self._archive:
            return False
        try:
            with closing(self._archive.execute(f'SELECT 1 FROM "{spec.name}" WHERE uuid = ? LIMIT 1', (uuid,))) as cur:
                return cur.fetchone() is not None
        except sqlite3.Error:
            return False

    def _close_archive(self):
        if self._archive:
            self._archive.close()
        self._archive = None

    def _merge_order(self, spec, order, values):
        if self.side == 'hub' and order.node_uuid != self.source_node_uuid:
            self.conflict(spec, order.uuid, '不可修改其他分店的訂單')
            return
        old_status, old_ds = order.status, (order.delivery_status or '')
        changed = False
        newer = values.get('updated_at') and (order.updated_at is None or values['updated_at'] > order.updated_at)
        # 線上端：分店換桌 / 併桌 (改寫 table_number) 或會員刪除後的去識別化，以同分店較新的版本為準
        # (已去識別化的訂單不再被尚未收到刪除紀錄的舊名稱蓋回)
        incoming_tn = values.get('table_number')
        if self.side == 'hub' and incoming_tn is not None and newer and incoming_tn != (order.table_number or '') \
                and order.table_number != _DELETED_MEMBER:
            order.table_number, changed = incoming_tn[:50], True
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
        if order.estimated_ready_at is None and values.get('estimated_ready_at'):   # 分店收到線上訂單後估的完成時間
            order.estimated_ready_at, changed = values['estimated_ready_at'], True
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
        # 對方已完成退款：本機已付款 / 已結帳 / 需退款都改成已退款 (同一批收到「已取消 + 已退款」時，
        # 呼叫端的 flag_refund_if_paid 只處理 paid / settled，不會再把它標成需退款)
        if incoming_pay == 'refunded' and (order.payment_status or '') in _REFUNDABLE_PAY:
            order.payment_status, changed = 'refunded', True
            if self.side == 'node':
                self._close_cancel_refunds(order)
        # 加急：以較新的為準 (線上端對已送到分店的訂單按加急，出餐分店的廚房也要看到)
        if 'is_rush' in values and newer and bool(values['is_rush']) != bool(order.is_rush):
            order.is_rush, changed = bool(values['is_rush']), True
        # 自有外送的配送狀態只往前走 (分店派車 / 送達後同步回線上端，顧客在追蹤頁看到)；同一階段改派外送員以較新的為準
        incoming_ds = values.get('delivery_status') or ''
        rank_in = DELIVERY_STATUS_RANK.get(incoming_ds, 0)
        rank_now = DELIVERY_STATUS_RANK.get(order.delivery_status or '', 0)
        if rank_in > rank_now or (rank_in == rank_now and rank_in and newer
                                  and (values.get('driver_name') or '') != (order.driver_name or '')):
            order.delivery_status = incoming_ds
            order.driver_name = values.get('driver_name') or order.driver_name
            order.dispatched_at = values.get('dispatched_at') or order.dispatched_at
            order.delivered_at = values.get('delivered_at') or order.delivered_at
            changed = True
        # 外送員定位憑證：指派時產生，之後不變 (外送員手機以此回報位置到線上端)
        if values.get('driver_token') and values['driver_token'] != (order.driver_token or ''):
            if self.side == 'hub' and order.driver_token:
                # 改派外送員 (換了新憑證)：清掉前一位外送員回報的位置，顧客追蹤頁不會先看到舊外送員的位置
                from app.models.order import DeliveryPosition
                db.session.execute(sa.delete(DeliveryPosition).where(
                    DeliveryPosition.order_uuid == order.uuid, DeliveryPosition.tenant_id == order.tenant_id)
                    .execution_options(synchronize_session=False, skip_tenant_filter=True))
            order.driver_token, changed = values['driver_token'], True
        if changed:
            order.updated_at = max(filter(None, (order.updated_at, values.get('updated_at'))), default=None)
            self.result.orders_changed = True
            self.result.changed_order_ids.add(order.id)
            if self.side == 'hub':
                # 出餐 / 外送出發在分店，推播訂閱在線上端：由線上端補發 (deferred，同步請求提交後才送出)
                from app.services import push_service
                push_service.notify_synced_order(order, old_status, old_ds)

    @staticmethod
    def _close_cancel_refunds(order):
        """
        分店：線上端已完成退款 (例如自動退刷)。退款紀錄只上傳不下發，本機這張訂單未完成的作廢退款一併標記完成，
        否則分店一直顯示「需人工退款」。
        """
        from app.models.payment import Refund
        rows = Refund.query.filter(Refund.order_id == order.id, Refund.kind == 'cancel', Refund.status != 'done') \
            .execution_options(skip_tenant_filter=True).all()
        for refund in rows:
            refund.status, refund.completed_at = 'done', datetime.now()
            refund.provider_result = '線上端已向金流退款'

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
        if self.side == 'hub':
            reason = self._tombstone_denied(spec, target)
            if reason:
                if reason is not True:
                    self.conflict(spec, row_uuid, reason)
                return
        exists = db.session.execute(sa.select(SyncTombstone.id).where(SyncTombstone.uuid == uuid)).first()
        if exists is None:
            db.session.add(SyncTombstone(uuid=uuid, tenant_id=self.tenant_id, table_name=table_name, row_uuid=row_uuid,
                                         deleted_at=_decode_value(SyncTombstone.__table__.c.deleted_at,
                                                                  data.get('deleted_at')) or datetime.now()))
        if spec.name == 'order':
            return  # 訂單不因同步刪除 (只會取消)
        if spec.name == 'user' and target is not None:
            # 會員在另一台主機刪除：本機訂單上的姓名 / 已結束配送的地址電話、進場紀錄一併去識別化 (刪除會員後就找不到這些訂單)
            from types import SimpleNamespace

            from app.services.privacy_service import _deidentify_member_records
            _deidentify_member_records(SimpleNamespace(id=target['id'], tenant_id=self.tenant_id, phone=target['phone']))
            if target['photo_path']:
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
    if table_name == 'user':   # 推薦人以 uuid 字串記錄 (不是外鍵)：一併改指 canonical
        _repoint_referrals(tenant_id, alias_uuid, canonical_uuid)
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


def _repoint_referrals(tenant_id, old_uuid, new_uuid):
    """會員的 uuid 因同手機合併而改變：被推薦人記錄的推薦人 uuid (referred_by_uuid，純字串) 跟著改"""
    if not old_uuid or old_uuid == new_uuid:
        return
    db.session.execute(sa.update(User).where(User.tenant_id == tenant_id, User.referred_by_uuid == old_uuid)
                       .values(referred_by_uuid=new_uuid)
                       .execution_options(skip_tenant_filter=True, synchronize_session='fetch'))
