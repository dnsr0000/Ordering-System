"""Excel 報表匯出與智慧匯入 (會員 / 菜單 / 優惠券)"""
import csv
import io
import re
import unicodedata
import zipfile

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from sqlalchemy.orm import selectinload

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon, UserCoupon, parse_discount_value
from app.models.enums import DiscountType
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.user import AdminLog, StaffAuditLog, User
from app.services.menu_service import MAX_AMOUNT, is_blank, parse_bool, upsert_menu_items_from_rows
from app.utils import day_bounds, parse_date, safe_int, validate_phone, workbook_bytes


def _write_sheet(wb, sheet_name, rows, startrow=0):
    """
    把 list[dict] 寫成一張工作表：標題列在第 startrow+1 列 (startrow=1 時第 1 列留給報表區間說明)，
    欄位順序依各列鍵值首次出現的順序；None 寫成空白儲存格。
    """
    ws = wb.create_sheet(sheet_name)
    headers = list(dict.fromkeys(key for row in rows for key in row))
    for _ in range(startrow):
        ws.append([])
    ws.append(headers)
    for row in rows:
        ws.append([_xlsx_value(row.get(key)) for key in headers])
    return ws


def _xlsx_value(val):
    """
    openpyxl 寫入含控制字元 (\\x00-\\x08、\\x0b、\\x0c、\\x0e-\\x1f) 的字串會拋 IllegalCharacterError；
    顧客可輸入的文字 (會員姓名、客製化) 可能夾帶這些字元，匯出前先去掉，避免整份報表 500。
    """
    return ILLEGAL_CHARACTERS_RE.sub('', val) if isinstance(val, str) else val


ORDER_TABLES = {'Order', 'OrderItem', 'Analytics'}
MAX_POINTS = 100_000_000          # 點數上限 (與後台表單 admin.MAX_POINTS 一致)；金額上限用 MAX_AMOUNT
MAX_PER_USER_LIMIT = 1_000_000


def _export_percent_value(c):
    """
    百分比券的折抵值：1~10 (例如 5 = 0.5 折) 寫成 0.05 這種小數，
    匯回時 parse_discount_value 才不會把它當成「8.5 折寫法」再乘 10；其他值照存檔值輸出。
    """
    if c.discount_type == DiscountType.PERCENT and c.discount_value is not None and 1 <= c.discount_value <= 10:
        return c.discount_value / 100
    return c.discount_value


