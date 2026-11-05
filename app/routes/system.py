"""後台：系統監控 (健康狀態、錯誤紀錄、自動備份、時鐘誤差、通知測試)"""
import os

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_from_directory, url_for

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.services import maintenance_service, monitor_service, settings_service
from app.services.audit import record_staff_audit
from app.extensions import db
from app.utils import is_platform_admin, platform_admin_required, role_required

system_bp = Blueprint('system', __name__)


@system_bp.route('/admin/system', endpoint='status')
@role_required(*MANAGER_ROLES)
def status():
    # 整庫備份含所有店家的資料與金鑰：只有平台管理者 (預設店家負責人) 能看到清單、建立與下載 (與 admin.backup_system 相同)
    from app.services import update_service
    from app.sync import runtime as sync_runtime
    update = None
    if is_platform_admin() and not sync_runtime.is_hub():   # 線上端 (Docker) 依 DEPLOY.txt 更新，不顯示
        try:
            update = update_service.check(force=request.args.get('update') == '1')
        except Exception as exc:  # noqa: BLE001  查版本失敗不影響監控頁
            update = {**update_service.current_version(), 'latest': None, 'error': str(exc), 'update_available': False,
                      'downloaded': '', 'can_apply': False, 'repo': update_service.repo()}
    return render_template('admin_system.html', health=maintenance_service.health(g.tenant_id), update=update,
                           events=monitor_service.recent_events(150),
                           backups=maintenance_service.list_backups() if is_platform_admin() else [],
                           is_platform_admin=is_platform_admin(),
                           clock=monitor_service.clock_status(g.tenant_id, force=request.args.get('clock') == '1'),
                           backup_cfg=settings_service.get('backup'), monitor_cfg=settings_service.get('monitor'))


@system_bp.route('/admin/system/backup', methods=['POST'], endpoint='backup_now')
@role_required('owner')
@platform_admin_required
def backup_now():
    path = maintenance_service.run_sqlite_backup(settings_service.get('backup').get('keep_days') or 14)
    if path is None:
        raise AppError('自動備份只適用於門市 SQLite；線上端 PostgreSQL 請使用 docker compose 的 backup 服務')
    record_staff_audit('SYSTEM_BACKUP', details=f'手動建立備份 {os.path.basename(path)}')
    db.session.commit()
    flash(f'已建立備份 {os.path.basename(path)}', 'success')
    return redirect(url_for('system.status'))


@system_bp.route('/admin/system/backup/<name>', endpoint='download_backup')
@role_required('owner')
@platform_admin_required
def download_backup(name):
    if not (name.startswith('menu-') and name.endswith('.zip') and '/' not in name and '\\' not in name and '..' not in name):
        abort(404)
    return send_from_directory(maintenance_service.backup_dir(), name, as_attachment=True)


@system_bp.route('/admin/system/test_alert', methods=['POST'], endpoint='test_alert')
@role_required(*MANAGER_ROLES)
def test_alert():
    sent = monitor_service.send_alert_now(g.tenant_id, 'info', '這是一則測試通知 (系統監控頁)。')
    flash(f"已送出：{', '.join(sent)}" if sent else '尚未設定任何通知管道 (系統設定 → 監控與錯誤通知)',
          'success' if sent else 'warning')
    return redirect(url_for('system.status'))


@system_bp.route('/admin/system/update/download', methods=['POST'], endpoint='update_download')
@role_required('owner')
@platform_admin_required
def update_download():
    from app.services import update_service
    path = update_service.download(request.form.get('tag'))
    record_staff_audit('SYSTEM_UPDATE', details=f'下載更新檔 {os.path.basename(path)}')
    db.session.commit()
    flash(f'已下載 {os.path.basename(path)}；確認營業空檔後按「套用並重啟」(或在主機執行 deploy\\windows\\update_store.cmd)', 'success')
    return redirect(url_for('system.status'))


@system_bp.route('/admin/system/update/apply', methods=['POST'], endpoint='update_apply')
@role_required('owner')
@platform_admin_required
def update_apply():
    from app.services import update_service
    path = (request.form.get('path') or '').strip()
    record_staff_audit('SYSTEM_UPDATE', details=f'套用更新 {os.path.basename(path)} (伺服器將重啟)')
    db.session.commit()
    update_service.apply(path)
    flash('更新腳本已啟動：伺服器會在幾秒後停止並重啟 (約 1~3 分鐘)，請稍後重新整理；過程記錄在 database/updates/update.log', 'warning')
    return redirect(url_for('system.status'))
