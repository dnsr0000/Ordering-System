"""後台：網路熱感印表機、廚房工作站、列印紀錄"""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db
from app.models.menu import MenuItem
from app.models.printing import KitchenStation, Printer, PrintJob
from app.services import print_service
from app.services.audit import record_staff_audit
from app.utils import get_own, role_required, safe_int

printing_bp = Blueprint('printing', __name__)


def _printer_form(p, form):
    name = (form.get('name') or '').strip()[:50]
    host = (form.get('host') or '').strip()[:100]
    if not name or not host:
        raise AppError('請輸入印表機名稱與 IP 位址')
    if any(c in host for c in ' /\\'):
        raise AppError('IP 位址格式不正確 (例如 192.168.1.50)')
    from app.services.webhook_service import check_public_host_on_hub
    check_public_host_on_hub(host)   # 線上端沒有門市印表機：不可拿印表機設定連線上端內網
    p.name, p.host = name, host
    p.port = min(65535, max(1, safe_int(form.get('port'), 9100)))
    p.role = 'kitchen' if form.get('role') == 'kitchen' else 'receipt'
    station_id = safe_int(form.get('station_id'), 0)
    p.station_id = get_own(KitchenStation, station_id).id if station_id else None
    p.paper_width = 58 if form.get('paper_width') == '58' else 80
    p.encoding = form.get('encoding') if form.get('encoding') in ('big5', 'gb18030', 'utf8') else 'big5'
    p.copies = min(5, max(1, safe_int(form.get('copies'), 1)))
    p.is_active = bool(form.get('is_active'))
    p.drawer = p.role == 'receipt' and bool(form.get('drawer'))


@printing_bp.route('/admin/printers', methods=['GET', 'POST'], endpoint='manage')
@role_required(*MANAGER_ROLES)
def manage():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_printer':
            p = Printer(tenant_id=g.tenant_id)
            _printer_form(p, request.form)
            db.session.add(p)
            flash(f'已新增印表機「{p.name}」', 'success')
        elif action == 'edit_printer':
            _printer_form(get_own(Printer, request.form.get('printer_id')), request.form)
            flash('已更新印表機', 'success')
        elif action == 'delete_printer':
            p = get_own(Printer, request.form.get('printer_id'))
            db.session.delete(p)
            flash(f'已刪除印表機「{p.name}」', 'success')
        elif action == 'test_printer':
            p = get_own(Printer, request.form.get('printer_id'))
            try:
                print_service.test_print(p)
                flash(f'已送出測試頁到「{p.name}」', 'success')
            except OSError as exc:
                flash(f'無法連線到 {p.host}:{p.port}：{exc}', 'danger')
        elif action == 'add_station':
            name = (request.form.get('name') or '').strip()[:50]
            if not name:
                raise AppError('請輸入工作站名稱')
            db.session.add(KitchenStation(tenant_id=g.tenant_id, name=name,
                                          categories=(request.form.get('categories') or '').strip()[:300],
                                          sort_order=safe_int(request.form.get('sort_order'), 0)))
            flash(f'已新增工作站「{name}」', 'success')
        elif action == 'edit_station':
            st = get_own(KitchenStation, request.form.get('station_id'))
            st.name = (request.form.get('name') or st.name).strip()[:50]
            st.categories = (request.form.get('categories') or '').strip()[:300]
            st.sort_order = safe_int(request.form.get('sort_order'), st.sort_order)
            st.is_active = bool(request.form.get('is_active'))
            flash('已更新工作站', 'success')
        elif action == 'delete_station':
            st = get_own(KitchenStation, request.form.get('station_id'))
            db.session.delete(st)
            flash(f'已刪除工作站「{st.name}」', 'success')
        elif action == 'item_station':
            item = get_own(MenuItem, request.form.get('item_id'))
            sid = safe_int(request.form.get('station_id'), 0)
            item.station_id = get_own(KitchenStation, sid).id if sid else None
            flash(f'「{item.name}」的工作站已更新', 'success')
        elif action == 'retry_job':
            print_service.retry_job(get_own(PrintJob, request.form.get('job_id')))
            db.session.commit()
            print_service.process_queue(g.tenant_id)
            flash('已重新送出列印', 'info')
        else:
            raise AppError('不支援的操作')
        record_staff_audit('PRINTER_STATION_CHANGE', details=f'印表機 / 工作站設定：{action}')
        db.session.commit()
        return redirect(url_for('printing.manage'))
    stations = KitchenStation.query.order_by(KitchenStation.sort_order, KitchenStation.id).all()
    return render_template('admin_printers.html', printers=Printer.query.order_by(Printer.id).all(), stations=stations,
                           items=MenuItem.query.order_by(MenuItem.category, MenuItem.name).all(),
                           jobs=PrintJob.query.order_by(PrintJob.id.desc()).limit(50).all())