def generate_excel_export(selected_tables, start_date_str=None, end_date_str=None):
    """產出 10 張工作表之 Excel 檔案流"""
    start = parse_date(start_date_str, '開始日期')
    end = parse_date(end_date_str, '結束日期')
    # 半開區間 [開始日 00:00, 結束日 + 1 天 00:00)，與報表的 day_bounds 一致 (不會漏掉 23:59:59.5 的資料)
    start_date = day_bounds(start)[0] if start else None
    end_date = day_bounds(end)[1] if end else None

    filtered_orders = []
    if ORDER_TABLES & set(selected_tables):   # 只勾菜單 / 會員 / 優惠券時不必載入整個訂單歷史
        orders_query = Order.query.options(selectinload(Order.items), selectinload(Order.customer_log))
        if start_date:
            orders_query = orders_query.filter(Order.created_at >= start_date)
        if end_date:
            orders_query = orders_query.filter(Order.created_at < end_date)
        filtered_orders = orders_query.all()

    wb = Workbook()
    wb.remove(wb.active)   # 只保留實際勾選的工作表
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
        _write_sheet(wb, '會員資料(User)', user_data if user_data else [{'資料': '目前無資料'}])

    if 'MenuItem' in selected_tables:
        menu_data = [{
            'ID': i.id, '餐點名稱(Name)': i.name, '分類(Category)': i.category, '單價(Price)': i.price,
            '客製化群組(Modifiers)': i.modifiers or 'none', '商品描述(Description)': i.description or '',
            '是否特價(Is Discount)': '是' if i.is_discount else '否', '特價金額(Discount Price)': i.discount_price,
            '熱門推薦(Recommended)': '是' if (i.is_manual_popular or i.is_recommended) else '否',
            '新品上市(Is New)': '是' if i.is_new else '否', '開放紅利兌換(Is Reward)': '是' if i.is_reward else '否',
            '兌換所需點數(Reward Points)': i.reward_points, '限時特惠點數(Reward Discount Points)': i.reward_discount_points
        } for i in MenuItem.query.all()]
        _write_sheet(wb, '菜單品項(MenuItem)', menu_data if menu_data else [{'資料': '目前無資料'}])

    if 'Coupon' in selected_tables:
        coupon_data = [{
            'ID': c.id, '代碼(Code)': c.code, '標題(Title)': c.title, '折抵類型(Discount Type)': c.discount_type,
            '折抵值(Discount Value)': _export_percent_value(c), '門檻(Min Spend)': c.min_spend,
            '兌換所需點數(Reward Points)': c.reward_points, '限時特惠點數(Reward Discount Points)': c.reward_discount_points,
            # 純數字 (或「無上限」)，匯回時才解析得回同樣的上限 (limit_display 的「2次」會被當成無上限)
            '每人兌換上限(Per User Limit)': c.per_user_limit if (c.per_user_limit or 0) > 0 else '無上限',
            '上架回饋商城(Is Reward)': '是' if c.is_reward else '否'
        } for c in Coupon.query.all()]
        _write_sheet(wb, '優惠券(Coupon)', coupon_data if coupon_data else [{'資料': '目前無資料'}])

    if 'UserCoupon' in selected_tables:
        user_coupons_data = [{
            'ID': uc.id, '關聯會員ID': uc.user_id, '優惠代碼(Code)': uc.code,
            '是否已使用(Is Used)': '是' if uc.is_used else '否',
            '領取時間': uc.created_at.strftime('%Y-%m-%d %H:%M') if uc.created_at else ''
        } for uc in UserCoupon.query.all()]
        _write_sheet(wb, '會員持券(UserCoupon)', user_coupons_data if user_coupons_data else [{'資料': '目前無資料'}])

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
        _write_sheet(wb, '訂單總覽(Order)', order_data if order_data else [{'資料': '目前無資料'}], startrow=1)

    if 'OrderItem' in selected_tables:
        # 直接用已 selectinload 的明細 (分批查詢)；不要把全部訂單 id 塞進同一個 IN，超過 SQLite 32766 /
        # PostgreSQL 65535 個綁定參數會整個匯出 500
        order_items = sorted((it for o in filtered_orders for it in o.items), key=lambda it: it.id)
        data = [{
            'ID': oi.id, '關聯訂單ID(Order ID)': oi.order_id, '品名(Item Name)': oi.item_name,
            '單價(Price)': oi.price, '數量(Qty)': oi.quantity, '客製化(Customization)': oi.customization
        } for oi in order_items]
        _write_sheet(wb, '訂單明細(OrderItem)', data if data else [{'資料': '目前無明細'}], startrow=1)

    if 'Analytics' in selected_tables:
        from app.services.report_service import is_revenue
        # 與營運報表 / 後台總覽同一口徑：待付款、授權中、桌邊未結帳、待退款都不計入營收與熱銷
        valid_orders = [o for o in filtered_orders if is_revenue(o)]
        tot_revenue = sum(o.refundable_amount for o in valid_orders)   # 實收：扣掉部分退款
        total_points = sum(o.points_used or 0 for o in valid_orders)
        analytics_data = [
            {'指標 (Metric)': '區間總訂單數', '數值 (Value)': len(filtered_orders)},
            {'指標 (Metric)': '有效訂單數', '數值 (Value)': len(valid_orders)},
            {'指標 (Metric)': '總營收', '數值 (Value)': tot_revenue},
            {'指標 (Metric)': '總折扣折抵', '數值 (Value)': sum(o.discount_amount or 0 for o in valid_orders)},
            {'指標 (Metric)': '紅利使用總額 (Points Used)', '數值 (Value)': total_points},
            {'指標 (Metric)': '平均客單價', '數值 (Value)': round(tot_revenue / len(valid_orders), 2) if valid_orders else 0}
        ]
        _write_sheet(wb, '區間營運總覽(Analytics)', analytics_data, startrow=1)

        item_stats = {}
        for o in valid_orders:
            for item in o.items:
                if item.is_reward:
                    continue
                if item.item_name not in item_stats:
                    item_stats[item.item_name] = {'qty': 0, 'rev': 0}
                qty = item.quantity if item.quantity is not None else 1   # 單品作廢後可能為 0
                item_stats[item.item_name]['qty'] += qty
                item_stats[item.item_name]['rev'] += (item.price or 0) * qty

        sales_data = [
            {'餐點名稱 (Item Name)': name, '銷售數量 (Qty)': stats['qty'], '創造營收 (Revenue)': stats['rev']}
            for name, stats in sorted(item_stats.items(), key=lambda x: x[1]['qty'], reverse=True)
        ]
        _write_sheet(wb, '區間熱銷排行(ItemSales)', sales_data if sales_data else [{'資料': '所選日期範圍內無銷售資料'}],
                     startrow=1)

    if 'AdminLog' in selected_tables:
        logs_query = AdminLog.query
        if start_date:
            logs_query = logs_query.filter(AdminLog.login_at >= start_date)
        if end_date:
            logs_query = logs_query.filter(AdminLog.login_at < end_date)
        log_data = [{
            'ID': lg.id, '管理員帳號': lg.username,
            '登入時間': lg.login_at.strftime('%Y-%m-%d %H:%M:%S') if lg.login_at else '',
            '登出時間': lg.logout_at.strftime('%Y-%m-%d %H:%M:%S') if lg.logout_at else '尚未登出',
            'IP 位址': lg.ip_address or '本機'
        } for lg in logs_query.order_by(AdminLog.id.desc()).all()]
        _write_sheet(wb, '管理員進出日誌(AdminLog)', log_data if log_data else [{'資料': '目前無紀錄'}], startrow=1)

        audit_query = StaffAuditLog.query
        if start_date:
            audit_query = audit_query.filter(StaffAuditLog.created_at >= start_date)
        if end_date:
            audit_query = audit_query.filter(StaffAuditLog.created_at < end_date)
        audit_data = [{
            'ID': au.id, '員工帳號(Staff)': au.staff_username, '角色(Role)': au.staff_role,
            '操作類型(Action)': au.action_type, '關聯訂單ID(Order ID)': au.target_order_id or '',
            '關聯會員ID(User ID)': au.target_user_id or '', '點數異動(Points Delta)': au.points_delta or 0,
            '詳細內容(Details)': au.details or '', 'IP 位址': au.ip_address or '',
            '發生時間(Created At)': au.created_at.strftime('%Y-%m-%d %H:%M:%S') if au.created_at else ''
        } for au in audit_query.order_by(StaffAuditLog.id.desc()).all()]
        _write_sheet(wb, '員工操作稽核(StaffAuditLog)', audit_data if audit_data else [{'資料': '目前無稽核紀錄'}], startrow=1)

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

    for ws in wb.worksheets:
        sheet_name = ws.title
        if sheet_name in date_dependent_sheets:
            ws.cell(row=1, column=1, value=date_range_text).font = Font(bold=True, color="0055aa")

        for col in ws.columns:
            max_len = 0
            column_letter = col[0].column_letter
            for cell in col:
                # 以 "=" 開頭的字串 (例如會員把姓名註冊成 =HYPERLINK(...)) 會被 openpyxl 當成公式寫入，
                # 店員用 Excel 開啟報表時就會執行。本系統匯出的資料一律是純文字，強制標記為字串即可。
                if sheet_name in date_dependent_sheets and cell.row == 1 and cell.column == 1:
                    continue
                cell_len = calculate_display_width(cell.value)
                if cell_len > max_len:
                    max_len = cell_len
            ws.column_dimensions[column_letter].width = max(max_len + 4, 12)

    return workbook_bytes(wb)   # 防公式注入 + BytesIO


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


