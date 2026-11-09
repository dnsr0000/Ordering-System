"""菜單、套餐與紅利商品的後台維護邏輯 (新增與編輯共用同一套驗證)"""
import os
import re
import unicodedata
import uuid

from sqlalchemy import case, func, or_, update

from app.errors import AppError
from app.extensions import db
from app.models.menu import MODIFIER_GROUPS, ComboOption, ComboOptionItem, MenuItem
from app.services.audit import record_staff_audit
from app.services.cv_service import save_and_fix_image
from app.utils import safe_float, safe_int, safe_round

ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
LOW_STOCK = 5   # 庫存 ≤ 此值的徽章顯示黃色 (樣板的 stock_badge 與前端 Kiosk.stockClass 都讀這個值)
# 金額 / 點數 / 庫存上限：資料庫欄位是 32 位元整數，超出時 PostgreSQL 會 500、分店存入後上傳線上端會卡住同步
MAX_AMOUNT = 1_000_000
MAX_STOCK = 100_000   # 與 API (api_v1) 的庫存上限一致
# 文字欄位長度上限 (與 models/menu.py 的欄位長度一致；OrderItem.item_name 也是 100)
NAME_MAX, CATEGORY_MAX, DESCRIPTION_MAX = 100, 50, 200


def _check_len(value, limit, label):
    """文字超過資料庫欄位長度時指出是哪個欄位 (PostgreSQL 會 500、分店存入後同步會卡住)"""
    if len(value or '') > limit:
        raise AppError(f'❌ {label}最多 {limit} 個字 (目前 {len(value)} 字)，請縮短後再儲存！')
    return value


def _check_range(value, limit, label):
    if not 0 <= value <= limit:
        raise AppError(f'❌ {label}必須介於 0 ~ {limit:,}！(目前為 {value:,})')
    return value


# ---------------------------------------------------------------------------
# 菜單品項
# ---------------------------------------------------------------------------
def save_menu_image(upload_dir, tenant_id, image):
    """儲存菜單圖片 (重新編碼為 JPEG、隨機檔名)，回傳資料庫相對路徑 "<店家>/<檔名>" """
    _, ext = os.path.splitext((image.filename or '').lower())
    if ext not in ALLOWED_IMAGE_EXTS:
        raise AppError('❌ 僅支援 JPG、PNG、WEBP 圖片格式！')
    store_dir = os.path.join(upload_dir, str(tenant_id))
    os.makedirs(store_dir, exist_ok=True)
    filename = f"menu_{uuid.uuid4().hex[:16]}.jpg"
    if not save_and_fix_image(image, os.path.join(store_dir, filename)):
        raise AppError('❌ 上傳的圖片已損壞或格式不支援，請重新選擇清晰的 JPG/PNG/WEBP 圖片！')
    return f"{tenant_id}/{filename}"


