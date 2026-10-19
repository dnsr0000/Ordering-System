import os
import io
import zipfile
import unicodedata
import pandas as pd
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from openpyxl.styles import Font
from app.config import Config
from app.extensions import db
from app.models.user import User, AdminLog, StaffAuditLog, CustomerLog
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.coupon import Coupon, UserCoupon
from app.services.order_service import update_popular_items

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


def create_full_backup_zip():
    """建立完整系統備份包（SQLite 實體資料庫 + 會員照片 + 菜單照片）"""
    db.session.execute(db.text("PRAGMA wal_checkpoint(TRUNCATE);"))
    db.session.commit()

    memory_file = io.BytesIO()
    db_path = getattr(db.engine.url, 'database', None)
    if db_path and not os.path.exists(db_path):
        db_path = None
    if not db_path:
        for candidate in ('menu.db', 'test_menu.db'):
            p = os.path.join(Config.DATABASE_DIR, candidate)
            if os.path.exists(p):
                db_path = p
                break

    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        if db_path and os.path.exists(db_path):
            zf.write(db_path, arcname='menu.db')

        if os.path.exists(Config.UPLOAD_FOLDER_MEMBER):
            for root, _, files in os.walk(Config.UPLOAD_FOLDER_MEMBER):
                for file in files:
                    zf.write(os.path.join(root, file), arcname=os.path.join('static', 'member', file).replace('\\', '/'))
        if os.path.exists(Config.UPLOAD_FOLDER_MENU):
            for root, _, files in os.walk(Config.UPLOAD_FOLDER_MENU):
                for file in files:
                    zf.write(os.path.join(root, file), arcname=os.path.join('static', 'menu', file).replace('\\', '/'))

    memory_file.seek(0)
    return memory_file


def restore_system_backup(file_storage, restore_db=True, restore_photos=True, restore_menu_photos=True, overwrite_photos=True):
    """執行系統備份包還原"""
    db_path = os.path.abspath(os.path.join(Config.DATABASE_DIR, 'menu.db'))
    member_dir = os.path.abspath(Config.UPLOAD_FOLDER_MEMBER)
    menu_dir = os.path.abspath(Config.UPLOAD_FOLDER_MENU)

    with zipfile.ZipFile(file_storage, 'r') as zf:
        namelist = zf.namelist()
        restored_db_status = False
        restored_photo_count = 0
        restored_menu_photo_count = 0

        if restore_db:
            db_entry = next((n for n in namelist if os.path.basename(n.replace('\\', '/')) == 'menu.db'), None)
            if db_entry:
                db_bytes = zf.read(db_entry)
                if db_bytes.startswith(b'SQLite format 3\x00'):
                    db.session.remove()
                    db.engine.dispose()
                    with open(db_path, 'wb') as f:
                        f.write(db_bytes)
                    restored_db_status = True

        for entry in namelist:
            norm_entry = entry.replace('\\', '/')
            if norm_entry.endswith('/'):
                continue
            safe_name = secure_filename(os.path.basename(norm_entry))
            if not safe_name:
                continue

            parts = [p.lower() for p in norm_entry.split('/')[:-1]]
            target_dir = None
            if restore_photos and ('member' in parts):
                target_dir = member_dir
            elif restore_menu_photos and ('menu' in parts):
                target_dir = menu_dir

            if target_dir:
                dest_path = os.path.abspath(os.path.join(target_dir, safe_name))
                if os.path.commonpath([target_dir, dest_path]) == target_dir:
                    if not os.path.exists(dest_path) or overwrite_photos:
                        with open(dest_path, 'wb') as f_out:
                            f_out.write(zf.read(entry))
                        if target_dir == member_dir:
                            restored_photo_count += 1
                        else:
                            restored_menu_photo_count += 1

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
        update_popular_items()
    return True, count
