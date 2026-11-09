"""
會計匯出 (#13)：

- 日結傳票：每天一張，依收款紀錄 (payment_record) 產生分錄
  · 結帳 / 桌位結帳收款：借 各付款方式科目 (儲值金、禮物卡為預收款項沖減)，貸 銷貨收入 + 銷項稅額 (售價內含稅)
  · 退款 (負數)：借 銷貨退回 + 銷項稅額，貸 原付款方式科目
  · 儲值 / 禮物卡售出：借 付款方式科目，貸 預收款項 (不是收入)
  · 儲值加值贈送：借 儲值贈送 (行銷費用)，貸 預收款項-儲值金 (會員花用時以含贈送的全額沖減預收)
  · 零用金存入 / 支出：借 現金 / 雜項費用，貸 零用金 / 現金
  每張傳票借貸平衡 (稅額 = 含稅金額 - round(含稅金額 / (1 + 稅率)))。
- 發票明細：依開立日期列出發票號碼、買方統編、銷售額、稅額、總計、狀態、折讓。
CSV 為 UTF-8 (含 BOM，Excel 直接開啟)；也可下載 Excel。科目代號在「系統設定 → 會計匯出科目」修改。
"""
import csv
import io
from collections import defaultdict
from datetime import timedelta

from openpyxl import Workbook
from openpyxl.styles import Font

from app.extensions import db
from app.models.enums import PaymentMethod
from app.models.member import WalletLedger
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus, PaymentKind, PaymentRecord
from app.services import settings_service
from app.utils import csv_safe, day_bounds, workbook_bytes

METHOD_ACCOUNT = {
    PaymentMethod.CASH: 'acct_cash', PaymentMethod.CREDIT_CARD: 'acct_card', PaymentMethod.APPLE_PAY: 'acct_card',
    PaymentMethod.LINE_PAY: 'acct_mobile', PaymentMethod.JKO_PAY: 'acct_mobile', PaymentMethod.ATM: 'acct_online',
    PaymentMethod.CVS: 'acct_online', PaymentMethod.PLATFORM: 'acct_platform', PaymentMethod.STORED_VALUE: 'acct_wallet',
    PaymentMethod.GIFT_CARD: 'acct_gift',
}
JOURNAL_HEADER = ['傳票日期', '傳票號碼', '序號', '科目代號', '科目名稱', '借方金額', '貸方金額', '摘要']
INVOICE_HEADER = ['發票號碼', '開立日期', '買方統編', '買方名稱', '銷售額', '稅額', '總計', '狀態', '折讓金額', '載具 / 捐贈',
                  '訂單編號']
INVOICE_STATUS_LABELS = {InvoiceStatus.ISSUED: '開立', InvoiceStatus.VOID: '作廢', InvoiceStatus.ALLOWANCE: '折讓'}
# 設定頁尚未提供的科目 (舊設定沒有這個欄位時的預設)
ACCOUNT_DEFAULTS = {'acct_bonus': '6150 儲值贈送 (行銷費用)', 'acct_deposit': '2153 預收款項-訂金'}
BONUS_MATCH_SECONDS = 120   # 加值贈送與加值收款在同一交易寫入，時間相差應在數秒內


def _account(cfg, key):
    """'1111 現金' → ('1111', '現金')"""
    raw = str(cfg.get(key) or ACCOUNT_DEFAULTS.get(key) or '').strip()
    code, _, name = raw.partition(' ')
    return (code, name.strip() or code) if name else ('', raw)


def split_tax(amount, rate):
    """含稅金額 → (未稅, 稅額)"""
    net = round(amount / (1 + rate / 100)) if rate else amount
    return net, amount - net


