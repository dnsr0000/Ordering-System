"""Excel 報表匯出與智慧匯入 (會員 / 菜單 / 優惠券)"""
import io
import unicodedata
import zipfile
from datetime import datetime, timedelta

import pandas as pd
from openpyxl.styles import Font
from sqlalchemy.orm import selectinload

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon, UserCoupon, parse_discount_value
from app.models.enums import DiscountType
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.user import AdminLog, StaffAuditLog, User
from app.services.menu_service import parse_bool, upsert_menu_items_from_df
from app.utils import safe_int


def generate_excel_export(selected_tables, start_date_str=None, end_date_str=None):
    """產出 10 張工作表之 Excel 檔案流"""
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1, seconds=-1) if end_date_str else None

    orders_query = Order.query.options(selectinload(Order.items), selectinload(Order.customer_log))
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
                log = o.customer_log
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
                    '訂單來源(Source)': o.order_source or 'Kiosk',
                    '收銀員(Cashier)': o.cashier_username or '-',
                    '身分/會員ID(Customer ID)': cust_identifier, '進場時間(Login At)': login_str, '點餐耗時(Duration)': duration_str,
                    '下單時間(Order Created)': o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else '',
                    '出餐時間(Completed At)': o.completed_at.strftime('%Y-%m-%d %H:%M:%S') if o.completed_at else '未出餐',
                    '應付總額(Total)': o.total_price,
                    '實收金額(Received)': o.cash_received if o.cash_received is not None else o.total_price,
                    '找零金額(Change)': o.cash_change or 0,
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
                    if item.is_reward:
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


# ============================================================================
# 智慧匯入
# ============================================================================
USER_COLUMNS = {
    '姓名(Name)': 'name', '姓名': 'name', 'name': 'name',
    '電話(Phone)': 'phone', '電話': 'phone', 'phone': 'phone',
    '紅利點數(Points)': 'points', '紅利點數': 'points', 'points': 'points',
}
COUPON_COLUMNS = {
    '代碼(Code)': 'code', '代碼': 'code', 'code': 'code', '優惠代碼': 'code',
    '標題(Title)': 'title', '標題': 'title', 'title': 'title', '優惠券名稱': 'title', '名稱': 'title',
    '折抵類型(Discount Type)': 'discount_type', '折抵類型': 'discount_type', '折抵方式': 'discount_type', 'discount_type': 'discount_type',
    '折抵值(Discount Value)': 'discount_value', '折抵值': 'discount_value', '折抵金額': 'discount_value', 'discount_value': 'discount_value',
    '門檻(Min Spend)': 'min_spend', '門檻': 'min_spend', '最低消費門檻': 'min_spend', '使用門檻': 'min_spend', 'min_spend': 'min_spend',
    '兌換所需點數(Reward Points)': 'reward_points', '兌換所需點數': 'reward_points', '兌換點數': 'reward_points', '點數': 'reward_points',
    'reward_points': 'reward_points',
    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '限時特惠點數': 'reward_discount_points',
    '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points',
    '上架回饋商城(Is Reward)': 'is_reward', '是否上架': 'is_reward', '上架商城': 'is_reward', 'is_reward': 'is_reward',
    '每人兌換上限(Per User Limit)': 'per_user_limit', '每人兌換上限': 'per_user_limit', '兌換上限': 'per_user_limit',
    'per_user_limit': 'per_user_limit',
}


def clean_phone_number(val):
    if val is None or pd.isna(val):
        return ''
    s = str(val).strip().removesuffix('.0')
    return '0' + s if len(s) == 9 and s.startswith('9') else s


def parse_per_user_limit(val):
    s = '' if val is None or (not isinstance(val, str) and pd.isna(val)) else str(val).strip()
    return 0 if s in ('', '無上限', '不限', 'none', 'null') else max(0, safe_int(s, 0))


def _value(row, key):
    val = row.get(key)
    return None if val is None or (not isinstance(val, str) and pd.isna(val)) else val


def _import_users(df):
    df = df.rename(columns=USER_COLUMNS)
    if 'name' not in df.columns or 'phone' not in df.columns:
        return 0
    has_points = 'points' in df.columns
    count = 0
    for _, row in df.iterrows():
        name = str(_value(row, 'name') or '').strip()
        phone = clean_phone_number(_value(row, 'phone'))
        if not name or not phone or name == '目前無資料':
            continue
        points = _value(row, 'points') if has_points else None
        user = User.query.filter_by(phone=phone).first()
        if user is None:
            # 檔案有提供點數就照填 (包含 0 點)；沒有點數欄才給新會員預設 20 點
            db.session.add(User(name=name, phone=phone, photo_path='',
                                points=max(0, safe_int(points, 0)) if points is not None else 20))
        else:
            user.name = name
            if points is not None:
                user.points = max(0, safe_int(points, 0))
        count += 1
    return count


