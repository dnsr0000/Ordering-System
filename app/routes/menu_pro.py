"""後台：菜單進階設定 (供應時段 / 排程、通路價、過敏原與素食、多語系、外送代碼、成本)"""
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db
from app.models.menu import ALLERGENS, DIETS, WEEKDAY_LABELS, CategoryTranslation, MenuItem, parse_size_options
from app.services import settings_service
from app.services.audit import record_staff_audit
from app.services.settings_service import TIME_RE
from app.utils import get_own, role_required, safe_int

menu_pro_bp = Blueprint('menu_pro', __name__)


def _date(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AppError(f'日期格式不正確：{value}')


def apply_pro_form(item, form):
    start, end = (form.get('avail_start') or '').strip(), (form.get('avail_end') or '').strip()
    for label, value in (('供應開始時間', start), ('供應結束時間', end)):
        if value and not TIME_RE.match(value):
            raise AppError(f'{label}格式應為 HH:MM')
    days = ''.join(sorted({d for d in form.getlist('avail_days') if d in '1234567'}))
    if not days:
        raise AppError('請至少勾選一個供應星期')
    sale_from, sale_until = _date(form.get('sale_from')), _date(form.get('sale_until'))
    if sale_from and sale_until and sale_until < sale_from:
        raise AppError('販售結束日不可早於開始日')
    item.avail_start, item.avail_end, item.avail_days = start, end, days
    item.sale_from, item.sale_until = sale_from, sale_until
    for ch in ('takeout_price', 'online_price', 'delivery_price'):
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
    item.nutrition = (form.get('nutrition') or '').strip()[:200]
    return item


@menu_pro_bp.route('/admin/menu_pro', methods=['GET', 'POST'], endpoint='manage')
@role_required(*MANAGER_ROLES)
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
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        return redirect(url_for('menu_pro.manage', _anchor=f"item-{request.form.get('item_id')}" if action == 'item' else None))
    items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
    categories = sorted({(i.category or '未分類').strip() for i in items})
    translations = {t.category: t for t in CategoryTranslation.query.all()}
    return render_template('admin_menu_pro.html', items=items, categories=categories, translations=translations,
                           allergens=ALLERGENS, diets=DIETS, weekdays=WEEKDAY_LABELS,
                           menu_cfg=settings_service.get('menu'))
