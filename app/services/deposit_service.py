"""
訂位訂金 (保證金)、未到 (no-show) 管理與訂位提醒 (#11)。

- 線上訂位人數達設定時須以綠界信用卡預付訂金 (每位 N 元)；30 分鐘內未付款自動取消並釋出座位。
- 入座：訂金自動退還 (保證金模式) 或標記「結帳折抵」由店員結帳時扣除。
- 取消：訂位前 N 小時以前取消自動退還；之後取消或未到 (no-show) 訂金不退。
- 退款以綠界 DoAction 退刷；失敗時標記「退款失敗」並通知店家到綠界後台處理。訂金狀態以條件式 UPDATE 推進，
  同時取消 / 入座只會退一次。
- 收款紀錄 (不綁訂單)：付款成功記一筆預收款 (deposit)、退款記負數；沒收記 deposit_forfeit (預收轉收入)、撤銷沒收記負數。
- 未到次數：同一支手機近 180 天未到達設定次數時暫停線上訂位 (請來電)。
- 提醒：訂位前 N 小時以簡訊 (有設定) 或 LINE (同手機的會員已綁定) 提醒一次。
訂位屬門市本機資料 (不同步)；綠界通知回到門市主機 (需 PUBLIC_BASE_URL 對外 HTTPS)。
"""
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import has_app_context
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import PaymentMethod
from app.models.payment import PaymentKind, PaymentRecord
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
        r.deposit_trade_no = payment_ecpay.new_trade_no()
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


# 訂金付款頁只開放信用卡 (aio_form 預設 ChoosePayment=Credit)，退款也以信用卡退刷
DEPOSIT_METHOD = PaymentMethod.CREDIT_CARD


def _net(tenant_id, trade_no, kind):
    return int(db.session.query(db.func.coalesce(db.func.sum(PaymentRecord.amount), 0)).filter(
        PaymentRecord.tenant_id == tenant_id, PaymentRecord.order_id.is_(None), PaymentRecord.kind == kind,
        PaymentRecord.reference == trade_no).execution_options(skip_tenant_filter=True).scalar() or 0)


def _record(tenant_id, trade_no, kind, amount, note, staff=None):
    """
    訂金的收款紀錄 (不綁訂單，以付款交易編號為 reference，與狀態轉換在同一個交易)：收款 / 退款寫 DEPOSIT (預收款)，
    沒收 / 撤銷沒收寫 DEPOSIT_FORFEIT (預收轉收入)。退款、沒收、撤銷沒收只在已有對應紀錄時才寫：
    更新前就已付款 / 已沒收的舊訂位沒有收款紀錄，不可沖出負的預收款或收入。
    """
    from app.services import payment_service
    if not trade_no or not amount:
        return None
    if amount < 0 or kind == PaymentKind.DEPOSIT_FORFEIT:
        basis = kind if amount < 0 else PaymentKind.DEPOSIT
        if _net(tenant_id, trade_no, basis) < abs(amount):
            return None
    return payment_service.record_payment(None, kind, DEPOSIT_METHOD, amount, reference=trade_no, note=note[:200],
                                          staff=staff, tenant_id=tenant_id)


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
    paid = payment_ecpay.parse_amount(amount)
    if paid != r.deposit_amount:
        monitor_service.alert(f'訂位 {r.name} {r.reserved_at:%m/%d %H:%M} 訂金付款金額 ${paid} 與應付 ${r.deposit_amount} 不符，'
                              f'請到綠界後台確認 (交易 {provider_no})', key=f'deposit-{r.id}', tenant_id=r.tenant_id,
                              level='warning', source='payment')
        return 'mismatch'
    values = dict(deposit_status='paid', deposit_provider_no=str(provider_no or '')[:30], deposit_paid_at=datetime.now(),
                  deposit_trade_no=str(trade_no or '')[:20])   # 記下實際付款的交易編號 (退刷用)
    if _claim(r, 'pending', **values):
        _paid_record(r, provider_no)
        db.session.commit()
        return 'paid'
    if r.deposit_status == 'expired' and _claim(r, 'expired', **values):
        # 逾時已自動取消後才付款成功：記下收款後立即退還
        _paid_record(r, provider_no)
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


def _paid_record(r, provider_no):
    _record(r.tenant_id, r.deposit_trade_no, PaymentKind.DEPOSIT, r.deposit_amount,
            f'訂位訂金 {r.name} {r.reserved_at:%m/%d %H:%M} (綠界 {provider_no or "-"})', staff='ecpay')


def mark_paid_from(params):
    """綠界通知 / 瀏覽器帶回的參數 → mark_paid (CustomField1 為 R:訂位代碼)"""
    return mark_paid(params.get('MerchantTradeNo'), params.get('TradeNo'), params.get('TradeAmt'), params.get('CustomField1'))


def handle_notification(form):
    """綠界 ReturnURL：回傳要回應綠界的字串"""
    return payment_ecpay.notification_response(form, '訂金', mark_paid_from, replies={'missing': '0|NotFound'})


