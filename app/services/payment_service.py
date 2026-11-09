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
from sqlalchemy import case, or_, update

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
    from app.sync import runtime as sync_runtime
    tid = tenant_id or order.tenant_id
    rec = PaymentRecord(
        tenant_id=tid, order_id=order.id if order is not None else None, kind=kind, method=str(method),
        amount=int(amount), received=int(received if received is not None else amount), change=int(change or 0),
        reference=str(reference or '')[:60], card_last4=str(card_last4 or '')[:4], note=str(note or '')[:200],
        staff_username=staff or _staff_name(), shift_id=open_shift_id(tid, lock=True), settlement_id=settlement_id,
        node_uuid=sync_runtime.local_node_uuid(), created_at=datetime.now())
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
def validate_card_details(payment_info, is_pos, tenant_id=None):
    """刷卡機記帳：依設定要求授權碼 / 卡號末四碼。回傳 (授權碼, 末四碼)。tenant_id：背景工作 (沒有請求情境) 時必須帶"""
    info = payment_info if isinstance(payment_info, dict) else {}
    auth = ''.join(str(info.get('auth_code') or '').split()).upper()[:12]
    last4 = ''.join(ch for ch in str(info.get('card_last4') or '') if ch.isdigit())[:4]
    if not is_pos:
        return auth, last4
    cfg = settings_service.get('card_terminal', tenant_id)
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
    allowed = set(allowed_methods('pos', tenant_id))
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
            tender['reference'], tender['last4'] = validate_card_details(t, True, tenant_id)
        elif method == PaymentMethod.STORED_VALUE and user is None:
            raise AppError('使用儲值金付款請先綁定會員！')
        elif method == PaymentMethod.GIFT_CARD:
            tender['gift_code'] = giftcard_service.find(tenant_id, t.get('gift_code')).code
            tender['reference'] = giftcard_service.mask_code(tender['gift_code'])
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


def allowed_methods(channel, tenant_id=None):
    """
    後台設定的各管道可用付款方式 (pos / kiosk)；啟用儲值金時 POS 一律可用儲值金，關閉儲值金時所有管道都不可用。
    tenant_id：背景工作 (例如團體預購自動送單，沒有請求情境) 時必須帶。
    """
    cfg = settings_service.get('payments', tenant_id)
    methods = list(cfg.get('pos_methods' if channel == 'pos' else 'kiosk_methods') or ['Cash'])
    if not settings_service.get('member', tenant_id).get('wallet_enabled'):
        methods = [m for m in methods if m != PaymentMethod.STORED_VALUE] or ['Cash']
    elif channel == 'pos' and '儲值金' not in methods:
        methods.append('儲值金')
    if channel == 'pos' and settings_service.get('gift_card', tenant_id).get('enabled') and '禮物卡' not in methods:
        methods.append('禮物卡')
    return methods


