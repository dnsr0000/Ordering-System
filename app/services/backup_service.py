"""
整庫備份與還原 (本機 SQLite 專用；線上 PostgreSQL 請使用 pg_dump / 雲端資料庫備份)。
備份包含所有店家的資料，路由層僅開放給平台管理者。
"""
import io
import logging
import os
import re
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime

from flask import current_app
from PIL import Image
from werkzeug.utils import secure_filename

from app.errors import AppError
from app.extensions import db

logger = logging.getLogger(__name__)


def require_sqlite():
    if db.engine.dialect.name != 'sqlite':
        raise AppError('整庫備份/還原僅適用於本機 SQLite；線上資料庫請使用 pg_dump 或雲端備份功能。', 400)


BACKUP_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
_TENANT_DIR_RE = re.compile(r'^\d{1,9}$')
# 還原時，備份包內 menu.db 至少要含有其中一張系統資料表，才視為本系統的資料庫
_KNOWN_DB_TABLES = {'user', 'menu_item', 'order', 'tenant', 'coupon'}


def _resolve_live_db_path():
    """取得目前實際使用中的 SQLite 資料庫檔案路徑"""
    db_path = getattr(db.engine.url, 'database', None)
    if db_path and os.path.exists(db_path):
        return os.path.abspath(db_path)
    for candidate in ('menu.db', 'test_menu.db'):
        p = os.path.join(current_app.config['DATABASE_DIR'], candidate)
        if os.path.exists(p):
            return os.path.abspath(p)
    return os.path.abspath(os.path.join(current_app.config['DATABASE_DIR'], 'menu.db'))


def _snapshot_database(src_path, dest_path):
    """以 SQLite Online Backup API 建立一致性快照 (含 WAL 內尚未 checkpoint 的資料，備份期間仍可正常寫入)"""
    src = sqlite3.connect(src_path, timeout=30)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


_ARCHIVE_DB_RE = re.compile(r'^archive_t\d{1,9}\.db$')


def archive_db_dir():
    """舊資料封存檔所在資料夾 (database/archive，檔名 archive_t<店家>.db)"""
    return os.path.join(current_app.config['DATABASE_DIR'], 'archive')


def _archive_db_files():
    folder = archive_db_dir()
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if _ARCHIVE_DB_RE.match(n)]


def write_archive_snapshots(zf, tmp_dir):
    """把每個封存檔的一致性快照 (Online Backup API) 寫進備份包的 archive/ 目錄；沒有封存資料夾就略過"""
    for src in _archive_db_files():
        name = os.path.basename(src)
        snap = os.path.join(tmp_dir, name)
        _snapshot_database(src, snap)
        try:
            zf.write(snap, arcname='archive/' + name)
        finally:
            try:
                os.remove(snap)
            except OSError:
                pass


def create_full_backup_zip():
    """
    建立完整系統備份包（SQLite 資料庫一致性快照 + 舊資料封存檔快照 + 會員照片 + 菜單照片）。
    注意：此備份包含「所有店家」的資料，路由層僅允許平台管理者使用。
    """
    require_sqlite()
    memory_file = io.BytesIO()
    db_path = _resolve_live_db_path()

    with tempfile.TemporaryDirectory(prefix='kiosk_backup_') as tmp_dir:
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(db_path):
                snapshot_path = os.path.join(tmp_dir, 'menu.db')
                _snapshot_database(db_path, snapshot_path)
                zf.write(snapshot_path, arcname='menu.db')
            write_archive_snapshots(zf, tmp_dir)

            if os.path.exists(current_app.config['UPLOAD_FOLDER_MEMBER']):
                for root, _, files in os.walk(current_app.config['UPLOAD_FOLDER_MEMBER']):
                    for file in files:
                        # 略過人臉登入的暫存檔與非圖片檔
                        if file.startswith('temp_') or os.path.splitext(file.lower())[1] not in BACKUP_IMAGE_EXTS:
                            continue
                        zf.write(os.path.join(root, file), arcname='static/member/' + file)

            if os.path.exists(current_app.config['UPLOAD_FOLDER_MENU']):
                for root, _, files in os.walk(current_app.config['UPLOAD_FOLDER_MENU']):
                    for file in files:
                        if os.path.splitext(file.lower())[1] not in BACKUP_IMAGE_EXTS:
                            continue
                        full_path = os.path.join(root, file)
                        # 保留「店家 ID 子資料夾」結構 (資料庫 image_path 為 "<tenant_id>/<檔名>")，不可攤平
                        rel_path = os.path.relpath(full_path, current_app.config['UPLOAD_FOLDER_MENU']).replace('\\', '/')
                        zf.write(full_path, arcname='static/menu/' + rel_path)

    memory_file.seek(0)
    return memory_file