def journal(tenant_id, start, end, node_uuid=None):
    """[{date, no, lines: [{code, name, debit, credit, memo}], debit, credit}]"""
    cfg = settings_service.get('accounting', tenant_id)
    rate = int(cfg.get('tax_rate') or 0)
    t0, t1 = day_bounds(start, end)
    q = PaymentRecord.query.filter(PaymentRecord.tenant_id == tenant_id, PaymentRecord.created_at >= t0,
                                   PaymentRecord.created_at < t1)
    if node_uuid:
        q = filter_payments_by_node(q, node_uuid)
    days = defaultdict(lambda: {'debit': defaultdict(int), 'credit': defaultdict(int), 'sales': 0, 'returns': 0})
    records = q.all()
    for p, bonus in _topup_bonuses(tenant_id, records):
        # 加值贈送：預收款項-儲值金要另外貸記贈送金額 (會員花用時以含贈送的全額沖減)，對方科目為行銷費用
        d = days[p.created_at.date()]
        d['debit']['acct_bonus'] += bonus
        d['credit']['acct_wallet'] += bonus
    for p in records:
        d = days[p.created_at.date()]
        acct = METHOD_ACCOUNT.get(p.method, 'acct_cash')
        amt = int(p.amount or 0)
        if p.kind in (PaymentKind.SALE, PaymentKind.SETTLE):
            if amt >= 0:
                d['debit'][acct] += amt
                d['sales'] += amt
            else:
                d['credit'][acct] += -amt
                d['returns'] += -amt
        elif p.kind == PaymentKind.REFUND:
            d['credit'][acct] += -amt if amt < 0 else amt
            d['returns'] += -amt if amt < 0 else amt
        elif p.kind in (PaymentKind.TOPUP, PaymentKind.GIFT_SALE):
            liability = 'acct_wallet' if p.kind == PaymentKind.TOPUP else 'acct_gift'
            if amt >= 0:
                d['debit'][acct] += amt
                d['credit'][liability] += amt
            else:   # 儲值 / 禮物卡退回
                d['debit'][liability] += -amt
                d['credit'][acct] += -amt
        elif p.kind == PaymentKind.DEPOSIT:
            # 訂位訂金：預收款項，不算營收
            if amt >= 0:
                d['debit'][acct] += amt
                d['credit']['acct_deposit'] += amt
            else:   # 退訂金
                d['debit']['acct_deposit'] += -amt
                d['credit'][acct] += -amt
        elif p.kind == PaymentKind.DEPOSIT_FORFEIT:
            # 訂金沒收：由預收轉為收入 (不是金流，不碰收款科目)，與沒收時開立的發票一致拆未稅與稅額
            if amt >= 0:
                d['debit']['acct_deposit'] += amt
                d['sales'] += amt
            else:   # 撤銷沒收
                d['credit']['acct_deposit'] += -amt
                d['returns'] += -amt
        elif p.kind == PaymentKind.PAY_IN:
            d['debit']['acct_cash'] += abs(amt)
            d['credit']['acct_petty'] += abs(amt)
        elif p.kind == PaymentKind.PAY_OUT:
            d['debit']['acct_misc'] += abs(amt)
            d['credit']['acct_cash'] += abs(amt)
    vouchers = []
    for day in sorted(days):
        d = days[day]
        if d['sales']:
            net, tax = split_tax(d['sales'], rate)
            d['credit']['acct_sales'] += net
            d['credit']['acct_tax'] += tax
        if d['returns']:
            net, tax = split_tax(d['returns'], rate)
            d['debit']['acct_returns'] += net
            d['debit']['acct_tax'] += tax
        no = f'JV{day:%Y%m%d}'
        lines = []
        memo = f'{day:%m/%d} 營業日結'
        for side in ('debit', 'credit'):
            for key in sorted(d[side], key=lambda k: _account(cfg, k)[0]):
                amount = d[side][key]
                if not amount:
                    continue
                code, name = _account(cfg, key)
                lines.append({'code': code, 'name': name, 'debit': amount if side == 'debit' else 0,
                              'credit': amount if side == 'credit' else 0, 'memo': memo})
        vouchers.append({'date': day, 'no': no, 'lines': lines, 'debit': sum(ln['debit'] for ln in lines),
                         'credit': sum(ln['credit'] for ln in lines)})
    return vouchers


