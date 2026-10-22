"""
導入 Alembic 之前的舊版 SQLite 資料庫升級 (只會對沒有 alembic_version 的資料庫執行一次)。

舊版程式每次啟動都「檢查欄位 → 補欄位 → 修資料」，各台電腦的資料庫因此結構不一
(多出廢棄欄位、預設值寫法不同、tenant_id 可為空…)。這裡用 SQLite 官方建議的「建新表 → 複製 → 換名」
把每張表整理成 baseline_schema 的結構，之後就交給 Alembic 遷移。

順帶處理：回填店家 (tenant_id)、合併同店重複手機的會員、紅利設定去重、
舊版 Pickle 人臉特徵轉為安全格式、舊訂單實收金額回填。
"""
import io
import logging
import pickle

import numpy as np
import sqlalchemy as sa

from app.models.user import FEATURE_MAGIC_PREFIX, as_valid_feature, decode_feature_bytes, encode_feature_array
from app.services.baseline_schema import BASELINE

logger = logging.getLogger(__name__)
OBSOLETE_TABLES = {'system_setting'}

# 舊版 Pickle 特徵只允許還原 NumPy 陣列 (拒絕任何可執行程式碼的 payload)
_NUMPY_MODULES = ('numpy', 'numpy.core.multiarray', 'numpy.core.numeric', 'numpy._core.multiarray', 'numpy._core.numeric')
_NUMPY_NAMES = ('_reconstruct', 'ndarray', 'dtype', 'scalar', 'frombuffer')


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module in _NUMPY_MODULES and name in _NUMPY_NAMES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Blocked unsafe pickle class: {module}.{name}")


def decode_legacy_feature(raw):
    """舊版 Pickle 特徵 → numpy 向量；不合法或含惡意 payload 時回傳 None"""
    try:
        arr = _RestrictedUnpickler(io.BytesIO(bytes(raw))).load()
    except Exception:
        return None
    return as_valid_feature(arr) if isinstance(arr, np.ndarray) else None


def _fill_value(column, default_tenant_id):
    """舊表缺少、但 baseline 要求非空的欄位要填的值"""
    if column.name == 'tenant_id':
        return default_tenant_id
    if isinstance(column.type, sa.DateTime):
        return sa.func.current_timestamp()
    if isinstance(column.type, (sa.String, sa.Text)):
        return ''
    return 0


def _rebuild_table(conn, table, default_tenant_id):
    """依 baseline 定義重建一張舊表：建新表 (含索引) → 複製共同欄位 → 刪舊表 → 換名"""
    old_cols = {c['name'] for c in sa.inspect(conn).get_columns(table.name)}
    tmp_md = sa.MetaData()
    for t in BASELINE.tables.values():  # 外鍵參照需要能在同一個 MetaData 中解析
        if t is not table:
            t.to_metadata(tmp_md)
    tmp = table.to_metadata(tmp_md, name=f'_legacy_{table.name}')
    conn.execute(sa.schema.CreateTable(tmp))  # 只建表，索引等換名後再建 (名稱與正式表相同)

    old = sa.table(table.name, *(sa.column(n) for n in old_cols))
    exprs = []
    for col in table.columns:
        fill = _fill_value(col, default_tenant_id)
        fill = fill if isinstance(fill, sa.sql.ClauseElement) else sa.literal(fill)
        if col.name in old_cols:
            expr = old.c[col.name] if col.nullable or col.name == 'id' else sa.func.coalesce(old.c[col.name], fill)
        else:
            expr = sa.null() if col.nullable else fill
        exprs.append(expr.label(col.name))
    conn.execute(tmp.insert().from_select([c.name for c in table.columns], sa.select(*exprs)))
    conn.exec_driver_sql(f'DROP TABLE "{table.name}"')
    conn.exec_driver_sql(f'ALTER TABLE "{tmp.name}" RENAME TO "{table.name}"')
    for index in table.indexes:
        index.create(conn)