def charge_linepay(order_id, one_time_key, invoice_info=None):
    """
    POS 已建立訂單 (payment_status='authorizing')，向 LINE Pay 扣款：
    成功 → 改為已付款並記錄收款；失敗 → 以系統身分取消訂單 (回補庫存、點數…) 並拋出錯誤給收銀員。
    invoice_info：收銀員在結帳時輸入的發票資訊 (載具 / 統編 / 捐贈)，扣款成功後開立發票時使用。
    """
    from app.services import linepay
    from app.services.order_service import system_cancel
    from app.services import monitor_service
    from app.services.member_rewards import grant_paid_rewards
    order = db.session.get(Order, order_id)
    try:
        tx = linepay.pay(order.tenant_id, order.uuid, order.total_price, one_time_key)
    except linepay.LinePayError as exc:
        if exc.code == linepay.UNKNOWN:
            # 結果不明 (可能已扣款)：先試著退回；退回成功才取消訂單，否則保留「授權中」請店員確認
            try:
                linepay.refund(order.tenant_id, order.uuid, order.total_price)
            except linepay.LinePayError:
                monitor_service.alert(f'POS 訂單 #{order.id} LINE Pay 付款結果不明 (${order.total_price})，'
                                      '請到 LINE Pay 商家後台確認是否已扣款', key=f'linepay-unknown-{order.id}',
                                      tenant_id=order.tenant_id, level='warning', source='payment')
                raise AppError('LINE Pay 回應逾時且無法確認付款結果：訂單保留為「授權中」，'
                               '請到 LINE Pay 商家後台確認是否已扣款後再處理', 504, payment_failed=True)
            system_cancel(order.id, order.tenant_id, 'LINE Pay 回應逾時，已自動退回款項')
            raise AppError('LINE Pay 回應逾時，已自動退回款項並取消訂單，請重新結帳', 402, payment_failed=True)
        system_cancel(order.id, order.tenant_id, f'LINE Pay 付款失敗：{exc.message}')
        raise AppError(exc.message, 402, payment_failed=True)
    claimed = db.session.execute(update(Order).where(Order.id == order.id, Order.payment_status == 'authorizing',
                                                     Order.status != OrderStatus.CANCELLED)
                                 .values(payment_status='', payment_reference=tx)
                                 .execution_options(synchronize_session=False)).rowcount
    if not claimed:
        # 扣款期間訂單被其他人取消：退回剛扣的款，不留下「已取消卻已收款」的訂單
        db.session.rollback()
        try:
            linepay.refund(order.tenant_id, order.uuid, order.total_price)
        except linepay.LinePayError as exc:
            monitor_service.alert(f'POS 訂單 #{order.id} 已取消但 LINE Pay 已扣款 ${order.total_price}，自動退款失敗：{exc.message}',
                                  key=f'linepay-cancelled-{order.id}', tenant_id=order.tenant_id, level='warning',
                                  source='payment')
            raise AppError('訂單已被取消，但 LINE Pay 已扣款且自動退款失敗，請到 LINE Pay 商家後台退款', 409)
        raise AppError('訂單在付款期間已被取消，剛扣的款項已自動退回', 409)
    db.session.refresh(order)
    record_payment(order, PaymentKind.SALE, PaymentMethod.LINE_PAY, order.total_price, reference=tx)
    grant_paid_rewards(order)   # 授權中時沒給的會員回饋 (贈點、累積消費、集點、電子收據)
    after_paid(order, invoice_info)
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
    if order.order_source in DELIVERY_SOURCES:
        return None   # 外送平台訂單由平台開立 (避免重複開立)
    # 儲值金 (含混合付款中的儲值金) 在消費時照常開立：加值時只收預收款、不開發票
    existing = Invoice.query.filter(Invoice.order_id == order.id,
                                    Invoice.status.notin_((InvoiceStatus.VOID,))).first()
    if existing is not None:
        return existing
    inv = einvoice_ecpay.create_invoice(order, invoice_info or {}, status=status)
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
        _void_invoices(order, amount)
        if order.payment_status == 'refund_required' and order.settlement_id and not _open_refunds(order.id):
            order.payment_status = 'refunded'   # 桌位混合付款結帳且全部當場退款 (現金…)：不留在「需退款」清單
        return refund
    if amount > 0 and (order.payment_status or '') != 'authorizing':
        from app.services import giftcard_service
        integrated = order.payment_method == PaymentMethod.LINE_PAY and bool(order.payment_reference) \
            and linepay.is_enabled(order.tenant_id)
        status = _refund_status(order.payment_method, integrated)
        if order.payment_status == 'refund_required' and not order.settlement_id:
            status = 'manual'   # 線上付款 (paid)：到綠界後台退刷；桌位已在櫃檯結帳 (settled) 的依付款方式由櫃檯退款
        # 禮物卡已作廢：不能退回卡片 (顧客拿不到錢)，改記人工退款 (現金等其他方式)
        gift_void = order.payment_method == PaymentMethod.GIFT_CARD and giftcard_service.voided_card(order) is not None
        if gift_void:
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
        if order.payment_method == PaymentMethod.GIFT_CARD and not gift_void:
            giftcard_service.refund(order, amount, _staff_name())
        if status == 'pending':
            deferred.defer(process_refund, refund.id)
    elif (order.payment_status or '') in ('pending', 'expired', 'unpaid') and (order.wallet_used or 0) > 0 \
            and order.payment_method == PaymentMethod.STORED_VALUE and order.user_id:
        # 未收款就取消、但結帳時已先扣儲值金 (例如舊版線上端以儲值金送出的待付款訂單逾時)：退回已扣的儲值金
        wallet_service.change_balance(order.tenant_id, order.user_id, order.wallet_used, 'refund',
                                      order_id=order.id, note=f'取消訂單 #{order.id}', clamp=True)
    _void_invoices(order, amount)
    if order.payment_status == 'refund_required' and (amount <= 0 or order.settlement_id) \
            and not _open_refunds(order.id):
        # 已全額部分退款後才作廢、或桌位結帳的訂單已當場退款 (現金…)：沒有待處理的退款，不留在「需退款」清單
        order.payment_status = 'refunded'
    elif amount > 0 and order.payment_status == 'refund_required' and ecpay_integrated(order) \
            and settings_service.get('payments', order.tenant_id).get('ecpay_auto_refund'):
        deferred.defer(auto_refund_online_order, order.id)
    return refund


