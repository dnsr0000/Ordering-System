"""
收款紀錄、付款方式細節 (刷卡授權碼、LINE Pay、儲值金) 與作廢 / 退款。

- 每一筆收款或退款寫一筆 payment_record (退款為負數)，交班應有現金與報表付款方式統計都以此為準。
- 作廢 (整單取消) 由 order_service 的取消流程呼叫 on_order_cancelled；部分退款呼叫 partial_refund。
- 需要呼叫外部服務的步驟 (LINE Pay 退款、發票作廢 / 折讓) 在交易提交後執行 (deferred)，失敗可在後台重試。
"""
import logging
from datetime import datetime

from flask import has_request_context
from flask_login import current_user
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus, PaymentMethod, normalize_payment_method
from app.models.order import Order
from app.models.payment import Invoice, InvoiceStatus, PaymentKind, PaymentRecord, Refund
from app.models.shift import CashShift
from app.services import deferred, settings_service
from app.services.audit import record_staff_audit

logger = logging.getLogger(__name__)


def _staff_name():
    if has_request_context() and getattr(current_user, 'is_authenticated', False):
        return current_user.username
    return 'system'


def open_shift_id(tenant_id, lock=False):
    """
    目前開著的班別。lock=True (交易中寫入收款 / 訂單時)：以 FOR SHARE 讀取 (PostgreSQL)，
    正在關班的那一刻會等關班提交，之後讀到「沒有開著的班別」，不會掛在已算完報表的班別上。
    """
    query = CashShift.query.filter_by(tenant_id=tenant_id, status='open').execution_options(skip_tenant_filter=True) \
        .order_by(CashShift.id.desc())
    if lock:
        query = query.with_for_update(read=True)
    shift = query.first()
    return shift.id if shift else None


def record_payment(order, kind, method, amount, *, received=None, change=0, reference='', card_last4='', note='',
                   staff=None, settlement_id=None, tenant_id=None):
    tid = tenant_id or order.tenant_id
    rec = PaymentRecord(
        tenant_id=tid, order_id=order.id if order is not None else None, kind=kind, method=str(method),
        amount=int(amount), received=int(received if received is not None else amount), change=int(change or 0),
        reference=str(reference or '')[:60], card_last4=str(card_last4 or '')[:4], note=str(note or '')[:200],
        staff_username=staff or _staff_name(), shift_id=open_shift_id(tid, lock=True), settlement_id=settlement_id,
        created_at=datetime.now())
    db.session.add(rec)
    if str(method) == PaymentMethod.CASH and int(amount) != 0:
        _kick_drawer(tid, rec.staff_username, kind)
    return rec


DRAWER_SKIP_STAFF = {'kiosk', 'system', 'ecpay', 'platform'}
DRAWER_REASONS = {PaymentKind.SALE: '現金結帳', PaymentKind.SETTLE: '桌位結帳', PaymentKind.REFUND: '現金退款',
                  PaymentKind.TOPUP: '儲值收款', PaymentKind.PAY_IN: '零用金存入', PaymentKind.PAY_OUT: '零用金支出'}


def _kick_drawer(tenant_id, staff, kind):
    """櫃檯收 / 退現金 (結帳、桌位結帳、儲值、零用金、退款)：交易提交後自動開錢櫃 (自助機與系統工作不開)"""
    from app.services import print_service
    if staff in DRAWER_SKIP_STAFF or not settings_service.get('pos', tenant_id).get('open_drawer_on_cash', True):
        return
    if any(fn is print_service.open_drawer for fn, _a, _k in db.session.info.get(deferred._PENDING, [])):
        return   # 同一筆交易 (例如分開付款) 只開一次
    deferred.defer(print_service.open_drawer, tenant_id, DRAWER_REASONS.get(kind, ''))


