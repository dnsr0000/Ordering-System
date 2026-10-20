import os
import io
import re
import sqlite3
import tempfile
import threading
import zipfile
import unicodedata
import pandas as pd
from datetime import datetime, timedelta
from flask import current_app
from PIL import Image
from werkzeug.utils import secure_filename
from openpyxl.styles import Font
from app.config import Config
from app.extensions import db
from app.models.user import User, AdminLog, StaffAuditLog, CustomerLog
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.coupon import Coupon, UserCoupon

MENU_COLUMN_MAPPING = {
    '餐點名稱(Name)': 'name', '名稱(Name)': 'name', '餐點名稱': 'name', '名稱': 'name', 'name': 'name',
    '分類(Category)': 'category', '分類': 'category', 'category': 'category',
    '單價(Price)': 'price', '單價': 'price', '原單價': 'price', 'price': 'price',
    '剩餘數量(Stock)': 'stock', '剩餘數量': 'stock', '庫存': 'stock', 'stock': 'stock',
    '總量(Total Stock)': 'total_stock', '總量': 'total_stock', 'total_stock': 'total_stock',
    '客製化群組(Modifiers)': 'modifiers', '客製化類型': 'modifiers', 'modifiers': 'modifiers',
    '商品描述(Description)': 'description', '簡介': 'description', '描述': 'description', 'description': 'description',
    '熱門推薦(Recommended)': 'is_recommended', '熱門(Popular)': 'is_recommended', 'is_recommended': 'is_recommended',
    '新品上市(Is New)': 'is_new', '新品': 'is_new', 'is_new': 'is_new',
    '是否特價(Is Discount)': 'is_discount', '是否特價': 'is_discount', '特價': 'is_discount', 'is_discount': 'is_discount',
    '特價金額(Discount Price)': 'discount_price', '特價金額': 'discount_price', 'discount_price': 'discount_price',
    '開放紅利兌換(Is Reward)': 'is_reward', '是否開放紅利兌換': 'is_reward', 'is_reward': 'is_reward',
    '兌換所需點數(Reward Points)': 'reward_points', '兌換點數': 'reward_points', 'reward_points': 'reward_points',
    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points'
}


def parse_bool(val):
    if pd.isna(val):
        return False
    s = str(val).strip().lower()
    return s in ['1', 'true', '是', 'yes', 'y']


def clean_phone_number(val):
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    if len(s) == 9 and s.startswith('9'):
        s = '0' + s
    return s


def parse_per_user_limit(val):
    if not val:
        return 0
    s = str(val).strip()
    if s in ['無上限', '不限', '0', 'none', 'null']:
        return 0
    try:
        return max(0, int(float(s)))
    except (ValueError, TypeError):
        return 0


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
        p = os.path.join(Config.DATABASE_DIR, candidate)
        if os.path.exists(p):
            return os.path.abspath(p)
    return os.path.abspath(os.path.join(Config.DATABASE_DIR, 'menu.db'))


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


def create_full_backup_zip():
    """
    建立完整系統備份包（SQLite 資料庫一致性快照 + 會員照片 + 菜單照片）。
    注意：此備份包含「所有店家」的資料，路由層僅允許平台管理者使用。
    """
    memory_file = io.BytesIO()
    db_path = _resolve_live_db_path()

    with tempfile.TemporaryDirectory(prefix='kiosk_backup_') as tmp_dir:
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(db_path):
                snapshot_path = os.path.join(tmp_dir, 'menu.db')
                _snapshot_database(db_path, snapshot_path)
                zf.write(snapshot_path, arcname='menu.db')

            if os.path.exists(Config.UPLOAD_FOLDER_MEMBER):
                for root, _, files in os.walk(Config.UPLOAD_FOLDER_MEMBER):
                    for file in files:
                        # 略過人臉登入的暫存檔與非圖片檔
                        if file.startswith('temp_') or os.path.splitext(file.lower())[1] not in BACKUP_IMAGE_EXTS:
                            continue
                        zf.write(os.path.join(root, file), arcname='static/member/' + file)

            if os.path.exists(Config.UPLOAD_FOLDER_MENU):
                for root, _, files in os.walk(Config.UPLOAD_FOLDER_MENU):
                    for file in files:
                        if os.path.splitext(file.lower())[1] not in BACKUP_IMAGE_EXTS:
                            continue
                        full_path = os.path.join(root, file)
                        # 保留「店家 ID 子資料夾」結構 (資料庫 image_path 為 "<tenant_id>/<檔名>")，不可攤平
                        rel_path = os.path.relpath(full_path, Config.UPLOAD_FOLDER_MENU).replace('\\', '/')
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
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise ValueError(f'備份包內的資料庫無法讀取：{e}')
    if not (tables & _KNOWN_DB_TABLES):
        raise ValueError('備份包內的資料庫不含本系統資料表，已拒絕還原！')


