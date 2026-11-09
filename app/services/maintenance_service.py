"""
維運：門市資料庫 (SQLite) 每日自動備份、系統健康狀態、背景排程。

背景排程 (每個行程一條執行緒；測試與 CLI 不啟動)：
- 每 5 秒：送出列印佇列、送出監控通知
- 每 5 分鐘：重送待開立的電子發票、重試失敗的 LINE Pay 退款、卡在「退款中」的退款改為失敗並通知
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
from datetime import datetime, timedelta

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


_backup_lock = threading.Lock()


def run_sqlite_backup(keep_days=14):
    """
    建立 database/backups/menu-YYYYmmdd-HHMMSS.zip (menu.db 與舊資料封存檔 archive/*.db 的一致性快照)
    並刪除超過保留天數的備份；回傳檔案路徑。
    同一行程內的備份依序執行 (連按兩下「立即備份」、手動備份碰上排程備份)；快照與 zip 先寫入唯一的暫存檔，
    完成後才換成正式檔名，不會有兩個備份寫同一個檔案、也不會留下寫到一半的 zip。
    """
    import tempfile
    from app.services.backup_service import _resolve_live_db_path, _snapshot_database, write_archive_snapshots
    if db.engine.dialect.name != 'sqlite':
        return None
    folder = backup_dir()
    os.makedirs(folder, exist_ok=True)
    with _backup_lock:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        target = os.path.join(folder, f'menu-{stamp}.zip')
        fd, tmp = tempfile.mkstemp(prefix=f'.menu-{stamp}-', suffix='.db', dir=folder)
        os.close(fd)
        fd, tmp_zip = tempfile.mkstemp(prefix=f'.menu-{stamp}-', suffix='.zip.tmp', dir=folder)
        os.close(fd)
        try:
            _snapshot_database(_resolve_live_db_path(), tmp)
            with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp, 'menu.db')
                with tempfile.TemporaryDirectory(prefix=f'.menu-{stamp}-', dir=folder) as snap_dir:
                    write_archive_snapshots(zf, snap_dir)   # 封存檔 (database/archive/*.db)：沒有就略過
            os.replace(tmp_zip, target)
        finally:
            for leftover in (tmp, tmp_zip):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        prune_backups(keep_days, keep=target)
    return target


def prune_backups(keep_days, keep=None):
    """刪除超過保留天數的備份；最新的一份 (以及 keep 指定的檔案) 一律保留，保留天數至少 1 天"""
    keep_days = max(1, int(keep_days or 0))
    cutoff = time.time() - keep_days * 86400
    paths = sorted(glob.glob(os.path.join(backup_dir(), 'menu-*.zip')), key=os.path.getmtime)
    keep_paths = {os.path.abspath(p) for p in paths[-1:]} | ({os.path.abspath(keep)} if keep else set())
    for path in paths:
        if os.path.abspath(path) in keep_paths:
            continue
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
    from app.services.cv_service import face_status
    info['face_ok'], info['face_reason'] = face_status()
    if sync_runtime.is_node():
        info['sync_last_ok'] = sync_runtime.get_state('last_success_at', '')
        info['sync_last_error'] = sync_runtime.get_state('last_error', '')
    from app.services import ai_service
    info['ai'] = ai_service.ai_status()
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


def _platform_tenant_id():
    """平台 (預設店家 slug=default) 的店家 id；沒有預設店家時用第一家啟用中的店家"""
    from app.models.tenant import Tenant
    t = Tenant.query.filter_by(slug='default').first()
    if t is not None:
        return t.id
    ids = _tenant_ids()
    return ids[0] if ids else None


def _run_daily_backup(now, done):
    """
    每日整庫備份 (SQLite)：備份含所有店家的資料，只依平台 (預設店家) 的「自動備份」設定執行一次，
    異地上傳也只用平台的設定 (其他店家的同名設定不會拿到整庫備份、也不能清掉平台的本機備份)。
    """
    if db.engine.dialect.name != 'sqlite':
        return
    tid = _platform_tenant_id()
    if tid is None:
        return
    cfg = settings_service.get('backup', tid)
    due, mark = _due_today('backup', cfg.get('time') or '03:30', now)
    if not (cfg.get('enabled', True) and due):
        return
    mark(now.date().isoformat())
    db.session.commit()
    try:
        path = run_sqlite_backup(cfg.get('keep_days') or 14)
        done.append('backup')
    except Exception as exc:  # noqa: BLE001
        from app.services import monitor_service
        monitor_service.alert(f'自動備份失敗：{exc}', key='auto-backup', tenant_id=tid)
    else:
        from app.services import offsite_backup_service
        offsite_backup_service.upload_after_backup(path, tid)   # 異地備份 (失敗只通知)


def run_daily_jobs(now=None):
    """每日工作 (排程每分鐘呼叫；也可由 CLI 手動執行)"""
    from app.services import report_service, wallet_service
    from app.sync import runtime as sync_runtime
    now = now or datetime.now()
    done = []
    tenant_ids = _tenant_ids()
    if tenant_ids:
        _run_daily_backup(now, done)
    for tid in tenant_ids:
        if not sync_runtime.is_node():
            due, mark = _due_today(f'birthday:{tid}', '09:00', now)
            if due:
                mark(now.date().isoformat())
                db.session.commit()
                if wallet_service.grant_birthday_gifts(tid, now.date()):
                    done.append('birthday')
            due, mark = _due_today(f'push_cleanup:{tid}', '04:30', now)
            if due:   # Web Push 訂閱：訂單早已結束的訂閱清掉
                mark(now.date().isoformat())
                db.session.commit()
                from app.services import push_service
                if push_service.cleanup(now):
                    db.session.commit()
                    done.append('push_cleanup')
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


def _local_refunds(q):
    """
    線上端不處理分店上傳的分店訂單退款 (由分店自己的 LINE Pay 設定退款；線上端再退一次會重複退款，
    或把狀態改成失敗後讓分店之後同步上來的完成狀態被忽略)；線上點餐訂單由線上端收款，仍由線上端處理。
    """
    from sqlalchemy import or_
    from app.models.enums import OrderSource
    from app.models.order import Order
    from app.models.payment import Refund
    from app.sync import runtime as sync_runtime
    if sync_runtime.is_hub():
        q = q.outerjoin(Order, Order.id == Refund.order_id).filter(
            or_(Order.node_uuid.is_(None), Order.order_source == OrderSource.ONLINE))
    return q


def retry_pending_refunds(now=None, limit=20):
    """重試建立超過 2 分鐘仍在處理中的線上退款 (延後工作遺失：重啟、當機)；線上端只處理本機的退款"""
    from app.models.payment import Refund
    from app.services import payment_service
    now = now or datetime.now()
    q = _local_refunds(Refund.query.filter(Refund.status == 'pending', Refund.created_at < now - timedelta(minutes=2)))
    ids = [r.id for r in q.order_by(Refund.id).execution_options(skip_tenant_filter=True).limit(limit).all()]
    for rid in ids:
        payment_service.process_refund(rid)
    return ids


REFUND_STUCK_MINUTES = 30
REFUND_STUCK_RESULT = '退款中斷 (伺服器重新啟動等)，請先到金流後台確認是否已退款再重試'
_processing_seen: dict = {}   # 退款 id → 本行程第一次看到它停在「退款中」的時間


def recover_stuck_refunds(now=None):
    """
    呼叫金流時行程重新啟動，退款會一直停在「退款中」(processing)，後台也無法重試。
    排程主控連續看到同一筆退款停在退款中超過 30 分鐘 (金流呼叫不會這麼久) 就改為「退款失敗」並通知；
    不自動重試：不知道金流端是否已經退款，需人工到金流後台確認後再按重試。線上端只處理本機的退款。
    (退款紀錄沒有更新時間欄位，以本行程的觀察時間判斷；換了排程主控行程時重新計時。)
    """
    from sqlalchemy import update
    from app.models.payment import Refund
    from app.services import monitor_service
    now = now or datetime.now()
    rows = _local_refunds(Refund.query.filter(Refund.status == 'processing')) \
        .order_by(Refund.id).execution_options(skip_tenant_filter=True).all()
    current = {r.id: r for r in rows}
    for rid in list(_processing_seen):
        if rid not in current:   # 已完成 / 失敗 (或不再處理)：不再追蹤
            _processing_seen.pop(rid, None)
    failed = []
    for rid, refund in current.items():
        first_seen = _processing_seen.setdefault(rid, now)
        if now - first_seen < timedelta(minutes=REFUND_STUCK_MINUTES):
            continue
        _processing_seen.pop(rid, None)
        changed = db.session.execute(
            update(Refund).where(Refund.id == rid, Refund.status == 'processing')
            .values(status='failed', provider_result=REFUND_STUCK_RESULT)
            .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
        db.session.commit()
        if changed:
            failed.append(rid)
            monitor_service.alert(f'退款中斷 (訂單 #{refund.order_id}，${refund.amount})：{REFUND_STUCK_RESULT}',
                                  key=f'refund-stuck-{rid}', tenant_id=refund.tenant_id)
    return failed


TABLE_PAY_STUCK_MINUTES = 10
TABLE_PAY_STUCK_NOTE = '結帳中斷 (伺服器重新啟動等)：顧客已付款，請確認這一桌的訂單是否已結帳，未結帳請退款或於櫃檯補結帳'


def recover_stuck_table_payments(now=None):
    """
    桌邊線上結帳 (綠界已收款) 結帳途中行程中斷會一直停在 processing：超過 10 分鐘改為 conflict 並通知店家確認 / 退款。
    搶到處理權時會同時寫入 paid_at，以它判斷逾時；paid_at 為空的是部署前就停住的舊資料，改用建立時間超過 60 分鐘判斷
    (付款頁時限 15 分鐘)。排程沒有店家情境，用條件式 UPDATE (同 recover_stuck_refunds)。
    """
    from sqlalchemy import and_, or_, update
    from app.models.table import TablePayment
    from app.services import monitor_service
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=TABLE_PAY_STUCK_MINUTES)
    rows = TablePayment.query.filter(
        TablePayment.status == 'processing',
        or_(TablePayment.paid_at < cutoff,
            and_(TablePayment.paid_at.is_(None), TablePayment.created_at < now - timedelta(minutes=60)))) \
        .order_by(TablePayment.id).execution_options(skip_tenant_filter=True).all()
    done = []
    for tp in rows:
        changed = db.session.execute(
            update(TablePayment).where(TablePayment.id == tp.id, TablePayment.status == 'processing')
            .values(status='conflict', note=TABLE_PAY_STUCK_NOTE)
            .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
        db.session.commit()
        if changed:
            done.append(tp.id)
            monitor_service.alert(
                f'桌邊線上結帳 {tp.trade_no} (${tp.amount}) 顧客已付款但結帳中斷，請確認訂單是否已結帳，'
                f'未結帳請到綠界後台退款 (交易 {tp.provider_trade_no or "未知"})',
                key=f'table-pay-stuck-{tp.id}', tenant_id=tp.tenant_id, level='warning', source='payment')
    return done


def _loop(app):
    from app.services import einvoice_ecpay, monitor_service, print_service
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
                    from app.services import menu_translate_service
                    menu_translate_service.run_due(app)   # Gemini 過載時沒翻成的菜單：時間到在背景執行緒補翻
                if ticks % 60 == 0:          # 每 5 分鐘
                    einvoice_ecpay.retry_pending()
                    from app.services import payment_service
                    retry_pending_refunds()
                    recover_stuck_refunds()
                    recover_stuck_table_payments()
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
