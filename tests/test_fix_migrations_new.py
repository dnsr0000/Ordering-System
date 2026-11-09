"""新遷移 (0030 ~ 0033) 的回歸測試：缺欄位補救、欄位加長與外鍵改 SET NULL、新增欄位、歷史資料回填、可降版再升級"""
import os
import sqlite3
from datetime import datetime

import sqlalchemy as sa

from tests.conftest import TestConfig


def _make_app(tmp_path):
    """獨立的 SQLite 資料庫 (不自動升級，由測試決定升級到哪一版)"""
    from app import create_app
    base = str(tmp_path)
    for sub in ('database', 'member', 'menu'):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    db_path = os.path.join(base, 'database', 'menu.db')
    cfg = type('MigrationsNewConfig', (TestConfig,), dict(
        SQLALCHEMY_DATABASE_URI='sqlite:///' + db_path, DATABASE_DIR=os.path.join(base, 'database'),
        UPLOAD_FOLDER_MEMBER=os.path.join(base, 'member'), UPLOAD_FOLDER_MENU=os.path.join(base, 'menu'),
        AUTO_MIGRATE=False))
    return create_app(cfg), db_path


def _columns(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return {r[1]: r for r in conn.execute(f'PRAGMA table_info("{table}")')}
    finally:
        conn.close()


def _insert(conn, table, **values):
    """插入一列：未指定的必填欄位 (沒有預設值) 自動填入假值"""
    for _cid, name, col_type, notnull, default, pk in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
        if name in values or pk or not notnull or default is not None:
            continue
        t = (col_type or '').upper()
        if 'INT' in t or 'BOOL' in t or 'FLOAT' in t or 'NUMERIC' in t:
            values[name] = 0
        elif 'DATE' in t or 'TIME' in t:
            values[name] = '2026-01-01 00:00:00.000000'
        else:
            values[name] = f'{name}-{len(values)}-{os.urandom(4).hex()}'
    cols = ', '.join(f'"{c}"' for c in values)
    cur = conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({", ".join("?" for _ in values)})', list(values.values()))
    return cur.lastrowid


def test_upgrade_to_head_schema(tmp_path):
    """升到最新版：欄位加長 / 改 Text、table_payment 外鍵改 SET NULL 可空、新增欄位與索引都在"""
    from flask_migrate import upgrade
    from app.extensions import db
    app2, db_path = _make_app(tmp_path)
    with app2.app_context():
        upgrade()
        insp = sa.inspect(db.engine)
        types = {(t, c['name']): str(c['type']) for t in ('admin_log', 'staff_audit_log', 'order', 'settlement',
                                                            'table_payment') for c in insp.get_columns(t)}
        assert types[('admin_log', 'username')] == 'VARCHAR(120)'
        assert types[('staff_audit_log', 'staff_username')] == 'VARCHAR(120)'
        assert types[('order', 'cashier_username')] == 'VARCHAR(120)'
        assert types[('settlement', 'order_ids')] == 'TEXT'
        assert types[('table_payment', 'order_ids')] == 'TEXT'
        fks = [fk for fk in insp.get_foreign_keys('table_payment') if fk['referred_table'] == 'dining_table']
        assert len(fks) == 1 and fks[0]['options'].get('ondelete') == 'SET NULL'
        assert fks[0]['name'] == 'fk_table_payment_dining_table_id'
        assert {c['name']: c for c in insp.get_columns('table_payment')}['dining_table_id']['nullable']
        assert [fk for fk in insp.get_foreign_keys('table_payment') if fk['referred_table'] == 'tenant']
        indexes = {(t, i['name']): i for t in ('order', 'payment_record', 'refund', 'gift_card', 'table_payment')
                   for i in insp.get_indexes(t)}
        for key in (('order', 'ix_order_tenant_feed_rev'), ('order', 'ix_order_paid_at'),
                    ('payment_record', 'ix_payment_record_node_uuid'), ('table_payment', 'ix_table_payment_dining_table_id')):
            assert key in indexes, key
        for key in (('payment_record', 'uq_payment_record_tenant_request'), ('refund', 'uq_refund_tenant_request'),
                    ('gift_card', 'ix_gift_card_tenant_request_id')):
            assert indexes[key]['unique'], key
        db.engine.dispose()
    assert {'feed_rev', 'promotion_details', 'payment_refs'} <= set(_columns(db_path, 'order'))
    assert 'prepared_stations' in _columns(db_path, 'order_item')
    assert {'node_uuid', 'request_id'} <= set(_columns(db_path, 'payment_record'))
    assert 'request_id' in _columns(db_path, 'refund') and 'request_id' in _columns(db_path, 'gift_card')
    assert 'previous_status' in _columns(db_path, 'marketing_campaign')
    assert 'own_delivery_json' in _columns(db_path, 'sync_node')


def test_b503_head_database_missing_deleted_at_repaired(tmp_path):
    """修正前就升到 0029、卻缺少 staff_account.deleted_at 的資料庫：0030 補上欄位"""
    from flask_migrate import upgrade
    from app.extensions import db
    app2, db_path = _make_app(tmp_path)
    with app2.app_context():
        upgrade(revision='0029_support_chat')
        db.engine.dispose()
    conn = sqlite3.connect(db_path)
    conn.execute('DROP INDEX IF EXISTS ix_staff_account_deleted_at')
    conn.execute('ALTER TABLE staff_account DROP COLUMN deleted_at')
    conn.commit()
    conn.close()
    assert 'deleted_at' not in _columns(db_path, 'staff_account')
    with app2.app_context():
        upgrade()
        db.engine.dispose()
    assert 'deleted_at' in _columns(db_path, 'staff_account')


def test_b499_deleting_table_keeps_table_payment(tmp_path):
    """刪除桌位：桌邊線上結帳紀錄保留 (桌位設為空)，長的訂單清單也存得下"""
    from flask_migrate import upgrade
    from app.extensions import db
    app2, db_path = _make_app(tmp_path)
    with app2.app_context():
        upgrade()
        db.engine.dispose()
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA foreign_keys=ON')
    tid = _insert(conn, 'tenant', name='遷移店', slug='mig-new', is_active=1)
    table_id = _insert(conn, 'dining_table', tenant_id=tid, name='A1')
    ids = ','.join(str(100000 + i) for i in range(80))
    tp_id = _insert(conn, 'table_payment', tenant_id=tid, dining_table_id=table_id, order_ids=ids, amount=100,
                    trade_no='MIGNEW0001', status='pending')
    conn.execute('DELETE FROM dining_table WHERE id = ?', (table_id,))
    conn.commit()
    assert conn.execute('SELECT dining_table_id, order_ids FROM table_payment WHERE id = ?', (tp_id,)).fetchone() == (None, ids)
    conn.close()


def test_b507_r2_198_backfill_and_downgrade(tmp_path):
    """回填會員累積消費 (與 recompute_spent 同條件) 與 payment_record 啟用前的歷史收款；降版只刪回填的紀錄"""
    from flask_migrate import downgrade, upgrade
    from app.extensions import db
    app2, db_path = _make_app(tmp_path)
    with app2.app_context():
        upgrade(revision='0032_local_cols_idempotency')
        db.engine.dispose()
    conn = sqlite3.connect(db_path)
    t1 = _insert(conn, 'tenant', name='舊店', slug='mig-old', is_active=1)
    t2 = _insert(conn, 'tenant', name='全新記錄店', slug='mig-none', is_active=1)
    uid = _insert(conn, 'user', tenant_id=t1, name='舊會員', phone='0900111222', archived_spent=30, total_spent=0)

    def order(tid, created, total=100, status='Completed', pay='', method='Cash', paid_at=None, settlement_id=None,
              refunded=0, user_id=None):
        return _insert(conn, 'order', tenant_id=tid, user_id=user_id, total_price=total, status=status,
                       payment_status=pay, payment_method=method, created_at=created, paid_at=paid_at,
                       settlement_id=settlement_id, refunded_amount=refunded)
    old = order(t1, '2026-01-01 10:00:00.000000', total=200, method='Credit Card', user_id=uid, refunded=50)
    old_paid = order(t1, '2026-01-02 10:00:00.000000', total=80, pay='paid', paid_at='2026-01-02 10:05:00.000000',
                     method=None, user_id=uid)
    order(t1, '2026-01-03 10:00:00.000000', status='Cancelled', user_id=uid)            # 取消：不補、不算
    order(t1, '2026-01-04 10:00:00.000000', pay='unpaid', user_id=uid)                  # 未收款：不補、不算
    order(t1, '2026-01-05 10:00:00.000000', total=0)                                    # 0 元：不補
    order(t1, '2026-01-06 10:00:00.000000', pay='settled', settlement_id=9)             # 桌位結帳：不補
    has_record = order(t1, '2026-01-07 10:00:00.000000', total=70)
    _insert(conn, 'payment_record', tenant_id=t1, order_id=has_record, kind='refund', method='Cash', amount=-70,
            received=0, change=0, reference='', card_last4='', note='', staff_username='x',
            created_at='2026-01-08 00:00:00.000000', uuid='mig-pr-1')
    _insert(conn, 'payment_record', tenant_id=t1, order_id=None, kind='sale', method='Cash', amount=10,
            received=10, change=0, reference='', card_last4='', note='', staff_username='x',
            created_at='2026-02-01 00:00:00.000000', uuid='mig-pr-2')
    after = order(t1, '2026-03-01 10:00:00.000000', user_id=uid)                        # cutoff 之後：不補，但算消費
    other = order(t2, '2026-05-01 10:00:00.000000', total=55)                           # 沒有紀錄的店家：不設上限
    conn.commit()
    conn.close()

    with app2.app_context():
        upgrade()
        db.engine.dispose()
    conn = sqlite3.connect(db_path)
    try:
        # (200 - 50) + 80 + 100 (cutoff 之後的訂單) + 30 (archived_spent)
        assert conn.execute('SELECT total_spent FROM "user" WHERE id = ?', (uid,)).fetchone()[0] == 360
        rows = conn.execute("SELECT order_id, kind, method, amount, received, change, staff_username, sync_rev, "
                            "created_at, tenant_id FROM payment_record WHERE staff_username = 'system' "
                            "ORDER BY order_id").fetchall()
        assert [(r[0], r[1], r[2], r[3], r[4], r[5], r[7], r[9]) for r in rows] == [
            (old, 'sale', 'Credit Card', 200, 200, 0, 0, t1),
            (old_paid, 'sale', 'Cash', 80, 80, 0, 0, t1),
            (other, 'sale', 'Cash', 55, 55, 0, 0, t2)]
        assert rows[1][8].startswith('2026-01-02 10:05')
        assert after not in [r[0] for r in rows] and has_record not in [r[0] for r in rows]
        # uuid 由訂單 uuid 固定算出：線上端與分店替同一張訂單回填時 uuid 相同 (insert_only 同步只留一筆)
        import importlib
        mod = importlib.import_module('migrations.versions.0033_backfill_spent_payments')
        pairs = conn.execute("SELECT p.uuid, o.uuid FROM payment_record p JOIN \"order\" o ON o.id = p.order_id "
                             "WHERE p.staff_username = 'system'").fetchall()
        assert len(pairs) == 3 and all(p == mod._backfill_uuid(o) for p, o in pairs)
    finally:
        conn.close()

    with app2.app_context():
        downgrade(revision='0029_support_chat')
        db.engine.dispose()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM payment_record WHERE staff_username = 'system'").fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM payment_record').fetchone()[0] == 2
        assert 'feed_rev' not in _columns(db_path, 'order')
        assert _columns(db_path, 'admin_log')['username'][2] == 'VARCHAR(50)'
    finally:
        conn.close()
    with app2.app_context():
        upgrade()   # 降版後可再升級
        db.engine.dispose()
    assert 'feed_rev' in _columns(db_path, 'order')


def test_feed_rev_backfilled_from_sync_rev(tmp_path):
    """feed_rev 以 sync_rev 回填：API 串接端已存的游標仍有效"""
    from flask_migrate import upgrade
    from app.extensions import db
    app2, db_path = _make_app(tmp_path)
    with app2.app_context():
        upgrade(revision='0031_widen_cols_tablepay_fk')
        db.engine.dispose()
    conn = sqlite3.connect(db_path)
    tid = _insert(conn, 'tenant', name='游標店', slug='mig-feed', is_active=1)
    oid = _insert(conn, 'order', tenant_id=tid, total_price=10, sync_rev=42, created_at=str(datetime(2026, 1, 1)))
    conn.commit()
    conn.close()
    with app2.app_context():
        upgrade()
        db.engine.dispose()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT feed_rev FROM "order" WHERE id = ?', (oid,)).fetchone()[0] == 42
    finally:
        conn.close()