def patch_pre_alembic_schema(conn):
    """conn：SQLite 連線 (呼叫端負責交易)。執行前須已建立 baseline 中缺少的資料表。"""
    conn.exec_driver_sql('PRAGMA foreign_keys=OFF')
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    tenant_id = conn.exec_driver_sql("SELECT id FROM tenant WHERE slug = 'default'").scalar()
    if tenant_id is None:
        conn.exec_driver_sql("INSERT INTO tenant (name, slug, is_active, created_at) "
                             "VALUES ('預設店家', 'default', 1, CURRENT_TIMESTAMP)")
        tenant_id = conn.exec_driver_sql("SELECT id FROM tenant WHERE slug = 'default'").scalar()

    def has_col(t, c):
        return c in {col['name'] for col in insp.get_columns(t)}

    # 唯一索引建立前先處理重複資料
    if 'user' in tables:
        tenant_expr = f'COALESCE(tenant_id, {tenant_id})' if has_col('user', 'tenant_id') else str(tenant_id)
        _merge_duplicate_phones(conn, tenant_expr)
    if 'reward_setting' in tables:  # 每店只能一筆：保留最早建立的設定
        group = f' GROUP BY COALESCE(tenant_id, {tenant_id})' if has_col('reward_setting', 'tenant_id') else ''
        conn.exec_driver_sql(f'DELETE FROM reward_setting WHERE id NOT IN (SELECT MIN(id) FROM reward_setting{group})')

    for table in BASELINE.sorted_tables:
        if table.name in tables:
            for index in insp.get_indexes(table.name):
                conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{index["name"]}"')
            _rebuild_table(conn, table, tenant_id)

    # 更早期版本留下、目前已不使用的資料表：沒有資料才刪除
    for obsolete in OBSOLETE_TABLES & tables:
        if not conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{obsolete}"').scalar():
            conn.exec_driver_sql(f'DROP TABLE "{obsolete}"')

    _convert_pickle_features(conn)
    # 舊訂單沒有記錄實收金額與來源：以應付金額回填，避免匯出報表實收顯示 $0
    conn.exec_driver_sql("""
        UPDATE "order" SET order_source = COALESCE(NULLIF(order_source, ''), 'Kiosk'), cash_received = total_price
        WHERE (cash_received IS NULL OR cash_received = 0)
          AND (order_source IS NULL OR order_source IN ('', 'Kiosk')) AND total_price > 0
    """)
    conn.exec_driver_sql('PRAGMA foreign_keys=ON')


def _merge_duplicate_phones(conn, tenant_expr):
    """同店重複手機的會員：保留點數最高者，訂單與優惠券歸戶後刪除其餘帳號"""
    groups = conn.exec_driver_sql(
        f'SELECT {tenant_expr} AS t, phone FROM user GROUP BY t, phone HAVING COUNT(*) > 1').all()
    for tid, phone in groups:
        rows = conn.execute(sa.text(f'SELECT id FROM user WHERE {tenant_expr} = :t AND phone = :p '
                                    'ORDER BY COALESCE(points, 0) DESC, id ASC'), {'t': tid, 'p': phone}).all()
        keep_id = rows[0][0]
        for (dup_id,) in rows[1:]:
            for table in ('"order"', 'user_coupon'):
                conn.execute(sa.text(f'UPDATE {table} SET user_id = :k WHERE user_id = :d'), {'k': keep_id, 'd': dup_id})
            conn.execute(sa.text('DELETE FROM user WHERE id = :d'), {'d': dup_id})


def _convert_pickle_features(conn):
    bad_ids = []
    for uid, raw in conn.exec_driver_sql('SELECT id, feature FROM user WHERE feature IS NOT NULL').all():
        raw = bytes(raw)
        if raw.startswith(FEATURE_MAGIC_PREFIX):
            if decode_feature_bytes(raw) is None:
                bad_ids.append(uid)
            continue
        vec = decode_legacy_feature(raw)
        if vec is None:
            bad_ids.append(uid)  # 解不開：保留原始資料，不覆寫成 NULL
            continue
        conn.execute(sa.text('UPDATE user SET feature = :f WHERE id = :id'), {'f': encode_feature_array(vec), 'id': uid})
    if bad_ids:
        logger.warning('有 %d 筆會員舊特徵無法解析或維度異常，已保留原始資料未覆寫 (user id: %s)', len(bad_ids), bad_ids[:20])
