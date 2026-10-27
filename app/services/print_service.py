"""
出單：結帳後依設定列印收據 (收據印表機) 與廚房單 (各廚房印表機只印自己工作站的品項)。
列印工作先寫入 print_job，再由背景送出 (印表機離線時保留在佇列中重試，後台可看到失敗紀錄並重印)。
"""
import logging
from datetime import datetime

from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import PAYMENT_METHOD_LABELS
from app.models.order import Order
from app.models.printing import Printer, PrintJob
from app.models.tenant import Tenant
from app.services import escpos, kitchen_service, settings_service

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def _printers(tenant_id, role):
    return Printer.query.filter_by(tenant_id=tenant_id, role=role, is_active=True) \
        .execution_options(skip_tenant_filter=True).order_by(Printer.id).all()


def build_receipt(order, printer, title='交易明細'):
    shop = db.session.get(Tenant, order.tenant_id)
    cfg = settings_service.get('pos', order.tenant_id)
    t = escpos.Ticket(printer.paper_width, printer.encoding)
    t.text(shop.name if shop else '', bold=True, size=2, align='center')
    t.text(title, align='center')
    t.rule('=')
    t.text(f'取餐號碼 #{order.display_pickup_number:03d}', bold=True, size=2, align='center')
    t.row('單號', f'#{order.id}')
    t.row('時間', order.created_at.strftime('%Y-%m-%d %H:%M') if order.created_at else '')
    t.row('顧客/桌號', order.table_number or '')
    t.row('用餐方式', order.order_type or '')
    if order.cashier_username:
        t.row('收銀', order.cashier_username)
    t.rule()
    for it in order.items:
        t.row(f'{it.display_name} x{it.quantity}', f'${it.price * it.quantity}')
        if it.customization:
            t.text(f'  {it.customization}')
    t.rule()
    t.row('小計', f'${order.subtotal}')
    for label, value in (('促銷', order.promotion_discount), ('會員折扣', order.member_discount),
                         ('整單折扣', order.manual_discount)):
        if value:
            t.row(label, f'-${value}')
    other = (order.discount_amount or 0) - (order.promotion_discount or 0) - (order.member_discount or 0) \
        - (order.manual_discount or 0)
    if other > 0:
        t.row('優惠券/點數', f'-${other}')
    if order.service_charge:
        t.row('服務費', f'+${order.service_charge}')
    if order.delivery_fee:
        t.row('外送費', f'+${order.delivery_fee}')
    if order.delivery_address:
        t.text(f'外送：{order.delivery_address}', bold=True)
        t.text(f'電話：{order.delivery_phone}　區域：{order.delivery_zone}')
    t.row('應付總額', f'${order.total_price}', bold=True)
    t.row('付款方式', PAYMENT_METHOD_LABELS.get(order.payment_method, order.payment_method))
    if order.payment_method == 'Cash':
        t.row('實收', f'${order.cash_received or order.total_price}')
        t.row('找零', f'${order.cash_change or 0}')
    if order.payment_reference:
        t.row('禮物卡' if order.payment_method == '禮物卡' else '授權碼/交易序號', order.masked_payment_reference[:20])
    inv = order.invoice_summary()
    if inv and inv.get('invoice_no'):
        t.rule()
        t.row('電子發票', inv['invoice_no'])
        t.row('隨機碼', inv.get('random_number') or '')
        t.row('載具', inv.get('carrier') or '')
    if order.user and order.points_earned:
        t.row('本次贈點', f'+{order.points_earned}')
    t.rule()
    base = (shop.table_qr_base_url if shop else '') or ''
    if base:
        t.text('掃描 QR code 查看出餐進度', align='center')
        t.qr(f'{base.rstrip("/")}/track/{order.uuid}', size=5)
    t.text(cfg.get('receipt_footer') or '謝謝光臨', align='center')
    t.cut()
    return t.bytes()


def build_kitchen_ticket(order, printer, items, station_name=''):
    t = escpos.Ticket(printer.paper_width, printer.encoding)
    if order.is_rush:
        t.text('*** 加急 ***', bold=True, size=2, align='center')
    t.text(station_name or '廚房單', bold=True, align='center')
    t.text(f'#{order.display_pickup_number:03d}', bold=True, size=3, align='center')
    t.row(order.order_type or '', order.created_at.strftime('%H:%M') if order.created_at else '')
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
        resolve = kitchen_service.station_resolver()
        for p in _printers(tenant_id, 'kitchen'):
            items = kitchen_service.items_for_station(order, p.station_id, resolve) if p.station_id else list(order.items)
            if items:
                enqueue(p, f'廚房單 #{order.id}', build_kitchen_ticket(order, p, items, p.station.name if p.station else ''))
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
    escpos.send_raw(printer.host, printer.port, t.bytes())


def process_queue(tenant_id=None, limit=50):
    """送出佇列中的列印工作 (依建立順序)；回傳成功張數"""
    q = PrintJob.query.filter(PrintJob.status == 'queued')
    if tenant_id is not None:
        q = q.filter(PrintJob.tenant_id == tenant_id)
    jobs = q.order_by(PrintJob.id).limit(limit).execution_options(skip_tenant_filter=True).all()
    done = 0
    failed_printers = set()
    for job in jobs:
        if job.printer_id in failed_printers:
            continue
        # 先搶下這筆工作 (多個執行緒同時處理佇列時只印一次)
        claimed = db.session.execute(update(PrintJob).where(PrintJob.id == job.id, PrintJob.status == 'queued')
                                     .values(status='sending', attempts=PrintJob.attempts + 1)
                                     .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
        db.session.commit()
        if not claimed:
            continue
        db.session.refresh(job)
        printer = job.printer
        try:
            escpos.send_raw(printer.host, printer.port, job.payload)
            job.status, job.printed_at, job.last_error = 'done', datetime.now(), ''
            done += 1
        except OSError as exc:
            failed_printers.add(job.printer_id)
            job.last_error = f'{exc}'[:300]
            job.status = 'failed' if job.attempts >= MAX_ATTEMPTS else 'queued'
            if job.status == 'failed':
                from app.services import monitor_service
                monitor_service.alert(f'印表機「{printer.name}」({printer.host}) 無法列印：{exc}',
                                      key=f'printer-{printer.id}', tenant_id=job.tenant_id, level='warning')
        db.session.commit()
    return done


def retry_job(job):
    job.status, job.attempts, job.last_error = 'queued', 0, ''