def _open_refunds(order_id):
    """這張訂單尚未完成 (status != done) 的退款筆數"""
    return Refund.query.filter(Refund.order_id == order_id, Refund.status != 'done') \
        .execution_options(skip_tenant_filter=True).count()


def _refund_one_tender(order, method, amount, kind, reason, approved_by):
    """退回混合付款中的一種付款方式 (儲值金退回會員、禮物卡退回卡片、其他依付款方式人工 / 現金退款)"""
    from app.services import giftcard_service, wallet_service
    status = _refund_status(method, False)
    # 禮物卡已作廢：不能退回卡片 (顧客拿不到錢)，改記人工退款 (現金等其他方式)
    gift_void = method == PaymentMethod.GIFT_CARD and giftcard_service.voided_card(order) is not None
    if gift_void:
        status = 'manual'
    refund = Refund(tenant_id=order.tenant_id, order_id=order.id, kind=kind, amount=amount, method=method,
                    reason=str(reason or '')[:200], status=status, staff_username=_staff_name(), approved_by=approved_by,
                    completed_at=datetime.now() if status == 'done' else None)
    db.session.add(refund)
    label = '作廢訂單' if kind == 'cancel' else '部分退款'
    record_payment(order, PaymentKind.REFUND, method, -amount, note=f'{label} #{order.id} ({method})'[:200])
    if method == PaymentMethod.STORED_VALUE and order.user_id:
        wallet_service.change_balance(order.tenant_id, order.user_id, amount, 'refund', order_id=order.id,
                                      note=f'訂單 #{order.id} 退款')
    if method == PaymentMethod.GIFT_CARD and not gift_void:
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


def _settlement_invoice_refund(order, amount, reason):
    """
    桌位合併結帳的發票 (一張發票含整桌多張訂單，order_id 為空、以 settlement_id 對應)：其中一張訂單取消或部分退款時，
    就退的金額調整 — 尚未開立時直接減少發票金額 (減到 0 作廢)，已開立時開折讓。
    """
    from app.services import einvoice_ecpay
    amount = int(amount or 0)
    if not order.settlement_id or amount <= 0:
        return
    for inv in Invoice.query.filter(Invoice.settlement_id == order.settlement_id, Invoice.order_id.is_(None)).all():
        if inv.status in (InvoiceStatus.PENDING, InvoiceStatus.WAITING, InvoiceStatus.FAILED):
            inv.amount = max(0, (inv.amount or 0) - amount)
            if inv.amount == 0:
                inv.status, inv.last_error = InvoiceStatus.VOID, '開立前整桌訂單已取消'
        elif inv.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE):
            deferred.defer(einvoice_ecpay.allowance, inv.id, amount, (reason or '退款')[:30])


UNISSUED_INVOICE = (InvoiceStatus.PENDING, InvoiceStatus.WAITING, InvoiceStatus.FAILED)
ISSUING_INVOICE = 'issuing'   # einvoice_ecpay.issue_invoice 已搶到、正在向綠界開立


