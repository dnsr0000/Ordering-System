"""後台：系統監控 (健康狀態、錯誤紀錄、自動備份、時鐘誤差、通知測試)"""
import os

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_from_directory, url_for

from app.errors import AppError
from app.services import maintenance_service, monitor_service, settings_service
from app.services.audit import record_staff_audit
from app.extensions import db
from app.utils import role_required

system_bp = Blueprint('system', __name__)


@system_bp.route('/admin/system', endpoint='status')
@role_required('owner', 'manager')
def status():
    return render_template('admin_system.html', health=maintenance_service.health(g.tenant_id),
                           events=monitor_service.recent_events(150), backups=maintenance_service.list_backups(),
                           clock=monitor_service.clock_status(g.tenant_id, force=request.args.get('clock') == '1'),
                           backup_cfg=settings_service.get('backup'), monitor_cfg=settings_service.get('monitor'))


@system_bp.route('/admin/system/backup', methods=['POST'], endpoint='backup_now')
@role_required('owner')
def backup_now():
    path = maintenance_service.run_sqlite_backup(settings_service.get('backup').get('keep_days') or 14)
    if path is None:
        raise AppError('自動備份只適用於門市 SQLite；線上端 PostgreSQL 請使用 docker compose 的 backup 服務')
    record_staff_audit('SYSTEM_BACKUP', details=f'手動建立備份 {os.path.basename(path)}')
    db.session.commit()
    flash(f'✅ 已建立備份 {os.path.basename(path)}', 'success')
    return redirect(url_for('system.status'))


@system_bp.route('/admin/system/backup/<name>', endpoint='download_backup')
@role_required('owner')
def download_backup(name):
    if not (name.startswith('menu-') and name.endswith('.zip') and '/' not in name and '\\' not in name and '..' not in name):
        abort(404)
    return send_from_directory(maintenance_service.backup_dir(), name, as_attachment=True)


@system_bp.route('/admin/system/test_alert', methods=['POST'], endpoint='test_alert')
@role_required('owner', 'manager')
def test_alert():
    sent = monitor_service.send_alert_now(g.tenant_id, 'info', '這是一則測試通知 (系統監控頁)。')
    flash(f"✅ 已送出：{', '.join(sent)}" if sent else '尚未設定任何通知管道 (系統設定 → 監控與錯誤通知)',
          'success' if sent else 'warning')
    return redirect(url_for('system.status'))
