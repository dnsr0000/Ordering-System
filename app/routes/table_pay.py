"""桌邊線上結帳：顧客查看本桌帳單、前往綠界付款、付款結果 (詳見 app/services/table_pay_service.py)"""
from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, url_for

from app.errors import AppError
from app.extensions import csrf, limiter
from app.routes.payment import ECPAY_FORM_ACTION
from app.services import payment_ecpay, table_pay_service, table_service

table_pay_bp = Blueprint('table_pay', __name__)


def _table():
    table = table_service.current_table()
    if table is None:
        raise AppError('請先掃描桌上的 QR code', 403)
    return table


@table_pay_bp.route('/api/table/bill')
def bill():
    """本桌未結帳的訂單與合計 (掃碼點餐的手機)"""
    table = _table()
    return jsonify(dict(table_pay_service.bill(table), success=True,
                        online_pay=table_pay_service.enabled(table.tenant_id)))


@table_pay_bp.route('/api/table/pay', methods=['POST'])
@limiter.limit('10/minute')
def pay():
    tp = table_pay_service.start(_table())
    return jsonify({'success': True, 'payment_url': url_for('table_pay.pay_page', trade_no=tp.trade_no),
                    'amount': tp.amount})


@table_pay_bp.route('/table/pay/<trade_no>', endpoint='pay_page')
def pay_page(trade_no):
    """自動送出表單到綠界付款頁"""
    tp = table_pay_service.find(trade_no)
    table = table_service.current_table()
    if tp is None or table is None or tp.dining_table_id != table.id:
        abort(404)
    if tp.status != 'pending':
        return redirect(url_for('table_pay.status', trade_no=tp.trade_no))
    fields = table_pay_service.form_fields(tp)
    from app.services import settings_service
    resp = Response(render_template('table_pay.html', tp=tp, table=table, fields=fields,
                                    action=payment_ecpay.checkout_url(), stage=current_app.config.get('ECPAY_STAGE', True),
                                    note=settings_service.get('table_pay', tp.tenant_id).get('note') or ''))
    from app import CSP_POLICY
    resp.headers['Content-Security-Policy'] = CSP_POLICY.replace("form-action 'self'", ECPAY_FORM_ACTION)
    return resp


@table_pay_bp.route('/payment/ecpay/table_notify', methods=['POST'], endpoint='notify')
@csrf.exempt
@limiter.exempt
def notify():
    """綠界伺服器的付款結果通知：必須回應純文字 1|OK"""
    return Response(table_pay_service.handle_notification(request.form), mimetype='text/plain')


@table_pay_bp.route('/payment/ecpay/table_result', methods=['POST'], endpoint='result')
@csrf.exempt
def result():
    """顧客付款後由綠界頁面帶回：驗證後導向結果頁 (門市主機收不到綠界通知時，以這裡帶回的已簽章結果完成結帳)"""
    params, status = payment_ecpay.parse_result(request.form, '桌邊結帳 (瀏覽器帶回)')
    if status == 'bad_mac':
        abort(400)
    trade_no = params.get('MerchantTradeNo')
    if status == 'paid':
        table_pay_service.mark_paid(trade_no, params.get('TradeNo'), params.get('TradeAmt'), params.get('PaymentType'))
    return redirect(url_for('table_pay.status', trade_no=trade_no), code=303)


@table_pay_bp.route('/table/pay/status/<trade_no>', endpoint='status')
def status(trade_no):
    tp = table_pay_service.find(trade_no)
    if tp is None:
        abort(404)
    return render_template('table_pay_status.html', tp=tp)