def filter_payments_by_node(q, node_uuid):
    """
    收款紀錄依分店篩選：有訂單的以訂單的分店為準。沒有訂單的收款 (桌位結帳、儲值、禮物卡、零用金) 需要收款紀錄本身的
    node_uuid 欄位才能判斷 (欄位由遷移加入；尚未加入時這些收款無法歸屬分店，維持排除)。
    """
    own = getattr(PaymentRecord, 'node_uuid', None)
    if own is None:
        return q.join(Order, Order.id == PaymentRecord.order_id).filter(Order.node_uuid == node_uuid)
    return q.outerjoin(Order, Order.id == PaymentRecord.order_id) \
        .filter(db.func.coalesce(Order.node_uuid, own) == node_uuid)


def _topup_bonuses(tenant_id, records):
    """
    [(加值收款紀錄, 贈送金額)]：儲值金異動帳的加值贈送 (reason=bonus) 與加值收款 (TOPUP) 在同一交易寫入，
    以經手人與時間 (BONUS_MATCH_SECONDS 內最接近的一筆) 配對。只配對這次查詢到的收款紀錄：
    異動帳會在各主機間同步，其他分店給的贈送不會被算進本機 (或篩選的分店) 的傳票。
    """
    topups = [p for p in records if p.kind == PaymentKind.TOPUP and (p.amount or 0) > 0]
    if not topups:
        return []
    window = timedelta(seconds=BONUS_MATCH_SECONDS)
    lo, hi = min(p.created_at for p in topups), max(p.created_at for p in topups)
    rows = WalletLedger.query.filter(WalletLedger.tenant_id == tenant_id, WalletLedger.reason == 'bonus',
                                     WalletLedger.delta > 0, WalletLedger.created_at >= lo - window,
                                     WalletLedger.created_at <= hi + window).order_by(WalletLedger.id).all()
    out, used = [], set()
    for p in sorted(topups, key=lambda r: (r.created_at, r.id)):
        best = None
        for w in rows:
            if w.id in used or (w.staff_username or '') != (p.staff_username or ''):
                continue
            gap = abs((w.created_at - p.created_at).total_seconds())
            if gap <= BONUS_MATCH_SECONDS and (best is None or gap < best[0]):
                best = (gap, w)
        if best is not None:
            used.add(best[1].id)
            out.append((p, int(best[1].delta)))
    return out


def journal_rows(vouchers):
    for v in vouchers:
        for i, ln in enumerate(v['lines'], start=1):
            yield [v['date'].isoformat(), v['no'], i, ln['code'], ln['name'], ln['debit'], ln['credit'], ln['memo']]


def invoices(tenant_id, start, end, node_uuid=None):
    """發票明細列；node_uuid：只列該分店訂單的發票 (與傳票相同，以訂單的分店為準)"""
    cfg = settings_service.get('accounting', tenant_id)
    rate = int(cfg.get('tax_rate') or 0)
    t0, t1 = day_bounds(start, end)
    q = Invoice.query.filter(Invoice.tenant_id == tenant_id, Invoice.invoice_no != '', Invoice.invoice_date >= t0,
                             Invoice.invoice_date < t1)
    if node_uuid:
        q = q.join(Order, Order.id == Invoice.order_id).filter(Order.node_uuid == node_uuid)
    rows = q.order_by(Invoice.invoice_date, Invoice.invoice_no).all()
    out = []
    for inv in rows:
        net, tax = split_tax(inv.amount or 0, rate)
        void = inv.status == InvoiceStatus.VOID
        out.append([inv.invoice_no, inv.invoice_date.strftime('%Y-%m-%d'), inv.buyer_identifier or '',
                    inv.buyer_name or '', 0 if void else net, 0 if void else tax, 0 if void else inv.amount,
                    INVOICE_STATUS_LABELS.get(inv.status, inv.status), inv.allowance_amount or 0,
                    '' if inv.carrier_type == 'print' else inv.carrier_label, inv.order_id or ''])
    return out


def to_csv(header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows([[csv_safe(v) for v in r] for r in rows])
    return io.BytesIO(('﻿' + buf.getvalue()).encode('utf-8'))


def to_xlsx(header, rows, title):
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append(list(r))
    return workbook_bytes(wb)