COUPON_TYPE_ALIASES = {
    'fixed': DiscountType.FIXED, 'amount': DiscountType.FIXED, '固定': DiscountType.FIXED, '固定金額': DiscountType.FIXED,
    '金額': DiscountType.FIXED, '現折': DiscountType.FIXED, '折抵金額': DiscountType.FIXED,
    'percent': DiscountType.PERCENT, 'percentage': DiscountType.PERCENT, '%': DiscountType.PERCENT,
    '百分比': DiscountType.PERCENT, '折扣': DiscountType.PERCENT, '打折': DiscountType.PERCENT, '折數': DiscountType.PERCENT,
}


def clean_phone_number(val):
    """
    匯入的手機號碼：Excel 數字欄位會掉開頭 0 (9 碼且 9 開頭補 0)；去掉 -、空白、括號等分隔符號，
    國碼 886 開頭換成 0 (+886 912 000 222 → 0912000222)。是否為有效手機由呼叫端 validate_phone 檢查。
    """
    if is_blank(val):
        return ''
    digits = re.sub(r'\D', '', str(val).strip().removesuffix('.0'))
    if digits.startswith('886') and len(digits) > 10:
        digits = digits[3:] if digits[3:].startswith('0') else '0' + digits[3:]
    return '0' + digits if len(digits) == 9 and digits.startswith('9') else digits