def remove_file_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def apply_menu_form(item, form, flag):
    """
    把後台表單寫入 MenuItem (新增與編輯共用)。flag(name) 判斷 checkbox。
    編輯時表單若帶有「開啟視窗當下的庫存」(loaded_stock)，只套用差額，
    不會用過期的表單值蓋掉編輯期間被買走的數量。
    """
    name = (form.get('name') or '').strip()
    price = safe_float(form.get('price'), None)
    if not name or price is None:
        raise AppError('❌ 請輸入餐點名稱，且單價必須是數字！')
    category = form.get('category', '主餐')
    description = form.get('description', '')
    _check_len(name, NAME_MAX, '餐點名稱')
    _check_len(category, CATEGORY_MAX, '分類')
    _check_len(description, DESCRIPTION_MAX, '餐點說明')
    price = _check_range(max(0, round(price)), MAX_AMOUNT, '單價')
    can_be_add_on = flag('can_be_add_on')
    add_on_price = _check_range(max(0, safe_round(form.get('add_on_price'))), MAX_AMOUNT, '加購專屬價')
    discount_price = _check_range(max(0, safe_round(form.get('discount_price'))), MAX_AMOUNT, '特價金額')
    if can_be_add_on and add_on_price <= 0:
        raise AppError('❌ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！')
    if can_be_add_on and add_on_price >= price:
        raise AppError(f'❌ 加購專屬價 (${add_on_price}) 不得高於或等於原單價 (${price})！')

    submitted_stock = max(0, safe_int(form.get('stock'), 0))
    loaded_stock = form.get('loaded_stock')
    if item.id is not None and loaded_stock not in (None, ''):
        delta = submitted_stock - max(0, safe_int(loaded_stock, 0))
        if delta:
            _check_range(submitted_stock, MAX_STOCK, '剩餘數量')
            new_stock = MenuItem.stock + delta
            db.session.execute(update(MenuItem).where(MenuItem.id == item.id)
                               .values(stock=case((new_stock < 0, 0), else_=new_stock))
                               .execution_options(synchronize_session=False))
            db.session.refresh(item, ['stock'])
    else:
        item.stock = _check_range(submitted_stock, MAX_STOCK, '剩餘數量')

    submitted_total = max(0, safe_int(form.get('total_stock'), item.stock or 0))
    loaded_total = form.get('loaded_total_stock')
    if item.id is not None and loaded_total not in (None, '') and submitted_total == max(0, safe_int(loaded_total, 0)):
        submitted_total = item.total_stock or 0  # 總量沒被改動：保留目前資料庫的值
    else:
        _check_range(submitted_total, MAX_STOCK, '總量')

    # 編輯「售完」：表單帶有開啟視窗當下的勾選狀態 (loaded_sold_out) 時，使用者沒改勾選就保留資料庫目前的值，
    # 不會把編輯期間已自動恢復供應 (訂單取消回補庫存) 的品項又用舊勾選標回售完
    sold_out = flag('is_sold_out')
    loaded_sold_out = form.get('loaded_sold_out')
    if item.id is not None and loaded_sold_out not in (None, '') \
            and sold_out == (loaded_sold_out not in ('0', 'false', 'False')):
        sold_out = bool(item.is_sold_out)

    if item.id is not None:  # 改名 / 改分類：以舊名稱為條件的加購品一併改指向新名稱
        _retarget_addon_rules(item, 'item', item.name, name, MenuItem.name)
        _retarget_addon_rules(item, 'category', item.category, category, MenuItem.category)

    modifiers = form.get('modifiers', 'none')
    item.name = name
    item.category = category
    item.modifiers = modifiers if modifiers in MODIFIER_GROUPS else 'none'
    item.price = price
    item.total_stock = max(item.stock or 0, submitted_total)
    item.description = description
    item.is_discount = flag('is_discount')
    item.discount_price = discount_price
    item.is_new = flag('is_new')
    item.is_manual_popular = flag('is_recommended')
    item.is_recommended = False   # 舊欄位：熱門一律以 is_manual_popular 為準，否則取消勾選後仍是熱門
    item.is_sold_out = sold_out or item.stock == 0
    item.can_be_add_on = can_be_add_on
    item.add_on_price = add_on_price
    item.addon_trigger_type = form.get('addon_trigger_type', 'any')
    item.addon_trigger_target = (form.get('addon_trigger_target') or '').strip()
    return item


def _retarget_addon_rules(item, trigger, old, new, column):
    """
    品項改名 / 改分類時，把加購條件「需購買特定餐點 / 分類」= 舊名稱的加購品改指向新名稱
    (條件存的是名稱字串，前後端都以名稱比對，不改的話加購品會全面失效)。
    舊名稱仍有其他品項在用時不動：條件對那些品項仍然有效。
    """
    old = (old or '').strip()
    new = (new or '').strip()
    if not old or not new or old.lower() == new.lower():
        return
    if MenuItem.query.filter(MenuItem.id != item.id, func.trim(column) == old).first() is not None:
        return
    rules = MenuItem.query.filter(MenuItem.addon_trigger_type == trigger, MenuItem.id != item.id).all()
    for rule in rules:
        if (rule.addon_trigger_target or '').strip().lower() == old.lower():
            rule.addon_trigger_target = new