def _import_coupons(df):
    df = df.rename(columns=COUPON_COLUMNS)
    if 'code' not in df.columns or 'title' not in df.columns:
        return 0
    count = 0
    for _, row in df.iterrows():
        code = str(_value(row, 'code') or '').strip().upper()
        title = str(_value(row, 'title') or '').strip()
        if not code or not title or code == '目前無資料':
            continue
        dtype = str(_value(row, 'discount_type') or DiscountType.FIXED).strip()
        dtype = dtype if dtype in (DiscountType.FIXED, DiscountType.PERCENT) else DiscountType.FIXED
        points = max(0, safe_int(_value(row, 'reward_points'), 0))
        is_reward = _value(row, 'is_reward')
        coupon = Coupon.query.filter_by(code=code).first()
        if coupon is None:
            coupon = Coupon(code=code)
            db.session.add(coupon)
        coupon.title = title
        coupon.discount_type = dtype
        coupon.discount_value = parse_discount_value(dtype, _value(row, 'discount_value') or 0)
        coupon.min_spend = max(0, safe_int(_value(row, 'min_spend'), 0))
        coupon.reward_points = points
        coupon.reward_discount_points = max(0, safe_int(_value(row, 'reward_discount_points'), 0))
        coupon.is_reward = parse_bool(is_reward) if is_reward is not None else points > 0
        coupon.per_user_limit = parse_per_user_limit(_value(row, 'per_user_limit'))
        count += 1
    return count


MAX_XLSX_UNCOMPRESSED = 30 * 1024 * 1024
MAX_IMPORT_ROWS = 20000


def _check_xlsx_size(file_storage):
    """xlsx 是 zip 壓縮檔：解析前先檢查解壓後的總大小，避免小檔案解開後耗盡記憶體 (壓縮炸彈)"""
    stream = file_storage.stream
    try:
        with zipfile.ZipFile(stream) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile:
        return  # 舊版 .xls 不是 zip，交給 pandas 判斷
    finally:
        stream.seek(0)
    if total > MAX_XLSX_UNCOMPRESSED:
        raise AppError('❌ 檔案內容過大 (解壓後超過 30MB)，請分批匯入！')


def import_workbook(file_storage):
    """
    匯入 CSV (菜單) 或 Excel (多工作表：會員 / 菜單 / 優惠券，依工作表名稱或欄位辨識)。
    全部工作表在同一筆交易：任一表失敗整批 rollback。回傳 {'User': n, 'MenuItem': n, 'Coupon': n}。
    """
    counts = {'User': 0, 'MenuItem': 0, 'Coupon': 0}
    if (file_storage.filename or '').lower().endswith('.csv'):
        counts['MenuItem'] = upsert_menu_items_from_df(pd.read_csv(file_storage, nrows=MAX_IMPORT_ROWS))
        db.session.commit()
        return counts

    _check_xlsx_size(file_storage)
    try:
        book = pd.ExcelFile(file_storage)
    except Exception as e:  # noqa: BLE001
        raise AppError(f'❌ 檔案解析失敗，請確認檔案格式是否正確！({e})')
    for sheet in book.sheet_names:
        df = pd.read_excel(book, sheet_name=sheet, nrows=MAX_IMPORT_ROWS)
        if df.empty:
            continue
        cols = {str(c).lower() for c in df.columns}
        if 'User' in sheet or '會員' in sheet or cols & {'電話(phone)', 'phone', '電話'}:
            counts['User'] += _import_users(df)
        elif 'MenuItem' in sheet or '菜單' in sheet or cols & {'單價(price)', 'price', '單價'}:
            try:
                counts['MenuItem'] += upsert_menu_items_from_df(df)
            except AppError as e:
                db.session.rollback()
                raise AppError(f'❌ 菜單工作表【{sheet}】解析失敗：{e.message}')
        elif 'Coupon' in sheet or '優惠券' in sheet or cols & {'代碼(code)', 'code', '代碼'}:
            counts['Coupon'] += _import_coupons(df)
    db.session.commit()
    return counts
