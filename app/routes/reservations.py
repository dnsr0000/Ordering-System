"""訂位 / 候位：店員看板 (/reservations) 與顧客頁 (/reserve 線上訂位、/waitlist 取號、/waitlist/<代碼> 查看進度)"""
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.errors import AppError
from app.extensions import csrf, db, limiter
from app.models.reservation import Reservation, WaitlistEntry
from app.models.table import DiningTable
from app.models.tenant import Tenant
from app.services import deposit_service, payment_ecpay, reservation_service, settings_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.services.order_service import todays_range
from app.sync import runtime as sync_runtime
from app.tenant_context import _use_tenant, tenant_bypass
from app.utils import get_own, safe_int

reservations_bp = Blueprint('reservations', __name__)
MIN_PAY_SECONDS = 120   # 訂金付款期限剩不到這麼久時不再產生付款表單 (來不及在綠界完成付款)


@reservations_bp.before_request
def only_on_store_servers():
    """訂位 / 候位 / 訂金是門市本機資料：線上端一律不提供 (否則線上端收到的訂位與訂金沒有任何分店看得到)"""
    if sync_runtime.is_hub():
        abort(404)


def _day():
    try:
        return date.fromisoformat(request.args.get('day') or '')
    except ValueError:
        return date.today()


@reservations_bp.route('/reservations', methods=['GET', 'POST'], endpoint='board')
@permission_required('reservations')
def board():
    if request.method == 'POST':
        f = request.form
        action = f.get('action')
        if action == 'reserve':
            try:
                when = datetime.fromisoformat(f'{f.get("date")}T{f.get("time")}')
            except ValueError:
                raise AppError('請輸入訂位日期與時間')
            r = reservation_service.create_reservation(g.tenant_id, f.get('name'), f.get('phone'), safe_int(f.get('party_size'), 0),
                                                       when, f.get('note'), 'staff', f.get('table_name'))
            record_staff_audit('RESERVATION', details=f'新增訂位：{r.name} {r.party_size} 位 {when:%m-%d %H:%M}')
            flash(f'已新增訂位：{r.name} {r.party_size} 位', 'success')
        elif action == 'reservation_status':
            r = get_own(Reservation, f.get('reservation_id'), '訂位')
            # 看板只對「已訂位」提供操作：畫面過期 (已被顧客取消 / 其他店員入座) 時拒絕，不把已取消改成未到
            # by_store=1 (店家因素取消)：不論距訂位時間多久一律全額退訂金 (只對取消有意義)
            msg = reservation_service.set_reservation_status(r, f.get('status'), f.get('table_name'),
                                                             expected=f.get('expected_status') or 'booked',
                                                             by_store=(f.get('by_store') == '1'))
            if msg:
                flash(f'{r.name}：{msg}', 'info')
        elif action == 'deposit_retry':   # 訂金退款失敗後重試退刷
            r = get_own(Reservation, f.get('reservation_id'), '訂位')
            status = deposit_service.retry_refund(r)
            record_staff_audit('RESERVATION', details=f'訂金重試退款：{r.name} ${r.deposit_amount} → {status}')
            flash(f'{r.name}：訂金' + ('已退還' if status == 'refunded' else '重試退款仍失敗，請到綠界後台退款'), 'info')
        elif action == 'deposit_manual':   # 店家已在綠界後台人工退款
            r = get_own(Reservation, f.get('reservation_id'), '訂位')
            deposit_service.mark_refunded_manually(r, current_user.username)
            record_staff_audit('RESERVATION', details=f'訂金標記人工退款完成：{r.name} ${r.deposit_amount}')
            flash(f'{r.name}：已標記為人工退款完成', 'success')
        elif action == 'waitlist_add':
            e = reservation_service.join_waitlist(g.tenant_id, f.get('name'), f.get('phone'), safe_int(f.get('party_size'), 0))
            flash(f'候位號碼 {e.label}', 'success')
        elif action == 'waitlist_status':
            e = get_own(WaitlistEntry, f.get('entry_id'), '候位')
            reservation_service.set_waitlist_status(e, f.get('status'), f.get('table_name'))
            if e.status == 'called':
                reservation_service.notify_called(e, g.tenant_id)
        else:
            raise AppError('不支援的操作')
        db.session.commit()
        order_event_bus.notify(g.tenant_id)   # 叫號看板即時更新
        return redirect(url_for('reservations.board', day=request.args.get('day') or None))
    day = _day()
    start, end = reservation_service.day_bounds(day)
    reservations = Reservation.query.filter(Reservation.reserved_at >= start, Reservation.reserved_at < end) \
        .order_by(Reservation.reserved_at, Reservation.id).all()
    business_date = todays_range()[0].date()   # 營業日 (跨午夜營業的店換日時間前仍是前一天)，與取號相同
    waitlist = WaitlistEntry.query.filter_by(business_date=business_date) \
        .filter(WaitlistEntry.status.in_(('waiting', 'called'))).order_by(WaitlistEntry.number).all()
    done = WaitlistEntry.query.filter_by(business_date=business_date) \
        .filter(WaitlistEntry.status.in_(('seated', 'cancelled'))).order_by(WaitlistEntry.number.desc()).limit(20).all()
    tables = DiningTable.query.filter_by(is_active=True).order_by(DiningTable.sort_order, DiningTable.id).all()
    return render_template('reservations.html', day=day, reservations=reservations, waitlist=waitlist, done=done,
                           tables=tables, cfg=settings_service.get('reservation'),
                           covers=sum(r.party_size for r in reservations if r.status in ('booked', 'seated')),
                           no_shows={r.phone: deposit_service.no_show_count(g.tenant_id, r.phone)
                                     for r in reservations if r.phone},
                           deposit_stats=deposit_service.month_stats(g.tenant_id))


