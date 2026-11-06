"""整庫備份與還原：權限、往返還原、惡意備份檔、較舊版本的備份還原後立即升級"""
import io
import os
import sqlite3
import zipfile

import pytest

from tests.conftest import IS_SQLITE
from tests.support import (
    assert_flash,
    head_revision,
    jpeg_bytes,
    login_staff,
    other_shop_owner_client,
)


@pytest.mark.skipif(not IS_SQLITE, reason='整庫備份只支援本機 SQLite')
def test_full_backup_permissions(app, admin_client, manager_client):
    """負責人可下載含 menu.db 的備份包；店長 403；還原未附檔案時被擋下"""
    r = admin_client.post('/admin/backup_system')
    assert r.status_code == 200 and r.mimetype == 'application/zip'
    assert 'menu.db' in zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert manager_client.post('/admin/backup_system').status_code == 403
    assert admin_client.post('/admin/restore_backup').status_code == 302
    assert_flash(admin_client, '請選取有效備份檔案')


sqlite_only = pytest.mark.skipif(not IS_SQLITE, reason='整庫備份/還原只支援本機 SQLite')


def test_full_backup_only_for_platform_admin(app, admin_client):
    other_owner = other_shop_owner_client(app)
    page = other_owner.get('/admin')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'pane-export-zip' not in html and 'pane-restore-zip' not in html
    assert other_owner.post('/admin/backup_system').status_code == 403
    r = other_owner.post('/admin/restore_backup', content_type='multipart/form-data',
                         data={'restore_db': '1', 'backup_zip': (io.BytesIO(b'PK\x05\x06' + b'\x00' * 18), 'x.zip')})
    assert r.status_code == 403
    html = admin_client.get('/admin').get_data(as_text=True)
    if IS_SQLITE:
        assert 'pane-export-zip' in html and 'pane-restore-zip' in html
    else:  # 整庫備份/還原只支援 SQLite：其他資料庫隱藏並顯示說明
        assert 'pane-export-zip' not in html and 'backup-unavailable' in html


def _restore(client, zip_bytes, **flags):
    return client.post('/admin/restore_backup', content_type='multipart/form-data',
                       data={'backup_zip': (io.BytesIO(zip_bytes), 'backup.zip'), **flags})


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


@sqlite_only
def test_backup_restore_roundtrip(app, admin_client):
    """備份保留菜單相片的店家子資料夾、排除暫存檔；還原前自動另存資料庫，往返還原後系統可用"""
    menu_dir, member_dir, db_dir = app.config['UPLOAD_FOLDER_MENU'], app.config['UPLOAD_FOLDER_MEMBER'], app.config['DATABASE_DIR']
    os.makedirs(os.path.join(menu_dir, '1'), exist_ok=True)
    files = [os.path.join(menu_dir, '1', 'menu_test_14.jpg'), os.path.join(member_dir, 'member_test14.jpg'),
             os.path.join(member_dir, 'temp_skip_14.jpg')]
    for path in files:
        with open(path, 'wb') as fh:
            fh.write(jpeg_bytes(60, 60).getvalue())
    other_shop_owner_client(app)  # 確保有第二家店，檢查備份包含所有店家
    try:
        r = admin_client.post('/admin/backup_system')
        names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
        assert {'menu.db', 'static/menu/1/menu_test_14.jpg', 'static/member/member_test14.jpg'} <= set(names)
        assert not any('temp_skip_14' in n for n in names)
        snapshot = zipfile.ZipFile(io.BytesIO(r.data)).read('menu.db')
        snap_path = os.path.join(db_dir, 'snap_check.db')
        with open(snap_path, 'wb') as fh:
            fh.write(snapshot)
        conn = sqlite3.connect(snap_path)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT COUNT(*) FROM tenant').fetchone()[0] >= 2
        conn.close()
        os.remove(snap_path)

        os.remove(files[0])
        before = {f for f in os.listdir(db_dir) if '.bak-' in f}
        resp = _restore(admin_client, r.data, restore_db='1', restore_photos='1', restore_menu_photos='1', overwrite_photos='1')
        assert resp.status_code == 302
        assert_flash(admin_client, '還原完成')
        assert os.path.exists(files[0])
        assert len({f for f in os.listdir(db_dir) if '.bak-' in f} - before) == 1
        assert admin_client.get('/admin').status_code == 200
    finally:
        for path in files:
            if os.path.exists(path):
                os.remove(path)