def _shrink_unissued_invoice(invoice_id, amount, note):
    """
    尚未開立 (待開立 / 等待付款 / 開立失敗) 的發票：以條件式更新減少金額 (amount 為 None 或減到 0 時作廢)。
    回傳是否成功 (False = 發票剛被背景開立搶走或已開立，需改走折讓 / 作廢)。
    整張作廢 (amount 為 None) 時，曾送到綠界 (attempts > 0) 的待開立 / 開立失敗發票不在本機直接作廢 (回傳 False)：
    可能逾時但綠界其實已開立，由呼叫端改走 einvoice_ecpay.void_or_allowance (先以 RelateNumber 查詢)。
    """
    conds = [Invoice.id == invoice_id, Invoice.status.in_(UNISSUED_INVOICE)]
    if amount is None:
        values = {'status': InvoiceStatus.VOID, 'last_error': note}
        conds.append(or_(Invoice.attempts == 0, Invoice.status == InvoiceStatus.WAITING))
    else:
        values = {'amount': case((Invoice.amount > amount, Invoice.amount - amount), else_=0)}
    done = db.session.execute(update(Invoice).where(*conds)
                              .values(**values)
                              .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    if done and amount is not None:
        db.session.execute(update(Invoice).where(Invoice.id == invoice_id, Invoice.status.in_(UNISSUED_INVOICE),
                                                 Invoice.amount <= 0)
                           .values(status=InvoiceStatus.VOID, last_error=note)
                           .execution_options(synchronize_session=False, skip_tenant_filter=True))
    inv = db.session.get(Invoice, invoice_id)
    if inv is not None:
        db.session.expire(inv, ['status', 'amount', 'last_error'])
    return bool(done)


def _invoice_remaining(inv):
    """已開立發票尚未折讓的金額"""
    return max(0, (inv.amount or 0) - (inv.allowance_amount or 0))


def adjust_invoice_after_issue(invoice_id, amount=None, reason=''):
    """
    交易後工作：發票在開立中 (issuing) 時訂單取消 (amount 為 None) 或部分退款 amount 元。
    重新讀取發票：已開立 → 作廢 / 折讓 (不超過剩餘金額)；仍未開立 → 直接作廢 / 減少金額；仍在開立中 → 告警請人工處理。
    """
    from app.services import einvoice_ecpay, monitor_service
    inv = db.session.get(Invoice, invoice_id)
    if inv is None:
        return None
    db.session.refresh(inv)
    if inv.status in UNISSUED_INVOICE:
        note = '開立前訂單已取消' if amount is None else '開立前已全額退款'
        if _shrink_unissued_invoice(inv.id, amount, note):
            db.session.commit()
            return db.session.get(Invoice, invoice_id).status
        db.session.refresh(inv)
        if amount is None and inv.status in UNISSUED_INVOICE:
            return einvoice_ecpay.void_or_allowance(inv.id, reason or '訂單取消')   # 曾送出過：先向綠界查詢再作廢
    if inv.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE):
        if amount is None:
            return einvoice_ecpay.void_or_allowance(inv.id, reason or '訂單取消')
        part = min(int(amount), _invoice_remaining(inv))
        return einvoice_ecpay.allowance(inv.id, part, (reason or '退款')[:30]) if part > 0 else inv.status
    if inv.status == ISSUING_INVOICE:
        todo = '作廢' if amount is None else f'開立 ${amount} 折讓'
        monitor_service.alert(f'電子發票 (編號 {inv.relate_number}) 開立中時訂單已{"取消" if amount is None else "退款"}，'
                              f'請在開立完成後到電子發票頁{todo}', key=f'invoice-adjust-{inv.id}', tenant_id=inv.tenant_id)
    return inv.status


def _void_invoices(order, amount=0):
    from app.services import einvoice_ecpay
    if not settings_service.get('einvoice', order.tenant_id).get('void_on_cancel', True):
        return
    _settlement_invoice_refund(order, amount, '訂單取消')
    for inv in Invoice.query.filter_by(order_id=order.id).all():
        if inv.status in UNISSUED_INVOICE and _shrink_unissued_invoice(inv.id, None, '開立前訂單已取消'):
            continue   # 開立失敗 (failed) 的也作廢：之後重送 / 修改載具不會替已取消的訂單開出發票
        if inv.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE) or (
                inv.status in (InvoiceStatus.PENDING, InvoiceStatus.FAILED) and (inv.attempts or 0) > 0):
            # 曾送到綠界的待開立 / 開立失敗發票 (可能逾時但已開立)：交易後先以 RelateNumber 查詢，已開立才真的作廢 / 折讓
            deferred.defer(einvoice_ecpay.void_or_allowance, inv.id, '訂單取消')
        elif inv.status in UNISSUED_INVOICE + (ISSUING_INVOICE,):
            deferred.defer(adjust_invoice_after_issue, inv.id, None, '訂單取消')   # 正在開立：開立完成後作廢