# ------------------------------------------------------------------------------
# 顧客：線上訂位
# ------------------------------------------------------------------------------
@reservations_bp.route('/reserve', methods=['GET', 'POST'], endpoint='public_reserve')
@limiter.limit('20/hour', methods=['POST'])
def public_reserve():
    cfg = settings_service.get('reservation')
    if not cfg.get('online_enabled'):
        return render_template('error.html', status=404, message='本店目前未開放線上訂位，請來電預約。'), 404
    if request.method == 'POST':
        f = request.form
        try:
            when = datetime.fromisoformat(f.get('slot') or '')
        except ValueError:
            raise AppError('請選擇訂位時段')
        r = reservation_service.create_reservation(g.tenant_id, f.get('name'), f.get('phone'), safe_int(f.get('party_size'), 0),
                                                   when, f.get('note'), 'online')
        db.session.commit()
        if r.deposit_status == 'pending':   # 需預付訂金：前往綠界付款
            return redirect(url_for('reservations.deposit_pay', token=r.token))
        return redirect(url_for('reservations.reservation_status', token=r.token))
    day = _day()
    today = date.today()
    max_day = today + timedelta(days=cfg.get('days_ahead') or 30)   # 與 create_reservation 的可預訂天數相同
    day = min(max(day, today), max_day)
    return render_template('reserve.html', day=day, slots=reservation_service.slots_for(day), cfg=cfg,
                           tenant=g.tenant, today=today, max_day=max_day)


def _by_token(token):
    with tenant_bypass():
        r = Reservation.query.filter_by(token=str(token)[:32]).first()
    if r is None:
        abort(404)
    return r


