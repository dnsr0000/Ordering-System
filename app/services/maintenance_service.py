"""
維運：門市資料庫 (SQLite) 每日自動備份、系統健康狀態、背景排程。

背景排程 (每個行程一條執行緒；測試與 CLI 不啟動)：
- 每 5 秒：送出列印佇列、送出監控通知
- 每 5 分鐘：重送待開立的電子發票、重試失敗的 LINE Pay 退款
- 每 10 分鐘：時鐘誤差檢查 (誤差過大時通知)
- 每 15 秒：Webhook 事件與送出
- 每分鐘：排程推播、訂位逾時與提醒；每天：自動備份、生日禮、點數到期、自動行銷、定期報表、員工異常通知、每月封存
多個 gunicorn worker：列印與監控通知每個行程各自處理 (列印以條件式更新先「搶」再印；通知佇列在行程記憶體中)，
其餘排程只由「排程主控」行程執行 (以 sync_state 的租約選出，30 秒未續約由其他行程接手)，避免發券、推播、Webhook 重複。
"""
import glob
import logging
import os
import shutil
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta

from flask import current_app

from app.extensions import db
from app.services import settings_service

logger = logging.getLogger(__name__)
_started = {'flag': False}
_LEADER_KEY = 'scheduler_leader'
_PROCESS_TOKEN = uuid.uuid4().hex
LEADER_LEASE_SECONDS = 30


def hold_leadership(now=None, token=None):
    """取得 / 續約「排程主控」租約；回傳本行程是否為主控 (同一時間只有一個行程執行排程工作)"""
    import sqlalchemy as sa
    from sqlalchemy.exc import IntegrityError
    from app.models.sync import SyncState
    now = now or datetime.now()
    token = token or _PROCESS_TOKEN
    table = SyncState.__table__
    renewed = db.session.execute(
        sa.update(table).where(table.c.key == _LEADER_KEY,
                               (table.c.value == token) | (table.c.updated_at < now - timedelta(seconds=LEADER_LEASE_SECONDS)))
        .values(value=token, updated_at=now)).rowcount
    if renewed:
        db.session.commit()
        return True
    exists = db.session.execute(sa.select(table.c.key).where(table.c.key == _LEADER_KEY)).first() is not None
    if exists:
        db.session.commit()
        return False
    try:
        db.session.execute(sa.insert(table).values(key=_LEADER_KEY, value=token, updated_at=now))
        db.session.commit()
        return True
    except IntegrityError:   # 另一個行程剛好同時建立
        db.session.rollback()
        return False


# ---------------------------------------------------------------------------
# 自動備份 (SQLite)
# ---------------------------------------------------------------------------
def backup_dir():
    return os.path.join(current_app.config['DATABASE_DIR'], 'backups')


def run_sqlite_backup(keep_days=14):
    """建立 database/backups/menu-YYYYmmdd-HHMMSS.zip (含一致性快照) 並刪除超過保留天數的備份；回傳檔案路徑"""
    from app.services.backup_service import _resolve_live_db_path, _snapshot_database
    if db.engine.dialect.name != 'sqlite':
        return None
    folder = backup_dir()
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    tmp = os.path.join(folder, f'.menu-{stamp}.db')
    target = os.path.join(folder, f'menu-{stamp}.zip')
    _snapshot_database(_resolve_live_db_path(), tmp)
    try:
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp, 'menu.db')
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    prune_backups(keep_days)
    return target


def prune_backups(keep_days):
    cutoff = time.time() - keep_days * 86400
    for path in glob.glob(os.path.join(backup_dir(), 'menu-*.zip')):
        if os.path.getmtime(path) < cutoff:
            try:
                os.remove(path)
            except OSError:
                pass


