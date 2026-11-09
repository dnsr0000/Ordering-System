"""
出單：結帳後依設定列印收據 (收據印表機) 與廚房單 (各廚房印表機只印自己工作站的品項)。
列印工作先寫入 print_job，再由背景送出 (印表機離線時保留在佇列中重試，後台可看到失敗紀錄並重印)。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.order import Order
from app.models.printing import KitchenStation, Printer, PrintJob
from app.services import escpos, kitchen_service, receipt_service, settings_service

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
UNASSIGNED_LABEL = '未分站'


def _printers(tenant_id, role):
    return Printer.query.filter_by(tenant_id=tenant_id, role=role, is_active=True) \
        .execution_options(skip_tenant_filter=True).order_by(Printer.id).all()


def _active_stations(tenant_id):
    """本店啟用中的工作站：明確限定店家 (出單多在背景執行緒，沒有請求情境時自動店家過濾不會生效，會撈到別店的工作站)"""
    return KitchenStation.query.filter_by(tenant_id=tenant_id, is_active=True) \
        .execution_options(skip_tenant_filter=True) \
        .order_by(KitchenStation.sort_order, KitchenStation.id).all()


def _menu_item(item):
    from app.models.menu import MenuItem
    return db.session.get(MenuItem, item.menu_item_id) if item.menu_item_id else None


def _send(host, port, payload):
    """送到網路印表機；主機名稱格式錯誤 (例如 192.168..50) 時 socket 拋的是 UnicodeError / ValueError 而非 OSError，
    統一轉成 OSError，呼叫端 (列印佇列、測試列印) 才會當成「印表機無法連線」處理，不會讓整個請求 / 佇列中斷"""
    try:
        escpos.send_raw(host, port, payload)
    except ValueError as exc:   # UnicodeError 是 ValueError 的子類別
        raise OSError(f'印表機位址格式不正確 ({host}:{port})：{exc}') from exc


def build_receipt(order, printer, title='交易明細'):
    """熱感機收據：內容來自 receipt_service.receipt_rows (與電子收據同一份)，頁尾讀 POS 設定的 receipt_footer"""
    t = escpos.Ticket(printer.paper_width, printer.encoding)
    for row in receipt_service.receipt_rows(order, detailed=True, title=title):
        kind = row['kind']
        if kind == 'text':
            t.text(row['text'], **{k: row[k] for k in ('bold', 'size', 'align') if k in row})
        elif kind == 'row':
            t.row(row['label'], row['value'], bold=row.get('bold', False))
        elif kind == 'item':
            t.row(row['label'], row['value'])
        elif kind == 'rule':
            t.rule(row['char'])
        elif kind == 'track':
            t.text('掃描 QR code 查看出餐進度', align='center')
            t.qr(row['url'], size=5)
    t.text(settings_service.get('pos', order.tenant_id).get('receipt_footer') or '謝謝光臨', align='center')
    t.cut()
    return t.bytes()


def build_kitchen_ticket(order, printer, items, station_name=''):
    t = escpos.Ticket(printer.paper_width, printer.encoding)
    if order.is_rush:
        t.text('*** 加急 ***', bold=True, size=2, align='center')
    t.text(station_name or '廚房單', bold=True, align='center')
    t.text(f'#{order.display_pickup_number:03d}', bold=True, size=3, align='center')
    if order.pickup_at:   # 預約單 (線上預約取餐 / 團購預購)：廚房才知道不是馬上要做
        t.text(f'預約 {order.pickup_at_label} 取餐', bold=True, size=2, align='center')
    t.row(order.order_type or '',order.created_at.strftime('%H:%M') if order.created_at else '')
    t.row('桌號/顧客', order.table_number or '')
    t.rule()
    for it in items:
        t.text(f'{it.item_name} x{it.quantity}', bold=True, size=2)
        if it.customization:
            t.text(f'  {it.customization}')
    if order.note:
        t.rule()
        t.text(f'備註：{order.note}', bold=True)
    t.beep().cut()
    return t.bytes()


def enqueue(printer, title, payload, copies=None):
    for _ in range(max(1, copies or printer.copies or 1)):
        db.session.add(PrintJob(tenant_id=printer.tenant_id, printer_id=printer.id, title=title[:100], payload=payload))


def print_order(order_id, tenant_id, receipt=None, kitchen=None):
    """結帳後的出單 (交易提交後執行)；receipt / kitchen 為 None 時依後台設定"""
    order = db.session.get(Order, order_id)
    if order is None:
        return 0
    cfg = settings_service.get('pos', tenant_id)
    receipt = cfg.get('auto_print_receipt') if receipt is None else receipt
    kitchen = cfg.get('auto_print_kitchen', True) if kitchen is None else kitchen
    count = 0
    if receipt:
        for p in _printers(tenant_id, 'receipt'):
            enqueue(p, f'收據 #{order.id}', build_receipt(order, p))
            count += 1
    if kitchen:
        stations = _active_stations(tenant_id)
        active_ids = {s.id for s in stations}
        resolve = kitchen_service.station_resolver(stations)
        printers = _printers(tenant_id, 'kitchen')
        for p in printers:
            if p.station_id is None:
                items = kitchen_service.items_for_station(order, None, resolve)   # 含套餐配餐的虛擬明細
            elif p.station_id in active_ids:
                items = kitchen_service.items_for_station(order, p.station_id, resolve)
            else:
                continue   # 綁定的工作站已停用：改印下面的「未分站」品項
            if items:
                enqueue(p, f'廚房單 #{order.id}', build_kitchen_ticket(order, p, items, p.station.name if p.station else ''))
                count += 1
        # 不屬於任何啟用中工作站的品項 (分類沒有工作站負責、指定的工作站已停用、餐點已刪除)：
        # 沒有「全部品項」的廚房印表機時，交給綁定停用工作站的印表機，都沒有就印在第一台廚房印表機 (標示未分站)
        if printers and all(p.station_id is not None for p in printers):
            unassigned = [it for it in kitchen_service.items_for_station(order, None, resolve)
                          if resolve(_menu_item(it)) is None]
            if unassigned:
                for p in [p for p in printers if p.station_id not in active_ids] or printers[:1]:
                    enqueue(p, f'廚房單 #{order.id}', build_kitchen_ticket(order, p, unassigned, UNASSIGNED_LABEL))
                    count += 1
    db.session.commit()
    if count:
        process_queue(tenant_id)
    return count


def reprint_receipt(order, title='補印收據'):
    printers = _printers(order.tenant_id, 'receipt')
    if not printers:
        raise AppError('尚未設定收據印表機 (系統設定 → 印表機與廚房工作站)')
    for p in printers:
        enqueue(p, f'{title} #{order.id}', build_receipt(order, p, title))
    db.session.commit()
    return process_queue(order.tenant_id)


def print_text_document(tenant_id, title, lines):
    """列印一般文字報表 (交班報表等) 到第一台收據印表機"""
    printers = _printers(tenant_id, 'receipt')
    if not printers:
        raise AppError('尚未設定收據印表機')
    p = printers[0]
    t = escpos.Ticket(p.paper_width, p.encoding)
    t.text(title, bold=True, size=2, align='center').rule('=')
    for line in lines:
        if isinstance(line, tuple):
            t.row(str(line[0]), str(line[1]))
        elif line == '-':
            t.rule()
        else:
            t.text(str(line))
    t.cut()
    enqueue(p, title, t.bytes(), copies=1)
    db.session.commit()
    return process_queue(tenant_id)


def drawer_printers(tenant_id):
    return [p for p in _printers(tenant_id, 'receipt') if p.drawer]


def open_drawer(tenant_id, reason=''):
    """送出開錢櫃指令到有接錢櫃的收據印表機；回傳台數 (沒有設定錢櫃時為 0)"""
    printers = drawer_printers(tenant_id)
    for p in printers:
        t = escpos.Ticket(p.paper_width, p.encoding).drawer()
        enqueue(p, f'開錢櫃 {reason}'.strip()[:100], t.bytes(), copies=1)
    if printers:
        db.session.commit()
        process_queue(tenant_id)
    return len(printers)


def test_print(printer):
    t = escpos.Ticket(printer.paper_width, printer.encoding)
    t.text('列印測試', bold=True, size=2, align='center')
    t.text(f'印表機：{printer.name}')
    t.text(f'位址：{printer.host}:{printer.port}')
    t.text(f'時間：{datetime.now():%Y-%m-%d %H:%M:%S}')
    t.row('中文測試 一二三', '$123')
    t.qr('https://example.com/test', size=4)
    t.cut()
    _send(printer.host, printer.port, t.bytes())


STUCK_AFTER = timedelta(minutes=2)   # 送出一張單只要幾秒；「送出中」超過這麼久 = 行程在送出途中中止


def recover_stuck_jobs(tenant_id=None):
    """
    送出途中伺服器重啟 / 當掉而卡在「送出中」的工作改為失敗 (後台可按「重印」)：原本會永遠卡住，廚房單就此遺失。
    不直接改回待送，避免與真的還在送的工作重複列印 (那一筆送完時會自己改成已完成)。
    """
    q = update(PrintJob).where(PrintJob.status == 'sending', PrintJob.updated_at < datetime.now() - STUCK_AFTER)
    if tenant_id is not None:
        q = q.where(PrintJob.tenant_id == tenant_id)
    n = db.session.execute(q.values(status='failed', last_error='送出途中中斷 (伺服器重啟？)，請確認後重印')
                           .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    if n:
        db.session.commit()
    return n


def process_queue(tenant_id=None, limit=50):
    """
    送出佇列中的列印工作 (依建立順序，每次查 limit 筆)；回傳成功張數。
    本輪已失敗的印表機之後的工作不再查詢 (下一輪再試)：否則一台離線印表機累積超過 limit 筆時會佔滿查詢範圍，
    其他印表機 (例如廚房單) 的工作永遠輪不到。
    """
    recover_stuck_jobs(tenant_id)
    done = 0
    failed_printers = set()
    last_id = 0
    while True:
        q = PrintJob.query.filter(PrintJob.status == 'queued', PrintJob.id > last_id)
        if tenant_id is not None:
            q = q.filter(PrintJob.tenant_id == tenant_id)
        if failed_printers:
            q = q.filter(PrintJob.printer_id.notin_(failed_printers))
        jobs = q.order_by(PrintJob.id).limit(limit).execution_options(skip_tenant_filter=True).all()
        for job in jobs:
            last_id = job.id
            if job.printer_id in failed_printers:
                continue
            done += _send_job(job, failed_printers)
        if len(jobs) < limit:
            return done


def _send_job(job, failed_printers):
    """送出一筆列印工作；成功回傳 1。失敗時把印表機加入 failed_printers (本輪不再送這台)"""
    # 先搶下這筆工作 (多個執行緒同時處理佇列時只印一次)
    claimed = db.session.execute(update(PrintJob).where(PrintJob.id == job.id, PrintJob.status == 'queued')
                                 .values(status='sending', attempts=PrintJob.attempts + 1)
                                 .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    db.session.commit()
    if not claimed:
        return 0
    db.session.refresh(job)
    printer = job.printer
    ok = 0
    try:
        _send(printer.host, printer.port, job.payload)
        job.status, job.printed_at, job.last_error = 'done', datetime.now(), ''
        ok = 1
    except OSError as exc:
        failed_printers.add(job.printer_id)
        job.last_error = f'{exc}'[:300]
        job.status = 'failed' if job.attempts >= MAX_ATTEMPTS else 'queued'
        if job.status == 'failed':
            from app.services import monitor_service
            monitor_service.alert(f'印表機「{printer.name}」({printer.host}) 無法列印：{exc}',
                                  key=f'printer-{printer.id}', tenant_id=job.tenant_id, level='warning')
    db.session.commit()
    return ok


def retry_job(job):
    job.status, job.attempts, job.last_error = 'queued', 0, ''
