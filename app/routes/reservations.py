"""訂位 / 候位：店員看板 (/reservations) 與顧客頁 (/reserve 線上訂位、/waitlist 取號、/waitlist/<代碼> 查看進度)"""
from datetime import date, datetime

from flask import Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from app.errors import AppError
from app.extensions import csrf, db, limiter
from app.models.reservation import Reservation, WaitlistEntry
from app.models.table import DiningTable
from app.services import deposit_service, payment_ecpay, reservation_service, settings_service
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.services.event_bus import order_event_bus
from app.sync import runtime as sync_runtime
from app.tenant_context import _use_tenant, tenant_bypass
from app.utils import safe_int

reservations_bp = Blueprint('reservations', __name__)


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
            flash(f'✅ 已新增訂位：{r.name} {r.party_size} 位', 'success')
        elif action == 'reservation_status':
            r = db.session.get(Reservation, safe_int(f.get('reservation_id'), 0))
            if r is None or r.tenant_id != g.tenant_id:
                raise AppError('查無此訂位', 404)
            msg = reservation_service.set_reservation_status(r, f.get('status'), f.get('table_name'))
            if msg:
                flash(f'{r.name}：{msg}', 'info')
        elif action == 'waitlist_add':
            e = reservation_service.join_waitlist(g.tenant_id, f.get('name'), f.get('phone'), safe_int(f.get('party_size'), 0))
            flash(f'✅ 候位號碼 {e.label}', 'success')
        elif action == 'waitlist_status':
            e = db.session.get(WaitlistEntry, safe_int(f.get('entry_id'), 0))
            if e is None or e.tenant_id != g.tenant_id:
                raise AppError('查無此候位', 404)
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
    waitlist = WaitlistEntry.query.filter_by(business_date=date.today()) \
        .filter(WaitlistEntry.status.in_(('waiting', 'called'))).order_by(WaitlistEntry.number).all()
    done = WaitlistEntry.query.filter_by(business_date=date.today()) \
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
    if day < date.today():
        day = date.today()
    return render_template('reserve.html', day=day, slots=reservation_service.slots_for(day), cfg=cfg,
                           tenant=g.tenant, today=date.today())


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
    with tenant_bypass():   # 每次開啟付款頁都換新的交易編號 (綠界不接受重複編號；重新整理 / 付款失敗再試都可以)
        deposit_service.new_attempt(r)
        db.session.commit()
    base_url = current_app.config.get('PUBLIC_BASE_URL') or request.url_root.rstrip('/')
    resp = Response(render_template('reservation_pay.html', r=r, fields=deposit_service.form_fields(r, base_url),
                                    action=payment_ecpay.checkout_url(), stage=current_app.config.get('ECPAY_STAGE', True),
                                    minutes=deposit_service.PENDING_MINUTES))
    from app import CSP_POLICY
    from app.routes.payment import ECPAY_FORM_ACTION
    resp.headers['Content-Security-Policy'] = CSP_POLICY.replace("form-action 'self'", ECPAY_FORM_ACTION)
    return resp


@reservations_bp.route('/payment/ecpay/reservation_notify', methods=['POST'], endpoint='deposit_notify')
@csrf.exempt
@limiter.exempt
def deposit_notify():
    """綠界伺服器的訂金付款通知 (以檢查碼驗證)：必須回應純文字 1|OK"""
    return Response(deposit_service.handle_notification(request.form), mimetype='text/plain')


@reservations_bp.route('/payment/ecpay/reservation_result', methods=['POST'], endpoint='deposit_result')
@csrf.exempt
def deposit_result():
    """顧客付款後由綠界頁面帶回 (已簽章)：完成訂金付款並導向訂位資訊"""
    params = dict(request.form.items())
    if not payment_ecpay.verify(params):
        abort(400)
    if str(params.get('RtnCode')) == '1' and str(params.get('SimulatePaid', '0')) != '1':
        deposit_service.mark_paid(params.get('MerchantTradeNo'), params.get('TradeNo'), params.get('TradeAmt'),
                                  params.get('CustomField1'))
    r = deposit_service.find(params.get('MerchantTradeNo'), params.get('CustomField1'))
    if r is None:
        abort(404)
    return redirect(url_for('reservations.reservation_status', token=r.token), code=303)


@reservations_bp.route('/reserve/<token>', methods=['GET', 'POST'], endpoint='reservation_status')
@limiter.limit('30/minute')
def reservation_status(token):
    r = _by_token(token)
    if request.method == 'POST' and request.form.get('action') == 'cancel' and r.status == 'booked':
        with tenant_bypass():
            msg = reservation_service.set_reservation_status(r, 'cancelled', by_customer=True)
            db.session.commit()
        flash('已取消訂位' + (f'；{msg}' if msg else ''), 'info')
        return redirect(url_for('reservations.reservation_status', token=r.token))
    cfg = settings_service.get('reservation', r.tenant_id)
    return render_template('reservation_status.html', r=r, refund_hours=int(cfg.get('deposit_refund_hours') or 0))


# ------------------------------------------------------------------------------
# 顧客：現場取號候位
# ------------------------------------------------------------------------------
@reservations_bp.route('/waitlist', methods=['GET', 'POST'], endpoint='public_waitlist')
@limiter.limit('20/hour', methods=['POST'])
def public_waitlist():
    if not settings_service.get('reservation').get('waitlist_public', True):
        abort(404)
    if request.method == 'POST':
        e = reservation_service.join_waitlist(g.tenant_id, request.form.get('name'), request.form.get('phone'),
                                              safe_int(request.form.get('party_size'), 0))
        db.session.commit()
        order_event_bus.notify(g.tenant_id)
        return redirect(url_for('reservations.waitlist_status', token=e.token))
    waiting = WaitlistEntry.query.filter_by(business_date=date.today(), status='waiting').count()
    return render_template('waitlist_join.html', waiting=waiting, tenant=g.tenant)


@reservations_bp.route('/waitlist/<token>', endpoint='waitlist_status')
@limiter.limit('60/minute')
def waitlist_status(token):
    with tenant_bypass():
        e = WaitlistEntry.query.filter_by(token=str(token)[:32]).first()
        if e is None:
            abort(404)
        ahead = reservation_service.waiting_ahead(e)
    if request.args.get('format') == 'json':
        return jsonify({'success': True, 'label': e.label, 'status': e.status, 'ahead': ahead})
    return render_template('waitlist_status.html', e=e, ahead=ahead)