def list_backups():
    files = sorted(glob.glob(os.path.join(backup_dir(), 'menu-*.zip')), reverse=True) if os.path.isdir(backup_dir()) else []
    return [{'name': os.path.basename(p), 'size': os.path.getsize(p),
             'at': datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M')} for p in files]


# ---------------------------------------------------------------------------
# 健康狀態
# ---------------------------------------------------------------------------
def health(tenant_id):
    from app.models.payment import Invoice, InvoiceStatus, Refund
    from app.models.printing import PrintJob
    from app.sync import runtime as sync_runtime
    info = {'dialect': db.engine.dialect.name, 'role': sync_runtime.role(), 'db_ok': True, 'db_size': None,
            'disk_free': None, 'revision': ''}
    try:
        info['revision'] = db.session.execute(db.text('SELECT version_num FROM alembic_version')).scalar() or ''
    except Exception:  # noqa: BLE001
        db.session.rollback()
        info['db_ok'] = False
    url = db.engine.url
    if info['dialect'] == 'sqlite' and url.database and os.path.exists(url.database):
        info['db_size'] = os.path.getsize(url.database)
    try:
        info['disk_free'] = shutil.disk_usage(current_app.config['DATABASE_DIR']).free
    except OSError:
        pass
    info['invoice_pending'] = Invoice.query.filter(Invoice.status == InvoiceStatus.PENDING).count()
    info['invoice_failed'] = Invoice.query.filter(Invoice.status == InvoiceStatus.FAILED).count()
    info['print_failed'] = PrintJob.query.filter(PrintJob.status == 'failed').count()
    info['print_queued'] = PrintJob.query.filter(PrintJob.status == 'queued').count()
    info['refund_manual'] = Refund.query.filter(Refund.status.in_(('manual', 'failed'))).count()
    if sync_runtime.is_node():
        info['sync_last_ok'] = sync_runtime.get_state('last_success_at', '')
        info['sync_last_error'] = sync_runtime.get_state('last_error', '')
    return info


# ---------------------------------------------------------------------------
# 背景排程
# ---------------------------------------------------------------------------
def _daily_marker(key):
    from app.sync.runtime import get_state, set_state
    return get_state(f'job:{key}', ''), lambda value: set_state(f'job:{key}', value)


def _due_today(key, at_hm, now):
    """今天設定時間已到、且今天還沒執行過 (記錄在 sync_state，所有行程共用)"""
    last, mark = _daily_marker(key)
    if last == now.date().isoformat() or now.strftime('%H:%M') < (at_hm or '00:00'):
        return False, mark
    return True, mark


def _tenant_ids():
    from app.models.tenant import Tenant
    return [t.id for t in Tenant.query.filter_by(is_active=True).all()]


def run_daily_jobs(now=None):
    """每日工作 (排程每分鐘呼叫；也可由 CLI 手動執行)"""
    from app.services import report_service, wallet_service
    from app.sync import runtime as sync_runtime
    now = now or datetime.now()
    done = []
    for tid in _tenant_ids():
        cfg = settings_service.get('backup', tid)
        due, mark = _due_today(f'backup', cfg.get('time') or '03:30', now)
        if cfg.get('enabled', True) and due and db.engine.dialect.name == 'sqlite':
            mark(now.date().isoformat())
            db.session.commit()
            try:
                run_sqlite_backup(cfg.get('keep_days') or 14)
                done.append('backup')
            except Exception as exc:  # noqa: BLE001
                from app.services import monitor_service
                monitor_service.alert(f'自動備份失敗：{exc}', key='auto-backup', tenant_id=tid)
        if not sync_runtime.is_node():
            due, mark = _due_today(f'birthday:{tid}', '09:00', now)
            if due:
                mark(now.date().isoformat())
                db.session.commit()
                if wallet_service.grant_birthday_gifts(tid, now.date()):
                    done.append('birthday')
            due, mark = _due_today(f'points_expiry:{tid}', '04:00', now)
            if due:   # 點數到期 (先進先出) 與到期前提醒：只在線上端 / 單機執行
                mark(now.date().isoformat())
                db.session.commit()
                from app.services import loyalty_service
                if loyalty_service.expire_points(tid, now):
                    done.append('points_expiry')
                if loyalty_service.notify_expiring(tid, now):
                    done.append('points_notice')
            from app.services import marketing_service
            if marketing_service.run_due(tid, now):   # 排程推播 (時間到就發)
                done.append('marketing_broadcast')
            due, mark = _due_today(f'marketing:{tid}', '10:00', now)
            if due:   # 久未消費自動發券
                mark(now.date().isoformat())
                db.session.commit()
                if marketing_service.run_winbacks(tid, now):
                    done.append('marketing_winback')
        from app.services import deposit_service
        if deposit_service.run_due(tid, now):   # 訂位：逾時未付訂金自動取消、訂位前提醒 (每分鐘)
            done.append('reservations')
        if not sync_runtime.is_hub():
            from app.services import group_order_service
            if any(group_order_service.run_due(tid, now)):   # 團體預購：截止自動送單 (每分鐘)
                done.append('group_orders')
        due, mark = _due_today(f'lot_expiry:{tid}', '07:00', now)
        if due:   # 原料批號效期提醒 (門市本機)
            mark(now.date().isoformat())
            db.session.commit()
            from app.services import inventory_service
            if inventory_service.check_expiring(tid, now.date()):
                done.append('lot_expiry')
        if now.day == 1:
            due, mark = _due_today(f'archive:{tid}', '04:30', now)
            if due and settings_service.get('archive', tid).get('auto'):   # 每月 1 日自動封存
                mark(now.date().isoformat())
                db.session.commit()
                from app.services import archive_service
                try:
                    if archive_service.run_monthly(tid, now.date()):
                        done.append('archive')
                except Exception as exc:  # noqa: BLE001
                    db.session.rollback()
                    from app.services import monitor_service
                    monitor_service.alert(f'自動封存失敗：{exc}', key=f'archive-{tid}', tenant_id=tid)
        acfg = settings_service.get('anomaly', tid)
        if acfg.get('notify'):
            due, mark = _due_today(f'anomaly:{tid}', acfg.get('notify_time') or '08:30', now)
            if due:   # 員工異常 (前一天)
                mark(now.date().isoformat())
                db.session.commit()
                from app.services import staff_anomaly_service
                if staff_anomaly_service.notify_daily(tid, now.date()):
                    done.append('anomaly')
        rcfg = settings_service.get('report_email', tid)
        if rcfg.get('enabled'):
            due, mark = _due_today(f'report:{tid}', rcfg.get('send_time') or '08:00', now)
            if due:
                mark(now.date().isoformat())
                db.session.commit()
                try:
                    if report_service.send_scheduled_report(tid, now.date()):
                        done.append('report')
                except Exception as exc:  # noqa: BLE001
                    from app.services import monitor_service
                    monitor_service.alert(f'定期報表寄送失敗：{exc}', key=f'report-{tid}', tenant_id=tid)
    return done


def check_clock():
    from app.services import monitor_service
    for tid in _tenant_ids()[:1]:
        status = monitor_service.clock_status(tid, force=True)
        if status['warn']:
            monitor_service.alert(f"主機時鐘誤差 {status['offset']} 秒 (標準時間：{status['server']})，請校正電腦時間",
                                  key='clock-drift', tenant_id=tid, level='warning')


def _loop(app):
    from app.services import einvoice_ecpay, monitor_service, print_service
    from app.models.payment import Refund
    ticks = 0
    while True:
        time.sleep(5)
        ticks += 1
        with app.app_context():
            try:
                print_service.process_queue()
                monitor_service.flush_alerts()
                if not hold_leadership():    # 其他行程是排程主控
                    continue
                if ticks % 3 == 0:           # 每 15 秒：Webhook 事件與送出
                    from app.services import webhook_service
                    webhook_service.run_all()
                if ticks % 12 == 0:          # 每分鐘
                    run_daily_jobs()
                if ticks % 60 == 0:          # 每 5 分鐘
                    einvoice_ecpay.retry_pending()
                    from app.services import payment_service
                    for r in Refund.query.filter_by(status='pending').execution_options(skip_tenant_filter=True).limit(20).all():
                        if r.created_at < datetime.now() - timedelta(minutes=2):
                            payment_service.process_refund(r.id)
                    payment_service.run_auto_refunds()
                if ticks % 120 == 1:         # 每 10 分鐘
                    check_clock()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                logger.exception('背景排程執行失敗 (下一輪重試)')
            finally:
                db.session.remove()


def start_scheduler(app):
    if _started['flag'] or app.testing or not app.config.get('BACKGROUND_JOBS', True):
        return
    _started['flag'] = True
    threading.Thread(target=_loop, args=(app,), name='kiosk-scheduler', daemon=True).start()
