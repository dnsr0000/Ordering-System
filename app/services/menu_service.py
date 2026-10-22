"""菜單、套餐與紅利商品的後台維護邏輯 (新增與編輯共用同一套驗證)"""
import os
import uuid

import pandas as pd
from sqlalchemy import case, update

from app.errors import AppError
from app.extensions import db
from app.models.menu import MODIFIER_GROUPS, ComboOption, ComboOptionItem, MenuItem
from app.services.audit import record_staff_audit
from app.services.cv_service import save_and_fix_image
from app.utils import safe_float, safe_int, safe_round

ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}


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
    price = max(0, round(price))
    can_be_add_on = flag('can_be_add_on')
    add_on_price = max(0, safe_round(form.get('add_on_price')))
    if can_be_add_on and add_on_price <= 0:
        raise AppError('❌ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！')
    if can_be_add_on and add_on_price >= price:
        raise AppError(f'❌ 加購專屬價 (${add_on_price}) 不得高於或等於原單價 (${price})！')

    submitted_stock = max(0, safe_int(form.get('stock'), 0))
    loaded_stock = form.get('loaded_stock')
    if item.id is not None and loaded_stock not in (None, ''):
        delta = submitted_stock - max(0, safe_int(loaded_stock, 0))
        if delta:
            new_stock = MenuItem.stock + delta
            db.session.execute(update(MenuItem).where(MenuItem.id == item.id)
                               .values(stock=case((new_stock < 0, 0), else_=new_stock))
                               .execution_options(synchronize_session=False))
            db.session.refresh(item, ['stock'])
    else:
        item.stock = submitted_stock

    submitted_total = max(0, safe_int(form.get('total_stock'), item.stock or 0))
    loaded_total = form.get('loaded_total_stock')
    if item.id is not None and loaded_total not in (None, '') and submitted_total == max(0, safe_int(loaded_total, 0)):
        submitted_total = item.total_stock or 0  # 總量沒被改動：保留目前資料庫的值

    modifiers = form.get('modifiers', 'none')
    item.name = name
    item.category = form.get('category', '主餐')
    item.modifiers = modifiers if modifiers in MODIFIER_GROUPS else 'none'
    item.price = price
    item.total_stock = max(item.stock or 0, submitted_total)
    item.description = form.get('description', '')
    item.is_discount = flag('is_discount')
    item.discount_price = max(0, safe_round(form.get('discount_price')))
    item.is_new = flag('is_new')
    item.is_manual_popular = flag('is_recommended')
    item.is_sold_out = flag('is_sold_out') or item.stock == 0
    item.can_be_add_on = can_be_add_on
    item.add_on_price = add_on_price
    item.addon_trigger_type = form.get('addon_trigger_type', 'any')
    item.addon_trigger_target = (form.get('addon_trigger_target') or '').strip()
    return item


def set_item_reward_status(item, is_reward, reward_points=0, reward_discount_points=0):
    """紅利商城上下架：兌換點數必須 > 0 且不高於原價、特惠點數必須小於兌換點數；有實質變動才寫稽核"""
    pts = max(0, safe_int(reward_points, 0))
    disc = max(0, safe_int(reward_discount_points, 0))
    if is_reward:
        if pts <= 0:
            raise AppError('啟用紅利兌換時，「兌換所需點數」必須大於 0！')
        if pts > item.price:
            raise AppError(f'兌換所需點數 ({pts} 點) 不得高於原單價 (${item.price})！')
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
        if set(other.side_item_ids) == {sid for sid, _ in sides}:
            raise AppError(f'❌ 該主餐已存在包含相同配餐內容的套餐【{other.name}】，不可重複建立相同組合！')

    combo.main_item_id = main_id
    combo.name = name
    combo.additional_price = max(0, safe_int(form.get('additional_price'), 0))
    combo.description = form.get('description', '')
    combo.can_addon = flag('can_addon')
    combo.sides = [ComboOptionItem(menu_item_id=sid, position=pos, customizable=bool(cust and items[sid].has_modifiers))
                   for pos, (sid, cust) in enumerate(sides)]
    return combo


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


def parse_bool(val):
    return not pd.isna(val) and str(val).strip().lower() in ('1', 'true', '是', 'yes', 'y')


def _cell(row, key, default=None):
    val = row.get(key, default)
    return default if val is None or (not isinstance(val, str) and pd.isna(val)) else val


def upsert_menu_items_from_df(df):
    """
    以餐點名稱為鍵新增或更新菜單 (由呼叫端 commit，多工作表匯入時整批同一交易)。
    - 檔案沒有庫存欄位時不覆蓋既有庫存；總量永遠不小於庫存。
    - 紅利防呆：兌換點數為 0 或高於單價時關閉紅利；特惠點數不小於兌換點數時歸 0。
    回傳處理筆數；缺少必要欄位時拋出 AppError。
    """
    df = df.rename(columns=MENU_COLUMN_MAPPING)
    if 'name' not in df.columns or 'price' not in df.columns:
        raise AppError('檔案缺少必要欄位：「餐點名稱」或「單價」！')
    count = 0
    for _, row in df.iterrows():
        name = str(_cell(row, 'name', '')).strip()
        if not name or name == '目前無資料':
            continue
        price = max(0, safe_round(_cell(row, 'price'), 0))
        points = max(0, safe_int(_cell(row, 'reward_points'), 0))
        disc_points = max(0, safe_int(_cell(row, 'reward_discount_points'), 0))
        is_reward = parse_bool(_cell(row, 'is_reward'))
        if is_reward and (points <= 0 or (price > 0 and points > price)):
            is_reward, points, disc_points = False, 0, 0
        elif disc_points >= points:
            disc_points = 0
        stock = _cell(row, 'stock')
        total = _cell(row, 'total_stock')
        stock = None if stock is None else max(0, safe_int(stock, 50))
        total = None if total is None else max(0, safe_int(total, 0))
        modifiers = str(_cell(row, 'modifiers', 'none')).strip()

        item = MenuItem.query.filter_by(name=name).first()
        if item is None:
            item = MenuItem(name=name, image_path='', stock=50 if stock is None else stock,
                            total_stock=50, can_be_add_on=False, add_on_price=0, addon_trigger_type='any')
            db.session.add(item)
        elif stock is not None:
            item.stock = stock
        if stock is not None:
            item.is_sold_out = stock <= 0
        if total is not None:
            item.total_stock = total
        item.total_stock = max(item.total_stock or 0, item.stock or 0)
        item.category = str(_cell(row, 'category', '主餐')).strip()
        item.price = price
        item.discount_price = max(0, safe_round(_cell(row, 'discount_price'), 0))
        item.modifiers = modifiers if modifiers in MODIFIER_GROUPS else 'none'
        item.description = str(_cell(row, 'description', '')).strip()
        item.is_manual_popular = parse_bool(_cell(row, 'is_recommended'))
        item.is_new = parse_bool(_cell(row, 'is_new'))
        item.is_discount = parse_bool(_cell(row, 'is_discount'))
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
