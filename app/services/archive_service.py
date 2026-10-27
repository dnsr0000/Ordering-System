"""
舊資料封存 (第三輪 #16)：把超過 N 個月的已結束訂單 (含明細、收款、退款、發票、評價) 與各種日誌，
搬到 database/archive/archive_t<店家>.db (SQLite，可下載、可在後台查詢)，再從主資料庫刪除，讓主資料庫保持輕量。

- 只封存已結束的訂單：已取餐 / 已出餐 / 已取消，且沒有待付款、待退款 / 未結帳。
- 先寫入封存檔再刪除主資料庫，每 500 張訂單一批 (每批先鎖住店家，多個行程同時封存也只處理一次)。
  封存檔以 uuid 為主鍵 (主資料庫的整數 id 刪除後可能被重複使用)，明細另記 _order_uuid 對應訂單；同一筆重跑只會覆蓋自己。
- 刪除不產生同步刪除紀錄 (只清本機；線上端與其他分店的資料不受影響，線上端可自行封存)。
- 會員累積消費 (等級升級依據) 先把封存訂單的金額記到 user.archived_spent (本機欄位)，等級不會因封存而改變。
- 點數 / 儲值金 / 禮物卡 / 寄杯異動帳不封存 (餘額以異動帳合計為準)。
- 依法帳簿憑證應保存 5 年以上：封存檔請妥善備份，不要刪除。
"""
import os
import threading
from datetime import date, datetime, timedelta

import sqlalchemy as sa
from flask import current_app

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.user import User
from app.services import settings_service

BATCH = 500
FINAL_STATUSES = (OrderStatus.PICKED_UP, OrderStatus.COMPLETED, OrderStatus.CANCELLED)
OPEN_PAYMENT = ('pending', 'unpaid', 'authorizing', 'refund_required')
ORDER_CHILDREN = ('order_item', 'order_feedback', 'payment_record', 'refund', 'invoice')
# 日誌：資料表 → 日期欄位 (額外條件)
LOG_TABLES = {
    'customer_log': ('login_at', None),
    'admin_log': ('login_at', None),
    'staff_audit_log': ('created_at', None),
    'stock_movement': ('created_at', None),
    'print_job': ('created_at', ('status', ('done', 'failed'))),
    'service_call': ('created_at', None),
    'webhook_delivery': ('created_at', ('status', ('sent', 'failed'))),
    'payment_record': ('created_at', ('order_id', None)),   # 沒有訂單的收款 (儲值、禮物卡、零用金)
}
LABELS = {'order': '訂單', 'order_item': '訂單明細', 'order_feedback': '評價', 'payment_record': '收款紀錄', 'refund': '退款',
          'invoice': '發票', 'customer_log': '顧客登入紀錄', 'admin_log': '後台登入紀錄', 'staff_audit_log': '員工稽核紀錄',
          'stock_movement': '原料異動', 'print_job': '列印紀錄', 'service_call': '桌邊呼叫', 'webhook_delivery': 'Webhook 紀錄'}
_lock = threading.Lock()


def cutoff_for(tenant_id, today=None):
    months = int(settings_service.get('archive', tenant_id).get('months') or 24)
    today = today or date.today()
    y, m = today.year, today.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    return datetime(y, m, 1)


def archive_dir():
    path = os.path.join(current_app.config['DATABASE_DIR'], 'archive')
    os.makedirs(path, exist_ok=True)
    return path


def archive_path(tenant_id):
    return os.path.join(archive_dir(), f'archive_t{tenant_id}.db')


def _main_table(name):
    return db.metadata.tables[name]


def _eligible_orders(tenant_id, cutoff):
    t = _main_table('order')
    return sa.select(t.c.id).where(t.c.tenant_id == tenant_id, t.c.created_at < cutoff, t.c.status.in_(FINAL_STATUSES),
                                   sa.func.coalesce(t.c.payment_status, '').notin_(OPEN_PAYMENT))


