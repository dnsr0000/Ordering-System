import hashlib
import io
import re
from datetime import date, datetime, time, timedelta
from functools import wraps

from flask import abort, current_app, g, has_request_context, redirect, request, send_file, url_for
from flask_login import current_user

from app.errors import AppError

PHONE_REGEX = re.compile(r'^09\d{8}$')
PIN_REGEX = re.compile(r'^\d{4,6}$')


# ---------------------------------------------------------------------------
# 權限
# ---------------------------------------------------------------------------
def role_required(*roles):
    """需登入員工帳號；有指定 roles 時角色必須在其中 (未登入 401/導向登入頁，角色不符 403)"""
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                from app.extensions import login_manager
                return login_manager.unauthorized()
            if roles and current_user.role not in roles:
                raise AppError(f"權限不足：您的角色 ({current_user.role}) 無法執行此操作！", 403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def get_own(model, obj_id, label='資料', status=404):
    """
    以主鍵取得「本店」的資料列，不存在或屬於其他店家時拋出 404 (AppError)。
    租戶自動過濾 (app/tenant_context.py) 對 db.session.get 只在真的查資料庫時生效：同一請求先以 tenant_bypass()
    載入過其他店的物件時，session.get 會從 identity map 直接回傳，所以這裡一律再比對 tenant_id。
    """
    from app.extensions import db
    obj = db.session.get(model, safe_int(obj_id, 0))
    if obj is None or getattr(obj, 'tenant_id', None) != g.tenant_id:
        raise AppError(f'查無此{label}', status)
    return obj


def find_own(model, obj_id):
    """同 get_own，但不存在或屬於其他店家時回傳 None (供「可選的」查詢參數使用)"""
    from app.extensions import db
    obj = db.session.get(model, safe_int(obj_id, 0))
    return obj if obj is not None and getattr(obj, 'tenant_id', None) == g.tenant_id else None


def is_platform_admin(staff=None):
    """平台管理者 = 預設店家 (slug=default) 的負責人；整庫備份/還原會觸及所有店家，只開放給平台管理者"""
    staff = staff if staff is not None else current_user
    return bool(
        getattr(staff, 'is_authenticated', False)
        and staff.role == 'owner'
        and staff.tenant is not None and staff.tenant.slug == 'default'
    )


def platform_admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not is_platform_admin():
            raise AppError('權限不足：整庫備份與還原會涉及所有店家的資料，僅限平台管理者 (預設店家負責人) 使用；'
                           '請改用 Excel 匯出您店家的資料。', 403)
        return fn(*args, **kwargs)
    return wrapped


def store_only(fn):
    """只在門市主機提供的頁面 / API (訂位、桌位、交班…是門市本機資料)：線上端 (hub) 回 404"""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from app.sync import runtime as sync_runtime
        if sync_runtime.is_hub():
            abort(404)
        return fn(*args, **kwargs)
    return wrapped


def hub_only(fn):
    """只在線上端提供 (例如選取餐分店)：門市主機回 404"""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from app.sync import runtime as sync_runtime
        if not sync_runtime.is_hub():
            abort(404)
        return fn(*args, **kwargs)
    return wrapped


def member_required(redirect_to=None):
    """
    顧客頁面 (非 API)：需已登入會員，否則導向會員登入頁 (redirect_to 可改為其他端點名稱)。
    會員物件以第一個位置參數傳給 view。API 請改用 rewards._require_member (回 401)。
    """
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            from app.services.customer_session import current_member
            member = current_member()
            if member is None:
                from app.routes.auth import member_login_url
                return redirect(url_for(redirect_to) if redirect_to else member_login_url())
            return fn(member, *args, **kwargs)
        return wrapped
    return decorator


def current_staff_name(default='system'):
    """目前登入員工的帳號；沒有請求情境或未登入 (排程、CLI、顧客端) 時回傳 default"""
    try:
        if has_request_context() and getattr(current_user, 'is_authenticated', False):
            return current_user.username
    except Exception:  # noqa: BLE001  (登入系統尚未初始化的情境)
        pass
    return default


# ---------------------------------------------------------------------------
# 輸入解析
# ---------------------------------------------------------------------------
def safe_float(val, default=0.0):
    """轉浮點數；空值、格式錯誤、NaN、Infinity 一律回傳 default"""
    try:
        if val is None or val == '':
            return default
        num = float(val)
    except (ValueError, TypeError, OverflowError):
        return default
    if num != num or num in (float('inf'), float('-inf')):
        return default
    return num


def safe_int(val, default=0):
    num = safe_float(val, None)
    return default if num is None else int(num)


def safe_round(val, default=0):
    num = safe_float(val, None)
    return default if num is None else int(round(num))


def form_flag(name):
    """HTML checkbox：有送出且不是 0/空字串即為 True"""
    return request.form.get(name) not in (None, '', '0')


def get_json_dict():
    """讀取 JSON 本文；不是合法 JSON 或根節點不是物件時回 400"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise AppError('請求內容必須是 JSON 物件！', 400)
    return data


# ---------------------------------------------------------------- 日期 / 時間
def parse_date(value, label='日期', required=False):
    """表單 / 查詢字串的 YYYY-MM-DD：空值回 None (required=True 時 400)，格式錯誤 400"""
    value = str(value or '').strip()
    if not value:
        if required:
            raise AppError(f'請填寫{label}')
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AppError(f'{label}格式不正確：{value}')


def parse_birthday(form):
    """
    生日 (選填)：表單的 birthday_year / birthday_month / birthday_day 三個下拉選單 (註冊與後台編輯會員共用)。
    三個都空 = 未填；只選一部分、日期不存在 (2/30) 或晚於今天一律 400。
    下拉選單只是介面限制，送來的值仍可能被竄改，所以這裡一律重新檢查。
    """
    parts = [str(form.get(f'birthday_{part}') or '').strip() for part in ('year', 'month', 'day')]
    if not any(parts):
        return None
    if not all(parts):
        raise AppError('請完整選擇生日的年、月、日')
    try:
        year, month, day = (int(p) for p in parts)
        birthday = date(year, month, day)
    except ValueError:
        raise AppError('生日日期不存在，請重新選擇')
    if birthday > date.today():
        raise AppError('生日不能是未來的日期')
    if birthday.year < date.today().year - 120:
        raise AppError('生日日期不存在，請重新選擇')
    return birthday


def parse_date_range(default_days=7, week=False, presets=False, max_days=None, strict=False, together=False):
    """
    request.args 的 start / end (YYYY-MM-DD) → (start, end)，end 早於 start 時對調。
    缺少或格式錯誤時的預設：最近 default_days 天 (含今天)；week=True 改為本週一起 7 天 (只給 start 時為 start 起 7 天)。
    together=True：任一個缺少或格式錯誤就整組套預設 (報表頁，避免只剩一端時查到超長區間)。
    presets=True 另外支援 ?preset=today|yesterday|month|last_month；strict=True 時格式錯誤直接 400 (不套預設)。
    max_days 為區間上限 (超過 400)。
    """
    today = date.today()
    preset = request.args.get('preset') or '' if presets else ''
    if preset == 'today':
        return today, today
    if preset == 'yesterday':
        return today - timedelta(days=1), today - timedelta(days=1)
    if preset == 'month':
        return today.replace(day=1), today
    if preset == 'last_month':
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    def parse(raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            if strict and raw:
                raise AppError('日期格式錯誤')
            return None
    start, end = parse(request.args.get('start') or ''), parse(request.args.get('end') or '')
    if together and (start is None or end is None):
        start = end = None
    if start is None:
        start = today - timedelta(days=today.weekday()) if week else today - timedelta(days=default_days - 1)
    if end is None:
        end = start + timedelta(days=6) if week else today
    if end < start:
        start, end = end, start
    if max_days and (end - start).days > max_days:
        raise AppError(f'查詢區間最長 {max_days} 天')
    return start, end


def day_bounds(start, end=None):
    """日期 (區間) 轉成半開的 datetime 區間 [start 00:00, end + 1 天 00:00)；end 省略時為同一天"""
    begin = datetime.combine(start, time.min)
    return begin, datetime.combine((end or start) + timedelta(days=1), time.min)


def parse_local_dt(raw, label=None):
    """<input type=datetime-local> 的 YYYY-MM-DDTHH:MM；label=None 時格式錯誤回 None，否則 400"""
    raw = str(raw or '').strip()
    try:
        return datetime.strptime(raw[:16], '%Y-%m-%dT%H:%M')
    except ValueError:
        if label:
            raise AppError(f'{label}格式不正確')
        return None


def parse_hm(text, default=None):
    """
    'HH:MM' → (時, 分)；格式錯誤時改解析 default，也沒有時回 None。
    結束時間可以是 24:00 (當天結束，例如班表 18:00~24:00)；要當 time 物件用時請自行處理 24。
    """
    for candidate in (text, default):
        try:
            h, m = str(candidate or '').split(':')
            h, m = int(h), int(m)
        except ValueError:
            continue
        if (0 <= h <= 23 and 0 <= m <= 59) or (h, m) == (24, 0):
            return h, m
    return None


def hm_minutes(text, default=None):
    """'HH:MM' → 自 0 點起的分鐘數 (24:00 = 1440)；格式錯誤時改用 default，也沒有時回 None"""
    hm = parse_hm(text, default)
    return None if hm is None else hm[0] * 60 + hm[1]


# ---------------------------------------------------------------- 回應
def json_etag_response(body):
    """已序列化的 JSON 字串加上 ETag；瀏覽器以 If-None-Match 輪詢且內容未變時回 304 (省流量)"""
    etag = '"' + hashlib.sha1(body.encode('utf-8')).hexdigest()[:20] + '"'
    if request.headers.get('If-None-Match') == etag:
        resp = current_app.response_class(status=304)
    else:
        resp = current_app.response_class(body, mimetype='application/json')
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


def paginate(query, per_page=50):
    """依 ?page= 分頁：回傳 (rows, page, pages, total)；page 會夾在 1..pages 之間"""
    total = query.count()
    pages = max(1, -(-total // per_page))
    page = min(max(1, safe_int(request.args.get('page'), 1)), pages)
    rows = query.offset((page - 1) * per_page).limit(per_page).all()
    return rows, page, pages, total


def sha256_hex(raw):
    """權杖 / 金鑰只存雜湊 (None 視為空字串)"""
    return hashlib.sha256(str(raw or '').encode('utf-8')).hexdigest()


def validate_pin(pin):
    return pin is not None and bool(PIN_REGEX.match(str(pin).strip()))


def is_weak_pin(pin, phone=''):
    """
    容易被猜中的 PIN：同一數字重複 (0000)、連續遞增 / 遞減 (1234、987654)、兩碼重複 (1212)、與手機末幾碼相同。
    攻擊者換 IP 對大量手機各試幾個常見 PIN 時，這些最先被猜中。
    """
    pin = str(pin or '').strip()
    if not pin.isdigit():
        return False
    digits = [int(c) for c in pin]
    steps = {(b - a) % 10 for a, b in zip(digits, digits[1:])}
    if len(set(pin)) == 1 or steps in ({1}, {9}):
        return True
    if len(pin) % 2 == 0 and pin == pin[:2] * (len(pin) // 2):
        return True
    phone = str(phone or '').strip()
    return bool(phone) and phone.endswith(pin)


WEAK_PIN_MESSAGE = '❌ PIN 碼太容易被猜中 (例如 0000、1234、1212 或手機末幾碼)，請換一組！'


def validate_phone(phone):
    return phone is not None and bool(PHONE_REGEX.match(str(phone).strip()))


# ---------------------------------------------------------------------------
# 隱私
# ---------------------------------------------------------------------------
def mask_name(name):
    """姓名遮罩 (王小明 → 王○明、王小 → 王○)，供公開叫號看板使用"""
    chars = list((name or '').strip())
    if len(chars) <= 1:
        return ''.join(chars)
    if len(chars) == 2:
        return chars[0] + '○'
    return chars[0] + '○' * (len(chars) - 2) + chars[-1]


def public_display_name(order):
    """公開看板顯示名稱：會員一律遮罩；訪客代號、櫃檯 POS、純英數桌號原樣顯示"""
    if order.user_id:
        return mask_name(order.user.name if order.user else (order.table_number or ''))
    table = (order.table_number or '').strip() or '訪客'
    if order.order_source == 'Table':  # 掃碼點餐的訪客：顯示桌號
        return table
    if table.startswith(('訪客', '櫃檯POS', 'POS')) and '(' not in table and '（' not in table:
        return table
    if re.fullmatch(r'[A-Za-z0-9_\-]{1,10}', table):
        return table
    # 例如 "A5 (王小明)"：保留括號外的桌號，其餘遮罩
    return mask_name(re.split(r'\s*[(（]', table)[0])


# ---------------------------------------------------------------------------
# 匯出檔防公式注入：會員 / 員工輸入的文字若以 = + - @ 開頭，Excel 開啟時會當成公式執行 (例如 =HYPERLINK(...))
# ---------------------------------------------------------------------------
FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def neutralize_formulas(wb):
    """openpyxl 活頁簿：所有被判定為公式的儲存格改為純文字 (本系統匯出的資料一律是文字或數字)"""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == 'f':
                    cell.data_type = 's'
    return wb


XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def workbook_bytes(wb):
    """openpyxl 活頁簿 → 已防公式注入的 BytesIO (指標在開頭)"""
    neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def send_xlsx(wb_or_buf, name):
    """以附件下載 .xlsx (可傳活頁簿或已產生的 BytesIO)"""
    buf = wb_or_buf if isinstance(wb_or_buf, io.BytesIO) else workbook_bytes(wb_or_buf)
    return send_file(buf, as_attachment=True, download_name=name, mimetype=XLSX_MIME)


def csv_safe(value):
    """CSV 欄位：以公式字元開頭的文字前面加 ' (Excel 會當成文字)"""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value