def set_item_reward_status(item, is_reward, reward_points=0, reward_discount_points=0):
    """紅利商城上下架：兌換點數必須 > 0 且不高於原價、特惠點數必須小於兌換點數；有實質變動才寫稽核"""
    pts = max(0, safe_int(reward_points, 0))
    disc = max(0, safe_int(reward_discount_points, 0))
    if is_reward:
        if pts <= 0:
            raise AppError('啟用紅利兌換時，「兌換所需點數」必須大於 0！')
        # $0 的紅利專用品 (例如非賣品贈品) 不受「不高於原價」限制，與後台前端驗證、Excel 匯入同一套規則
        if (item.price or 0) > 0 and pts > item.price:
            raise AppError(f'兌換所需點數 ({pts} 點) 不得高於原單價 (${item.price})！')
        if pts > MAX_AMOUNT:
            raise AppError(f'兌換所需點數不得超過 {MAX_AMOUNT:,} 點！')
        if disc and disc >= pts:
            raise AppError('「限時特惠點數」必須小於「兌換所需點數」！')
    else:
        pts = disc = 0
    changed = (bool(item.is_reward), item.reward_points or 0, item.reward_discount_points or 0) != (bool(is_reward), pts, disc)
    item.is_reward, item.reward_points, item.reward_discount_points = bool(is_reward), pts, disc
    if changed:
        detail = (f"上架/更新紅利商品：{item.name} (兌換點數:{pts}, 特惠點數:{disc})" if is_reward
                  else f"下架紅利商品：{item.name}")
        record_staff_audit('TOGGLE_REWARD_ITEM', details=detail)


# ---------------------------------------------------------------------------
# 套餐
# ---------------------------------------------------------------------------
def save_combo(combo, form, flag):
    """
    新增/編輯套餐。配餐以 side_ids (或舊表單的 item1_id~item3_id) 傳入，
    customizable 依 item{n}_customizable，配餐本身沒有客製化群組時一律存為 False。
    """
    main_id = safe_int(form.get('main_item_id'), 0)
    name = str(form.get('name') or '').strip()
    side_ids = [safe_int(form.get(f'item{n}_id'), 0) for n in (1, 2, 3)]
    custom_flags = [flag(f'item{n}_customizable') for n in (1, 2, 3)]
    sides = [(sid, cust) for sid, cust in zip(side_ids, custom_flags) if sid]

    if not name:
        raise AppError('❌ 請輸入套餐名稱！')
    _check_len(name, NAME_MAX, '套餐名稱')
    description = form.get('description', '')
    _check_len(description, DESCRIPTION_MAX, '套餐說明')
    additional_price = _check_range(max(0, safe_int(form.get('additional_price'), 0)), MAX_AMOUNT, '套餐加價')
    if not main_id:
        raise AppError('❌ 請選擇主餐！')
    if len({sid for sid, _ in sides}) != len(sides):
        raise AppError('❌ 配餐不可重複選擇相同的餐點！')
    ids = {main_id, *(sid for sid, _ in sides)}
    items = {m.id: m for m in MenuItem.query.filter(MenuItem.id.in_(ids)).all()}
    if len(items) != len(ids):
        raise AppError('❌ 套餐包含不存在或不屬於本店的餐點，請重新選擇！')

    others = ComboOption.query.filter(ComboOption.main_item_id == main_id, ComboOption.id != combo.id).all()
    for other in others:
        if other.name == name:
            raise AppError(f'❌ 該主餐已存在名為【{name}】的套餐，請使用不同名稱！')
        if not other.choice_groups and not str(form.get('choices') or '').strip() \
                and set(other.side_item_ids) == {sid for sid, _ in sides}:
            raise AppError(f'❌ 該主餐已存在包含相同配餐內容的套餐【{other.name}】，不可重複建立相同組合！')

    current = list(combo.sides)
    choices = parse_combo_choices(form.get('choices'), current)
    combo.main_item_id = main_id
    combo.name = name
    combo.additional_price = additional_price
    combo.description = description
    combo.can_addon = flag('can_addon')
    specs = [(sid, '', 0, bool(cust and items[sid].has_modifiers)) for sid, cust in sides]
    specs += [(item.id, group, extra, bool(item.has_modifiers)) for group, item, extra in choices]
    # 沿用既有的配餐列 (以 餐點 + 任選群組 對應)，uuid 不變；換掉的列明確刪除，
    # 讓同步寫入刪除紀錄 (delete-orphan 的孤兒列不會出現在 before_flush 的 session.deleted，其他主機會一直累加)
    reusable, stale = {}, []
    for s in current:   # 先前同步累加出來的重複列也一併清掉
        key = (s.menu_item_id, s.choice_group or '')
        if key in reusable:
            stale.append(s)
        else:
            reusable[key] = s
    new_sides = []
    for pos, (mid, group, extra, cust) in enumerate(specs):
        side = reusable.pop((mid, group), None)
        if side is None:
            side = ComboOptionItem(menu_item_id=mid, choice_group=group)
        side.position, side.extra_price, side.customizable = pos, extra, cust
        new_sides.append(side)
    combo.sides = new_sides
    for side in stale + list(reusable.values()):
        db.session.delete(side)
    return combo


