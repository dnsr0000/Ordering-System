"""
稽核紀錄完整頁：/admin/audit
- 員工操作稽核 (StaffAuditLog) 與後台登入 / 登出日誌 (AdminLog) 伺服器端分頁 (每頁 50 筆)，
  依員工、動作類型、日期區間、關鍵字 (單號 / 會員 ID / 內容) 篩選；後台首頁的稽核分頁只顯示最近 200 筆。
- 匯出 Excel (目前篩選條件、最多 20000 筆；文字欄位防公式注入)。
"""
import io
from datetime import datetime, timedelta

from flask import Blueprint, g, render_template, request, send_file
from sqlalchemy import or_

from app.errors import AppError
from app.models.enums import MANAGER_ROLES
from app.models.user import AdminLog, StaffAuditLog
from app.services.audit import ACTION_LABELS, record_staff_audit
from app.utils import neutralize_formulas, role_required, safe_int

audit_bp = Blueprint('audit', __name__)

PAGE_SIZE = 50
EXPORT_LIMIT = 20000


def _date(raw):
    raw = str(raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d')
    except ValueError:
        raise AppError('日期格式應為 YYYY-MM-DD')


def _filters():
    return {
        'tab': 'logins' if request.args.get('tab') == 'logins' else 'actions',
        'staff': (request.args.get('staff') or '').strip()[:100],
        'action': (request.args.get('action') or '').strip()[:60],
        'q': (request.args.get('q') or '').strip()[:100],
        'start': (request.args.get('start') or '').strip()[:10],
        'end': (request.args.get('end') or '').strip()[:10],
    }


def _audit_query(f):
    q = StaffAuditLog.query
    if f['staff']:
        q = q.filter(StaffAuditLog.staff_username == f['staff'])
    if f['action']:
        q = q.filter(StaffAuditLog.action_type == f['action'])
    start, end = _date(f['start']), _date(f['end'])
    if start:
        q = q.filter(StaffAuditLog.created_at >= start)
    if end:
        q = q.filter(StaffAuditLog.created_at < end + timedelta(days=1))
    if f['q']:
        like = f'%{f["q"]}%'
        conds = [StaffAuditLog.details.ilike(like), StaffAuditLog.ip_address.ilike(like)]
        if f['q'].lstrip('#').isdigit():
            n = int(f['q'].lstrip('#'))
            conds += [StaffAuditLog.target_order_id == n, StaffAuditLog.target_user_id == n]
        q = q.filter(or_(*conds))
    return q.order_by(StaffAuditLog.id.desc())


def _login_query(f):
    q = AdminLog.query
    if f['staff']:
        q = q.filter(AdminLog.username == f['staff'])
    start, end = _date(f['start']), _date(f['end'])
    if start:
        q = q.filter(AdminLog.login_at >= start)
    if end:
        q = q.filter(AdminLog.login_at < end + timedelta(days=1))
    if f['q']:
        q = q.filter(AdminLog.ip_address.ilike(f'%{f["q"]}%'))
    return q.order_by(AdminLog.id.desc())


def _page(query):
    total = query.count()
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, safe_int(request.args.get('page'), 1)), pages)
    rows = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return rows, page, pages, total


@audit_bp.route('/admin/audit', endpoint='index')
@role_required(*MANAGER_ROLES)
def index():
    f = _filters()
    query = _login_query(f) if f['tab'] == 'logins' else _audit_query(f)
    rows, page, pages, total = _page(query)
    staff_names = sorted({r[0] for r in StaffAuditLog.query.with_entities(StaffAuditLog.staff_username).distinct().all()}
                         | {r[0] for r in AdminLog.query.with_entities(AdminLog.username).distinct().all()})
    return render_template('admin_audit.html', f=f, rows=rows, page=page, pages=pages, total=total,
                           page_size=PAGE_SIZE, action_labels=ACTION_LABELS, staff_names=staff_names,
                           tenant=g.tenant, args={k: v for k, v in f.items() if v})


@audit_bp.route('/admin/audit/export', endpoint='export')
@role_required(*MANAGER_ROLES)
def export():
    from openpyxl import Workbook
    f = _filters()
    wb = Workbook()
    ws = wb.active
    if f['tab'] == 'logins':
        ws.title = '登入日誌'
        ws.append(['ID', '員工帳號', '登入時間', '登出時間', 'IP'])
        for lg in _login_query(f).limit(EXPORT_LIMIT).all():
            ws.append([lg.id, lg.username, lg.login_at.strftime('%Y-%m-%d %H:%M:%S') if lg.login_at else '',
                       lg.logout_at.strftime('%Y-%m-%d %H:%M:%S') if lg.logout_at else '', lg.ip_address or ''])
    else:
        ws.title = '操作稽核'
        ws.append(['ID', '時間', '員工', '角色', '動作', '動作代碼', '單號', '會員ID', '點數異動', '說明', 'IP'])
        for au in _audit_query(f).limit(EXPORT_LIMIT).all():
            ws.append([au.id, au.created_at.strftime('%Y-%m-%d %H:%M:%S') if au.created_at else '', au.staff_username,
                       au.staff_role, ACTION_LABELS.get(au.action_type, (au.action_type,))[0], au.action_type,
                       au.target_order_id or '', au.target_user_id or '', au.points_delta or 0, au.details or '',
                       au.ip_address or ''])
    neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    record_staff_audit('AUDIT_EXPORT', details=f"匯出{'登入日誌' if f['tab'] == 'logins' else '操作稽核'} (篩選：{f})"[:500])
    from app.extensions import db
    db.session.commit()
    name = f"audit_{f['tab']}_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