# ---------------------------------------------------------------------------
# 退款 / 不退
# ---------------------------------------------------------------------------
def refund(r, reason):
    """
    訂金狀態須已由呼叫端以 _claim 取得 (paid → refunding)；退刷成功 → refunded，失敗 → refund_failed 並通知。
    呼叫綠界前先提交 (訂位狀態與 refunding 一起提交)：退刷的網路往返 (最長數十秒) 不佔住 SQLite 寫入鎖；
    結果由呼叫端提交。
    """
    from app.services import monitor_service
    if r.deposit_status != 'refunding':
        if not _claim(r, 'paid', deposit_status='refunding'):
            return r.deposit_status
    rid, tenant_id = r.id, r.tenant_id
    ns = SimpleNamespace(payment_method=PaymentMethod.CREDIT_CARD, payment_ref=r.deposit_trade_no,
                         payment_trade_no=r.deposit_provider_no, total_price=r.deposit_amount)
    amount, name, reserved_at, provider_no = r.deposit_amount, r.name, r.reserved_at, r.deposit_provider_no
    trade_no = r.deposit_trade_no
    db.session.commit()
    error = None
    try:
        text = payment_ecpay.refund(ns, amount)
    except payment_ecpay.EcpayRefundError as exc:
        error = exc.message
    except Exception as exc:  # noqa: BLE001  已提交 refunding：任何錯誤都要標記退款失敗，不可停在「退款中」
        logger.exception('訂位 %s 訂金退款發生錯誤', rid)
        error = f'退款發生錯誤：{exc}'
    if error is None:
        r.deposit_status, r.deposit_note = 'refunded', f'{reason}：{text}'[:200]
        _record(tenant_id, trade_no, PaymentKind.DEPOSIT, -amount, f'退還訂位訂金：{reason}')
    else:
        r.deposit_status, r.deposit_note = 'refund_failed', f'{reason}；{error}'[:200]
        monitor_service.alert(f'訂位 {name} {reserved_at:%m/%d %H:%M} 的訂金 ${amount} 退款失敗：{error}，'
                              f'請到綠界後台退款 (交易 {provider_no})', key=f'deposit-refund-{rid}',
                              tenant_id=tenant_id, level='warning', source='payment')
    return r.deposit_status


def retry_refund(r):
    """退款失敗 (refund_failed) 後重試退刷 (由呼叫端提交)；回傳新的訂金狀態"""
    if not _claim(r, 'refund_failed', deposit_status='refunding'):
        raise AppError('此訂位的訂金不是「退款失敗」狀態，請重新整理', 409)
    return refund(r, '重試退款')


def mark_refunded_manually(r, staff=''):
    """店家已在綠界後台人工退款：refund_failed → refunded 並記下處理人 (由呼叫端提交)"""
    note = f'已於綠界後台人工退款 ({staff or "-"} {datetime.now():%m/%d %H:%M})'
    if not _claim(r, 'refund_failed', deposit_status='refunded', deposit_note=note[:200]):
        raise AppError('此訂位的訂金不是「退款失敗」狀態，請重新整理', 409)
    _record(r.tenant_id, r.deposit_trade_no, PaymentKind.DEPOSIT, -r.deposit_amount, '訂位訂金已於綠界後台人工退款',
            staff=staff or None)
    return r.deposit_status


def on_status(r, new_status, now=None, by_customer=False, by_store=False):
    """
    訂位狀態改變時處理訂金；回傳給顧客 / 店員看的說明 (沒有訂金時為空字串)。
    by_store=True：店家因素取消，不論時間一律全額退款。誤標未到 / 取消而沒收的訂金，改回入座 / 預約中時恢復為已付款。
    """
    now = now or datetime.now()
    if r.deposit_status == 'pending' and new_status in ('cancelled', 'no_show'):
        _claim(r, 'pending', deposit_status='expired', deposit_note='付款前已取消')
        return ''
    if r.deposit_status == 'forfeited' and new_status in ('seated', 'booked'):
        if not _claim(r, 'forfeited', deposit_status='paid', deposit_note='狀態改回，撤銷訂金沒收'):
            return ''
        _forfeit_record(r, -r.deposit_amount, '撤銷訂位訂金沒收')
        if new_status == 'booked':
            return f'已撤銷訂金沒收，訂金 ${r.deposit_amount} 恢復為已付款'
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
        if hours >= limit or by_store:
            status = refund(r, '顧客取消' if by_customer else ('店家因素取消' if by_store else '取消訂位'))
            return f'訂金 ${r.deposit_amount} ' + ('將退回原信用卡' if status == 'refunded' else '退款處理中，店家將與您聯繫')
        if _claim(r, 'paid', deposit_status='forfeited', deposit_note=f'訂位前 {limit} 小時內取消，訂金不退'):
            _forfeit_record(r, r.deposit_amount, '訂位訂金沒收 (訂位前取消)')
        return f'訂位前 {limit} 小時內取消，訂金 ${r.deposit_amount} 不退還'
    if new_status == 'no_show':
        if _claim(r, 'paid', deposit_status='forfeited', deposit_note='未到，訂金不退'):
            _forfeit_record(r, r.deposit_amount, '訂位訂金沒收 (未到)')
        return f'未到，訂金 ${r.deposit_amount} 不退'
    return ''


def _forfeit_record(r, amount, note):
    _record(r.tenant_id, r.deposit_trade_no, PaymentKind.DEPOSIT_FORFEIT, amount, note)


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
    """提醒簡訊 / LINE 裡的連結：只用對外的 PUBLIC_BASE_URL (沒有設定就不附連結，不印店內區網網址)"""
    from app.services import table_service
    base = table_service.public_base(shop=False, fallback=False) if has_app_context() else ''
    return f'{base}{path}' if base else ''


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
    """本月 (訂位時間在本月內)：未到次數、訂金不退金額；退款失敗為目前待處理的筆數 (不限月份) (看板顯示)"""
    from datetime import date
    today = today or date.today()
    first = today.replace(day=1)
    start = datetime.combine(first, datetime.min.time())
    end = datetime.combine((first + timedelta(days=32)).replace(day=1), datetime.min.time())
    q = Reservation.query.filter(Reservation.reserved_at >= start, Reservation.reserved_at < end)
    return {'no_show': q.filter(Reservation.status == 'no_show').count(),
            'forfeited': sum(r.deposit_amount for r in q.filter(Reservation.deposit_status == 'forfeited').all()),
            'refund_failed': Reservation.query.filter(Reservation.deposit_status == 'refund_failed').count()}