def _install_restored_database(tmp_path, db_path):
    """
    把驗證通過的資料庫檔案安裝成目前使用中的資料庫。
    1. 優先以「清除舊 -wal/-shm 後 os.replace 原子替換」。
    2. Windows 上只要還有任何連線開著資料庫 (waitress 其他執行緒、SSE 等)，檔案就無法被替換 (WinError 5)，
       此時改用 SQLite Online Backup API 把內容「原地」寫進現有資料庫：整個過程是單一寫入交易，
       其他連線不需先關閉，也不會看到寫到一半的狀態；舊的 WAL 內容由 SQLite 自行處理，不會被錯誤重播。
    """
    try:
        for suffix in ('-wal', '-shm'):
            sidecar = db_path + suffix
            if os.path.exists(sidecar):
                os.remove(sidecar)
        os.replace(tmp_path, db_path)
        return 'replace'
    except OSError:
        pass

    src = sqlite3.connect(tmp_path)
    try:
        dst = sqlite3.connect(db_path, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    try:
        os.remove(tmp_path)
    except OSError:
        pass  # Windows 上暫存檔偶爾仍被短暫占用，留著不影響結果 (下次還原會建立新的暫存檔)
    return 'in-place'


def _validate_sqlite_file(path):
    """驗證檔案是完整且可讀的 SQLite 資料庫，且包含本系統資料表；不合格即丟出 ValueError"""
    try:
        with open(path, 'rb') as f:
            if not f.read(16).startswith(b'SQLite format 3\x00'):
                raise ValueError('備份包內的 menu.db 不是有效的 SQLite 資料庫檔案！')
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            result = conn.execute('PRAGMA integrity_check;').fetchone()
            if not result or str(result[0]).lower() != 'ok':
                raise ValueError('備份包內的資料庫完整性檢查失敗 (檔案可能已損毀)！')
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # 本系統不使用觸發程序與檢視表：備份檔裡有的話可能是被動過手腳 (例如每次寫入訂單就改密碼)
            extra = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise ValueError(f'備份包內的資料庫無法讀取：{e}')
    if not (tables & _KNOWN_DB_TABLES):
        raise ValueError('備份包內的資料庫不含本系統資料表，已拒絕還原！')
    if extra:
        raise ValueError('備份包內的資料庫含有本系統不使用的觸發程序或檢視表，已拒絕還原！')


def _plan_photo_restore(zf, restore_photos, restore_menu_photos):
    """
    預先規劃要還原的相片 (不寫入任何檔案)：
    - 僅允許圖片副檔名；檔名一律經 secure_filename，不接受路徑穿越
    - 菜單相片保留「店家 ID 子資料夾」；舊版攤平格式的備份則放回菜單根目錄
    - 單檔與總量依 MAX_SINGLE_FILE_SIZE / MAX_TOTAL_EXTRACT_SIZE 限制 (以 zip 標頭宣告之未壓縮大小判斷)
    """
    plan = []
    total = 0
    for info in zf.infolist():
        norm = info.filename.replace('\\', '/')
        if norm.endswith('/'):
            continue
        parts = [p for p in norm.split('/') if p not in ('', '.')]
        if not parts:
            continue
        safe_name = secure_filename(parts[-1])
        if not safe_name or os.path.splitext(safe_name.lower())[1] not in BACKUP_IMAGE_EXTS:
            continue
        dir_parts = [p.lower() for p in parts[:-1]]

        kind, sub_dir = None, ''
        if restore_photos and 'member' in dir_parts:
            kind = 'member'
        elif restore_menu_photos and 'menu' in dir_parts:
            kind = 'menu'
            after_menu = parts[:-1][dir_parts.index('menu') + 1:]
            if len(after_menu) == 1 and _TENANT_DIR_RE.match(after_menu[0]):
                sub_dir = after_menu[0]
        if kind is None:
            continue

        if info.file_size > current_app.config['MAX_SINGLE_FILE_SIZE']:
            raise ValueError(f'備份包內相片【{safe_name}】超過單檔大小上限，已拒絕還原！')
        total += info.file_size
        if total > current_app.config['MAX_TOTAL_EXTRACT_SIZE']:
            raise ValueError('備份包內相片總容量超過上限，已拒絕還原！')
        plan.append((info, kind, sub_dir, safe_name))
    return plan


def _run_post_restore_upgrade(app):
    """
    還原後立即執行資料庫遷移，讓舊版備份馬上升級為目前結構 (不必等到下次重啟)。
    於獨立執行緒執行：該執行緒沒有 request context，遷移不會被目前請求的租戶過濾條件限縮。
    """
    from app.services.db_migrate import upgrade_database
    errors = []

    def _worker():
        try:
            with app.app_context():
                upgrade_database()
        except BaseException as e:  # noqa: BLE001  含 SystemExit (遷移工具遇錯 sys.exit)：執行緒內會被靜默吞掉、誤報成功
            errors.append(e)

    t = threading.Thread(target=_worker, name='post-restore-migration')
    t.start()
    t.join()
    if errors:
        err = errors[0]
        detail = f'{type(err).__name__} (exit {err.code})' if isinstance(err, SystemExit) else str(err)
        raise RuntimeError(f'資料庫已還原，但自動升級失敗，請重新啟動系統後再確認：{detail}')


def _check_backup_revision(path):
    """
    備份內資料庫的結構版本 (alembic_version) 必須是目前程式認得的版本：較新版本主機的備份還原到舊版程式，
    升級會失敗、重開機後伺服器也起不來。認不得就拒絕還原 (丟出 ValueError，現有資料庫不會被更動)。
    """
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        try:
            rows = [r[0] for r in conn.execute('SELECT version_num FROM alembic_version')]
        except sqlite3.DatabaseError:
            return   # 沒有版本表 (很舊的備份)：維持原本行為，交給還原後的升級處理
    finally:
        conn.close()
    try:
        from alembic.script import ScriptDirectory
        script = ScriptDirectory.from_config(current_app.extensions['migrate'].migrate.get_config())
    except Exception:  # noqa: BLE001  遷移設定無法載入 (例如未初始化 Flask-Migrate)：無從比對，不擋
        logger.warning('無法載入資料庫遷移設定，略過備份版本檢查', exc_info=True)
        return
    for rev in rows:
        try:
            known = script.get_revision(rev) is not None
        except Exception:  # noqa: BLE001  alembic：Can't locate revision (ResolutionError / CommandError)
            known = False
        if not known:
            raise ValueError(f'備份的資料庫版本 ({rev}) 比目前程式新或無法辨識，請先把程式更新到與備份相同或更新的版本再還原！')


def _extract_db_entry(zf, info, folder, prefix):
    """把備份包內的一個資料庫檔解壓到 folder 的暫存檔 (限制大小並驗證)；回傳暫存檔路徑，不合格時刪除暫存檔並丟出 ValueError"""
    if info.file_size > current_app.config['MAX_RESTORE_DB_SIZE']:
        raise ValueError('備份包內的資料庫檔案超過大小上限，已拒絕還原！')
    os.makedirs(folder, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as out, zf.open(info) as src:
            written = 0
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > current_app.config['MAX_RESTORE_DB_SIZE']:
                    raise ValueError('備份包內的資料庫檔案超過大小上限，已拒絕還原！')
                out.write(chunk)
        _validate_sqlite_file(tmp_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


def _archive_entries(zf):
    """備份包內的封存檔 (archive/archive_t<店家>.db)；舊版備份沒有 archive/ 時回傳空清單"""
    out = []
    for info in zf.infolist():
        parts = [p for p in info.filename.replace('\\', '/').split('/') if p not in ('', '.')]
        if len(parts) == 2 and parts[0].lower() == 'archive' and _ARCHIVE_DB_RE.match(parts[1]):
            out.append((info, parts[1]))
    return out


def _install_archive_files(planned):
    """資料庫還原後把封存檔放回 database/archive (覆蓋前另存 .bak-<時間>)；單一檔案失敗只記錄，不中斷還原"""
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    restored = 0
    for tmp_path, dest in planned:
        try:
            if os.path.exists(dest):
                _snapshot_database(dest, f'{dest}.bak-{stamp}')
            _install_restored_database(tmp_path, dest)
            restored += 1
        except Exception as e:  # noqa: BLE001
            logger.warning('還原封存檔失敗，已略過：%s (%s)', os.path.basename(dest), e)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return restored


def restore_system_backup(file_storage, restore_db=True, restore_photos=True, restore_menu_photos=True, overwrite_photos=True):
    """
    執行系統備份包還原 (路由層僅允許平台管理者)：
    1. 先完整驗證備份包 (SQLite 完整性、資料表、相片副檔名與容量)，任何一項不合格就不動現有資料
    2. 覆蓋前先把「目前的資料庫」另存成 database/menu.db.bak-<時間>，還原錯了可手動換回
    3. 先寫入暫存檔再以 os.replace 原子替換，並清除舊的 -wal / -shm
    4. 還原後立即執行遷移，舊版備份即刻升級
    """
    require_sqlite()
    member_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER_MEMBER'])
    menu_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER_MENU'])
    restored_db_status = False
    restored_photo_count = 0
    restored_menu_photo_count = 0
    app = current_app._get_current_object()

    with zipfile.ZipFile(file_storage, 'r') as zf:
        # ---------- 階段 1：驗證 (不寫入任何現有檔案) ----------
        photo_plan = _plan_photo_restore(zf, restore_photos, restore_menu_photos)

        tmp_db_path = None
        archive_plan = []   # [(暫存檔, 目的地)]：封存檔與 menu.db 一起還原 (舊版備份沒有 archive/ 時保留現有封存檔)
        db_path = _resolve_live_db_path()
        if restore_db:
            db_entry = next(
                (i for i in zf.infolist() if os.path.basename(i.filename.replace('\\', '/')) == 'menu.db'),
                None
            )
            if db_entry is not None:
                tmp_db_path = _extract_db_entry(zf, db_entry, os.path.dirname(db_path), 'menu.db.restore-')
                try:
                    _check_backup_revision(tmp_db_path)
                    archive_folder = archive_db_dir()
                    for info, name in _archive_entries(zf):
                        tmp = _extract_db_entry(zf, info, archive_folder, f'{name}.restore-')
                        archive_plan.append((tmp, os.path.join(archive_folder, name)))
                except Exception:
                    for leftover in [tmp_db_path] + [t for t, _ in archive_plan]:
                        try:
                            os.remove(leftover)
                        except OSError:
                            pass
                    raise

        # ---------- 階段 2：替換資料庫 ----------
        if tmp_db_path:
            try:
                if os.path.exists(db_path):
                    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    _snapshot_database(db_path, f'{db_path}.bak-{stamp}')
                db.session.remove()
                db.engine.dispose()
                _install_restored_database(tmp_db_path, db_path)
                restored_db_status = True
            except Exception as e:  # noqa: BLE001
                for leftover in [tmp_db_path] + [t for t, _ in archive_plan]:
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
                raise RuntimeError(f'替換資料庫檔案失敗 (現有資料庫未被更動)：{e}')
            _install_archive_files(archive_plan)

        # ---------- 階段 3：還原相片 ----------
        for info, kind, sub_dir, safe_name in photo_plan:
            base_dir = member_dir if kind == 'member' else menu_dir
            target_dir = os.path.abspath(os.path.join(base_dir, sub_dir)) if sub_dir else base_dir
            dest_path = os.path.abspath(os.path.join(target_dir, safe_name))
            if os.path.commonpath([base_dir, dest_path]) != base_dir:
                continue
            if os.path.exists(dest_path) and not overwrite_photos:
                continue
            try:
                # 資料庫可能已經替換：單張相片損毀 (CRC 不符) 或無法寫入只略過該檔，不中斷還原 (後面還要執行升級)
                data = zf.read(info)
            except Exception as e:  # noqa: BLE001  BadZipFile / zlib.error
                logger.warning("還原略過無法讀取的相片：%s (%s)", safe_name, e)
                continue
            try:
                # 內容必須真的是圖片，避免把 .jpg 檔名偽裝的網頁/腳本寫進可被公開存取的 static 目錄
                Image.open(io.BytesIO(data)).verify()
            except Exception:
                logger.warning("還原略過非圖片內容的檔案：%s", safe_name)
                continue
            try:
                os.makedirs(target_dir, exist_ok=True)
                with open(dest_path, 'wb') as f_out:
                    f_out.write(data)
            except OSError as e:
                logger.warning("還原相片寫入失敗，已略過：%s (%s)", safe_name, e)
                continue
            if kind == 'member':
                restored_photo_count += 1
            else:
                restored_menu_photo_count += 1

    if restored_db_status:
        _run_post_restore_upgrade(app)
        db.session.remove()

    return restored_db_status, restored_photo_count, restored_menu_photo_count
