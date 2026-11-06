"""後台：菜單與套餐頁 (/admin/menu)：品項與套餐清單 (新增 / 編輯 / 刪除的 POST 在 admin.py)，
以及每個品項的進階設定 (供應時段 / 排程、通路價、過敏原與素食、多語系、外送代碼、成本) 與分類翻譯"""

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.errors import AppError
from app.extensions import db
from app.models.menu import ALLERGENS, DIETS, WEEKDAY_LABELS, CategoryTranslation, MenuItem, parse_size_options
from app.services import ai_service, menu_service, settings_service
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.settings_service import TIME_RE
from app.services.approval_service import permission_required
from app.utils import get_own, parse_date, safe_int

menu_pro_bp = Blueprint('menu_pro', __name__)


def apply_pro_form(item, form):
    start, end = (form.get('avail_start') or '').strip(), (form.get('avail_end') or '').strip()
    for label, value in (('供應開始時間', start), ('供應結束時間', end)):
        if value and not TIME_RE.match(value):
            raise AppError(f'{label}格式應為 HH:MM')
    days = ''.join(sorted({d for d in form.getlist('avail_days') if d in '1234567'}))
    if not days:
        raise AppError('請至少勾選一個供應星期')
    sale_from, sale_until = parse_date(form.get('sale_from')), parse_date(form.get('sale_until'))
    if sale_from and sale_until and sale_until < sale_from:
        raise AppError('販售結束日不可早於開始日')
    item.avail_start, item.avail_end, item.avail_days = start, end, days
    item.sale_from, item.sale_until = sale_from, sale_until
    for ch in ('takeout_price', 'online_price', 'delivery_price', 'member_price'):
        setattr(item, ch, max(0, safe_int(form.get(ch), 0)))
    item.allergens = ','.join(a for a in form.getlist('allergens') if a in ALLERGENS)
    item.diet = form.get('diet') if form.get('diet') in DIETS else ''
    item.spicy_level = min(3, max(0, safe_int(form.get('spicy_level'), 0)))
    for f, size in (('name_en', 100), ('name_ja', 100), ('description_en', 300), ('description_ja', 300),
                    ('external_code', 50)):
        setattr(item, f, (form.get(f) or '').strip()[:size])
    item.unit_cost = max(0, safe_int(form.get('unit_cost'), 0))
    raw_sizes = (form.get('size_options') or '').strip()
    sizes = parse_size_options(raw_sizes)
    if raw_sizes and not sizes:
        raise AppError('規格格式應為「名稱:加減價」，例如 中杯:0, 大杯:15')
    item.size_options = ', '.join(f'{n}:{d:+d}' if d else f'{n}:0' for n, d in sizes)
    item.calories = min(5000, max(0, safe_int(form.get('calories'), 0)))
    item.carbon_auto = form.get('carbon_auto') == '1'
    if item.carbon_auto:
        from app.services import carbon_service
        item.carbon_grams = carbon_service.recipe_carbon(item) if item.id else 0
    else:
        item.carbon_grams = min(1_000_000, max(0, safe_int(form.get('carbon_grams'), 0)))
    item.nutrition = (form.get('nutrition') or '').strip()[:200]
    item.prep_minutes = min(120, max(0, safe_int(form.get('prep_minutes'), 0)))
    item.prep_note = (form.get('prep_note') or '').strip()[:500]
    return item