# ---------------------------------------------------------------------------
# 結帳時的付款細節
# ---------------------------------------------------------------------------
def validate_card_details(payment_info, is_pos):
    """刷卡機記帳：依設定要求授權碼 / 卡號末四碼。回傳 (授權碼, 末四碼)"""
    info = payment_info if isinstance(payment_info, dict) else {}
    auth = ''.join(str(info.get('auth_code') or '').split()).upper()[:12]
    last4 = ''.join(ch for ch in str(info.get('card_last4') or '') if ch.isdigit())[:4]
    if not is_pos:
        return auth, last4
    cfg = settings_service.get('card_terminal')
    if cfg.get('require_auth_code') and not (auth.isalnum() and 4 <= len(auth) <= 12):
        raise AppError('請輸入刷卡機簽單上的授權碼 (4~12 碼英數字)', need_card_auth=True)
    if cfg.get('require_last4') and len(last4) != 4:
        raise AppError('請輸入信用卡末四碼', need_card_auth=True)
    return auth, last4


SPLIT_TENDER_METHODS = (PaymentMethod.CASH, PaymentMethod.CREDIT_CARD, PaymentMethod.STORED_VALUE, PaymentMethod.GIFT_CARD,
                        PaymentMethod.LINE_PAY, PaymentMethod.JKO_PAY, PaymentMethod.APPLE_PAY)


def parse_tenders(tenant_id, raw, user):
    """
    POS 混合付款的各筆付款 [{method, amount, auth_code / card_last4 (刷卡), gift_code (禮物卡)}]：
    2 ~ 5 筆、每筆金額 > 0；現金 / 儲值金 / 禮物卡各最多一筆；整合扣款的 LINE Pay 不可混合 (需當場向 LINE Pay 扣全額)。
    回傳 [{method, amount, reference, last4, gift_code}] (金額合計由結帳交易比對應付金額)。
    """
    from app.services import giftcard_service, linepay
    from app.utils import safe_int
    if not isinstance(raw, list) or not 2 <= len(raw) <= 5:
        raise AppError('混合付款請輸入 2 ~ 5 筆付款方式與金額')
    allowed = set(allowed_methods('pos'))
    out, seen = [], set()
    for t in raw:
        t = t if isinstance(t, dict) else {}
        method = normalize_payment_method(t.get('method'))
        amount = safe_int(t.get('amount'), 0)
        if method not in SPLIT_TENDER_METHODS or method not in allowed:
            raise AppError(f'混合付款不支援「{t.get("method")}」')
        if amount <= 0:
            raise AppError('混合付款的每一筆金額都必須大於 0')
        if method in (PaymentMethod.CASH, PaymentMethod.STORED_VALUE, PaymentMethod.GIFT_CARD) and method in seen:
            raise AppError(f'混合付款中「{method}」只能有一筆')
        seen.add(method)
        tender = {'method': method, 'amount': amount, 'reference': '', 'last4': '', 'gift_code': ''}
        if method == PaymentMethod.CREDIT_CARD:
            tender['reference'], tender['last4'] = validate_card_details(t, True)
        elif method == PaymentMethod.STORED_VALUE and user is None:
            raise AppError('使用儲值金付款請先綁定會員！')
        elif method == PaymentMethod.GIFT_CARD:
            tender['gift_code'] = giftcard_service.find(tenant_id, t.get('gift_code')).code
            tender['reference'] = f"{tender['gift_code'][:4]}****{tender['gift_code'][-4:]}"
        elif method == PaymentMethod.LINE_PAY and linepay.is_enabled(tenant_id):
            raise AppError('LINE Pay 整合扣款不支援混合付款，請改用單一 LINE Pay 付款或其他方式')
        out.append(tender)
    return out


def paid_by_method(order):
    """這張訂單各付款方式的淨收款 {method: 金額} (收款 - 退款，依收款紀錄)"""
    out = {}
    for method, amount in db.session.query(PaymentRecord.method, PaymentRecord.amount).filter(
            PaymentRecord.order_id == order.id,
            PaymentRecord.kind.in_((PaymentKind.SALE, PaymentKind.SETTLE, PaymentKind.REFUND))).all():
        out[method] = out.get(method, 0) + (amount or 0)
    return {m: a for m, a in out.items() if a > 0}