def parse_per_user_limit(val, default=0):
    """每人兌換上限：空白 / 無上限 → 0 (不限)；數字或「2次」→ 2；無法辨識時回傳 default"""
    s = '' if is_blank(val) else str(val).strip()
    if s.lower() in ('', '無上限', '不限', 'none', 'null'):
        return 0
    num = safe_int(s.removesuffix('次').strip(), None)
    return default if num is None else min(MAX_PER_USER_LIMIT, max(0, num))


def _int_field(row, key, current, upper):
    """
    非負整數欄位 (上限 upper)：空白或無法解析時沿用 current (既有資料的原值；新資料傳預設值)。
    夾上限避免 '1e20' 這類值寫入 Integer 欄位時溢位，讓整批匯入 500。
    """
    num = safe_int(_value(row, key), None)
    return current if num is None else min(upper, max(0, num))


def _value(row, key):
    val = row.get(key)
    return None if is_blank(val) else val


def rename_columns(rows, mapping):
    """把每列的標題鍵換成系統欄位名 (對照表沒有的標題原樣保留)，回傳 (欄位集合, 新列清單)"""
    renamed = [{mapping.get(key, key): val for key, val in row.items()} for row in rows]
    return set().union(*renamed) if renamed else set(), renamed


def _import_users(rows):
    columns, rows = rename_columns(rows, USER_COLUMNS)
    if 'name' not in columns or 'phone' not in columns:
        return 0
    has_points = 'points' in columns
    count = 0
    for row in rows:
        name = str(_value(row, 'name') or '').strip()[:100]   # User.name 為 String(100)，PostgreSQL 超長會整批失敗
        phone = clean_phone_number(_value(row, 'phone'))
        if not name or not phone or name == '目前無資料':
            continue
        if not validate_phone(phone):
            # 不是 09 開頭 10 碼的手機：存進去之後查不到、無法登入，顧客再註冊時還會變成重複會員，整列略過
            continue
        points = _value(row, 'points') if has_points else None
        user = User.query.filter_by(phone=phone).first()
        if user is None:
            # 檔案有提供點數就照填 (包含 0 點)；沒有點數欄才給新會員預設 20 點
            db.session.add(User(name=name, phone=phone, photo_path='',
                                points=min(MAX_POINTS, max(0, safe_int(points, 0))) if points is not None else 20))
        else:
            # 既有會員不以檔案覆寫點數：匯出檔的點數是匯出當時的快照，拿舊檔改菜單再匯入時
            # 會把之後賺到 / 用掉的點數全部改回去 (點數等同金額；要調整請在會員頁逐筆修改，會留下異動紀錄)
            user.name = name
        count += 1
    return count