def translate_menu(overwrite=False):
    """AI 一鍵翻譯：品項的 name / description 與分類的英 / 日文。預設只填空白欄位 (不覆蓋手填)；回 (填入欄位數, 來源)"""
    items = MenuItem.query.order_by(MenuItem.id).all()
    categories = sorted({(i.category or '未分類').strip() for i in items})
    translations = {t.category: t for t in CategoryTranslation.query.all()}
    entries = [{'name': i.name, 'description': i.description or ''} for i in items] + [{'name': c, 'description': ''} for c in categories]
    results, source = ai_service.translate_entries(entries)
    filled = 0

    def put(obj, field, value, limit):
        nonlocal filled
        if value and (overwrite or not getattr(obj, field)):
            setattr(obj, field, value[:limit])
            filled += 1
    for item, got in zip(items, results[:len(items)]):
        put(item, 'name_en', got['name_en'], 100)
        put(item, 'name_ja', got['name_ja'], 100)
        if item.description:
            put(item, 'description_en', got['description_en'], 300)
            put(item, 'description_ja', got['description_ja'], 300)
    for cat, got in zip(categories, results[len(items):]):
        row = translations.get(cat)
        if row is None and (got['name_en'] or got['name_ja']):
            row = CategoryTranslation(tenant_id=g.tenant_id, category=cat)
            db.session.add(row)
            translations[cat] = row
        if row is not None:
            put(row, 'name_en', got['name_en'], 60)
            put(row, 'name_ja', got['name_ja'], 60)
    return filled, source


@menu_pro_bp.route('/admin/menu', methods=['GET', 'POST'], endpoint='manage')
@permission_required('menu')
def manage():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'item':
            item = get_own(MenuItem, request.form.get('item_id'), '品項')
            if item.external_code != (request.form.get('external_code') or '').strip() and request.form.get('external_code'):
                dup = MenuItem.query.filter(MenuItem.external_code == request.form.get('external_code').strip(),
                                            MenuItem.id != item.id).first()
                if dup is not None:
                    raise AppError(f'外送 / 條碼代碼已被「{dup.name}」使用')
            apply_pro_form(item, request.form)
            record_staff_audit('MENU_PRO_CHANGE', details=f'更新「{item.name}」的供應時段 / 通路價 / 標示 / 翻譯')
            flash(f'已更新「{item.name}」', 'success')
        elif action == 'categories':
            for cat in request.form.getlist('category'):
                cat = cat.strip()[:50]
                if not cat:
                    continue
                row = CategoryTranslation.query.filter_by(category=cat).first()
                if row is None:
                    row = CategoryTranslation(tenant_id=g.tenant_id, category=cat)
                    db.session.add(row)
                row.name_en = (request.form.get(f'en__{cat}') or '').strip()[:60]
                row.name_ja = (request.form.get(f'ja__{cat}') or '').strip()[:60]
            flash('已更新分類翻譯', 'success')
        elif action == 'translate':
            filled, source = translate_menu(overwrite=request.form.get('overwrite') == '1')
            record_staff_audit('MENU_TRANSLATE', details=f'AI 翻譯菜單：填入 {filled} 個欄位 ({source})')
            if filled:
                flash(f'已填入 {filled} 個翻譯欄位 ({"Gemini" if source == "gemini" else "規則字典 (未設定 GEMINI_API_KEY，只翻常見分類 / 品名)"})；'
                      '請逐一檢查後再儲存修改', 'success')
            else:
                flash('沒有可填的欄位 (都已有翻譯，或規則字典沒有對應；設定 GEMINI_API_KEY 後可翻譯任意品名)', 'warning')
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        order_event_bus.notify(g.tenant_id)   # 數位看板：供應時段 / 價格 / 翻譯變更即時更新
        return redirect(url_for('menu_pro.manage', _anchor=f"item-{request.form.get('item_id')}" if action == 'item' else None))
    from sqlalchemy.orm import selectinload
    from app.models.menu import ComboOption
    from app.routes.admin import data_modal_context
    # 套餐清單逐筆列出每個品項的套餐與配餐：一次載入 (否則每個品項各查一次 combo_option，N+1)
    items = (MenuItem.query.options(selectinload(MenuItem.combo_options).selectinload(ComboOption.sides))
             .order_by(MenuItem.category, MenuItem.name).all())
    categories = sorted({(i.category or '未分類').strip() for i in items})
    translations = {t.category: t for t in CategoryTranslation.query.all()}
    return render_template('admin_menu.html', items=items, categories=categories, translations=translations,
                           admin_menu_items=[menu_service.item_form_data(i) for i in items],
                           allergens=ALLERGENS, diets=DIETS, weekdays=WEEKDAY_LABELS,
                           menu_cfg=dict(settings_service.get('menu'), ai_ready=ai_service.has_api_key()),
                           **data_modal_context())