@sqlite_only
def test_malicious_backups_rejected(app, admin_client):
    """非 SQLite / 無系統資料表 / 危險檔名 / 路徑穿越 / 超過大小：拒絕或安全處理，且不動現有資料庫"""
    menu_dir, member_dir, db_dir = app.config['UPLOAD_FOLDER_MENU'], app.config['UPLOAD_FOLDER_MEMBER'], app.config['DATABASE_DIR']
    jpg = jpeg_bytes(50, 50).getvalue()
    before = {f for f in os.listdir(db_dir) if '.bak-' in f}

    r = _restore(admin_client, _zip([('menu.db', b'this is not a sqlite database')]), restore_db='1')
    assert r.status_code == 500 and '還原過程發生錯誤' in r.get_data(as_text=True)
    foreign = os.path.join(db_dir, 'foreign.db')
    conn = sqlite3.connect(foreign)
    conn.execute('CREATE TABLE foo (id INTEGER)')
    conn.commit()
    conn.close()
    with open(foreign, 'rb') as fh:
        assert _restore(admin_client, _zip([('menu.db', fh.read())]), restore_db='1').status_code == 500
    os.remove(foreign)

    evil = b'<html><script>alert(1)</script></html>'
    r = _restore(admin_client, _zip([('static/menu/evil.html', evil), ('static/menu/fake.jpg', evil),
                                     ('static/menu/../../escaped_14.jpg', jpg), ('static/member/member_ok14.jpg', jpg)]),
                 restore_menu_photos='1', restore_photos='1', overwrite_photos='1')
    assert r.status_code == 302
    assert not os.path.exists(os.path.join(menu_dir, 'evil.html'))
    assert not os.path.exists(os.path.join(menu_dir, 'fake.jpg'))
    assert not os.path.exists(os.path.join(os.path.dirname(menu_dir), 'escaped_14.jpg'))
    for ok_path in (os.path.join(menu_dir, 'escaped_14.jpg'), os.path.join(member_dir, 'member_ok14.jpg')):
        assert os.path.exists(ok_path)
        os.remove(ok_path)

    app.config['MAX_SINGLE_FILE_SIZE'], old = 100, app.config['MAX_SINGLE_FILE_SIZE']
    try:
        assert _restore(admin_client, _zip([('static/member/big14.jpg', jpg)]), restore_photos='1').status_code == 500
    finally:
        app.config['MAX_SINGLE_FILE_SIZE'] = old
    assert {f for f in os.listdir(db_dir) if '.bak-' in f} == before


def _schema(conn):
    """資料表與欄位清單 (比對還原升級後的結構是否與目前一致)"""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return {t: sorted(r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')) for t in tables}


def older_revision_backup_zip(folder):
    """把目前的資料庫複製一份，執行最新遷移的 downgrade() 退回上一版結構，當作「舊版本的備份」(新增遷移後不必改這裡)"""
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    src = sqlite3.connect(os.path.join(folder, 'menu.db'))
    path = os.path.join(folder, 'older_make.db')
    if os.path.exists(path):
        os.remove(path)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    cfg = AlembicConfig()
    cfg.set_main_option('script_location', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrations'))
    head = ScriptDirectory.from_config(cfg).get_revision(head_revision())
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            head.module.downgrade()
        conn.execute(sa.text('UPDATE alembic_version SET version_num = :v'), {'v': head.down_revision})
    engine.dispose()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.write(path, 'menu.db')
    os.remove(path)
    return buf.getvalue()


@pytest.mark.parametrize('file_locked', [False, True], ids=['replace-file', 'windows-in-place'])
@pytest.mark.skipif(not IS_SQLITE, reason='整庫還原只支援本機 SQLite')
def test_restore_older_backup_upgrades_immediately(app, monkeypatch, file_locked):
    db_dir = app.config['DATABASE_DIR']
    with sqlite3.connect(os.path.join(db_dir, 'menu.db')) as live:
        expected_schema = _schema(live)
    backup = older_revision_backup_zip(db_dir)
    probe_path = os.path.join(db_dir, 'older_probe.db')
    with zipfile.ZipFile(io.BytesIO(backup)) as zf, open(probe_path, 'wb') as fh:
        fh.write(zf.read('menu.db'))
    probe = sqlite3.connect(probe_path)
    try:
        assert _schema(probe) != expected_schema, '備份確實是較舊的結構'
    finally:
        probe.close()
        os.remove(probe_path)
    if file_locked:  # 模擬 Windows：資料庫仍被其他連線開啟，無法以換檔方式替換
        real_replace = os.replace

        def locked_replace(src, dst, *a, **k):
            if 'menu.db.restore-' in str(src):
                raise PermissionError(5, '存取被拒', str(dst))
            return real_replace(src, dst, *a, **k)
        monkeypatch.setattr(os, 'replace', locked_replace)

    admin = login_staff(app, 'admin_test')  # 前一次還原已換掉資料庫，重新登入
    r = admin.post('/admin/restore_backup', content_type='multipart/form-data',
                   data={'restore_db': '1', 'backup_zip': (io.BytesIO(backup), 'older.zip')})
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    assert_flash(admin, '還原完成')

    conn = sqlite3.connect(os.path.join(db_dir, 'menu.db'))
    try:
        assert _schema(conn) == expected_schema, '還原後已升級成與目前相同的結構'
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()[0] == head_revision()
    finally:
        conn.close()
    assert login_staff(app, 'admin_test').get('/admin').status_code == 200


def test_backup_with_triggers_or_views_is_rejected(tmp_path):
    """備份檔裡的觸發程序會在之後每次寫入時執行 (例如偷改密碼)：本系統不使用，一律拒絕還原"""
    from app.services.backup_service import _validate_sqlite_file
    path = str(tmp_path / 'evil.db')
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE staff_account (id INTEGER PRIMARY KEY, password_hash TEXT);
        CREATE TABLE "order" (id INTEGER PRIMARY KEY);
        CREATE TRIGGER evil AFTER INSERT ON "order" BEGIN UPDATE staff_account SET password_hash = 'x'; END;
    """)
    conn.close()
    with pytest.raises(ValueError, match='觸發程序'):
        _validate_sqlite_file(path)
