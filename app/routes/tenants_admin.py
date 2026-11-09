"""
平台端店家管理：/admin/tenants，只有平台管理者 (預設店家 default 的負責人) 可用。
- 店家清單：啟用狀態、建立日期、員工 / 會員數、分店數 (線上端：最近連線時間)、近 30 天訂單數與營業額、最近一筆訂單。
- 停用 / 啟用：停用後該店家的員工無法登入、點餐頁 / 店家入口 404、分店同步被拒 (tenant_context 與 sync 已依 is_active 判斷)；
  預設店家 (平台管理者所屬) 不可停用。每次切換寫入稽核 (TENANT_TOGGLE)。
- 建立新店家沿用 /tenant/register (平台管理者登入後不受 ALLOW_TENANT_REGISTER 限制)。
"""
from datetime import datetime, timedelta

from flask import Blueprint, current_app, flash, g, redirect, render_template, url_for
from sqlalchemy import func

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.sync import SyncNode
from app.models.tenant import StaffAccount, Tenant
from app.models.user import User
from app.services.audit import record_staff_audit
from app.services.report_service import REVENUE_STATUSES
from app.tenant_context import tenant_bypass
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
            'is_default': t.slug == 'default', 'staff': staff.get(t.id, 0), 'members': members.get(t.id, 0),
            'nodes': nodes.get(t.id, {'count': 0, 'online': 0, 'last_seen': None}),
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
    record_staff_audit('TENANT_TOGGLE', details=f"{'啟用' if target.is_active else '停用'}店家「{target.name}」({target.slug})")
    db.session.commit()
    flash(f"已{'啟用' if target.is_active else '停用'}店家「{target.name}」", 'success' if target.is_active else 'warning')
    return redirect(url_for('tenants.index'))