def _plan_photo_restore(zf, restore_photos, restore_menu_photos):
    """
    預先規劃要還原的相片 (不寫入任何檔案)：
    - 僅允許圖片副檔名；檔名一律經 secure_filename，不接受路徑穿越
    - 菜單相片保留「店家 ID 子資料夾」；舊版攤平格式的備份則放回菜單根目錄
    - 單檔與總量依 Config.MAX_SINGLE_FILE_SIZE / MAX_TOTAL_EXTRACT_SIZE 限制 (以 zip 標頭宣告之未壓縮大小判斷)
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

        if info.file_size > Config.MAX_SINGLE_FILE_SIZE:
            raise ValueError(f'備份包內相片【{safe_name}】超過單檔大小上限，已拒絕還原！')
        total += info.file_size
        if total > Config.MAX_TOTAL_EXTRACT_SIZE:
            raise ValueError('備份包內相片總容量超過上限，已拒絕還原！')
        plan.append((info, kind, sub_dir, safe_name))
    return plan


def _run_post_restore_upgrade(app):
    """
    還原後立即補齊資料表並執行資料庫遷移，讓舊版備份馬上升級為目前結構 (不必等到下次重啟)。
    於獨立執行緒執行：該執行緒沒有 request context，遷移才不會被當前請求的租戶過濾條件限縮。
    """
    from app.services.migration_service import run_database_migrations
    errors = []

    def _worker():
        try:
            with app.app_context():
                db.create_all()
            run_database_migrations(app)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=_worker, name='post-restore-migration')
    t.start()
    t.join()
    if errors:
        raise RuntimeError(f'資料庫已還原，但自動升級失敗，請重新啟動系統後再確認：{errors[0]}')


def restore_system_backup(file_storage, restore_db=True, restore_photos=True, restore_menu_photos=True, overwrite_photos=True):
    """
    執行系統備份包還原 (路由層僅允許平台管理者)：
    1. 先完整驗證備份包 (SQLite 完整性、資料表、相片副檔名與容量)，任何一項不合格就不動現有資料
    2. 覆蓋前先把「目前的資料庫」另存成 database/menu.db.bak-<時間>，還原錯了可手動換回
    3. 先寫入暫存檔再以 os.replace 原子替換，並清除舊的 -wal / -shm
    4. 還原後立即執行遷移，舊版備份即刻升級
    """
    member_dir = os.path.abspath(Config.UPLOAD_FOLDER_MEMBER)
    menu_dir = os.path.abspath(Config.UPLOAD_FOLDER_MENU)
    restored_db_status = False
    restored_photo_count = 0
    restored_menu_photo_count = 0
    app = current_app._get_current_object()

    with zipfile.ZipFile(file_storage, 'r') as zf:
        # ---------- 階段 1：驗證 (不寫入任何現有檔案) ----------
        photo_plan = _plan_photo_restore(zf, restore_photos, restore_menu_photos)

        tmp_db_path = None
        db_path = _resolve_live_db_path()
        if restore_db:
            db_entry = next(
                (i for i in zf.infolist() if os.path.basename(i.filename.replace('\\', '/')) == 'menu.db'),
                None
            )
            if db_entry is not None:
                if db_entry.file_size > Config.MAX_RESTORE_DB_SIZE:
                    raise ValueError('備份包內的資料庫檔案超過大小上限，已拒絕還原！')
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                fd, tmp_db_path = tempfile.mkstemp(prefix='menu.db.restore-', suffix='.tmp', dir=os.path.dirname(db_path))
                try:
                    with os.fdopen(fd, 'wb') as out, zf.open(db_entry) as src:
                        written = 0
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > Config.MAX_RESTORE_DB_SIZE:
                                raise ValueError('備份包內的資料庫檔案超過大小上限，已拒絕還原！')
                            out.write(chunk)
                    _validate_sqlite_file(tmp_db_path)
                except Exception:
                    try:
                        os.remove(tmp_db_path)
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
                try:
                    os.remove(tmp_db_path)
                except OSError:
                    pass
                raise RuntimeError(f'替換資料庫檔案失敗 (現有資料庫未被更動)：{e}')

        # ---------- 階段 3：還原相片 ----------
        for info, kind, sub_dir, safe_name in photo_plan:
            base_dir = member_dir if kind == 'member' else menu_dir
            target_dir = os.path.abspath(os.path.join(base_dir, sub_dir)) if sub_dir else base_dir
            dest_path = os.path.abspath(os.path.join(target_dir, safe_name))
            if os.path.commonpath([base_dir, dest_path]) != base_dir:
                continue
            if os.path.exists(dest_path) and not overwrite_photos:
                continue
            data = zf.read(info)
            try:
                # 內容必須真的是圖片，避免把 .jpg 檔名偽裝的網頁/腳本寫進可被公開存取的 static 目錄
                Image.open(io.BytesIO(data)).verify()
            except Exception:
                print(f"[!] 還原略過非圖片內容的檔案：{safe_name}")
                continue
            os.makedirs(target_dir, exist_ok=True)
            with open(dest_path, 'wb') as f_out:
                f_out.write(data)
            if kind == 'member':
                restored_photo_count += 1
            else:
                restored_menu_photo_count += 1

    if restored_db_status:
        _run_post_restore_upgrade(app)
        db.session.remove()

    return restored_db_status, restored_photo_count, restored_menu_photo_count


def generate_excel_export(selected_tables, start_date_str=None, end_date_str=None):
    """產出 10 張工作表之 Excel 檔案流"""
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1, seconds=-1) if end_date_str else None

    orders_query = Order.query
    if start_date:
        orders_query = orders_query.filter(Order.created_at >= start_date)
    if end_date:
        orders_query = orders_query.filter(Order.created_at <= end_date)
    filtered_orders = orders_query.all()
    filtered_order_ids = [o.id for o in filtered_orders]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        if 'User' in selected_tables:
            user_data = []
            for u in User.query.all():
                login_str = u.last_login_at.strftime('%Y-%m-%d %H:%M:%S') if u.last_login_at else '尚未記錄'
                if not u.last_login_at:
                    logout_str = '尚未記錄'
                elif not u.last_logout_at or u.last_logout_at < u.last_login_at:
                    logout_str = '尚未登出'
                else:
                    logout_str = u.last_logout_at.strftime('%Y-%m-%d %H:%M:%S')
                user_data.append({
                    'ID': u.id, '姓名(Name)': u.name, '電話(Phone)': u.phone, '紅利點數(Points)': u.points,
                    '最後登入時間(Last Login)': login_str, '最後登出時間(Last Logout)': logout_str
                })
            pd.DataFrame(user_data if user_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='會員資料(User)', index=False)

        if 'MenuItem' in selected_tables:
            menu_data = [{
                'ID': i.id, '餐點名稱(Name)': i.name, '分類(Category)': i.category, '單價(Price)': i.price,
                '客製化群組(Modifiers)': i.modifiers or 'none', '商品描述(Description)': i.description or '',
                '是否特價(Is Discount)': '是' if i.is_discount else '否', '特價金額(Discount Price)': i.discount_price,
                '熱門推薦(Recommended)': '是' if (i.is_manual_popular or i.is_recommended) else '否',
                '新品上市(Is New)': '是' if i.is_new else '否', '開放紅利兌換(Is Reward)': '是' if i.is_reward else '否',
                '兌換所需點數(Reward Points)': i.reward_points, '限時特惠點數(Reward Discount Points)': i.reward_discount_points
            } for i in MenuItem.query.all()]
            pd.DataFrame(menu_data if menu_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='菜單品項(MenuItem)', index=False)

        if 'Coupon' in selected_tables:
            coupon_data = [{
                'ID': c.id, '代碼(Code)': c.code, '標題(Title)': c.title, '折抵類型(Discount Type)': c.discount_type,
                '折抵值(Discount Value)': c.discount_value, '門檻(Min Spend)': c.min_spend,
                '兌換所需點數(Reward Points)': c.reward_points, '限時特惠點數(Reward Discount Points)': c.reward_discount_points,
                '每人兌換上限(Per User Limit)': c.limit_display, '上架回饋商城(Is Reward)': '是' if c.is_reward else '否'
            } for c in Coupon.query.all()]
            pd.DataFrame(coupon_data if coupon_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='優惠券(Coupon)', index=False)

        if 'UserCoupon' in selected_tables:
            user_coupons_data = [{
                'ID': uc.id, '關聯會員ID': uc.user_id, '優惠代碼(Code)': uc.code,
                '是否已使用(Is Used)': '是' if uc.is_used else '否',
                '領取時間': uc.created_at.strftime('%Y-%m-%d %H:%M') if uc.created_at else ''
            } for uc in UserCoupon.query.all()]
            pd.DataFrame(user_coupons_data if user_coupons_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='會員持券(UserCoupon)', index=False)

        if 'Order' in selected_tables:
            order_data = []
            for o in filtered_orders:
                log = db.session.get(CustomerLog, o.customer_log_id) if getattr(o, 'customer_log_id', None) else None
                cust_identifier = o.user_id if o.user_id else (o.table_number if o.table_number else f"訪客-{o.pickup_number:03d}")
                login_dt = log.login_at if (log and log.login_at) else (o.user.last_login_at if (o.user and o.user.last_login_at) else None)

                if login_dt:
                    login_str = login_dt.strftime('%Y-%m-%d %H:%M:%S')
                    sec = int((o.created_at - login_dt).total_seconds()) if o.created_at else 0
                    duration_str = f"{sec // 60}分{sec % 60}秒" if sec > 0 else "即時下單"
                else:
                    login_str = "未記錄"
                    duration_str = "無進場紀錄"

                order_data.append({
                    'ID': o.id, '取餐編號(Pickup No)': f"#{o.pickup_number:03d}" if o.pickup_number else f"#{o.id}",
                    '訂單來源(Source)': getattr(o, 'order_source', 'Kiosk') or 'Kiosk',
                    '收銀員(Cashier)': getattr(o, 'cashier_username', '') or '-',
                    '身分/會員ID(Customer ID)': cust_identifier, '進場時間(Login At)': login_str, '點餐耗時(Duration)': duration_str,
                    '下單時間(Order Created)': o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else '',
                    '出餐時間(Completed At)': o.completed_at.strftime('%Y-%m-%d %H:%M:%S') if o.completed_at else '未出餐',
                    '應付總額(Total)': o.total_price,
                    '實收金額(Received)': getattr(o, 'cash_received', None) if getattr(o, 'cash_received', None) is not None else o.total_price,
                    '找零金額(Change)': getattr(o, 'cash_change', 0) or 0,
                    '付款方式(Payment)': o.payment_method, '狀態(Status)': o.status, '使用紅利': o.points_used
                })
            pd.DataFrame(order_data if order_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='訂單總覽(Order)', index=False, startrow=1)

        if 'OrderItem' in selected_tables:
            order_items = OrderItem.query.filter(OrderItem.order_id.in_(filtered_order_ids)).all() if filtered_order_ids else []
            data = [{
                'ID': oi.id, '關聯訂單ID(Order ID)': oi.order_id, '品名(Item Name)': oi.item_name,
                '單價(Price)': oi.price, '數量(Qty)': oi.quantity, '客製化(Customization)': oi.customization
            } for oi in order_items]
            pd.DataFrame(data if data else [{'資料': '目前無明細'}]).to_excel(writer, sheet_name='訂單明細(OrderItem)', index=False, startrow=1)

        if 'Analytics' in selected_tables:
            valid_orders = [o for o in filtered_orders if o.status != 'Cancelled']
            tot_revenue = sum(o.total_price or 0 for o in valid_orders)
            total_points = sum(o.points_used or 0 for o in valid_orders)
            analytics_data = [
                {'指標 (Metric)': '區間總訂單數', '數值 (Value)': len(filtered_orders)},
                {'指標 (Metric)': '有效訂單數', '數值 (Value)': len(valid_orders)},
                {'指標 (Metric)': '總營收', '數值 (Value)': tot_revenue},
                {'指標 (Metric)': '總折扣折抵', '數值 (Value)': sum(o.discount_amount or 0 for o in valid_orders)},
                {'指標 (Metric)': '紅利使用總額 (Points Used)', '數值 (Value)': total_points},
                {'指標 (Metric)': '平均客單價', '數值 (Value)': round(tot_revenue / len(valid_orders), 2) if valid_orders else 0}
            ]
            pd.DataFrame(analytics_data).to_excel(writer, sheet_name='區間營運總覽(Analytics)', index=False, startrow=1)

            item_stats = {}
            for o in valid_orders:
                for item in o.items:
                    if '[點數兌換]' in item.item_name or item.item_name.startswith('🎁') or item.customization == '紅利免費兌換':
                        continue
                    if item.item_name not in item_stats:
                        item_stats[item.item_name] = {'qty': 0, 'rev': 0}
                    item_stats[item.item_name]['qty'] += (item.quantity or 1)
                    item_stats[item.item_name]['rev'] += ((item.price or 0) * (item.quantity or 1))

            sales_data = [
                {'餐點名稱 (Item Name)': name, '銷售數量 (Qty)': stats['qty'], '創造營收 (Revenue)': stats['rev']}
                for name, stats in sorted(item_stats.items(), key=lambda x: x[1]['qty'], reverse=True)
            ]
            pd.DataFrame(sales_data if sales_data else [{'資料': '所選日期範圍內無銷售資料'}]).to_excel(writer, sheet_name='區間熱銷排行(ItemSales)', index=False, startrow=1)

        if 'AdminLog' in selected_tables:
            logs_query = AdminLog.query
            if start_date:
                logs_query = logs_query.filter(AdminLog.login_at >= start_date)
            if end_date:
                logs_query = logs_query.filter(AdminLog.login_at <= end_date)
            log_data = [{
                'ID': lg.id, '管理員帳號': lg.username,
                '登入時間': lg.login_at.strftime('%Y-%m-%d %H:%M:%S') if lg.login_at else '',
                '登出時間': lg.logout_at.strftime('%Y-%m-%d %H:%M:%S') if lg.logout_at else '尚未登出',
                'IP 位址': lg.ip_address or '本機'
            } for lg in logs_query.order_by(AdminLog.id.desc()).all()]
            pd.DataFrame(log_data if log_data else [{'資料': '目前無紀錄'}]).to_excel(writer, sheet_name='管理員進出日誌(AdminLog)', index=False, startrow=1)

            audit_query = StaffAuditLog.query
            if start_date:
                audit_query = audit_query.filter(StaffAuditLog.created_at >= start_date)
            if end_date:
                audit_query = audit_query.filter(StaffAuditLog.created_at <= end_date)
            audit_data = [{
                'ID': au.id, '員工帳號(Staff)': au.staff_username, '角色(Role)': au.staff_role,
                '操作類型(Action)': au.action_type, '關聯訂單ID(Order ID)': au.target_order_id or '',
                '關聯會員ID(User ID)': au.target_user_id or '', '點數異動(Points Delta)': au.points_delta or 0,
                '詳細內容(Details)': au.details or '', 'IP 位址': au.ip_address or '',
                '發生時間(Created At)': au.created_at.strftime('%Y-%m-%d %H:%M:%S') if au.created_at else ''
            } for au in audit_query.order_by(StaffAuditLog.id.desc()).all()]
            pd.DataFrame(audit_data if audit_data else [{'資料': '目前無稽核紀錄'}]).to_excel(writer, sheet_name='員工操作稽核(StaffAuditLog)', index=False, startrow=1)

        display_start = start_date_str if start_date_str else '全部區間 (All)'
        display_end = end_date_str if end_date_str else '全部區間 (All)'
        date_range_text = f"報表資料區間：{display_start} ~ {display_end}"

        date_dependent_sheets = {
            '訂單總覽(Order)', '訂單明細(OrderItem)',
            '區間營運總覽(Analytics)', '區間熱銷排行(ItemSales)',
            '管理員進出日誌(AdminLog)', '員工操作稽核(StaffAuditLog)'
        }

        def calculate_display_width(val):
            if val is None:
                return 0
            text = str(val)
            width = 0.0
            for char in text:
                status = unicodedata.east_asian_width(char)
                if status in ('F', 'W') or ord(char) >= 0x2600 or ord(char) >= 0x1F000:
                    width += 2.2
                else:
                    width += 1.1
            return int(width)

        for sheet_name, ws in writer.sheets.items():
            if sheet_name in date_dependent_sheets:
                ws.cell(row=1, column=1, value=date_range_text).font = Font(bold=True, color="0055aa")

            for col in ws.columns:
                max_len = 0
                column_letter = col[0].column_letter
                for cell in col:
                    # 以 "=" 開頭的字串 (例如會員把姓名註冊成 =HYPERLINK(...)) 會被 openpyxl 當成公式寫入，
                    # 店員用 Excel 開啟報表時就會執行。本系統匯出的資料一律是純文字，強制標記為字串即可。
                    if cell.data_type == 'f':
                        cell.data_type = 's'
                    if sheet_name in date_dependent_sheets and cell.row == 1 and cell.column == 1:
                        continue
                    cell_len = calculate_display_width(cell.value)
                    if cell_len > max_len:
                        max_len = cell_len
                ws.column_dimensions[column_letter].width = max(max_len + 4, 12)

    output.seek(0)
    return output

def upsert_menu_items_from_df(df, update_existing=True, auto_commit=True):
    """
    共用菜單 DataFrame 解析與寫入/更新函式
    - auto_commit: 是否於內部提交交易（多工作表批次匯入時設為 False 以確保單一交易原子性）
    - 智慧庫存保留：檔案未提供庫存欄位時，既有品項不覆蓋庫存
    - 紅利商城防呆：自動檢驗點數不得大於單價
    """
    df_menu = df.rename(columns=MENU_COLUMN_MAPPING)
    if 'name' not in df_menu.columns or 'price' not in df_menu.columns:
        return False, "檔案缺少必要欄位：「餐點名稱」或「單價」！"

    count = 0
    has_stock_col = 'stock' in df_menu.columns
    has_total_stock_col = 'total_stock' in df_menu.columns

    for _, row in df_menu.iterrows():
        name = str(row.get('name', '')).strip()
        if not name or pd.isna(row.get('name')) or name == '目前無資料':
            continue

        category = str(row.get('category', '主餐')).strip() if not pd.isna(row.get('category')) else '主餐'
        try:
            price = max(0, round(float(row.get('price', 0))))
        except (ValueError, TypeError):
            price = 0
        try:
            discount_price = max(0, round(float(row.get('discount_price', 0))))
        except (ValueError, TypeError):
            discount_price = 0
        try:
            reward_points = max(0, int(row.get('reward_points', 0)))
        except (ValueError, TypeError):
            reward_points = 0
        try:
            reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
        except (ValueError, TypeError):
            reward_discount_points = 0

        # 紅利商城防呆：檢驗兌換點數不可高於原價、特惠點數不可大於等於兌換點數
        is_rew = parse_bool(row.get('is_reward'))
        if is_rew:
            if reward_points <= 0 or (price > 0 and reward_points > price):
                is_rew = False
                reward_points = 0
                reward_discount_points = 0
            elif reward_discount_points >= reward_points:
                reward_discount_points = 0

        # 庫存欄位解析（標記該列是否明確提供庫存）
        row_has_stock = has_stock_col and not pd.isna(row.get('stock'))
        row_has_total_stock = has_total_stock_col and not pd.isna(row.get('total_stock'))

        if row_has_stock:
            try: stock_val = max(0, int(row.get('stock', 50)))
            except (ValueError, TypeError): stock_val = 50
        else:
            stock_val = 50

        # 總量解析：未提供或格式錯誤時為 None (不覆蓋既有總量)
        total_raw = None
        if row_has_total_stock:
            try: total_raw = max(0, int(row.get('total_stock')))
            except (ValueError, TypeError): total_raw = None
        # 新品項：總量不得小於庫存 (未提供時沿用預設 50)
        total_stock_val = max(stock_val, total_raw if total_raw is not None else 50)

        modifiers = str(row.get('modifiers', 'none')).strip() if not pd.isna(row.get('modifiers')) else 'none'
        if modifiers not in ['none', 'ice_sugar', 'spicy', 'addons']:
            modifiers = 'none'

        description = str(row.get('description', '')).strip() if not pd.isna(row.get('description')) else ''
        is_rec = parse_bool(row.get('is_recommended'))
        is_new = parse_bool(row.get('is_new'))
        is_disc = parse_bool(row.get('is_discount'))

        existing = MenuItem.query.filter_by(name=name).first()
        if existing and update_existing:
            existing.category = category
            existing.price = price
            existing.discount_price = discount_price
            existing.reward_points = reward_points
            existing.reward_discount_points = reward_discount_points
            existing.modifiers = modifiers
            existing.description = description
            existing.is_manual_popular = is_rec
            existing.is_new = is_new
            existing.is_discount = is_disc
            existing.is_reward = is_rew
            # 僅在檔案「明確包含庫存欄位且非空」時才覆蓋既有庫存
            if row_has_stock:
                existing.stock = stock_val
                existing.is_sold_out = (stock_val <= 0)
            if total_raw is not None:
                existing.total_stock = total_raw
            # 不變式：總量不得小於庫存 (只給庫存或只給較小總量時自動上調總量)
            if (existing.total_stock or 0) < (existing.stock or 0):
                existing.total_stock = existing.stock
        elif not existing:
            new_item = MenuItem(
                name=name, category=category, price=price, modifiers=modifiers,
                description=description, image_path='', is_recommended=False,
                is_manual_popular=is_rec, is_discount=is_disc, discount_price=discount_price,
                is_new=is_new, is_reward=is_rew, reward_points=reward_points,
                reward_discount_points=reward_discount_points, stock=stock_val,
                total_stock=total_stock_val, is_sold_out=(stock_val <= 0), can_be_add_on=False,
                add_on_price=0, addon_trigger_type='any', addon_trigger_target=''
            )
            db.session.add(new_item)
        count += 1

    if auto_commit:
        db.session.commit()
    return True, count