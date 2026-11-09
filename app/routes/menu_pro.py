"""後台：菜單與套餐頁 (/admin/menu)：品項與套餐清單 (新增 / 編輯 / 刪除的 POST 在 admin.py)，
以及每個品項的進階設定 (供應時段 / 排程、通路價、過敏原與素食、多語系、外送代碼、成本) 與分類翻譯"""

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.errors import AppError
from app.extensions import db
from app.models.menu import ALLERGENS, DIETS, WEEKDAY_LABELS, CategoryTranslation, MenuItem, invalid_size_options, \
    parse_size_options
from app.services import ai_service, menu_service, menu_translate_service, settings_service
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.settings_service import TIME_RE
from app.services.approval_service import permission_required
from app.utils import get_own, parse_date, safe_int

menu_pro_bp = Blueprint('menu_pro', __name__)


def _bounded_amount(raw, label):
    """通路價 / 會員價 / 成本：負數視為 0，超過 MAX_AMOUNT 擋下 (超出資料庫 INTEGER 範圍會 500)"""
    value = max(0, safe_int(raw, 0))
    if value > menu_service.MAX_AMOUNT:
        raise AppError(f'❌ {label}必須介於 0 ~ {menu_service.MAX_AMOUNT:,}！')
    return value


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
    for ch, label in (('takeout_price', '外帶價'), ('online_price', '線上價'), ('delivery_price', '外送價'),
                      ('member_price', '會員價')):
        setattr(item, ch, _bounded_amount(form.get(ch), label))
    item.allergens = ','.join(a for a in form.getlist('allergens') if a in ALLERGENS)
    item.diet = form.get('diet') if form.get('diet') in DIETS else ''
    item.spicy_level = min(3, max(0, safe_int(form.get('spicy_level'), 0)))
    for f, size in (('name_en', 100), ('name_ja', 100), ('description_en', 300), ('description_ja', 300),
                    ('external_code', 50)):
        setattr(item, f, (form.get(f) or '').strip()[:size])
    item.unit_cost = _bounded_amount(form.get('unit_cost'), '成本')
    raw_sizes = (form.get('size_options') or '').strip()
    sizes = parse_size_options(raw_sizes)
    bad = invalid_size_options(raw_sizes)
    if bad:
        raise AppError(f'規格格式錯誤：{"、".join(bad[:3])}；格式應為「名稱:加減價」，例如 中杯:0, 大杯:+15')
    if raw_sizes and not sizes:
        raise AppError('規格格式應為「名稱:加減價」，例如 中杯:0, 大杯:15')
    item.size_options = ', '.join(f'{n}:{d:+d}' if d else f'{n}:0' for n, d in sizes)
    item.calories = min(5000, max(0, safe_int(form.get('calories'), 0)))
    was_auto = bool(item.carbon_auto) if item.id else False
    item.carbon_auto = form.get('carbon_auto') == '1'
    if item.carbon_auto:
        from app.models.inventory import RecipeLine
        from app.services import carbon_service
        # 配方是各主機的本機資料，carbon_grams 卻會同步：本機沒有這道菜的配方 (例如分店沒有總部的配方) 時保留原本
        # (同步來) 的值，否則分店存一次進階設定就把全店家的碳足跡改成 0；從手動改回自動時照常依本機配方重算
        if not item.id:
            item.carbon_grams = 0
        elif not was_auto or RecipeLine.query.filter_by(menu_item_id=item.id).first() is not None:
            item.carbon_grams = carbon_service.recipe_carbon(item)
    else:
        item.carbon_grams = min(1_000_000, max(0, safe_int(form.get('carbon_grams'), 0)))
    item.nutrition = (form.get('nutrition') or '').strip()[:200]
    item.prep_minutes = min(120, max(0, safe_int(form.get('prep_minutes'), 0)))
    item.prep_note = (form.get('prep_note') or '').strip()[:500]
    return item


def _translate_message(filled, stats):
    """AI 翻譯結果訊息 (flash 的內容與類別)：有金鑰但 AI 服務過載時照實說明，並提示再按一次只翻剩下的"""
    if not stats['pending']:
        return '每個品項與分類都已有英 / 日文翻譯 (要重翻請勾「連已填的也重新翻譯」)', 'info'
    check = '請逐一檢查後再儲存修改'
    who = stats.get('provider') or 'AI'
    reused = f"沿用店家既有譯名 {stats['store']} 項；" if stats.get('store') else ''
    if not ai_service.has_api_key():
        if filled:
            return (f'已填入 {filled} 個翻譯欄位 ({reused}規則字典，未設定 AI 金鑰，只翻常見分類 / 品名)；{check}', 'success')
        return '沒有可填的欄位 (規則字典沒有對應；到系統設定「AI 服務」填金鑰後可翻譯任意品名)', 'warning'
    if not stats['fallback']:
        done = f"{who} 已翻譯 {stats['ai']} 項，" if stats['ai'] else ''
        return f"{reused}{done}填入 {filled} 個欄位；{check}", 'success'
    from app.services import ai_usage
    over = ai_usage.budget_exceeded(g.tenant_id)
    if over:   # 達每月預算上限：不是忙碌，也不會在背景補翻 (note_result 不排)
        done = f"{who} 已翻譯 {stats['ai']} 項；" if stats['ai'] else ''
        return (f"{reused}{done}{stats['fallback']} 項沒翻成：{over}。先以規則字典填入常見分類 / 品名，"
                f'共填入 {filled} 個欄位；{check}', 'warning')
    reason = ai_service.last_ai_error() or '回應格式不完整'
    busy = f'AI 服務目前忙碌或失敗 ({reason})，已自動重試數次'
    retry = '系統會在背景每隔幾分鐘自動補翻還沒翻好的項目，不用再按 (也可以稍後自己再按一次)'
    if stats['ai'] or stats.get('store'):
        return (f"{reused}{who} 已翻譯 {stats['ai']} 項；另外 {stats['fallback']} 項沒翻成 ({busy})，先以規則字典填常見分類 / 品名。"
                f'共填入 {filled} 個欄位；{retry}', 'warning')
    return f"這次 {stats['fallback']} 項都沒翻成：{busy}。規則字典填入 {filled} 個欄位 (只有常見分類 / 品名)；{retry}", 'warning'


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
            filled, source, stats = menu_translate_service.translate_menu(overwrite=request.form.get('overwrite') == '1')
            menu_translate_service.note_result(g.tenant_id, stats)   # 還有沒翻成的：排背景自動補翻
            record_staff_audit('MENU_TRANSLATE', details=f"AI 翻譯菜單：填入 {filled} 個欄位 ({stats.get('provider') or 'AI'} {stats['ai']} 項、"
                                                         f"未翻成 {stats['fallback']} 項)")
            flash(*_translate_message(filled, stats))
        elif action == 'translate_cancel':
            menu_translate_service.cancel(g.tenant_id)
            flash('已停止背景自動補翻', 'info')
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        order_event_bus.notify(g.tenant_id)   # 數位看板：供應時段 / 價格 / 翻譯變更即時更新
        # 儲存進階設定後捲回剛編輯的品項列 (品項列上有 id 的是標籤欄 item-tags-<id>)
        return redirect(url_for('menu_pro.manage', _anchor=f"item-tags-{request.form.get('item_id')}" if action == 'item' else None))
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
                           translate_pending=menu_translate_service.get_pending(g.tenant_id),
                           **data_modal_context())
