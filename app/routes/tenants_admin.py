"""
平台端店家管理：/admin/tenants，只有平台管理者 (預設店家 default 的負責人) 可用。
- 店家清單：啟用狀態、建立日期、員工 / 會員數、分店數 (線上端：最近連線時間)、近 30 天訂單數與營業額、最近一筆訂單。
- 停用 / 啟用：停用後該店家的員工無法登入、點餐頁 / 店家入口 404、分店同步被拒 (tenant_context 與 sync 已依 is_active 判斷)；
  預設店家 (平台管理者所屬) 不可停用。每次切換寫入稽核 (TENANT_TOGGLE)。
- 建立新店家沿用 /tenant/register (平台管理者登入後不受 ALLOW_TENANT_REGISTER 限制)。
"""
import io
import json
import re
import zipfile
from datetime import datetime, timedelta

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, logout_user
from sqlalchemy import func

from app.errors import AppError
from app.extensions import db, ip_key, limiter
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.sync import SyncNode
from app.models.tenant import StaffAccount, Tenant, TenantClosureRequest
from app.models.user import User
from app.services.audit import record_staff_audit
from app.services import excel_service, settings_service
from app.services.report_service import REVENUE_STATUSES
from app.tenant_context import _use_tenant, tenant_bypass
from app.utils import platform_admin_required, role_required, safe_int

tenants_bp = Blueprint('tenants', __name__)


def _counts(model, column=None):
    column = column if column is not None else model.tenant_id
    rows = db.session.query(column, func.count()).group_by(column).all()
    return {tid: n for tid, n in rows}


def tenant_overview(days=30):
    """所有店家的概況 (tenant_bypass：跨店查詢)"""
    since = datetime.now() - timedelta(days=days)
    with tenant_bypass():
        tenants = Tenant.query.order_by(Tenant.id).all()
        staff = {tid: n for tid, n in db.session.query(StaffAccount.tenant_id, func.count())
                 .filter(StaffAccount.deleted_at.is_(None)).group_by(StaffAccount.tenant_id).all()}   # 不含已刪除的帳號
        members = _counts(User)
        node_rows = SyncNode.query.all()
        closure_rows = (TenantClosureRequest.query.filter_by(status='pending')
                        .order_by(TenantClosureRequest.requested_at.desc()).all())
        close_requests = {}
        for request_row in closure_rows:
            close_requests.setdefault(request_row.tenant_id, request_row)
        # 與店家報表的營業額同一口徑 (report_service.is_revenue)：未取消且已收款 (或免付款)
        order_rows = (db.session.query(Order.tenant_id, func.count(), func.coalesce(func.sum(Order.total_price), 0),
                                       func.max(Order.created_at))
                      .filter(Order.created_at >= since, Order.status != OrderStatus.CANCELLED,
                              func.coalesce(Order.payment_status, '').in_(REVENUE_STATUSES))
                      .group_by(Order.tenant_id).all())
        last_any = {tid: at for tid, at in db.session.query(Order.tenant_id, func.max(Order.created_at))
                    .group_by(Order.tenant_id).all()}
    orders = {tid: (n, int(total or 0), last) for tid, n, total, last in order_rows}
    offline_after = current_app.config.get('SYNC_NODE_OFFLINE_AFTER', 120)
    nodes = {}
    for n in node_rows:
        info = nodes.setdefault(n.tenant_id, {'count': 0, 'online': 0, 'last_seen': None})
        info['count'] += 1
        if n.is_active and n.is_online(offline_after):
            info['online'] += 1
        if n.last_seen_at and (info['last_seen'] is None or n.last_seen_at > info['last_seen']):
            info['last_seen'] = n.last_seen_at
    out = []
    for t in tenants:
        n, total, _last = orders.get(t.id, (0, 0, None))
        out.append({
            'id': t.id, 'name': t.name, 'slug': t.slug, 'is_active': bool(t.is_active), 'created_at': t.created_at,
            'contact_email': t.contact_email or '', 'contact_phone': t.contact_phone or '',
            'is_default': t.slug == 'default', 'staff': staff.get(t.id, 0), 'members': members.get(t.id, 0),
            'nodes': nodes.get(t.id, {'count': 0, 'online': 0, 'last_seen': None}),
            'close_request': close_requests.get(t.id),
            'orders': n, 'revenue': total, 'last_order_at': last_any.get(t.id),
        })
    return out


@tenants_bp.route('/admin/tenants', endpoint='index')
@role_required('owner')
@platform_admin_required
def index():
    rows = tenant_overview()
    return render_template('admin_tenants.html', tenants=rows, days=30, tenant=g.tenant,
                           active_count=sum(1 for r in rows if r['is_active']),
                           register_open=bool(current_app.config.get('ALLOW_TENANT_REGISTER')))