_CHOICE_SEPARATORS = ',，、'
_CHOICE_EXTRA_RE = re.compile(r'(.+?)\s*[+＋]\s*(\d+)')


def _choice_readings(raw):
    """選項文字可能的解讀 [(品名, 加價)]：先試「品名+加價」(加號、數字全形半形皆可)，再試整段就是品名"""
    out = []
    m = _CHOICE_EXTRA_RE.fullmatch(raw)
    if m:
        digits = unicodedata.normalize('NFKC', m.group(2))
        out.append((m.group(1).strip(), int(digits) if len(digits) <= 9 else MAX_AMOUNT + 1))
    out.append((raw, 0))
    return out


def _resolve_choice(raw, group, bound, seen):
    """
    以名稱找出任選選項的餐點，回傳 (品名, MenuItem, 加價)。
    編輯既有套餐時優先沿用這個群組目前綁定的品項 (bound)；同名品項有多個又無從判斷時要求改名，
    不隨機綁到其中一個 (PostgreSQL 沒有排序時每次儲存都可能換一個)。
    """
    readings = []
    for name, extra in _choice_readings(raw):
        found = MenuItem.query.filter_by(name=name).order_by(MenuItem.id).all()
        if found:
            readings.append((name, extra, found))
    if not readings:
        raise AppError(f'❌ 任選群組「{group}」的餐點「{_choice_readings(raw)[0][0]}」不在菜單中')
    for name, extra, found in readings:
        for item in found:
            if (group, item.id) in bound and item.id not in seen:
                return name, item, extra
    name, extra, found = readings[0]
    if len(found) > 1:
        raise AppError(f'❌ 任選群組「{group}」的餐點「{name}」在菜單中有 {len(found)} 個同名品項，'
                       f'無法判斷要用哪一個，請先將品項改成不同名稱')
    return name, found[0], extra


def _split_choice_options(rest, names_with_sep):
    """
    以「,，、」切出選項；品名本身含分隔字元 (例如「薯條、雞塊拼盤」) 時，相鄰片段合起來剛好是這類品名就不拆開
    (後台編輯視窗會把 choices_text 原樣放回，品名含分隔字元的套餐才能再次儲存)。
    """
    parts = re.split(f'([{_CHOICE_SEPARATORS}])', rest)
    tokens, seps = parts[0::2], parts[1::2]
    options, i = [], 0
    while i < len(tokens):
        end = i
        if names_with_sep:
            for j in range(len(tokens) - 1, i, -1):
                raw = (tokens[i] + ''.join(seps[k] + tokens[k + 1] for k in range(i, j))).strip()
                if any(name in names_with_sep for name, _ in _choice_readings(raw)):
                    end = j
                    break
        raw = (tokens[i] + ''.join(seps[k] + tokens[k + 1] for k in range(i, end))).strip()
        if raw:
            options.append(raw)
        i = end + 1
    return options