def allowed_methods(channel):
    """後台設定的各管道可用付款方式 (pos / kiosk)；啟用儲值金時 POS 一律可用儲值金"""
    cfg = settings_service.get('payments')
    methods = list(cfg.get('pos_methods' if channel == 'pos' else 'kiosk_methods') or ['Cash'])
    if channel == 'pos' and settings_service.get('member').get('wallet_enabled') and '儲值金' not in methods:
        methods.append('儲值金')
    if channel == 'pos' and settings_service.get('gift_card').get('enabled') and '禮物卡' not in methods:
        methods.append('禮物卡')
    return methods


def charge_linepay(order_id, one_time_key):
    """
    POS 已建立訂單 (payment_status='authorizing')，向 LINE Pay 扣款：
    成功 → 改為已付款並記錄收款；失敗 → 以系統身分取消訂單 (回補庫存、點數…) 並拋出錯誤給收銀員。
    """
    from app.services import linepay
    from app.services.order_service import system_cancel
    order = db.session.get(Order, order_id)
    try:
        tx = linepay.pay(order.tenant_id, order.uuid, order.total_price, one_time_key)
    except linepay.LinePayError as exc:
        system_cancel(order.id, order.tenant_id, f'LINE Pay 付款失敗：{exc.message}')
        raise AppError(exc.message, 402, payment_failed=True)
    db.session.execute(update(Order).where(Order.id == order.id, Order.payment_status == 'authorizing')
                       .values(payment_status='', payment_reference=tx).execution_options(synchronize_session=False))
    db.session.refresh(order)
    record_payment(order, PaymentKind.SALE, PaymentMethod.LINE_PAY, order.total_price, reference=tx)
    after_paid(order)
    db.session.commit()
    return order


def after_paid(order, invoice_info=None, create_invoice=True):
    """訂單完成收款：建立電子發票 (提交後開立) 與列印工作。由呼叫端 commit。"""
    if create_invoice:
        maybe_create_invoice(order, invoice_info)
    from app.services import print_service
    deferred.defer(print_service.print_order, order.id, order.tenant_id)


def maybe_create_invoice(order, invoice_info=None, status=InvoiceStatus.PENDING):
    from app.models.enums import DELIVERY_SOURCES
    from app.services import einvoice_ecpay
    cfg = settings_service.get('einvoice', order.tenant_id)
    if not cfg.get('enabled') or (order.total_price or 0) <= 0:
        return None
    if order.order_source in DELIVERY_SOURCES or order.payment_method == PaymentMethod.STORED_VALUE:
        return None   # 外送平台訂單由平台開立；儲值金已於加值時開立 (避免重複開立)
    existing = Invoice.query.filter(Invoice.order_id == order.id,
                                    Invoice.status.notin_((InvoiceStatus.VOID,))).first()
    if existing is not None:
        return existing
    amount = None
    if order.payment_method == PaymentMethod.SPLIT and (order.wallet_used or 0) > 0:
        amount = (order.total_price or 0) - order.wallet_used   # 混合付款中的儲值金已於加值時開立發票
        if amount <= 0:
            return None
    inv = einvoice_ecpay.create_invoice(order, invoice_info or {}, status=status, amount=amount)
    db.session.flush()
    if status == InvoiceStatus.PENDING and cfg.get('auto_issue', True):
        deferred.defer(einvoice_ecpay.issue_invoice, inv.id)
    return inv


# ---------------------------------------------------------------------------
# 作廢與退款
# ---------------------------------------------------------------------------
def ecpay_integrated(order, method=None):
    """線上端、綠界信用卡 / Apple Pay 付款的線上訂單：可由系統向綠界退刷"""
    from app.models.enums import OrderSource
    from app.services import payment_ecpay
    method = method or order.payment_method
    return (payment_ecpay.is_enabled() and order.order_source == OrderSource.ONLINE and bool(order.payment_trade_no)
            and method in (PaymentMethod.CREDIT_CARD, PaymentMethod.APPLE_PAY))