def _import_coupons(rows):
    columns, rows = rename_columns(rows, COUPON_COLUMNS)
    if 'code' not in columns or 'title' not in columns:
        return 0
    count = 0
    for row in rows:
        from app.services.coupon_service import CODE_RE
        code = str(_value(row, 'code') or '').strip().upper()
        title = str(_value(row, 'title') or '').strip()[:100]
        if not code or not title or code == '目前無資料' or not CODE_RE.match(code):
            continue   # 代碼格式不合 (空白、符號、過短) 的列略過
        raw_type = _value(row, 'discount_type')
        dtype = COUPON_TYPE_ALIASES.get(str(raw_type).strip().lower()) if raw_type is not None else None
        if raw_type is not None and dtype is None:
            continue   # 折抵類型無法辨識的列略過 (不可默默當成固定金額：9 折券會變成現折 $90)
        coupon = Coupon.query.filter_by(code=code).first()
        is_new = coupon is None
        # 既有券只更新檔案裡有填的欄位 (與菜單匯入一致)；新券才套用預設值
        dtype = dtype or (DiscountType.FIXED if is_new else coupon.discount_type)
        raw_value = _value(row, 'discount_value')
        if raw_value is not None:
            value = parse_discount_value(dtype, raw_value)
        elif not is_new and dtype == coupon.discount_type:
            value = coupon.discount_value
        else:   # 新的百分比券或改了折抵類型卻沒填折抵值：無法決定折扣，略過
            value = 0 if is_new and dtype == DiscountType.FIXED else None
        if value is None:
            continue
        if is_new:
            coupon = Coupon(code=code, discount_type=dtype, discount_value=value, min_spend=0,
                            reward_points=0, reward_discount_points=0, per_user_limit=0)
            db.session.add(coupon)
        coupon.title = title
        coupon.discount_type = dtype
        coupon.discount_value = value
        coupon.min_spend = _int_field(row, 'min_spend', coupon.min_spend or 0, MAX_AMOUNT)
        coupon.reward_points = _int_field(row, 'reward_points', coupon.reward_points or 0, MAX_POINTS)
        coupon.reward_discount_points = _int_field(row, 'reward_discount_points', coupon.reward_discount_points or 0,
                                                   MAX_POINTS)
        is_reward = _value(row, 'is_reward')
        if is_reward is not None or is_new or _value(row, 'reward_points') is not None:
            # 上架回饋商城空白時，有兌換點數就自動上架；兌換點數為 0 一律不上架 (0 點即可兌換，
            # 與後台表單 / 菜單匯入的防呆一致)
            wanted = parse_bool(is_reward) if is_reward is not None else coupon.reward_points > 0
            coupon.is_reward = wanted and coupon.redeem_points > 0
        raw_limit = _value(row, 'per_user_limit')
        if raw_limit is not None or is_new:   # 空白 = 沿用原上限 (新券為不限)
            coupon.per_user_limit = parse_per_user_limit(raw_limit, default=coupon.per_user_limit or 0)
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
        # 舊版 .xls (或其他非 zip 檔) openpyxl 無法讀取
        raise AppError('❌ 無法讀取此檔案：僅支援 .xlsx 與 .csv，舊版 .xls 請先用 Excel 另存為 .xlsx 再匯入！')
    finally:
        stream.seek(0)
    if total > MAX_XLSX_UNCOMPRESSED:
        raise AppError('❌ 檔案內容過大 (解壓後超過 30MB)，請分批匯入！')