def parse_combo_choices(text, current=()):
    """
    任選群組文字：每行「群組: 餐點, 餐點+加價」(冒號 / 逗號 / 加號全形半形皆可)。回傳 [(群組, MenuItem, 加價)]。
    每組至少兩個選項；餐點以名稱對應本店菜單。current 為套餐目前的配餐列 (編輯時用來沿用同名品項的綁定)。
    """
    out = []
    groups = set()
    bound = {(s.choice_group, s.menu_item_id) for s in current if s.choice_group}
    names_with_sep = None
    for raw in str(text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        group, sep, rest = line.replace('：', ':').partition(':')
        group = group.strip()[:30]
        if not sep or not group:
            raise AppError(f'❌ 任選群組格式錯誤：「{line}」，請寫成「群組: 餐點, 餐點+加價」')
        if group in groups:
            raise AppError(f'❌ 任選群組「{group}」重複')
        groups.add(group)
        if names_with_sep is None:
            names_with_sep = {n for (n,) in MenuItem.query.with_entities(MenuItem.name).filter(
                or_(*(MenuItem.name.contains(c) for c in _CHOICE_SEPARATORS))).all()}
        options = _split_choice_options(rest, names_with_sep)
        if len(options) < 2:
            raise AppError(f'❌ 任選群組「{group}」至少要有兩個選項')
        seen = set()
        for opt in options:
            name, item, extra = _resolve_choice(opt, group, bound, seen)
            extra = _check_range(extra, MAX_AMOUNT, f'任選群組「{group}」的餐點「{name}」加價')
            if item.id in seen:
                raise AppError(f'❌ 任選群組「{group}」的餐點「{name}」重複')
            seen.add(item.id)
            out.append((group, item, extra))
    return out


# ---------------------------------------------------------------------------
# 菜單匯入 (CSV / Excel)
# ---------------------------------------------------------------------------
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
    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '特惠點數': 'reward_discount_points',
    'reward_discount_points': 'reward_discount_points',
}


def is_blank(val):
    """匯入檔的空值：openpyxl 空儲存格是 None、CSV 空欄位是 ''，兩者一律視為「沒填」"""
    return val is None or val == ''


def parse_bool(val):
    return not is_blank(val) and str(val).strip().lower() in ('1', 'true', '是', 'yes', 'y')


def _cell(row, key, default=None):
    val = row.get(key, default)
    return default if is_blank(val) else val


_NUMBER_NOISE_RE = re.compile(r'(?i)ntd?|[$元,\s]')


def _import_number(row, key, label, item_name, limit, to_int=round):
    """
    匯入檔的金額 / 點數 / 庫存：接受「120元」「NT$120」「$95」「1,280」(其他 POS 匯出、Excel 另存 CSV 的顯示文字)。
    無法辨識、負數或超出範圍時報錯讓整批 rollback，不默默寫成 0 (否則餐點會以 $0 販售)。空白回傳 0。
    """
    val = _cell(row, key)
    if val is None:
        return 0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        num = safe_float(val, None)
    else:
        num = safe_float(_NUMBER_NOISE_RE.sub('', unicodedata.normalize('NFKC', str(val))), None)
    if num is None or num < 0:
        raise AppError(f'❌ 餐點「{item_name}」的{label}「{val}」無法辨識，請填寫 0 以上的數字')
    num = to_int(num)
    if num > limit:
        raise AppError(f'❌ 餐點「{item_name}」的{label}必須介於 0 ~ {limit:,}！(目前為 {num:,})')
    return num


