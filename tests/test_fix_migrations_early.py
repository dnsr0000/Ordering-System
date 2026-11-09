"""遷移修正的回歸測試：舊資料庫升級 (baseline 缺欄位、孤兒兌換憑證)、SQLite 遷移中斷後可重跑"""
import os
import sqlite3

import pytest
import sqlalchemy as sa

from tests.conftest import TestConfig


def _make_app(tmp_path):
    """獨立資料庫的 app (不自動升級，由測試決定升級到哪一版)"""
    from app import create_app
    base = str(tmp_path)
    for sub in ('database', 'member', 'menu'):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    db_path = os.path.join(base, 'database', 'menu.db')
    cfg = type('MigrationsEarlyConfig', (TestConfig,), dict(
        SQLALCHEMY_DATABASE_URI='sqlite:///' + db_path, DATABASE_DIR=os.path.join(base, 'database'),
        UPLOAD_FOLDER_MEMBER=os.path.join(base, 'member'), UPLOAD_FOLDER_MENU=os.path.join(base, 'menu'),
        AUTO_MIGRATE=False))
    return create_app(cfg), db_path


def _legacy_db(db_path, drop_columns=()):
    """建立重構前 (無 alembic_version) 的舊資料庫：baseline 結構 + 一家店、一位會員"""
    from app.services.baseline_schema import BASELINE
    engine = sa.create_engine('sqlite:///' + db_path)
    BASELINE.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO tenant (id, name, slug, is_active, created_at) VALUES (1, '舊店', 'default', 1, '2026-01-01')")
    conn.execute("INSERT INTO user (id, name, phone, photo_path, points, tenant_id) VALUES (1, '舊會員', '0900000001', '', 50, 1)")
    for table, column in drop_columns:
        conn.execute(f'ALTER TABLE "{table}" DROP COLUMN {column}')
    conn.commit()
    conn.close()


def _columns(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    finally:
        conn.close()


def _version(db_path):
    conn = sqlite3.connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'alembic_version'").fetchone():
            return None
        row = conn.execute('SELECT version_num FROM alembic_version').fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_b505_orphan_reward_redemption_upgrades(tmp_path):
    """兌換憑證指向已刪除的餐點 (舊版刪餐點留下的孤兒)：0002 先改成可空再清孤兒，不會 NOT NULL 失敗"""
    from flask_migrate import upgrade
    app2, db_path = _make_app(tmp_path)
    _legacy_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute('INSERT INTO reward_redemption (id, user_id, menu_item_id, points_cost, is_used, is_refunded, tenant_id) '
                 'VALUES (1, 1, 99, 30, 1, 0, 1)')
    conn.commit()
    conn.close()
    with app2.app_context():
        upgrade(revision='0002_refactor_sync_ready')
    assert _version(db_path) == '0002_refactor_sync_ready'
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT menu_item_id, status FROM reward_redemption WHERE id = 1').fetchone() == (None, 'used')
    finally:
        conn.close()


def test_b503_baseline_adds_missing_nullable_columns(tmp_path):
    """10/22 前的資料庫沒有 staff_account.deleted_at：baseline 補上欄位，升級到最新後員工查詢正常"""
    from flask_migrate import upgrade
    app2, db_path = _make_app(tmp_path)
    _legacy_db(db_path, drop_columns=[('staff_account', 'deleted_at'), ('order', 'applied_user_coupon_id')])
    assert 'deleted_at' not in _columns(db_path, 'staff_account')
    with app2.app_context():
        upgrade()
        from app.extensions import db
        from app.models.tenant import StaffAccount
        StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(deleted_at=None).all()
        db.session.remove()
    assert 'deleted_at' in _columns(db_path, 'staff_account')
    assert 'applied_user_coupon_id' in _columns(db_path, 'order')


def test_b503_missing_required_column_aborts_cleanly(tmp_path, caplog):
    """缺少必填欄位：明確中止並說明原因，資料庫不留下半升級的狀態 (Flask-Migrate 把錯誤記錄後以 SystemExit 結束)"""
    from flask_migrate import upgrade
    app2, db_path = _make_app(tmp_path)
    _legacy_db(db_path, drop_columns=[('coupon', 'title')])
    with app2.app_context(), pytest.raises(SystemExit):
        upgrade()
    assert '缺少必要欄位 coupon.title' in caplog.text
    assert _version(db_path) is None
    conn = sqlite3.connect(db_path)
    try:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'daily_counter'").fetchone()
    finally:
        conn.close()


def test_b504_sqlite_migration_is_atomic_and_rerunnable(tmp_path):
    """SQLite 遷移在 DDL 之後中斷：整個遷移復原 (欄位與版本一致)，重跑可以完成，連線恢復原本設定"""
    from flask_migrate import upgrade
    from app.extensions import db
    app2, db_path = _make_app(tmp_path)
    with app2.app_context():
        upgrade(revision='0023_order_pickup_at_index')
        engine = db.engine

        def _interrupt(conn, cursor, statement, *args):
            if statement.startswith('UPDATE "order" SET client_order_id'):
                raise RuntimeError('模擬升級中斷')
        sa.event.listen(engine, 'before_cursor_execute', _interrupt)
        try:
            with pytest.raises(SystemExit):   # Flask-Migrate 把 RuntimeError 記錄後以 SystemExit 結束
                upgrade()
        finally:
            sa.event.remove(engine, 'before_cursor_execute', _interrupt)
        assert _version(db_path) == '0023_order_pickup_at_index'
        assert 'client_order_id' not in _columns(db_path, 'order')

        upgrade()
        from tests.support import head_revision
        assert _version(db_path) == head_revision()
        assert 'client_order_id' in _columns(db_path, 'order')
        with engine.connect() as conn:
            assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
            assert conn.connection.driver_connection.isolation_level is not None
        engine.dispose()


def test_b506_downgrade_message_points_to_real_backups():
    import importlib
    mod = importlib.import_module('migrations.versions.0002_refactor_sync_ready')
    with pytest.raises(NotImplementedError) as exc:
        mod.downgrade()
    assert 'menu-before-' in str(exc.value) and 'menu.db.bak-' not in str(exc.value)
