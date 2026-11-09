"""
門市桌位 QR code (手機掃碼點餐)：
- /t/<token>           顧客掃碼：綁定桌位後進入點餐頁 (公開)
- /admin/tables        桌位管理：新增 (可用範圍 A1-A10)、改名、停用、重新產生 QR code、刪除、設定門市網址 (店長以上)
- /admin/tables/print  列印 QR code 桌卡 (店長以上)
  桌位管理頁也列出「桌邊線上結帳已付款卻無法結帳」(conflict) 的紀錄，店員到綠界後台退款後按「已到綠界退款」標記。
POS 的「桌位結帳」API 在 app/routes/pos.py。線上端沒有門市桌位，這些頁面一律 404。
"""
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from sqlalchemy.exc import IntegrityError

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db, limiter
from app.models.table import DiningTable, TablePayment
from app.services import i18n_service, table_service
from app.services.audit import record_staff_audit
from app.tenant_context import _use_tenant, tenant_bypass
from app.utils import get_json_dict, get_own, role_required, safe_int, store_only

tables_bp = Blueprint('tables', __name__)

SORT_ORDER_MIN, SORT_ORDER_MAX = -99999, 99999  # 桌位排序允許的範圍 (DiningTable.sort_order 是 32 位元整數)
BASE_URL_MAX_LEN = 200  # Tenant.table_qr_base_url 是 String(200)


@tables_bp.before_request
@store_only
def only_on_store_servers():
    """線上端沒有門市桌位：整個 blueprint 404"""


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
    message = f'已通知服務人員：{label}' if created else f'已通知過服務人員 ({label})，請稍候'
    return jsonify({'success': True, 'created': created, 'message': message,
                    'message_i18n': i18n_service.translate_message(message)})


def _table(table_id):
    return get_own(DiningTable, table_id, '桌位')


def _mark_table_payment_refunded(payment_id):
    """
    桌邊線上結帳 conflict (顧客已付款卻無法完成桌位結帳) → 店員已到綠界後台退款：
    以條件式 UPDATE 由 conflict 改成 refunded (兩人同時按只會成功一次)，並在 note 記下操作人員與時間。
    """
    from datetime import datetime
    from flask_login import current_user
    from sqlalchemy import update
    tp = get_own(TablePayment, payment_id, '線上結帳紀錄')
    mark = f"已到綠界退款 ({getattr(current_user, 'username', '') or '員工'} {datetime.now():%Y-%m-%d %H:%M})"
    note_len = TablePayment.__table__.c.note.type.length or 200
    old = tp.note or ''
    note = f'{old[:max(0, note_len - len(mark) - 1)]}；{mark}' if old else mark
    done = db.session.execute(
        update(TablePayment)
        .where(TablePayment.id == tp.id, TablePayment.tenant_id == g.tenant_id, TablePayment.status == 'conflict')
        .values(status='refunded', note=note[:note_len])
        .execution_options(synchronize_session=False)).rowcount
    if not done:
        db.session.rollback()
        raise AppError('這筆線上結帳不是待退款狀態 (可能已被其他人標記)，請重新整理', 409)
    label = tp.dining_table.label if tp.dining_table else '已刪除的桌位'
    record_staff_audit('REFUND_DONE', details=f'桌邊線上結帳 {tp.trade_no} ({label} ${tp.amount}，'
                                              f'綠界交易 {tp.provider_trade_no or "—"}) 已到綠界退款')
    db.session.commit()
    return f'已標記 {label} 的線上結帳 ${tp.amount} 為「已到綠界退款」'


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
            url = table_service.normalize_base_url(request.form.get('base_url'))
            if len(url) > BASE_URL_MAX_LEN:  # 欄位是 String(200)：PostgreSQL 超長會 DataError (500)
                raise AppError(f'門市網址太長 (最多 {BASE_URL_MAX_LEN} 字)')
            g.tenant.table_qr_base_url = url
            db.session.commit()
            flash('已更新 QR code 使用的門市網址 (已印出的 QR code 需重新列印)', 'success')
        elif action == 'table_pay_refunded':
            flash(_mark_table_payment_refunded(request.form.get('payment_id')), 'success')
        else:
            table = _table(request.form.get('table_id'))
            category = 'success'
            if action == 'rename':
                old = table.name
                sort_order = safe_int(request.form.get('sort_order'), table.sort_order)
                # 超出整數欄位範圍時 PostgreSQL / SQLite 會在 commit 時 500；沒改動的舊值照常放行
                if sort_order != table.sort_order and not SORT_ORDER_MIN <= sort_order <= SORT_ORDER_MAX:
                    raise AppError(f'排序請輸入 {SORT_ORDER_MIN} 到 {SORT_ORDER_MAX} 之間的整數')
                table_service.rename_table(table, request.form.get('name'))
                table.sort_order = sort_order
                table_service.update_table_details(table, request.form.get('seats', table.seats),
                                                   request.form.get('area', table.area), request.form.get('shape', table.shape))
                record_staff_audit('TABLE_CHANGE', details=f'桌位 {old} 改為 {table.name}')
                msg = f'已更新 {table.label}'
            elif action == 'toggle':
                table.is_active = not table.is_active
                record_staff_audit('TABLE_CHANGE', details=f"{'啟用' if table.is_active else '停用'}桌位 {table.name}")
                msg = f"{table.label}已{'啟用' if table.is_active else '停用 (掃碼後無法點餐)'}"
                unpaid = 0 if table.is_active else table_service.table_counts().get(table.id, 0)
                if unpaid:  # 停用後 POS 平面圖不再顯示這桌：提醒到 POS 未結帳清單結帳，避免漏收款
                    msg += f'；注意：此桌仍有 {unpaid} 張未結帳訂單，平面圖上不再顯示，請到 POS 未結帳清單結帳'
                    category = 'warning'
            elif action == 'rotate':
                table_service.rotate_token(table)
                record_staff_audit('TABLE_CHANGE', details=f'重新產生桌位 {table.name} 的 QR code')
                msg = f'已重新產生 {table.label}的 QR code：舊的 QR code 已失效，請重新列印'
            elif action == 'delete':
                name = table.name
                table_service.delete_table(table)
                record_staff_audit('TABLE_CHANGE', details=f'刪除桌位 {name}')
                msg = f'已刪除 {name} 桌'
            else:
                abort(400)
            try:
                db.session.commit()
            except IntegrityError:  # 同時把兩桌改成同一個名稱
                db.session.rollback()
                raise AppError('桌號剛被其他人使用，請重新整理後再試', 409)
            flash(msg, category)  # commit 成功後才提示，避免撞名回滾時同時出現「已更新」
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
        # 桌邊線上結帳已付款卻無法完成結帳 (需到綠界後台退款)：除了 monitor 告警之外，這裡也要列出來，避免漏退
        conflict_payments=TablePayment.query.filter(TablePayment.status == 'conflict')
        .order_by(TablePayment.id.desc()).all(),
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
