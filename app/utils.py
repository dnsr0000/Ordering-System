import re
from functools import wraps

from flask import request
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


def csv_safe(value):
    """CSV 欄位：以公式字元開頭的文字前面加 ' (Excel 會當成文字)"""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value
