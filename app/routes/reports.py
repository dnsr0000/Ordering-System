"""後台：進階營運報表 (可匯出 Excel、立即寄送)"""
from datetime import date, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, send_file, url_for

from app.errors import AppError
from app.models.sync import SyncNode
from app.services import report_service
from app.services.approval_service import permission_required
from app.sync import runtime as sync_runtime

reports_bp = Blueprint('reports', __name__)


def _range():
    preset = request.args.get('preset') or ''
    today = date.today()
    if preset == 'today':
        return today, today
    if preset == 'yesterday':
        y = today - timedelta(days=1)
        return y, y
    if preset == 'month':
        return today.replace(day=1), today
    if preset == 'last_month':
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    try:
        start = date.fromisoformat(request.args.get('start') or '')
        end = date.fromisoformat(request.args.get('end') or '')
    except ValueError:
        return today - timedelta(days=6), today
    if end < start:
        start, end = end, start
    if (end - start).days > 400:
        raise AppError('查詢區間最長 400 天')
    return start, end


@reports_bp.route('/admin/reports', endpoint='index')
@permission_required('reports')
def index():
    start, end = _range()
    node = request.args.get('node') or None
    report = report_service.build_report(g.tenant_id, start, end, node)
    nodes = SyncNode.query.order_by(SyncNode.id).all() if sync_runtime.is_hub() else []
    names = {n.uuid: n.name for n in nodes}
    return render_template('admin_reports.html', r=report, start=start, end=end, node=node, nodes=nodes, node_names=names,
                           weekdays=report_service.WEEKDAYS)


@reports_bp.route('/admin/reports/export', endpoint='export')
@permission_required('reports')
def export():
    start, end = _range()
    report = report_service.build_report(g.tenant_id, start, end, request.args.get('node') or None)
    return send_file(report_service.report_to_excel(report, g.tenant.name), as_attachment=True,
                     download_name=f'營運報表_{start}_{end}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@reports_bp.route('/admin/reports/send', methods=['POST'], endpoint='send_now')
@permission_required('reports')
def send_now():
    try:
        n = report_service.send_scheduled_report(g.tenant_id, force=True)
        flash(f'已寄出報表給 {n} 位收件者', 'success')
    except AppError as exc:
        flash(f'{exc.message}', 'danger')
    return redirect(url_for('reports.index'))


@reports_bp.route('/admin/reports/menu-engineering', endpoint='menu_engineering')
@permission_required('reports')
def menu_engineering():
    """菜單工程分析：人氣 × 毛利四象限 (可依分店、分類篩選，匯出 Excel)"""
    from app.services import menu_engineering_service
    start, end = _range()
    node = request.args.get('node') or None
    category = (request.args.get('category') or '').strip()
    result = menu_engineering_service.analyze(g.tenant_id, start, end, node, category)
    if request.args.get('format') == 'xlsx':
        return send_file(menu_engineering_service.to_excel(result, g.tenant.name), as_attachment=True,
                         download_name=f'菜單工程_{start}_{end}.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    nodes = SyncNode.query.order_by(SyncNode.id).all() if sync_runtime.is_hub() else []
    return render_template('admin_menu_engineering.html', r=result, start=start, end=end, node=node, nodes=nodes,
                           category=category, classes=menu_engineering_service.CLASSES,
                           class_order=menu_engineering_service.ORDER)


@reports_bp.route('/admin/staff-anomalies', endpoint='staff_anomalies')
@permission_required('reports')
def staff_anomalies():
    """員工異常偵測：各收銀員的作廢 / 退款、折扣 / 改價、交班帳差與全店平均比較"""
    from app.services import staff_anomaly_service
    start, end = _range()
    node = request.args.get('node') or None
    result = staff_anomaly_service.analyze(g.tenant_id, start, end, node)
    nodes = SyncNode.query.order_by(SyncNode.id).all() if sync_runtime.is_hub() else []
    return render_template('admin_staff_anomalies.html', r=result, start=start, end=end, node=node, nodes=nodes)


@reports_bp.route('/admin/accounting', endpoint='accounting')
@permission_required('reports')
def accounting():
    """會計匯出：日結傳票 (收款紀錄 → 借貸分錄) 與發票明細 (CSV / Excel)"""
    from app.services import accounting_service as acc
    start, end = _range()
    node = request.args.get('node') or None
    export = request.args.get('export')
    if export in ('journal', 'invoices'):
        fmt = 'xlsx' if request.args.get('format') == 'xlsx' else 'csv'
        if export == 'journal':
            header, rows, title = acc.JOURNAL_HEADER, list(acc.journal_rows(acc.journal(g.tenant_id, start, end, node))), '傳票'
        else:
            header, rows, title = acc.INVOICE_HEADER, acc.invoices(g.tenant_id, start, end), '發票明細'
        buf = acc.to_xlsx(header, rows, title) if fmt == 'xlsx' else acc.to_csv(header, rows)
        mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if fmt == 'xlsx' \
            else 'text/csv; charset=utf-8'
        return send_file(buf, as_attachment=True, download_name=f'{title}_{start}_{end}.{fmt}', mimetype=mimetype)
    vouchers = acc.journal(g.tenant_id, start, end, node)
    nodes = SyncNode.query.order_by(SyncNode.id).all() if sync_runtime.is_hub() else []
    return render_template('admin_accounting.html', vouchers=vouchers, invoice_count=len(acc.invoices(g.tenant_id, start, end)),
                           start=start, end=end, node=node, nodes=nodes)