def _refund_status(method, linepay_integrated, ecpay=False):
    if (method == PaymentMethod.LINE_PAY and linepay_integrated) or ecpay:
        return 'pending'
    if method in (PaymentMethod.CASH, PaymentMethod.STORED_VALUE, PaymentMethod.GIFT_CARD):
        return 'done'
    return 'manual'   # 刷卡機、街口、線上付款 (綠界後台)、平台代收：需人工在對應系統退款


def on_order_cancelled(order, reason='', approved_by=''):
    """
    訂單取消 (作廢) 時呼叫 (與取消同一交易)：已收款的訂單建立退款紀錄、退回儲值金、發票作廢 / 折讓。
    回傳建立的 Refund 或 None。
    """
    from app.services import linepay, wallet_service
    db.session.refresh(order, ['payment_status', 'refunded_amount'])   # 取消流程剛以 UPDATE 標記需退款
    paid_like = (order.payment_status or '') in ('', 'settled', 'paid', 'refund_required') \
        and order.payment_status != 'authorizing'
    amount = order.refundable_amount if paid_like else 0
    refund = None
    if amount > 0 and order.payment_method == PaymentMethod.SPLIT:
        refund = _refund_split(order, amount, 'cancel', reason or '訂單取消', approved_by)
        _void_invoices(order)
        return refund
    if amount > 0 and (order.payment_status or '') != 'authorizing':
        integrated = order.payment_method == PaymentMethod.LINE_PAY and bool(order.payment_reference) \
            and linepay.is_enabled(order.tenant_id)
        status = _refund_status(order.payment_method, integrated)
        if order.payment_status == 'refund_required':
            status = 'manual'
        refund = Refund(tenant_id=order.tenant_id, order_id=order.id, kind='cancel', amount=amount,
                        method=order.payment_method, reason=(reason or '訂單取消')[:200], status=status,
                        staff_username=_staff_name(), approved_by=approved_by,
                        completed_at=datetime.now() if status == 'done' else None)
        db.session.add(refund)
        record_payment(order, PaymentKind.REFUND, order.payment_method, -amount, note=f'作廢訂單 #{order.id}')
        db.session.execute(update(Order).where(Order.id == order.id)
                           .values(refunded_amount=Order.refunded_amount + amount)
                           .execution_options(synchronize_session=False))
        db.session.flush()
        if order.payment_method == PaymentMethod.STORED_VALUE and order.user_id:
            back = min(order.wallet_used or amount, amount)   # 已部分退款的金額不再重複退回
            wallet_service.change_balance(order.tenant_id, order.user_id, back, 'refund',
                                          order_id=order.id, note=f'取消訂單 #{order.id}', clamp=True)
        if order.payment_method == PaymentMethod.GIFT_CARD:
            from app.services import giftcard_service
            giftcard_service.refund(order, amount, _staff_name())
        if status == 'pending':
            deferred.defer(process_refund, refund.id)
    _void_invoices(order)
    if order.payment_status == 'refund_required' and ecpay_integrated(order) \
            and settings_service.get('payments', order.tenant_id).get('ecpay_auto_refund'):
        deferred.defer(auto_refund_online_order, order.id)
    return refund