def _wallet_not_invoiced(order, inv):
    """
    舊版建立的混合付款發票不含儲值金 (發票金額 = 應付 - 儲值金)：退回儲值金的部分不可再對這張發票折讓。
    (只看已開立的發票：已開立的發票金額不會再變動，尚未開立的會因部分退款減少金額而無法以金額判斷)
    """
    wallet = order.wallet_used or 0
    return order.payment_method == PaymentMethod.SPLIT and wallet > 0 \
        and inv.status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE) \
        and (inv.amount or 0) == (order.total_price or 0) - wallet


def _refund_order_invoices(order, amount, reason, method=None):
    """
    部分退款 / 單品作廢調整這張訂單的發票：尚未開立 → 直接減少發票金額 (減到 0 作廢)；
    已開立 → 開折讓 (不超過剩餘金額)；開立中 → 交易後重新檢查再處理。
    """
    from app.services import einvoice_ecpay
    for inv in Invoice.query.filter(Invoice.order_id == order.id,
                                    Invoice.status.notin_((InvoiceStatus.VOID, InvoiceStatus.SKIPPED))).all():
        if method == PaymentMethod.STORED_VALUE and _wallet_not_invoiced(order, inv):
            continue
        status = inv.status
        if status in UNISSUED_INVOICE and _shrink_unissued_invoice(inv.id, amount, '開立前已全額退款'):
            continue
        if status in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE):
            part = min(amount, _invoice_remaining(inv))
            if part > 0:
                deferred.defer(einvoice_ecpay.allowance, inv.id, part, reason)
        else:   # 開立中 (或剛被背景開立搶走)
            deferred.defer(adjust_invoice_after_issue, inv.id, amount, reason)


def partial_refund(order_id, amount, reason, method=None, approved_by='', tenant_id=None):
    """部分退款 (訂單不取消)：金額不可超過可退金額；LINE Pay 自動退款、發票開折讓 (尚未開立的直接減少金額)"""
    from app.services import linepay, wallet_service
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
    from app.models.enums import OrderSource
    from app.sync import runtime as sync_runtime
    if sync_runtime.is_hub() and order.node_uuid and order.order_source != OrderSource.ONLINE:
        # 退款與收付紀錄只上傳不下發：分店建立的訂單在線上端退款，分店的交班報表與退款清單都看不到
        raise AppError('這張訂單由分店建立，請在出餐分店退款', 409)
    from app.utils import safe_int
    amount = safe_int(amount, 0)
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
    from app.services import pass_service
    try:   # 寄杯購買訂單：依累計退款比例收回未使用次數 (超過未使用次數的價值時 409)；其他訂單不做任何事
        pass_service.on_partial_refund(order, _staff_name())
    except AppError:
        db.session.rollback()
        raise
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
        _refund_order_invoices(order, amount, reason or '部分退款', method)
        _settlement_invoice_refund(order, amount, reason or '部分退款')
        _recompute_member_spent(order)
        return refund
    from app.services import giftcard_service
    method = order.payment_method   # 非混合付款：一律退回原付款方式 (不接受請求指定，避免把現金改記成儲值金等)
    integrated = method == PaymentMethod.LINE_PAY and bool(order.payment_reference) and linepay.is_enabled(order.tenant_id)
    ecpay = ecpay_integrated(order, method) and bool(settings_service.get('payments', order.tenant_id).get('ecpay_auto_refund'))
    status = _refund_status(method, integrated, ecpay)
    # 禮物卡已作廢：不能退回卡片 (顧客拿不到錢)，改記人工退款 (現金等其他方式)
    gift_void = method == PaymentMethod.GIFT_CARD and giftcard_service.voided_card(order) is not None
    if gift_void:
        status = 'manual'
    refund = Refund(tenant_id=order.tenant_id, order_id=order.id, kind='partial', amount=amount, method=method,
                    reason=reason[:200], status=status, staff_username=_staff_name(), approved_by=approved_by,
                    completed_at=datetime.now() if status == 'done' else None)
    db.session.add(refund)
    record_payment(order, PaymentKind.REFUND, method, -amount, note=f'部分退款：{reason}'[:200])
    if method == PaymentMethod.STORED_VALUE and order.user_id:
        wallet_service.change_balance(order.tenant_id, order.user_id, amount, 'refund', order_id=order.id,
                                      note=f'訂單 #{order.id} 部分退款')
    if method == PaymentMethod.GIFT_CARD and not gift_void:
        giftcard_service.refund(order, amount, _staff_name())
    record_staff_audit('PARTIAL_REFUND', target_order_id=order.id, target_user_id=order.user_id, tenant_id=order.tenant_id,
                       details=f'訂單 #{order.id} 部分退款 ${amount} ({method})，原因：{reason}；核准：{approved_by or "-"}')
    db.session.flush()
    if status == 'pending':
        deferred.defer(process_refund, refund.id)
    _refund_order_invoices(order, amount, reason or '部分退款', method)
    _settlement_invoice_refund(order, amount, reason or '部分退款')   # 桌位合併結帳的發票
    _recompute_member_spent(order)
    return refund