def _log_where(name, tenant_id, cutoff):
    t = _main_table(name)
    col, extra = LOG_TABLES[name]
    cond = [t.c.tenant_id == tenant_id, t.c[col] < cutoff]
    if extra:
        field, values = extra
        cond.append(t.c[field].is_(None) if values is None else t.c[field].in_(values))
    return cond


def preview(tenant_id, cutoff):
    """{資料表: 筆數}"""
    out = {}
    ids = sa.select(sa.func.count()).select_from(_eligible_orders(tenant_id, cutoff).subquery())
    out['order'] = db.session.execute(ids).scalar() or 0
    for name in ORDER_CHILDREN:
        t = _main_table(name)
        out[name] = db.session.execute(sa.select(sa.func.count()).select_from(t).where(
            t.c.order_id.in_(_eligible_orders(tenant_id, cutoff)))).scalar() or 0
    for name in LOG_TABLES:
        t = _main_table(name)
        out[name] = out.get(name, 0) + (db.session.execute(sa.select(sa.func.count()).select_from(t)
                                                           .where(*_log_where(name, tenant_id, cutoff))).scalar() or 0)
    return out


def _archive_engine(tenant_id):
    engine = sa.create_engine('sqlite:///' + archive_path(tenant_id))
    meta = sa.MetaData()
    for name in ('order',) + ORDER_CHILDREN + tuple(LOG_TABLES):
        if name in meta.tables:
            continue
        src = _main_table(name)
        key = 'uuid' if 'uuid' in src.c else 'id'
        cols = [sa.Column(c.name, c.type, primary_key=(c.name == key)) for c in src.columns]
        if name in ORDER_CHILDREN:
            cols.append(sa.Column('_order_uuid', sa.String(36), index=True))
        sa.Table(name, meta, *cols)
    meta.create_all(engine)
    return engine, meta


def _copy(conn, meta, name, rows, order_uuids=None):
    if not rows:
        return 0
    table = meta.tables[name]
    data = [dict(r._mapping) for r in rows]
    if name in ORDER_CHILDREN:
        for row in data:
            row['_order_uuid'] = (order_uuids or {}).get(row.get('order_id'))
    conn.execute(table.insert().prefix_with('OR REPLACE'), data)
    return len(data)


def run(tenant_id, cutoff, staff='system'):
    """執行封存；回傳 {資料表: 筆數, 'file': 路徑}。由這裡分批 commit。"""
    if cutoff > datetime.now() - timedelta(days=150):
        raise AppError('只能封存 6 個月以前的資料')
    if not _lock.acquire(blocking=False):
        raise AppError('封存正在進行中，請稍候', 409)
    try:
        engine, meta = _archive_engine(tenant_id)
        counts = {name: 0 for name in LABELS}
        order_t = _main_table('order')
        from app.services.db_locks import lock_tenant
        while True:
            lock_tenant(tenant_id)   # 每批先鎖店家 (另一個行程同時封存時等待，之後只會選到還沒封存的訂單)
            ids = [r[0] for r in db.session.execute(_eligible_orders(tenant_id, cutoff).order_by(order_t.c.id).limit(BATCH))]
            if not ids:
                db.session.commit()
                break
            # 會員累積消費：封存前記到 archived_spent (等級不因封存而降)
            spent = db.session.execute(
                sa.select(order_t.c.user_id, sa.func.sum(order_t.c.total_price - sa.func.coalesce(order_t.c.refunded_amount, 0)))
                .where(order_t.c.id.in_(ids), order_t.c.user_id.is_not(None), order_t.c.status != OrderStatus.CANCELLED,
                       sa.func.coalesce(order_t.c.payment_status, '').in_(('', 'settled', 'paid')))
                .group_by(order_t.c.user_id)).all()
            order_rows = db.session.execute(sa.select(order_t).where(order_t.c.id.in_(ids))).all()
            order_uuids = {r.id: r.uuid for r in order_rows}
            with engine.begin() as conn:
                counts['order'] += _copy(conn, meta, 'order', order_rows)
                for name in ORDER_CHILDREN:
                    t = _main_table(name)
                    counts[name] += _copy(conn, meta, name, db.session.execute(sa.select(t).where(t.c.order_id.in_(ids))).all(),
                                          order_uuids)
            for name in ORDER_CHILDREN:
                t = _main_table(name)
                db.session.execute(sa.delete(t).where(t.c.order_id.in_(ids)))
            db.session.execute(sa.delete(order_t).where(order_t.c.id.in_(ids)))
            for user_id, amount in spent:
                db.session.execute(sa.update(User.__table__).where(User.__table__.c.id == user_id)
                                   .values(archived_spent=User.__table__.c.archived_spent + int(amount or 0)))
            db.session.commit()
        for name in LOG_TABLES:
            t = _main_table(name)
            while True:
                rows = db.session.execute(sa.select(t).where(*_log_where(name, tenant_id, cutoff)).order_by(t.c.id)
                                          .limit(BATCH)).all()
                if not rows:
                    break
                with engine.begin() as conn:
                    counts[name] += _copy(conn, meta, name, rows)
                db.session.execute(sa.delete(t).where(t.c.id.in_([r.id for r in rows])))
                db.session.commit()
        engine.dispose()
        from app.services.audit import record_staff_audit
        record_staff_audit('ARCHIVE', tenant_id=tenant_id, as_system=staff == 'system',
                           details=f'封存 {cutoff:%Y-%m-%d} 以前的資料：' + '、'.join(
                               f'{LABELS[k]} {v}' for k, v in counts.items() if v))
        db.session.commit()
        counts['file'] = archive_path(tenant_id)
        return counts
    finally:
        _lock.release()