def _refund_one_tender(order, method, amount, kind, reason, approved_by):
    """退回混合付款中的一種付款方式 (儲值金退回會員、禮物卡退回卡片、其他依付款方式人工 / 現金退款)"""
    from app.services import wallet_service
    status = _refund_status(method, False)
    refund = Refund(tenant_id=order.tenant_id, order_id=order.id, kind=kind, amount=amount, method=method,
                    reason=str(reason or '')[:200], status=status, staff_username=_staff_name(), approved_by=approved_by,
                    completed_at=datetime.now() if status == 'done' else None)
    db.session.add(refund)
    label = '作廢訂單' if kind == 'cancel' else '部分退款'
    record_payment(order, PaymentKind.REFUND, method, -amount, note=f'{label} #{order.id} ({method})'[:200])
    if method == PaymentMethod.STORED_VALUE and order.user_id:
        wallet_service.change_balance(order.tenant_id, order.user_id, amount, 'refund', order_id=order.id,
                                      note=f'訂單 #{order.id} 退款')
    if method == PaymentMethod.GIFT_CARD:
        from app.services import giftcard_service
        giftcard_service.refund(order, amount, _staff_name())
    return refund


def _refund_split(order, amount, kind, reason, approved_by):
    """混合付款整單作廢：每種付款方式退回各自的淨收款 (現金優先；合計 = amount)"""
    remaining, first = amount, None
    for method, paid in sorted(paid_by_method(order).items(), key=lambda kv: (kv[0] != PaymentMethod.CASH, kv[0])):
        part = min(paid, remaining)
        if part <= 0:
            continue
        refund = _refund_one_tender(order, method, part, kind, reason, approved_by)
        first = first or refund
        remaining -= part
    db.session.execute(update(Order).where(Order.id == order.id)
                       .values(refunded_amount=Order.refunded_amount + (amount - remaining))
                       .execution_options(synchronize_session=False))
    db.session.flush()
    return first


def _void_invoices(order):
    from app.services import einvoice_ecpay
    if not settings_service.get('einvoice', order.tenant_id).get('void_on_cancel', True):
        return
    for inv in Invoice.query.filter_by(order_id=order.id).all():
        if inv.status in (InvoiceStatus.PENDING, InvoiceStatus.WAITING):
            inv.status, inv.last_error = InvoiceStatus.VOID, '開立前訂單已取消'
        elif inv.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE):
            deferred.defer(einvoice_ecpay.void_or_allowance, inv.id, '訂單取消')