def _recompute_member_spent(order):
    """部分退款後立即扣回會員的累積消費 (以 SQL 讀取同一交易中已更新的 refunded_amount)"""
    if not order.user_id:
        return
    from app.models.user import User
    from app.services import wallet_service
    user = db.session.get(User, order.user_id)
    if user is not None:
        wallet_service.recompute_spent(user)


def process_refund(refund_id, ecpay_post=None):
    """
    線上自動退款 (pending → done / failed)：LINE Pay 退款，或綠界信用卡 / Apple Pay 退刷 (線上端)。
    失敗可在後台「作廢與退款紀錄」重試。
    """
    from app.models.enums import OrderSource
    from app.services import linepay, monitor_service, payment_ecpay
    from app.sync import runtime as sync_runtime
    refund = db.session.get(Refund, refund_id)
    if refund is None or refund.status not in ('pending', 'failed'):
        return None
    order = refund.order
    if sync_runtime.is_hub() and (order is None or (order.node_uuid is not None
                                                    and order.order_source != OrderSource.ONLINE)):
        # 線上端：分店上傳的退款 (分店自有訂單，或訂單尚未同步上來) 由分店自己退款，不呼叫金流、不改狀態
        return None
    # 先搶到這筆退款 (pending / failed → processing) 才呼叫金流：同時兩個請求只有一個會真的退款
    claimed = db.session.execute(update(Refund).where(Refund.id == refund.id, Refund.status.in_(('pending', 'failed')))
                                 .values(status='processing')
                                 .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    db.session.commit()
    if not claimed:
        return None
    db.session.refresh(refund)
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
        monitor_service.alert(f'{provider} 退款失敗 (訂單 #{order.id}，${refund.amount})：{exc.message}',
                              key=f'refund-{refund.id}', tenant_id=refund.tenant_id)
    except Exception as exc:
        # 非預期錯誤 (解密金鑰、程式錯誤…)：改為失敗 (可在後台重試)，不停在「處理中」
        db.session.rollback()
        logger.exception('退款 #%s 呼叫 %s 時發生非預期錯誤', refund_id, provider)
        refund = db.session.get(Refund, refund_id)
        refund.status, refund.provider_result = 'failed', f'非預期錯誤：{exc}'[:300]
        db.session.commit()
        monitor_service.alert(f'{provider} 退款發生非預期錯誤 (訂單 #{refund.order_id}，${refund.amount})：{exc}，'
                              '請先到金流後台確認是否已退款再重試', key=f'refund-{refund_id}', tenant_id=refund.tenant_id)
        return refund.status
    db.session.commit()
    return refund.status


AUTO_REFUND_FAILED = '退刷失敗：'
AUTO_REFUNDING = '退刷處理中 '   # 後接開始時間 (YYYY-MM-DD HH:MM:SS)：搶到退刷的標記，逾時視為中斷可重新退刷
AUTO_REFUND_STALE_MINUTES = 10


def auto_refund_online_order(order_id, ecpay_post=None):
    """
    線上端：已付款但被取消的綠界線上訂單 (含分店取消後同步上來的) 向綠界退刷。
    退刷金額為作廢退款紀錄的金額 (沒有紀錄時為訂單金額扣掉已登記的部分退款；為 0 時不呼叫綠界)。
    先以條件式更新搶到這張訂單 (付款備註記為「退刷處理中」) 才呼叫綠界：延後工作 / 排程 / 後台按鈕同時執行只會退刷一次。
    成功 → 付款狀態改為「已退款」(同步回分店)，這次退刷的作廢退款紀錄標記完成 (其他部分退款保持原狀)；
    失敗 → 記下原因不再自動重試 (後台可手動重試)。回傳 (成功與否, 訊息)。
    """
    from datetime import timedelta

    from app.services import monitor_service, payment_ecpay
    from app.tenant_context import tenant_bypass
    with tenant_bypass():
        order = db.session.get(Order, order_id)
        if order is None or order.payment_status != 'refund_required' or not ecpay_integrated(order):
            return False, '此訂單不是可自動退刷的線上信用卡訂單'
        cancel = Refund.query.filter_by(order_id=order.id, kind='cancel').order_by(Refund.id.desc()) \
            .execution_options(skip_tenant_filter=True).first()
        if cancel is not None and cancel.amount > 0:
            amount = cancel.amount
        else:
            # 沒有作廢退款紀錄：取消前已部分退款到全額 (作廢時可退金額為 0)，或分店的作廢退款紀錄尚未同步上來
            partial = db.session.query(db.func.coalesce(db.func.sum(Refund.amount), 0)).filter(
                Refund.order_id == order.id, Refund.kind != 'cancel').execution_options(skip_tenant_filter=True).scalar()
            amount = max(0, int(order.total_price or 0) - int(partial or 0))
        if amount <= 0:
            if _open_refunds(order.id):
                msg = '已無可退刷金額 (取消前已全額部分退款)，請確認部分退款紀錄完成後標記'
                order.payment_note = f'{AUTO_REFUND_FAILED}{msg}'[:120]   # 不再自動重試
                db.session.commit()
                return False, msg
            msg = '取消前已全額退款，無需向綠界退刷'
            order.payment_status, order.payment_note = 'refunded', msg
            db.session.commit()
            return True, msg
        now = datetime.now()
        stale = f'{AUTO_REFUNDING}{(now - timedelta(minutes=AUTO_REFUND_STALE_MINUTES)):%Y-%m-%d %H:%M:%S}'
        claimed = db.session.execute(
            update(Order).where(Order.id == order.id, Order.payment_status == 'refund_required',
                                db.or_(~Order.payment_note.like(f'{AUTO_REFUNDING}%'), Order.payment_note < stale))
            .values(payment_note=f'{AUTO_REFUNDING}{now:%Y-%m-%d %H:%M:%S}')
            .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
        db.session.commit()
        if not claimed:
            return False, '這張訂單正在向綠界退刷或已處理，請稍後重新整理'
        db.session.refresh(order)
        try:
            msg = payment_ecpay.refund(order, amount, post=ecpay_post)
        except payment_ecpay.EcpayRefundError as exc:
            order.payment_note = f'{AUTO_REFUND_FAILED}{exc.message}'[:120]
            monitor_service.alert(f'綠界退刷失敗 (訂單 #{order.id}，${amount})：{exc.message}',
                                  key=f'ecpay-refund-{order.id}', tenant_id=order.tenant_id)
            db.session.commit()
            return False, exc.message
        order.payment_status, order.payment_note = 'refunded', msg[:120]
        if cancel is not None and cancel.status != 'done':
            cancel.status, cancel.completed_at, cancel.provider_result = 'done', datetime.now(), msg[:300]
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
    cond = (Order.payment_status == 'refund_required', Order.order_source == OrderSource.ONLINE,
            Order.payment_trade_no.isnot(None), Order.payment_trade_no != '',
            Order.payment_method.in_((PaymentMethod.CREDIT_CARD, PaymentMethod.APPLE_PAY)),
            db.or_(Order.payment_note.is_(None), ~Order.payment_note.like(f'{AUTO_REFUND_FAILED}%')))
    with tenant_bypass():
        # 先挑出開啟自動退刷的店家再取前 limit 筆：沒開的店家累積的待人工退款訂單不會擋住其他店家
        tenant_ids = [tid for (tid,) in db.session.query(Order.tenant_id).filter(*cond).distinct().all()]
        enabled = [tid for tid in tenant_ids if settings_service.get('payments', tid).get('ecpay_auto_refund')]
        if not enabled:
            return 0
        rows = db.session.query(Order.id).filter(*cond, Order.tenant_id.in_(enabled)) \
            .order_by(Order.id).limit(limit).all()
    done = 0
    for (order_id,) in rows:
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
    db.session.flush()
    # 這張訂單的退款全部完成才改為「已退款」(先標完較早的部分退款時，作廢退款仍待處理)
    if refund.order is not None and refund.order.payment_status == 'refund_required' \
            and not _open_refunds(refund.order_id):
        refund.order.payment_status = 'refunded'
    return refund
