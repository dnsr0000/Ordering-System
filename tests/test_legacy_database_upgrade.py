"""舊版資料庫 (Alembic 之前的結構) 升級：人臉特徵轉檔、重複手機合併、舊訂單補值、舊上傳資料夾搬移"""
import os
import pickle
import sqlite3
import tempfile

import numpy as np
import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.order import Order
from app.models.user import FEATURE_MAGIC_PREFIX, RewardSetting, User
from app.services.legacy_upgrade import decode_legacy_feature
from tests.conftest import IS_SQLITE, TestConfig
from tests.support import (
    head_revision,
)

LEGACY_VEC = np.linspace(0.1, 0.9, 128, dtype=np.float32)


GARBAGE_FEATURE = b"\x80\x04not-a-valid-pickle-or-npf32"


@pytest.fixture(scope='module')
def legacy_app():
    """建立一個『導入 Alembic 之前』的舊版 SQLite 資料庫，再以它啟動應用程式 (觸發舊庫升級)"""
    if not IS_SQLITE:
        pytest.skip('舊版資料庫只會是 SQLite')
    folder = tempfile.mkdtemp(prefix='kiosk_legacy_')
    path = os.path.join(folder, 'menu.db')
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE user (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, phone VARCHAR(50) NOT NULL,
                           photo_path VARCHAR(200) NOT NULL, feature BLOB, points INTEGER, failed_login_attempts INTEGER DEFAULT 0);
        CREATE TABLE "order" (id INTEGER PRIMARY KEY, user_id INTEGER, table_number VARCHAR(50), total_price INTEGER NOT NULL,
                              payment_method VARCHAR(50), order_type VARCHAR(50), status VARCHAR(50),
                              note VARCHAR(200) DEFAULT "", order_source VARCHAR(20) DEFAULT "Kiosk", cash_received INTEGER DEFAULT 0);
        CREATE TABLE user_coupon (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, coupon_id INTEGER NOT NULL, code VARCHAR(50) NOT NULL);
        CREATE TABLE system_setting (id INTEGER PRIMARY KEY, "key" VARCHAR(100), value VARCHAR(200));
        CREATE TABLE reward_setting (id INTEGER PRIMARY KEY, is_enabled BOOLEAN, points_per_dollar INTEGER,
                                     max_discount_per_order INTEGER, spend_per_point INTEGER);
        INSERT INTO reward_setting (is_enabled, points_per_dollar, max_discount_per_order, spend_per_point) VALUES (1, 2, 0, 50);
        INSERT INTO reward_setting (is_enabled, points_per_dollar, max_discount_per_order, spend_per_point) VALUES (1, 1, 0, 100);
    ''')
    rows = [('舊版Pickle測試員', '0910000001', pickle.dumps(LEGACY_VEC, protocol=4), 25),
            ('壞特徵測試員', '0910000002', GARBAGE_FEATURE, 0),
            ('重複舊帳A', '0910000003', None, 20),
            ('重複舊帳B_高點數', '0910000003', None, 95)]
    conn.executemany('INSERT INTO user (name, phone, feature, points, photo_path) VALUES (?, ?, ?, ?, "x.jpg")', rows)
    dup_low_id = conn.execute("SELECT id FROM user WHERE name = '重複舊帳A'").fetchone()[0]
    conn.execute('INSERT INTO "order" (user_id, table_number, total_price, status) VALUES (?, "測試桌", 180, "Completed")', (dup_low_id,))
    conn.execute('INSERT INTO "order" (table_number, total_price, status, order_source, cash_received) '
                 'VALUES ("舊單測試", 320, "Completed", "", 0)')
    conn.commit()
    conn.close()

    from app import create_app
    cfg = type('LegacyConfig', (TestConfig,), {'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + path})
    return create_app(cfg)


def test_legacy_pickle_features_converted(legacy_app):
    """舊版 Pickle 人臉特徵在升級時轉為 NPF32v1 安全格式；惡意 Pickle 被攔截"""
    with legacy_app.app_context():
        raw = db.session.execute(db.text("SELECT feature FROM user WHERE phone = '0910000001'")).scalar()
        assert bytes(raw).startswith(FEATURE_MAGIC_PREFIX)
        user = User.query.filter_by(phone='0910000001').first()
        assert np.allclose(user.feature, LEGACY_VEC, atol=1e-6)

    class Evil:
        def __reduce__(self):
            return (eval, ("'PWNED'",))
    assert decode_legacy_feature(pickle.dumps(Evil())) is None


def test_undecodable_features_preserved(legacy_app):
    """無法解析的舊特徵保留原始資料，不被覆寫成 NULL；ORM 讀出為 None"""
    with legacy_app.app_context():
        raw = db.session.execute(db.text("SELECT feature FROM user WHERE phone = '0910000002'")).scalar()
        assert bytes(raw) == GARBAGE_FEATURE
        assert User.query.filter_by(phone='0910000002').first().feature is None


def test_duplicate_phones_merged(legacy_app):
    """同店重複手機合併為一筆 (保留最高點數、訂單歸戶)，並建立唯一索引"""
    with legacy_app.app_context():
        users = User.query.filter_by(phone='0910000003').all()
        assert len(users) == 1 and users[0].points == 95
        assert Order.query.filter_by(table_number='測試桌').first().user_id == users[0].id
        db.session.add(User(tenant_id=users[0].tenant_id, name='再次重複', phone='0910000003', photo_path='x.jpg'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_legacy_orders_backfilled(legacy_app):
    """舊訂單補值 order_source='Kiosk'、cash_received=total_price，且全部歸屬預設店家"""
    with legacy_app.app_context():
        order = Order.query.filter_by(table_number='舊單測試').first()
        assert order.order_source == 'Kiosk' and order.cash_received == 320
        settings = RewardSetting.query.all()
        assert len(settings) == 1 and settings[0].points_per_dollar == 2, '歷史重複的紅利設定應只保留最早的一筆'
        tables = db.session.execute(db.text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        assert 'system_setting' not in tables, '沒有資料的廢棄資料表應被移除'
        assert db.session.execute(db.text("SELECT version_num FROM alembic_version")).scalar() == head_revision()


def test_legacy_upload_folders_are_moved(tmp_path):
    """舊版放在 static/member、static/menu 的上傳檔，啟動時搬到新的上傳資料夾 (不覆蓋既有檔案)"""
    from types import SimpleNamespace

    from app import _move_legacy_uploads
    static = tmp_path / 'static'
    (static / 'member').mkdir(parents=True)
    (static / 'menu' / '1').mkdir(parents=True)
    (static / 'member' / 'a.jpg').write_bytes(b'old-a')
    (static / 'member' / 'b.jpg').write_bytes(b'old-b')
    (static / 'member' / '.gitkeep').write_bytes(b'')
    (static / 'menu' / '1' / 'm.jpg').write_bytes(b'menu')
    member_dir, menu_dir = tmp_path / 'uploads' / 'member', tmp_path / 'uploads' / 'menu'
    member_dir.mkdir(parents=True)
    menu_dir.mkdir(parents=True)
    (member_dir / 'b.jpg').write_bytes(b'new-b')
    config = {'BASE_DIR': str(tmp_path), 'UPLOAD_FOLDER_MEMBER': str(member_dir), 'UPLOAD_FOLDER_MENU': str(menu_dir)}
    # 測試 / 腳本 (testing=True) 或上傳資料夾在專案外時絕不搬移 (暫存資料夾會被刪除，等於刪掉使用者的照片)
    _move_legacy_uploads(SimpleNamespace(static_folder=str(static), testing=True, config=config))
    assert (static / 'member' / 'a.jpg').exists()
    outside = dict(config, BASE_DIR=str(tmp_path / 'project'))
    _move_legacy_uploads(SimpleNamespace(static_folder=str(static), testing=False, config=outside))
    assert (static / 'member' / 'a.jpg').exists()
    fake = SimpleNamespace(static_folder=str(static), testing=False, config=config)
    _move_legacy_uploads(fake)
    assert (member_dir / 'a.jpg').read_bytes() == b'old-a' and not (static / 'member' / 'a.jpg').exists()
    assert (member_dir / 'b.jpg').read_bytes() == b'new-b' and (static / 'member' / 'b.jpg').exists()
    assert (menu_dir / '1' / 'm.jpg').read_bytes() == b'menu'
    assert not (member_dir / '.gitkeep').exists()
