"""後台：原物料、配方、進貨 / 盤點 / 報廢、異動紀錄"""
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.errors import AppError
from app.extensions import db
from app.models.inventory import Ingredient, IngredientLot, RecipeLine, StockMovement, Supplier
from app.models.menu import MenuItem
from app.services import inventory_service, settings_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.utils import safe_float, safe_int

inventory_bp = Blueprint('inventory', __name__)

REASON_LABELS = {'purchase': '進貨', 'consume': '銷售扣料', 'restore': '取消回補', 'stocktake': '盤點',
                 'waste': '報廢', 'adjust': '調整', 'transfer_in': '叫貨 / 調撥收貨', 'transfer_out': '叫貨 / 調撥出貨'}


def _ingredient(ing_id):
    ing = db.session.get(Ingredient, safe_int(ing_id, 0))
    if ing is None or ing.tenant_id != g.tenant_id:
        raise AppError('查無此原料', 404)
    return ing


@inventory_bp.route('/admin/inventory', methods=['GET', 'POST'], endpoint='manage')
@permission_required('inventory')
def manage():
    if request.method == 'POST':
        action = request.form.get('action')
        f = request.form
        if action == 'add':
            name = (f.get('name') or '').strip()[:60]
            if not name:
                raise AppError('請輸入原料名稱')
            if Ingredient.query.filter_by(name=name).first():
                raise AppError(f'原料「{name}」已存在', 409)
            db.session.add(Ingredient(tenant_id=g.tenant_id, name=name, unit=(f.get('unit') or 'g').strip()[:20],
                                      stock=round(safe_float(f.get('stock'), 0), 4),
                                      low_threshold=max(0.0, safe_float(f.get('low_threshold'), 0)),
                                      unit_cost=max(0.0, safe_float(f.get('unit_cost'), 0))))
            flash(f'✅ 已新增原料「{name}」', 'success')
        elif action == 'edit':
            ing = _ingredient(f.get('ingredient_id'))
            ing.name = (f.get('name') or ing.name).strip()[:60]
            ing.unit = (f.get('unit') or ing.unit).strip()[:20]
            ing.low_threshold = max(0.0, safe_float(f.get('low_threshold'), ing.low_threshold))
            ing.unit_cost = max(0.0, safe_float(f.get('unit_cost'), ing.unit_cost))
            ing.is_active = bool(f.get('is_active'))
            if 'supplier_id' in f:
                sup = db.session.get(Supplier, safe_int(f.get('supplier_id'), 0)) if f.get('supplier_id') else None
                ing.supplier_id = sup.id if sup is not None and sup.tenant_id == g.tenant_id else None
            if 'par_level' in f:
                ing.par_level = max(0.0, safe_float(f.get('par_level'), ing.par_level))
            if 'pack_size' in f:
                ing.pack_size = max(0.0, safe_float(f.get('pack_size'), ing.pack_size)) or 1
            flash('✅ 已更新原料', 'success')
        elif action == 'delete':
            ing = _ingredient(f.get('ingredient_id'))
            db.session.delete(ing)
            flash(f'已刪除原料「{ing.name}」(相關配方一併移除)', 'success')
        elif action in ('purchase', 'waste', 'adjust'):
            ing = _ingredient(f.get('ingredient_id'))
            qty = safe_float(f.get('quantity'), 0)
            if action == 'waste':
                qty = -abs(qty)
            cost = safe_float(f.get('unit_cost'), None) if action == 'purchase' and f.get('unit_cost') else None
            expires_on = None
            if action == 'purchase' and f.get('expires_on'):
                try:
                    expires_on = date.fromisoformat(f.get('expires_on'))
                except ValueError:
                    raise AppError('有效期限格式不正確')
            inventory_service.adjust(ing, qty, action, f.get('note'), unit_cost=cost, expires_on=expires_on)
            record_staff_audit('INVENTORY_' + action.upper(), details=f'{REASON_LABELS[action]}：{ing.name} {qty:+g} {ing.unit}')
            flash(f'✅ {REASON_LABELS[action]}：{ing.name} {qty:+g} {ing.unit}', 'success')
        elif action == 'stocktake':
            changed = []
            for ing in Ingredient.query.all():
                raw = f.get(f'count__{ing.id}')
                if raw not in (None, ''):
                    delta = inventory_service.stocktake(ing, safe_float(raw, ing.stock), f.get('note') or '盤點')
                    if delta:
                        changed.append(f'{ing.name} {delta:+g}')
            record_staff_audit('INVENTORY_STOCKTAKE', details='盤點調整：' + ('、'.join(changed) or '無差異'))
            flash(f"✅ 盤點完成：{len(changed)} 項有差異", 'success')
        elif action == 'discard_lot':
            lot = db.session.get(IngredientLot, safe_int(f.get('lot_id'), 0))
            if lot is None or lot.tenant_id != g.tenant_id:
                raise AppError('查無此批號', 404)
            qty = inventory_service.discard_lot(lot)
            record_staff_audit('INVENTORY_WASTE', details=f'批號報廢：{lot.ingredient.name} -{qty:g} (有效期限 {lot.expires_on})')
            flash(f'✅ 已報廢 {lot.ingredient.name} {qty:g} {lot.ingredient.unit}', 'success')
        elif action == 'recipe':
            item = db.session.get(MenuItem, safe_int(f.get('menu_item_id'), 0))
            if item is None or item.tenant_id != g.tenant_id:
                raise AppError('查無此餐點', 404)
            RecipeLine.query.filter_by(menu_item_id=item.id).delete(synchronize_session=False)
            for ing_id, qty in zip(f.getlist('ingredient_id'), f.getlist('quantity')):
                q = safe_float(qty, 0)
                if safe_int(ing_id, 0) and q > 0:
                    ing = _ingredient(ing_id)
                    db.session.add(RecipeLine(tenant_id=g.tenant_id, menu_item_id=item.id, ingredient_id=ing.id,
                                              quantity=round(q, 4)))
            record_staff_audit('RECIPE_CHANGE', details=f'更新「{item.name}」的配方')
            flash(f'✅ 已更新「{item.name}」的配方', 'success')
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        return redirect(url_for('inventory.manage', item=f.get('menu_item_id') or None))
    ingredients = Ingredient.query.order_by(Ingredient.name).all()
    items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
    recipes = {}
    for rl in RecipeLine.query.all():
        recipes.setdefault(rl.menu_item_id, []).append(rl)
    selected = db.session.get(MenuItem, safe_int(request.args.get('item'), 0)) if request.args.get('item') else None
    movements = StockMovement.query.order_by(StockMovement.id.desc()).limit(100).all()
    costs = {i.id: inventory_service.item_unit_cost(i) for i in items if i.id in recipes}
    return render_template('admin_inventory.html', ingredients=ingredients, items=items, recipes=recipes,
                           selected=selected, movements=movements, labels=REASON_LABELS, costs=costs,
                           cfg=settings_service.get('inventory'), low=[i for i in ingredients if i.is_low],
                           lots=inventory_service.expiring_lots(g.tenant_id), today=date.today(),
                           suppliers=Supplier.query.filter_by(is_active=True).order_by(Supplier.name).all())