def _table_rows(header, data_rows):
    """
    把「標題列 + 資料列」整理成 list[dict]：標題一律轉成字串 (空白標題補成 Unnamed: n)，
    短列補 None、多餘欄位忽略；整列空白的資料列略過。
    """
    headers = [f'Unnamed: {i}' if is_blank(h) else str(h) for i, h in enumerate(header)]
    rows = []
    for values in data_rows:
        values = list(values)[:len(headers)] + [None] * (len(headers) - len(values))
        if all(is_blank(v) for v in values):
            continue
        rows.append(dict(zip(headers, values)))
    return headers, rows


def _read_csv(file_storage):
    """讀取 CSV 的標題列與最多 MAX_IMPORT_ROWS 筆資料列 (空白列不計)；回傳 (標題, 列清單)"""
    try:
        text = file_storage.stream.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        raise AppError('❌ CSV 檔案編碼無法辨識，請以 UTF-8 編碼另存後再匯入！')
    reader = (r for r in csv.reader(io.StringIO(text)) if any(not is_blank(v) for v in r))
    header = next(reader, None)
    if header is None:
        return [], []
    data_rows = []
    for row in reader:
        if len(data_rows) >= MAX_IMPORT_ROWS:
            break
        data_rows.append(row)
    return _table_rows(header, data_rows)


def _read_sheet(ws):
    """讀取工作表的標題列與最多 MAX_IMPORT_ROWS 筆資料列；回傳 (標題, 列清單)"""
    rows = ws.iter_rows(min_row=1, max_row=MAX_IMPORT_ROWS + 1, values_only=True)
    header = next(rows, None)
    if header is None:
        return [], []
    return _table_rows(header, rows)


def import_workbook(file_storage, allow_users=True, allow_coupons=True):
    """
    匯入 CSV (菜單) 或 Excel (多工作表：會員 / 菜單 / 優惠券，依工作表名稱或欄位辨識)。
    全部工作表在同一筆交易：任一表失敗整批 rollback。回傳 {'User': n, 'MenuItem': n, 'Coupon': n}。
    """
    counts = {'User': 0, 'MenuItem': 0, 'Coupon': 0}
    if (file_storage.filename or '').lower().endswith('.csv'):
        headers, rows = _read_csv(file_storage)
        counts['MenuItem'] = upsert_menu_items_from_rows(rows, columns=headers)
        db.session.commit()
        return counts

    _check_xlsx_size(file_storage)
    try:
        book = load_workbook(file_storage.stream, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise AppError(f'❌ 檔案解析失敗，請確認檔案格式是否正確！({e})')
    try:
        for sheet in book.sheetnames:
            headers, rows = _read_sheet(book[sheet])
            if not rows:
                continue
            cols = {c.lower() for c in headers}
            if 'User' in sheet or '會員' in sheet or cols & {'電話(phone)', 'phone', '電話'}:
                if not allow_users:
                    db.session.rollback()
                    raise AppError(f'❌ 工作表【{sheet}】是會員資料，您沒有「會員」頁面權限，無法匯入')
                counts['User'] += _import_users(rows)
            elif 'MenuItem' in sheet or '菜單' in sheet or cols & {'單價(price)', 'price', '單價'}:
                try:
                    counts['MenuItem'] += upsert_menu_items_from_rows(rows, columns=headers)
                except AppError as e:
                    db.session.rollback()
                    raise AppError(f'❌ 菜單工作表【{sheet}】解析失敗：{e.message}')
            elif 'Coupon' in sheet or '優惠券' in sheet or cols & {'代碼(code)', 'code', '代碼'}:
                if not allow_coupons:
                    db.session.rollback()
                    raise AppError(f'❌ 工作表【{sheet}】是優惠券，您沒有「優惠券」頁面權限，無法匯入')
                counts['Coupon'] += _import_coupons(rows)
    finally:
        book.close()
    db.session.commit()
    return counts