@tenants_bp.route('/admin/tenants/<int:tenant_id>/toggle', methods=['POST'], endpoint='toggle')
@role_required('owner')
@platform_admin_required
def toggle(tenant_id):
    with tenant_bypass():
        target = db.session.get(Tenant, safe_int(tenant_id, 0))
    if target is None:
        raise AppError('查無此店家', 404)
    if target.slug == 'default' or target.id == g.tenant_id:
        raise AppError('預設店家 (平台管理者所屬) 不可停用', 409)
    target.is_active = not target.is_active
    if target.is_active:
        pending = TenantClosureRequest.query.filter_by(tenant_id=target.id, status='pending').all()
        for closure in pending:
            closure.status = 'resolved'
            closure.resolved_at = datetime.now()
            closure.resolved_by_username = current_user.username
            closure.resolution_note = '平台管理者重新啟用店家'
    record_staff_audit('TENANT_TOGGLE', details=f"{'啟用' if target.is_active else '停用'}店家「{target.name}」({target.slug})")
    db.session.commit()
    flash(f"已{'啟用' if target.is_active else '停用'}店家「{target.name}」", 'success' if target.is_active else 'warning')
    return redirect(url_for('tenants.index'))


@tenants_bp.route('/admin/tenant/account', endpoint='account')
@role_required('owner')
def account():
    closure_request = (TenantClosureRequest.query.filter_by(tenant_id=g.tenant_id, status='pending')
                       .order_by(TenantClosureRequest.requested_at.desc()).first())
    return render_template('tenant_account.html', tenant=g.tenant, closure_request=closure_request)


@tenants_bp.route('/admin/tenant/account/contact', methods=['POST'], endpoint='update_contact')
@role_required('owner')
def update_contact():
    email = (request.form.get('contact_email') or '').strip().lower()
    phone = (request.form.get('contact_phone') or '').strip()
    if email and (len(email) > 254 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email)):
        raise AppError('聯絡 Email 格式不正確。', 400)
    if phone and (len(phone) > 40 or not re.fullmatch(r'[0-9+().\-\s]{6,40}', phone)
                  or sum(char.isdigit() for char in phone) < 6):
        raise AppError('聯絡電話格式不正確。', 400)
    g.tenant.contact_email = email or None
    g.tenant.contact_phone = phone or None
    record_staff_audit('TENANT_CONTACT_UPDATE', details='Owner updated optional tenant contact details')
    db.session.commit()
    flash('店家聯絡方式已更新。', 'success')
    return redirect(url_for('tenants.account'))


@tenants_bp.route('/admin/tenant/account/export', endpoint='export_bundle')
@role_required('owner')
def export_bundle():
    tenant = g.tenant
    workbook = excel_service.generate_excel_export(
        ['MenuItem', 'User', 'UserCoupon', 'Coupon', 'Order', 'OrderItem', 'Analytics'])
    settings = settings_service.export_all(tenant_id=tenant.id, include_secrets=False, allow_owner_only=True)
    branches = [{'uuid': node.uuid, 'name': node.name, 'is_active': bool(node.is_active),
                 'accepts_online_orders': bool(node.accepts_online_orders),
                 'last_seen_at': node.last_seen_at.isoformat() if node.last_seen_at else None}
                for node in SyncNode.query.order_by(SyncNode.id).all()]
    generated_at = datetime.now().isoformat(timespec='seconds')
    manifest = {
        'version': 1,
        'generated_at': generated_at,
        'tenant': {'id': tenant.id, 'name': tenant.name, 'slug': tenant.slug},
        'files': ['business_data.xlsx', 'settings.json', 'branches.json'],
        'excluded': ['credentials and secret settings', 'face data and photos', 'archived records', 'system-wide backup'],
        'restorable': False,
    }
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('settings.json', json.dumps(settings, ensure_ascii=False, indent=2, default=str))
        archive.writestr('branches.json', json.dumps(branches, ensure_ascii=False, indent=2))
        archive.writestr('business_data.xlsx', workbook.getvalue())
        archive.writestr('README.txt', '本資料包供店家匯出與保存，不可用於系統還原。金鑰、登入憑證、人臉資料、照片與封存檔不包含在內。')
    record_staff_audit('TENANT_DATA_EXPORT', details='下載本店營運資料包 (不含金鑰、人臉資料、照片與封存檔)')
    db.session.commit()
    bundle.seek(0)
    return send_file(bundle, mimetype='application/zip', as_attachment=True,
                     download_name=f'{tenant.slug}_tenant_export_{datetime.now():%Y%m%d_%H%M%S}.zip')