def _is_placeholder_sheet(rows, columns):
    """匯出時菜單為空所寫出的佔位工作表 (只有「資料」欄、內容「目前無資料」)"""
    return set(columns) == {'資料'} and all(
        str(r.get('資料') or '').strip() in ('', '目前無資料') for r in rows)


def upsert_menu_items_from_rows(rows, columns=None):
    """
    以餐點名稱為鍵新增或更新菜單 (由呼叫端 commit，多工作表匯入時整批同一交易)。
    rows 是 list[dict] (檔案標題 → 儲存格值)；columns 是檔案的標題清單 (沒有資料列時仍可檢查必要欄位)。
    - 既有餐點只更新檔案裡「有附上的欄位」：只匯入「名稱、分類、單價」改價時，客製化、說明、特價、紅利等設定都保留。
      單價、分類、客製化、庫存的儲存格空白時也保留原值 (空白的單價不會變成 $0)。
    - 新餐點的單價不可空白；沒附上的欄位用預設值。
    - 檔案沒有庫存欄位時不覆蓋既有庫存；總量永遠不小於庫存。
    - 數字欄位接受「120元」「NT$120」「1,280」；無法辨識、負數或超出上限時報錯 (不會默默寫成 $0)。
    - 紅利防呆：兌換點數為 0 或高於單價時關閉紅利；特惠點數不小於兌換點數時歸 0。
    - 匯出時菜單為空所寫出的佔位表 (只有「資料」欄) 直接略過。
    回傳處理筆數；缺少必要欄位、新餐點單價空白、數字無法辨識或文字超過欄位長度時拋出 AppError。
    """
    if columns is None:
        columns = set().union(*rows) if rows else set()
    if _is_placeholder_sheet(rows, columns):   # 匯出時菜單為空的佔位表：略過，不讓整份活頁簿匯入失敗
        return 0
    columns = {MENU_COLUMN_MAPPING.get(c, c) for c in columns}
    if 'name' not in columns or 'price' not in columns:
        raise AppError('檔案缺少必要欄位：「餐點名稱」或「單價」！')
    count = 0
    for row in rows:
        row = {MENU_COLUMN_MAPPING.get(key, key): val for key, val in row.items()}
        name = str(_cell(row, 'name', '')).strip()
        if not name or name == '目前無資料':
            continue
        _check_len(name, NAME_MAX, '餐點名稱')

        def filled(key):
            return key in columns and not is_blank(row.get(key))

        item = MenuItem.query.filter_by(name=name).first()
        is_new = item is None
        if is_new and not filled('price'):
            raise AppError(f'新餐點「{name}」的單價空白，請填寫後再匯入')
        stock = _import_number(row, 'stock', '剩餘數量', name, MAX_STOCK, int) if filled('stock') else None
        total = _import_number(row, 'total_stock', '總量', name, MAX_STOCK, int) if filled('total_stock') else None
        price = _import_number(row, 'price', '單價', name, MAX_AMOUNT) if filled('price') else None
        category = str(_cell(row, 'category')).strip() if filled('category') else None
        description = str(_cell(row, 'description', '')).strip() if 'description' in columns else None
        _check_len(category, CATEGORY_MAX, f'餐點「{name}」的分類')
        _check_len(description, DESCRIPTION_MAX, f'餐點「{name}」的商品描述')
        if is_new:
            item = MenuItem(name=name, image_path='', stock=50 if stock is None else stock, total_stock=50,
                            can_be_add_on=False, add_on_price=0, addon_trigger_type='any', category='主餐', price=0,
                            discount_price=0, modifiers='none', description='', is_manual_popular=False, is_new=False,
                            is_discount=False, is_reward=False, reward_points=0, reward_discount_points=0)
            db.session.add(item)
        elif stock is not None:
            item.stock = stock
        if stock is not None:
            item.is_sold_out = stock <= 0
        if total is not None:
            item.total_stock = total
        item.total_stock = max(item.total_stock or 0, item.stock or 0)
        if price is not None:
            item.price = price
        if category is not None:
            if not is_new:   # 改分類：以舊分類為條件的加購品一併改指向新分類
                _retarget_addon_rules(item, 'category', item.category, category, MenuItem.category)
            item.category = category
        if filled('modifiers'):
            modifiers = str(_cell(row, 'modifiers')).strip()
            item.modifiers = modifiers if modifiers in MODIFIER_GROUPS else 'none'
        # 有附上的欄位才更新 (儲存格空白 = 清空說明 / 特價金額 0 / 勾選取消)
        if description is not None:
            item.description = description
        if 'discount_price' in columns:
            item.discount_price = _import_number(row, 'discount_price', '特價金額', name, MAX_AMOUNT)
        for key, attr in (('is_recommended', 'is_manual_popular'), ('is_new', 'is_new'), ('is_discount', 'is_discount')):
            if key in columns:
                setattr(item, attr, parse_bool(_cell(row, key)))
        if 'is_recommended' in columns:
            item.is_recommended = False   # 舊欄位：熱門一律以 is_manual_popular 為準
        if columns & {'is_reward', 'reward_points', 'reward_discount_points'}:
            is_reward = parse_bool(_cell(row, 'is_reward')) if 'is_reward' in columns else bool(item.is_reward)
            points = _import_number(row, 'reward_points', '兌換所需點數', name, MAX_AMOUNT, int) \
                if 'reward_points' in columns else (item.reward_points or 0)
            disc_points = _import_number(row, 'reward_discount_points', '限時特惠點數', name, MAX_AMOUNT, int) \
                if 'reward_discount_points' in columns else (item.reward_discount_points or 0)
            price = item.price or 0
            if is_reward and (points <= 0 or (price > 0 and points > price)):
                is_reward, points, disc_points = False, 0, 0
            elif disc_points >= points:
                disc_points = 0
            item.is_reward, item.reward_points, item.reward_discount_points = is_reward, points, disc_points
        count += 1
    return count


