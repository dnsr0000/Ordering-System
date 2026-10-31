"""
訂位訂金 (保證金)、未到 (no-show) 管理與訂位提醒 (第三輪 #11)。

- 線上訂位人數達設定時須以綠界信用卡預付訂金 (每位 N 元)；30 分鐘內未付款自動取消並釋出座位。
- 入座：訂金自動退還 (保證金模式) 或標記「結帳折抵」由店員結帳時扣除。
- 取消：訂位前 N 小時以前取消自動退還；之後取消或未到 (no-show) 訂金不退。
- 退款以綠界 DoAction 退刷；失敗時標記「退款失敗」並通知店家到綠界後台處理。訂金狀態以條件式 UPDATE 推進，
  同時取消 / 入座只會退一次。
- 未到次數：同一支手機近 180 天未到達設定次數時暫停線上訂位 (請來電)。
- 提醒：訂位前 N 小時以簡訊 (有設定) 或 LINE (同手機的會員已綁定) 提醒一次。
訂位屬門市本機資料 (不同步)；綠界通知回到門市主機 (需 PUBLIC_BASE_URL 對外 HTTPS)。
"""
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import current_app, has_app_context
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import PaymentMethod
from app.models.reservation import Reservation
from app.services import payment_ecpay, settings_service

logger = logging.getLogger(__name__)
PENDING_MINUTES = 30
NO_SHOW_DAYS = 180


def deposit_for(tenant_id, party_size):
    cfg = settings_service.get('reservation', tenant_id)
    per = int(cfg.get('deposit_per_person') or 0)
    if per <= 0 or party_size < int(cfg.get('deposit_min_party') or 1):
        return 0
    return per * party_size


def no_show_count(tenant_id, phone, now=None):
    if not phone:
        return 0
    now = now or datetime.now()
    return Reservation.query.filter(Reservation.tenant_id == tenant_id, Reservation.phone == phone,
                                    Reservation.status == 'no_show',
                                    Reservation.reserved_at >= now - timedelta(days=NO_SHOW_DAYS)).count()


def check_no_show_limit(tenant_id, phone, now=None):
    limit = int(settings_service.get('reservation', tenant_id).get('no_show_limit') or 0)
    if limit and no_show_count(tenant_id, phone, now) >= limit:
        raise AppError(f'此手機近 {NO_SHOW_DAYS} 天有 {limit} 次以上訂位未到，暫停線上訂位，請來電預約')


# ---------------------------------------------------------------------------
# 付款
# ---------------------------------------------------------------------------
def form_fields(r, base_url=None):
    # CustomField1 帶訂位代碼：每次付款嘗試的交易編號不同 (綠界不可重複)，通知回來時以代碼找回訂位
    return payment_ecpay.aio_form(r.deposit_trade_no, r.deposit_amount, f'訂位訂金 {r.reserved_at:%m/%d %H:%M} {r.party_size} 位',
                                  'ReservationDeposit', f'R:{r.token}',
                                  '/payment/ecpay/reservation_notify', '/payment/ecpay/reservation_result', base_url)


def new_attempt(r):
    """開啟付款頁 (含重新整理、付款失敗後再試) 時換一個新的交易編號：綠界不接受重複的 MerchantTradeNo"""
    if r.deposit_status == 'pending':
        r.deposit_trade_no = payment_ecpay._new_trade_no()
    return r


def find(trade_no, custom=''):
    """以綠界回傳的 CustomField1 (R:訂位代碼) 找訂位；沒有時以交易編號"""
    from app.tenant_context import tenant_bypass
    custom = str(custom or '')
    with tenant_bypass():
        q = Reservation.query.execution_options(skip_tenant_filter=True)
        if custom.startswith('R:') and len(custom) > 2:
            return q.filter_by(token=custom[2:34]).first()
        if not trade_no:
            return None
        return q.filter_by(deposit_trade_no=str(trade_no)[:20]).first()