@reservations_bp.route('/reserve/<token>/pay', endpoint='deposit_pay')
@limiter.limit('30/minute')
def deposit_pay(token):
    """訂金付款：自動送出表單到綠界"""
    r = _by_token(token)
    if r.deposit_status != 'pending':
        return redirect(url_for('reservations.reservation_status', token=r.token))
    # 付款期限從建立訂位起算 (expire_pending)，重開付款頁不會延長：顯示實際剩餘時間；剩不到能完成付款的時間就不產生付款表單
    left = (r.created_at + timedelta(minutes=deposit_service.PENDING_MINUTES) - datetime.now()).total_seconds()
    if left < MIN_PAY_SECONDS:
        raise AppError('訂金付款期限已過，此訂位將自動取消，請重新訂位', 410)
    payment_ecpay.require_keys_allowed()
    with tenant_bypass():   # 每次開啟付款頁都換新的交易編號 (綠界不接受重複編號；重新整理 / 付款失敗再試都可以)
        deposit_service.new_attempt(r)
        db.session.commit()
    return payment_ecpay.render_checkout_page('reservation_pay.html', r=r, fields=deposit_service.form_fields(r),
                                              minutes=max(1, int(left // 60)))


@reservations_bp.route('/payment/ecpay/reservation_notify', methods=['POST'], endpoint='deposit_notify')
@csrf.exempt
@limiter.exempt
def deposit_notify():
    """綠界伺服器的訂金付款通知 (以檢查碼驗證)：必須回應純文字 1|OK"""
    return payment_ecpay.notify_reply(deposit_service.handle_notification)


def _deposit_status_url(params):
    r = deposit_service.find(params.get('MerchantTradeNo'), params.get('CustomField1'))
    if r is None:
        abort(404)
    return url_for('reservations.reservation_status', token=r.token)


@reservations_bp.route('/payment/ecpay/reservation_result', methods=['POST'], endpoint='deposit_result')
@csrf.exempt
def deposit_result():
    """顧客付款後由綠界頁面帶回 (已簽章)：完成訂金付款並導向訂位資訊"""
    return payment_ecpay.browser_return(request.form, '訂金 (瀏覽器帶回)', deposit_service.mark_paid_from, _deposit_status_url)


@reservations_bp.route('/reserve/<token>', methods=['GET', 'POST'], endpoint='reservation_status')
@limiter.limit('30/minute')
def reservation_status(token):
    r = _by_token(token)
    with tenant_bypass():
        shop = db.session.get(Tenant, r.tenant_id)
    if shop is not None:   # 顯示語言等依紀錄所屬店家 (網址不含店家時，目前 session 可能是別家店)
        _use_tenant(shop, remember=False)
    if request.method == 'POST' and request.form.get('action') == 'cancel' and r.status == 'booked':
        if r.reserved_at <= datetime.now():   # 訂位時間已過：由店家處理 (入座 / 未到)，顧客不能改成取消來避開未到紀錄
            raise AppError('訂位時間已過，如需取消請洽店家',
                           redirect_to=url_for('reservations.reservation_status', token=r.token))
        with tenant_bypass():
            msg = reservation_service.set_reservation_status(r, 'cancelled', by_customer=True, expected='booked')
            db.session.commit()
        flash('已取消訂位' + (f'；{msg}' if msg else ''), 'info')
        return redirect(url_for('reservations.reservation_status', token=r.token))
    cfg = settings_service.get('reservation', r.tenant_id)
    return render_template('reservation_status.html', r=r, refund_hours=int(cfg.get('deposit_refund_hours') or 0),
                           can_cancel=r.status == 'booked' and r.reserved_at > datetime.now())


# ------------------------------------------------------------------------------
# 顧客：現場取號候位
# ------------------------------------------------------------------------------
def _trusted_waitlist_client():
    """
    門口取號平板 (已在「裝置管理」註冊的本店裝置) 或已登入的本店員工：不受取號頻率限制。
    同一台平板 / 店內 Wi-Fi 共用同一個對外 IP，尖峰時段一小時遠超過 20 組。
    """
    from flask_login import current_user
    from app.models.enums import ALL_STAFF_ROLES
    from app.services import device_service
    if device_service.current() is not None:
        return True
    return bool(current_user.is_authenticated and getattr(current_user, 'role', '') in ALL_STAFF_ROLES
                and getattr(current_user, 'tenant_id', None) == getattr(g, 'tenant_id', None))


@reservations_bp.route('/waitlist', methods=['GET', 'POST'], endpoint='public_waitlist')
@limiter.limit('60/hour', methods=['POST'], exempt_when=_trusted_waitlist_client)
def public_waitlist():
    if not settings_service.get('reservation').get('waitlist_public', True):
        abort(404)
    if request.method == 'POST':
        e = reservation_service.join_waitlist(g.tenant_id, request.form.get('name'), request.form.get('phone'),
                                              safe_int(request.form.get('party_size'), 0))
        db.session.commit()
        order_event_bus.notify(g.tenant_id)
        return redirect(url_for('reservations.waitlist_status', token=e.token))
    waiting = WaitlistEntry.query.filter_by(business_date=todays_range()[0].date(), status='waiting').count()
    return render_template('waitlist_join.html', waiting=waiting, tenant=g.tenant)


@reservations_bp.route('/waitlist/<token>', endpoint='waitlist_status')
@limiter.limit('60/minute')
def waitlist_status(token):
    with tenant_bypass():
        e = WaitlistEntry.query.filter_by(token=str(token)[:32]).first()
        if e is None:
            abort(404)
        ahead = reservation_service.waiting_ahead(e)
        shop = db.session.get(Tenant, e.tenant_id)
    if shop is not None:   # 顯示語言等依紀錄所屬店家 (網址不含店家時，目前 session 可能是別家店)
        _use_tenant(shop, remember=False)
    if request.args.get('format') == 'json':
        return jsonify({'success': True, 'label': e.label, 'status': e.status, 'ahead': ahead})
    return render_template('waitlist_status.html', e=e, ahead=ahead)