@tenants_bp.route('/admin/tenant/account/close-request', methods=['POST'], endpoint='close_request')
@role_required('owner')
def close_request():
    tenant = g.tenant
    if tenant.slug == 'default':
        raise AppError('平台預設店家不能申請關閉。', 409)
    if not tenant.is_active:
        raise AppError('此店家已停用；請聯絡平台管理者處理。', 409)
    if not current_user.check_password(request.form.get('current_password') or ''):
        raise AppError('目前密碼不正確。', 403)
    if current_user.totp_enabled:
        from app.services import security_service
        if not security_service.verify_second_factor(current_user, (request.form.get('totp_code') or '').strip()):
            raise AppError('兩步驟驗證碼不正確。', 403)
    if request.form.get('confirm') != '1':
        raise AppError('請確認申請關閉此店家。', 400)
    if TenantClosureRequest.query.filter_by(tenant_id=tenant.id, status='pending').first():
        raise AppError('此店家已有待處理的關閉申請。', 409)

    reason = (request.form.get('reason') or '').strip()
    if len(reason) > 300:
        raise AppError('關閉原因最多 300 字。', 400)
    username = current_user.username
    closure = TenantClosureRequest(tenant_id=tenant.id, requested_by_staff_id=current_user.id,
                                   requested_by_username=username, reason=reason)
    db.session.add(closure)
    tenant.is_active = False
    db.session.flush()
    record_staff_audit('TENANT_CLOSE_REQUEST', details=f'租戶關閉申請 #{closure.id}；原因：{reason or "未提供"}')
    db.session.commit()
    tenant_name = tenant.name
    tenant_slug = tenant.slug
    logout_user()
    session.clear()
    return render_template('tenant_closed.html', tenant_name=tenant_name, tenant_slug=tenant_slug)


@tenants_bp.route('/tenant/reactivate', methods=['GET', 'POST'], endpoint='reactivate')
@limiter.limit('5/hour', methods=['POST'], key_func=ip_key)
def reactivate():
    """Owner can restore only a tenant suspended by its own pending closure request."""
    slug = (request.values.get('slug') or '').strip().lower()[:80]
    username = (request.form.get('username') or '').strip()[:120]
    password = request.form.get('current_password') or ''
    with tenant_bypass():
        target = Tenant.query.filter_by(slug=slug).first() if slug else None
        fallback_tenant = Tenant.query.filter_by(slug='default').first() if target is None else None
    display_tenant = target or fallback_tenant
    if display_tenant is not None:
        _use_tenant(display_tenant, remember=False)
    if request.method == 'GET':
        return render_template('tenant_reactivate.html', tenant_slug=slug)

    if target is not None:
        _use_tenant(target, remember=False)
        owner = StaffAccount.query.filter_by(tenant_id=target.id, username=username, role='owner',
                                             is_active=True, deleted_at=None).first()
        closure = TenantClosureRequest.query.filter_by(tenant_id=target.id, status='pending').first()
    else:
        owner = closure = None

    if owner is None:
        from app.services.auth_throttle import spend_hash_time
        spend_hash_time(password)
        password_ok = False
    else:
        password_ok = owner.check_password(password)
    valid_request = (target is not None and target.slug != 'default' and not target.is_active
                     and closure is not None and owner is not None and password_ok)
    if not valid_request:
        flash('店家代號、負責人帳號或密碼無效，或沒有可恢復的待處理關閉申請。', 'danger')
        return render_template('tenant_reactivate.html', tenant_slug=slug), 401

    if owner.totp_enabled:
        from app.services import security_service
        if not security_service.verify_second_factor(owner, (request.form.get('totp_code') or '').strip()):
            flash('店家代號、負責人帳號或驗證碼無效。', 'danger')
            return render_template('tenant_reactivate.html', tenant_slug=slug), 401

    target.is_active = True
    closure.status = 'owner_reactivated'
    closure.resolved_at = datetime.now()
    closure.resolved_by_username = owner.username
    closure.resolution_note = 'Owner self-reactivated the tenant'
    record_staff_audit('TENANT_CLOSE_RESCINDED', tenant_id=target.id, as_system=True,
                       details=f'店家負責人 {owner.username} 驗證後自行恢復店家')
    db.session.commit()
    _use_tenant(target, remember=True)
    from app.routes.admin import complete_staff_login
    response = complete_staff_login(owner)
    flash('店家已恢復，並已登入管理後台。', 'success')
    return response