def partial_refund(order_id, amount, reason, method=None, approved_by='', tenant_id=None):
    """部分退款 (訂單不取消)：金額不可超過可退金額；LINE Pay 自動退款、發票開折讓"""
    from app.services import einvoice_ecpay, linepay, wallet_service
    cfg = settings_service.get('refund', tenant_id)
    if not cfg.get('allow_partial', True):
        raise AppError('目前設定不允許部分退款，請改用作廢 (取消訂單)')
    order = db.session.get(Order, order_id)
    if order is None or (tenant_id is not None and order.tenant_id != tenant_id):
        raise AppError('查無此訂單', 404)
    if order.status == OrderStatus.CANCELLED:
        raise AppError('訂單已取消 (作廢)，無法再退款', 409)
    if not order.is_paid:
        raise AppError('訂單尚未收款，無需退款', 409)
    amount = int(amount or 0)
    reason = str(reason or '').strip()
    if amount <= 0:
        raise AppError('退款金額必須大於 0')
    if cfg.get('require_reason', True) and not reason:
        raise AppError('請填寫退款原因')
    # 條件式更新：同一張單同時退款，總額也不會超過訂單金額
    claimed = db.session.execute(
        update(Order).where(Order.id == order.id, Order.total_price - Order.refunded_amount >= amount)
        .values(refunded_amount=Order.refunded_amount + amount).execution_options(synchronize_session=False)).rowcount
    if not claimed:
        db.session.rollback()
        raise AppError(f'退款金額超過可退金額 (${order.refundable_amount})', 409)
    if order.payment_method == PaymentMethod.SPLIT:
        # 混合付款：退回指定的付款方式 (預設現金，沒有現金時為金額最大的一種)，不超過該方式的淨收款
        paid = paid_by_method(order)
        method = normalize_payment_method(method) if method else None
        if method is None:
            method = PaymentMethod.CASH if PaymentMethod.CASH in paid else max(paid, key=paid.get, default=None)
        if method not in paid or paid[method] < amount:
            db.session.rollback()
            raise AppError(f'混合付款中「{method}」可退金額為 ${paid.get(method, 0)}，請改選其他付款方式或降低金額', 409)
        refund = _refund_one_tender(order, method, amount, 'partial', reason, approved_by)
        record_staff_audit('PARTIAL_REFUND', target_order_id=order.id, target_user_id=order.user_id,
                           tenant_id=order.tenant_id,
                           details=f'訂單 #{order.id} 部分退款 ${amount} (混合付款：{method})，原因：{reason}；'
                                   f'核准：{approved_by or "-"}')
        db.session.flush()
        for inv in Invoice.query.filter(Invoice.order_id == order.id,
                                        Invoice.status.in_((InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE))).all():
            deferred.defer(einvoice_ecpay.allowance, inv.id, amount, reason or '部分退款')
        return refund
    method = method or order.payment_method
    integrated = method == PaymentMethod.LINE_PAY and bool(order.payment_reference) and linepay.is_enabled(order.tenant_id)
    ecpay = ecpay_integrated(order, method) and bool(settings_service.get('payments', order.tenant_id).get('ecpay_auto_refund'))
    status = _refund_status(method, integrated, ecpay)
    refund = Refund(tenant_id=order.tenant_id, order_id=order.id, kind='partial', amount=amount, method=method,
                    reason=reason[:200], status=status, staff_username=_staff_name(), approved_by=approved_by,
                    completed_at=datetime.now() if status == 'done' else None)
    db.session.add(refund)
    record_payment(order, PaymentKind.REFUND, method, -amount, note=f'部分退款：{reason}'[:200])
    if method == PaymentMethod.STORED_VALUE and order.user_id:
        wallet_service.change_balance(order.tenant_id, order.user_id, amount, 'refund', order_id=order.id,
                                      note=f'訂單 #{order.id} 部分退款')
    if method == PaymentMethod.GIFT_CARD:
        from app.services import giftcard_service
        giftcard_service.refund(order, amount, _staff_name())
    record_staff_audit('PARTIAL_REFUND', target_order_id=order.id, target_user_id=order.user_id, tenant_id=order.tenant_id,
                       details=f'訂單 #{order.id} 部分退款 ${amount} ({method})，原因：{reason}；核准：{approved_by or "-"}')
    db.session.flush()
    if status == 'pending':
        deferred.defer(process_refund, refund.id)
    for inv in Invoice.query.filter(Invoice.order_id == order.id,
                                    Invoice.status.in_((InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE))).all():
        deferred.defer(einvoice_ecpay.allowance, inv.id, amount, reason or '部分退款')
    return refund


def process_refund(refund_id, ecpay_post=None):
    """
    線上自動退款 (pending → done / failed)：LINE Pay 退款，或綠界信用卡 / Apple Pay 退刷 (線上端)。
    失敗可在後台「作廢與退款紀錄」重試。
    """
    from app.services import linepay, payment_ecpay
    refund = db.session.get(Refund, refund_id)
    if refund is None or refund.status not in ('pending', 'failed'):
        return None
    order = refund.order
    provider = 'LINE Pay' if refund.method == PaymentMethod.LINE_PAY else '綠界'
    try:
        if refund.method == PaymentMethod.LINE_PAY:
            tx = linepay.refund(refund.tenant_id, order.uuid, refund.amount)
            result = f'LINE Pay 退款 {tx}'
        else:
            result = payment_ecpay.refund(order, refund.amount, post=ecpay_post)
        refund.status, refund.provider_result, refund.completed_at = 'done', result[:300], datetime.now()
    except (linepay.LinePayError, payment_ecpay.EcpayRefundError) as exc:
        refund.status, refund.provider_result = 'failed', exc.message[:300]
        from app.services import monitor_service
        monitor_service.alert(f'{provider} 退款失敗 (訂單 #{order.id}，${refund.amount})：{exc.message}',
                              key=f'refund-{refund.id}', tenant_id=refund.tenant_id)
    db.session.commit()
    return refund.status