def file_info(tenant_id):
    path = archive_path(tenant_id)
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    engine = sa.create_engine('sqlite:///' + path)
    try:
        with engine.connect() as conn:
            n = conn.execute(sa.text('SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM "order"')).first()
    except sa.exc.SQLAlchemyError:
        n = (0, None, None)
    finally:
        engine.dispose()
    return {'path': path, 'size': st.st_size, 'modified': datetime.fromtimestamp(st.st_mtime), 'orders': n[0],
            'first': n[1], 'last': n[2]}


def search(tenant_id, q, limit=50):
    """封存檔中的訂單：依訂單編號、uuid、取餐號 + 日期 (YYYY-MM-DD) 或會員手機查詢"""
    path = archive_path(tenant_id)
    if not os.path.exists(path):
        return []
    q = str(q or '').strip()
    if not q:
        return []
    engine = sa.create_engine('sqlite:///' + path)
    try:
        with engine.connect() as conn:
            where, params = [], {'q': q}
            if q.isdigit():
                where.append('id = :q_int')
                params['q_int'] = int(q)
            where.append('uuid = :q')
            where.append('substr(created_at, 1, 10) = :q')
            user_ids = [u.id for u in User.query.filter(User.tenant_id == tenant_id, User.phone == q).all()]
            if user_ids:
                where.append('user_id IN (' + ','.join(str(int(i)) for i in user_ids) + ')')
            rows = conn.execute(sa.text(f'SELECT id, uuid, created_at, pickup_number, status, total_price, payment_method, '
                                        f'cashier_username FROM "order" WHERE tenant_id = :tid AND ({" OR ".join(where)}) '
                                        f'ORDER BY created_at DESC LIMIT {int(limit)}'), dict(params, tid=tenant_id)).all()
            out = []
            for r in rows:
                items = conn.execute(sa.text('SELECT item_name, quantity, price FROM order_item WHERE _order_uuid = :ou'),
                                     {'ou': r.uuid}).all()
                out.append({'id': r.id, 'uuid': r.uuid, 'created_at': str(r.created_at)[:16], 'pickup': r.pickup_number,
                            'status': r.status, 'total': r.total_price, 'method': r.payment_method,
                            'cashier': r.cashier_username, 'items': [f'{i.item_name} x{i.quantity}' for i in items]})
            return out
    finally:
        engine.dispose()


def run_monthly(tenant_id, today=None):
    """每月 1 日自動封存 (設定開啟時)"""
    if not settings_service.get('archive', tenant_id).get('auto'):
        return None
    return run(tenant_id, cutoff_for(tenant_id, today))