# ---------------------------------------------------------------------------
# 前台 (Kiosk / POS) 頁面資料
# ---------------------------------------------------------------------------
def storefront_payload(items, modifiers, reward_setting):
    """點餐頁面一次取得的菜單資料 (以 JSON 嵌入頁面，前端不再從 HTML 屬性拼湊)"""
    return {
        'items': [i.to_dict() for i in items],
        'modifiers': [m.to_dict() for m in modifiers],
        'reward': {
            'isEnabled': bool(reward_setting.is_enabled),
            'pointsPerDollar': reward_setting.points_per_dollar or 1,
            'maxDiscount': reward_setting.max_discount_per_order or 0,
            'spendPerPoint': reward_setting.spend_per_point or 100,
        },
    }


def menu_categories(items):
    return sorted({(i.category or '未分類').strip() for i in items})


def item_form_data(item):
    """後台「編輯品項」視窗的預設值 (以 JSON 嵌入菜單頁，取代按鈕上的大量 data-* 屬性)"""
    return {
        'id': item.id, 'name': item.name, 'category': item.category or '', 'modifiers': item.modifiers or 'none',
        'price': item.price or 0, 'stock': item.stock or 0, 'total_stock': item.total_stock or 0,
        'is_sold_out': bool(item.is_sold_out), 'is_discount': bool(item.is_discount),
        'discount_price': item.discount_price or 0, 'description': item.description or '',
        'image_path': item.image_path or '', 'is_recommended': bool(item.is_manual_popular or item.is_recommended),
        'is_new': bool(item.is_new), 'is_reward': bool(item.is_reward), 'reward_points': item.reward_points or 0,
        'reward_discount_points': item.reward_discount_points or 0, 'can_be_add_on': bool(item.can_be_add_on),
        'add_on_price': item.add_on_price or 0, 'addon_trigger_type': item.addon_trigger_type or 'any',
        'addon_trigger_target': item.addon_trigger_target or '',
    }