process_linepay_refund = process_refund   # 舊名稱 (相容)

AUTO_REFUND_FAILED = '退刷失敗：'


def auto_refund_online_order(order_id, ecpay_post=None):
    """
    線上端：已付款但被取消的綠界線上訂單 (含分店取消後同步上來的) 向綠界退刷。
    成功 → 付款狀態改為「已退款」(同步回分店)，相關退款紀錄標記完成；失敗 → 記下原因不再自動重試 (後台可手動重試)。
    回傳 (成功與否, 訊息)。
    """
    from app.services import monitor_service, payment_ecpay
    from app.tenant_context import tenant_bypass
    with tenant_bypass():
        order = db.session.get(Order, order_id)
        if order is None or order.payment_status != 'refund_required' or not ecpay_integrated(order):
            return False, '此訂單不是可自動退刷的線上信用卡訂單'
        cancel = Refund.query.filter_by(order_id=order.id, kind='cancel').order_by(Refund.id.desc()) \
            .execution_options(skip_tenant_filter=True).first()
        amount = cancel.amount if cancel is not None and cancel.amount > 0 else int(order.total_price or 0)
        try:
            msg = payment_ecpay.refund(order, amount, post=ecpay_post)
        except payment_ecpay.EcpayRefundError as exc:
            order.payment_note = f'{AUTO_REFUND_FAILED}{exc.message}'[:120]
            monitor_service.alert(f'綠界退刷失敗 (訂單 #{order.id}，${amount})：{exc.message}',
                                  key=f'ecpay-refund-{order.id}', tenant_id=order.tenant_id)
            db.session.commit()
            return False, exc.message
        order.payment_status, order.payment_note = 'refunded', msg[:120]
        for r in Refund.query.filter(Refund.order_id == order.id, Refund.status != 'done') \
                .execution_options(skip_tenant_filter=True).all():
            r.status, r.completed_at, r.provider_result = 'done', datetime.now(), msg[:300]
        record_staff_audit('REFUND_DONE', target_order_id=order.id, tenant_id=order.tenant_id,
                           details=f'訂單 #{order.id} {msg}')
        db.session.commit()
        return True, msg


def run_auto_refunds(limit=20, ecpay_post=None):
    """背景排程 (線上端)：處理尚未退刷、且設定自動退刷的已取消線上訂單。回傳成功筆數。"""
    from app.models.enums import OrderSource
    from app.services import payment_ecpay
    from app.tenant_context import tenant_bypass
    if not payment_ecpay.is_enabled():
        return 0
    with tenant_bypass():
        rows = db.session.query(Order.id, Order.tenant_id).filter(
            Order.payment_status == 'refund_required', Order.order_source == OrderSource.ONLINE,
            Order.payment_trade_no.isnot(None), Order.payment_trade_no != '',
            Order.payment_method.in_((PaymentMethod.CREDIT_CARD, PaymentMethod.APPLE_PAY)),
            db.or_(Order.payment_note.is_(None), ~Order.payment_note.like(f'{AUTO_REFUND_FAILED}%'))) \
            .order_by(Order.id).limit(limit).all()
    done = 0
    for order_id, tenant_id in rows:
        if settings_service.get('payments', tenant_id).get('ecpay_auto_refund'):
            done += 1 if auto_refund_online_order(order_id, ecpay_post=ecpay_post)[0] else 0
    return done


def mark_refund_done(refund_id, staff_username, note=''):
    refund = db.session.get(Refund, refund_id)
    if refund is None:
        raise AppError('查無退款紀錄', 404)
    if refund.status == 'done':
        raise AppError('此筆已完成退款', 409)
    refund.status, refund.completed_at = 'done', datetime.now()
    refund.provider_result = (f'人工完成 ({staff_username}) {note}'.strip())[:300]
    if refund.order is not None and refund.order.payment_status == 'refund_required':
        refund.order.payment_status = 'refunded'
    return refund
