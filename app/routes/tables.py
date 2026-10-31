"""
門市桌位 QR code (手機掃碼點餐)：
- /t/<token>           顧客掃碼：綁定桌位後進入點餐頁 (公開)
- /admin/tables        桌位管理：新增 (可用範圍 A1-A10)、改名、停用、重新產生 QR code、刪除、設定門市網址 (店長以上)
- /admin/tables/print  列印 QR code 桌卡 (店長以上)
POS 的「桌位結帳」API 在 app/routes/pos.py。線上端沒有門市桌位，這些頁面一律 404。
"""
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from sqlalchemy.exc import IntegrityError

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db, limiter
from app.models.table import DiningTable
from app.services import table_service
from app.services.audit import record_staff_audit
from app.sync import runtime as sync_runtime
from app.tenant_context import _use_tenant, tenant_bypass
from app.utils import get_json_dict, get_own, role_required, safe_int

tables_bp = Blueprint('tables', __name__)


@tables_bp.before_request
def only_on_store_servers():
    if sync_runtime.is_hub():
        abort(404)


@tables_bp.route('/t/<token>', endpoint='scan')
@limiter.limit('30/minute')
def scan(token):
    with tenant_bypass():
        table = DiningTable.query.filter_by(token=str(token)[:32]).first()
    if table is None or not table.is_active or table.tenant is None or not table.tenant.is_active:
        return render_template('error.html', status=404,
                               message='這個桌位 QR code 已失效，請洽服務人員或掃描桌上最新的 QR code。'), 404
    _use_tenant(table.tenant)
    table_service.bind_table(table)
    flash(f'您在 {table.label}：點餐後餐點會送到桌上，用餐後請至櫃檯結帳。', 'info')
    return redirect(url_for('customer.customer_index'))


@tables_bp.route('/api/table/call', methods=['POST'], endpoint='call_service')
@limiter.limit('10/minute')
def call_service():
    """顧客在桌邊點餐頁按「呼叫服務」(需已掃描有效的桌位 QR code)"""
    from app.utils import get_json_dict
    table = table_service.current_table()
    if table is None:
        raise AppError('請先掃描桌上的 QR code', 403)
    data = get_json_dict()
    call, created = table_service.call_service(table, str(data.get('kind') or 'help'), data.get('note'))
    label = table_service.CALL_KINDS[call.kind]
    return jsonify({'success': True, 'created': created,
                    'message': f'已通知服務人員：{label}' if created else f'已通知過服務人員 ({label})，請稍候'})


def _table(table_id):
    return get_own(DiningTable, table_id, '桌位')


@tables_bp.route('/admin/tables', methods=['GET', 'POST'], endpoint='manage')
@role_required(*MANAGER_ROLES)
def manage():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            for attempt in range(2):  # 兩位店長同時新增同名桌位：撞到唯一索引時重試一次 (已存在的會被略過)
                try:
                    created, skipped = table_service.create_tables(g.tenant_id, request.form.get('names'))
                    if created:
                        record_staff_audit('TABLE_CHANGE', details=f"新增桌位：{', '.join(created)}")
                    db.session.commit()
                    break
                except IntegrityError:
                    db.session.rollback()
                    if attempt:
                        raise AppError('桌號剛被其他人新增，請重新整理後再試', 409)
            msg = f"✅ 已新增 {len(created)} 桌" + (f"；已存在略過：{', '.join(skipped)}" if skipped else '')
            flash(msg, 'success' if created else 'warning')
        elif action == 'base_url':
            g.tenant.table_qr_base_url = table_service.normalize_base_url(request.form.get('base_url'))
            db.session.commit()
            flash('已更新 QR code 使用的門市網址 (已印出的 QR code 需重新列印)', 'success')
        else:
            table = _table(request.form.get('table_id'))
            if action == 'rename':
                old = table.name
                table_service.rename_table(table, request.form.get('name'))
                table.sort_order = safe_int(request.form.get('sort_order'), table.sort_order)
                table_service.update_table_details(table, request.form.get('seats', table.seats),
                                                   request.form.get('area', table.area), request.form.get('shape', table.shape))
                record_staff_audit('TABLE_CHANGE', details=f'桌位 {old} 改為 {table.name}')
                flash(f'已更新 {table.label}', 'success')
            elif action == 'toggle':
                table.is_active = not table.is_active
                record_staff_audit('TABLE_CHANGE', details=f"{'啟用' if table.is_active else '停用'}桌位 {table.name}")
                flash(f"{table.label}已{'啟用' if table.is_active else '停用 (掃碼後無法點餐)'}", 'success')
            elif action == 'rotate':
                table_service.rotate_token(table)
                record_staff_audit('TABLE_CHANGE', details=f'重新產生桌位 {table.name} 的 QR code')
                flash(f'已重新產生 {table.label}的 QR code：舊的 QR code 已失效，請重新列印', 'success')
            elif action == 'delete':
                name = table.name
                table_service.delete_table(table)
                record_staff_audit('TABLE_CHANGE', details=f'刪除桌位 {name}')
                flash(f'已刪除 {name} 桌', 'success')
            else:
                abort(400)
            try:
                db.session.commit()
            except IntegrityError:  # 同時把兩桌改成同一個名稱
                db.session.rollback()
                raise AppError('桌號剛被其他人使用，請重新整理後再試', 409)
        return redirect(url_for('tables.manage'))

    tables = DiningTable.query.order_by(DiningTable.sort_order, DiningTable.id).all()
    base = table_service.base_url(g.tenant)
    return render_template(
        'admin_tables.html', tenant=g.tenant, tables=tables, base_url=base,
        saved_base_url=g.tenant.table_qr_base_url, suggested_base_url=table_service.suggest_base_url(),
        urls={t.id: table_service.table_url(t, base) for t in tables},
        unpaid_counts=table_service.table_counts(),
        shapes=table_service.TABLE_SHAPES,
        areas=sorted({t.area for t in tables if t.area}),
        floor=[{'id': t.id, 'name': t.name, 'seats': t.seats, 'area': t.area, 'shape': t.shape, 'x': t.pos_x, 'y': t.pos_y}
               for t in tables if t.is_active],
    )


@tables_bp.route('/admin/tables/layout', methods=['POST'], endpoint='save_layout')
@role_required(*MANAGER_ROLES)
def save_layout():
    """平面圖拖拉後儲存：{tables: [{id, x, y}]}"""
    n = table_service.save_layout((get_json_dict() or {}).get('tables'))
    record_staff_audit('TABLE_CHANGE', details=f'更新桌位平面圖 ({n} 桌)')
    db.session.commit()
    return jsonify({'success': True, 'updated': n})


@tables_bp.route('/admin/tables/print', endpoint='print_cards')
@role_required(*MANAGER_ROLES)
def print_cards():
    ids = {safe_int(i, 0) for i in request.args.getlist('id')}
    query = DiningTable.query.filter_by(is_active=True)
    if ids:
        query = query.filter(DiningTable.id.in_(ids))
    tables = query.order_by(DiningTable.sort_order, DiningTable.id).all()
    base = table_service.base_url(g.tenant)
    cards = []
    for t in tables:
        url = table_service.table_url(t, base)
        cards.append({'table': t, 'url': url, 'qr': table_service.qr_data_uri(url)})
    return render_template('table_qr_print.html', tenant=g.tenant, cards=cards, base_url=base)