def _claim(r, expected, **values):
    ok = db.session.execute(update(Reservation).where(Reservation.id == r.id, Reservation.deposit_status == expected)
                            .values(**values).execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    db.session.refresh(r)
    return bool(ok)


def mark_paid(trade_no, provider_no, amount, custom=''):
    """
    綠界付款成功：回傳 'paid' / 'already' / 'duplicate' (同一筆訂位付了兩次，通知退款) /
    'refunded_late' (逾時已取消，立即退款) / 'mismatch' / 'missing'
    """
    from app.models.tenant import Tenant
    from app.services import monitor_service
    from app.tenant_context import _use_tenant
    r = find(trade_no, custom)
    if r is None:
        return 'missing'
    _use_tenant(db.session.get(Tenant, r.tenant_id), remember=False)
    try:
        paid = int(float(amount))
    except (TypeError, ValueError):
        paid = -1
    if paid != r.deposit_amount:
        monitor_service.alert(f'訂位 {r.name} {r.reserved_at:%m/%d %H:%M} 訂金付款金額 ${paid} 與應付 ${r.deposit_amount} 不符，'
                              f'請到綠界後台確認 (交易 {provider_no})', key=f'deposit-{r.id}', tenant_id=r.tenant_id,
                              level='warning', source='payment')
        return 'mismatch'
    values = dict(deposit_status='paid', deposit_provider_no=str(provider_no or '')[:30], deposit_paid_at=datetime.now(),
                  deposit_trade_no=str(trade_no or '')[:20])   # 記下實際付款的交易編號 (退刷用)
    if _claim(r, 'pending', **values):
        db.session.commit()
        return 'paid'
    if r.deposit_status == 'expired' and _claim(r, 'expired', **values):
        # 逾時已自動取消後才付款成功：立即退還
        refund(r, '訂位已逾時取消，退還訂金')
        db.session.commit()
        return 'refunded_late'
    if trade_no and r.deposit_trade_no and str(trade_no)[:20] != r.deposit_trade_no and r.deposit_provider_no != provider_no:
        # 顧客開了兩個付款頁、兩筆都付款成功：第二筆要退
        monitor_service.alert(f'訂位 {r.name} {r.reserved_at:%m/%d %H:%M} 的訂金重複付款 ${paid} (交易 {provider_no})，'
                              f'請到綠界後台退款', key=f'deposit-dup-{provider_no}', tenant_id=r.tenant_id,
                              level='warning', source='payment')
        return 'duplicate'
    return 'already'


def handle_notification(form):
    params, status = payment_ecpay.parse_result(form, '訂金')
    if status != 'paid':
        return payment_ecpay.RESULT_REPLY[status]
    result = mark_paid(params.get('MerchantTradeNo'), params.get('TradeNo'), params.get('TradeAmt'),
                       params.get('CustomField1'))
    return '0|NotFound' if result == 'missing' else '1|OK'


# ---------------------------------------------------------------------------
# 退款 / 不退
# ---------------------------------------------------------------------------
def refund(r, reason):
    """訂金狀態須已由呼叫端以 _claim 取得 (paid → refunding)；退刷成功 → refunded，失敗 → refund_failed 並通知"""
    from app.services import monitor_service
    if r.deposit_status != 'refunding':
        if not _claim(r, 'paid', deposit_status='refunding'):
            return r.deposit_status
    ns = SimpleNamespace(payment_method=PaymentMethod.CREDIT_CARD, payment_ref=r.deposit_trade_no,
                         payment_trade_no=r.deposit_provider_no, total_price=r.deposit_amount)
    try:
        text = payment_ecpay.refund(ns, r.deposit_amount)
        r.deposit_status, r.deposit_note = 'refunded', f'{reason}：{text}'[:200]
    except payment_ecpay.EcpayRefundError as exc:
        r.deposit_status, r.deposit_note = 'refund_failed', f'{reason}；{exc.message}'[:200]
        monitor_service.alert(f'訂位 {r.name} {r.reserved_at:%m/%d %H:%M} 的訂金 ${r.deposit_amount} 退款失敗：{exc.message}，'
                              f'請到綠界後台退款 (交易 {r.deposit_provider_no})', key=f'deposit-refund-{r.id}',
                              tenant_id=r.tenant_id, level='warning', source='payment')
    return r.deposit_status


def on_status(r, new_status, now=None, by_customer=False):
    """訂位狀態改變時處理訂金；回傳給顧客 / 店員看的說明 (沒有訂金時為空字串)"""
    now = now or datetime.now()
    if r.deposit_status == 'pending' and new_status in ('cancelled', 'no_show'):
        _claim(r, 'pending', deposit_status='expired', deposit_note='付款前已取消')
        return ''
    if r.deposit_status != 'paid':
        return ''
    cfg = settings_service.get('reservation', r.tenant_id)
    if new_status == 'seated':
        if (cfg.get('deposit_on_seat') or 'refund') == 'apply':
            if _claim(r, 'paid', deposit_status='applied', deposit_note='入座，結帳時折抵'):
                return f'訂金 ${r.deposit_amount} 請於結帳時折抵'
            return ''
        return f'訂金 ${r.deposit_amount} ' + ('已退還' if refund(r, '入座退還保證金') == 'refunded' else '退款失敗，請到綠界後台退款')
    if new_status == 'cancelled':
        hours = (r.reserved_at - now).total_seconds() / 3600
        limit = int(cfg.get('deposit_refund_hours') or 0)
        if hours >= limit:
            status = refund(r, '顧客取消' if by_customer else '取消訂位')
            return f'訂金 ${r.deposit_amount} ' + ('將退回原信用卡' if status == 'refunded' else '退款處理中，店家將與您聯繫')
        _claim(r, 'paid', deposit_status='forfeited', deposit_note=f'訂位前 {limit} 小時內取消，訂金不退')
        return f'訂位前 {limit} 小時內取消，訂金 ${r.deposit_amount} 不退還'
    if new_status == 'no_show':
        _claim(r, 'paid', deposit_status='forfeited', deposit_note='未到，訂金不退')
        return f'未到，訂金 ${r.deposit_amount} 不退'
    return ''


# ---------------------------------------------------------------------------
# 排程：逾時未付款、訂位提醒
# ---------------------------------------------------------------------------
def expire_pending(tenant_id, now=None):
    now = now or datetime.now()
    rows = Reservation.query.filter(Reservation.tenant_id == tenant_id, Reservation.deposit_status == 'pending',
                                    Reservation.created_at < now - timedelta(minutes=PENDING_MINUTES)) \
        .execution_options(skip_tenant_filter=True).all()
    n = 0
    for r in rows:
        if _claim(r, 'pending', deposit_status='expired', deposit_note=f'{PENDING_MINUTES} 分鐘內未付款，訂位已取消'):
            if r.status == 'booked':
                r.status, r.status_changed_at = 'cancelled', now
            n += 1
    db.session.commit()
    return n


def _public_url(path):
    base = current_app.config.get('PUBLIC_BASE_URL') if has_app_context() else ''
    return f'{base.rstrip("/")}{path}' if base else ''


def send_reminders(tenant_id, now=None):
    """訂位前 N 小時提醒一次 (簡訊優先，否則 LINE)；回傳送出則數"""
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services import line_service, sms_service
    cfg = settings_service.get('reservation', tenant_id)
    hours = int(cfg.get('reminder_hours') or 0)
    if hours <= 0:
        return 0
    now = now or datetime.now()
    rows = Reservation.query.filter(Reservation.tenant_id == tenant_id, Reservation.status == 'booked',
                                    Reservation.reminded_at.is_(None), Reservation.reserved_at > now,
                                    Reservation.reserved_at <= now + timedelta(hours=hours),
                                    Reservation.deposit_status.notin_(('pending', 'expired'))) \
        .execution_options(skip_tenant_filter=True).all()
    shop = db.session.get(Tenant, tenant_id)
    sent = 0
    for r in rows:
        claimed = db.session.execute(update(Reservation).where(Reservation.id == r.id, Reservation.reminded_at.is_(None))
                                     .values(reminded_at=now)
                                     .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
        db.session.commit()
        if not claimed or not r.phone:
            continue
        link = _public_url(f'/reserve/{r.token}')
        text = (f"{shop.name if shop else ''}提醒您：{r.reserved_at:%m/%d %H:%M} {r.party_size} 位的訂位，期待您的光臨！"
                + (f'如需取消請至 {link}' if link else '如需取消請來電'))
        ok = False
        if sms_service.is_enabled(tenant_id):
            try:
                sms_service.send_sms(tenant_id, r.phone, text)
                ok = True
            except AppError:
                ok = False
        if not ok:
            user = User.query.filter(User.tenant_id == tenant_id, User.phone == r.phone, User.line_user_id != '') \
                .execution_options(skip_tenant_filter=True).first()
            ok = bool(user) and line_service.push_text(tenant_id, user.line_user_id, text)
        sent += 1 if ok else 0
    return sent


def run_due(tenant_id, now=None):
    return expire_pending(tenant_id, now) + send_reminders(tenant_id, now)


def month_stats(tenant_id, today=None):
    """本月：未到次數、訂金不退金額、退款失敗筆數 (看板顯示)"""
    from datetime import date
    today = today or date.today()
    start = datetime.combine(today.replace(day=1), datetime.min.time())
    q = Reservation.query.filter(Reservation.reserved_at >= start)
    return {'no_show': q.filter(Reservation.status == 'no_show').count(),
            'forfeited': sum(r.deposit_amount for r in q.filter(Reservation.deposit_status == 'forfeited').all()),
            'refund_failed': Reservation.query.filter(Reservation.deposit_status == 'refund_failed').count()}
